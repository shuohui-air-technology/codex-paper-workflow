"""Crash-safe persistence and evidence replay for custom workflow runs.

The public boundary is intentionally small: drafts and selections are managed
by :class:`WorkflowStore`; runtime mutations happen only inside
``with store.locked_run()`` and are committed as hash-linked events before the
authoritative snapshot is replaced.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from .catalog import SkillIdentity, ValidatorIdentity
from .compiler import CompiledEdge, CompiledNode, CompiledPlan
from .conditions import evaluate_condition
from .fs import (
    PathSafetyError,
    MAX_EVENT_BYTES,
    MAX_JSON_BYTES,
    _fsync_directory,
    append_event,
    atomic_write_json,
    ensure_project_directory,
    resolve_project_path,
)
from .scheduler import (
    ArtifactRuntime,
    ControlTransition,
    EdgeRuntime,
    EdgeStatus,
    NodeRuntime,
    NodeStatus,
    RunState,
    _complete_control,
    _edge_outputs,
    condition_facts,
    initial_run,
    mark_descendants_stale,
    refresh_ready,
    result_transition,
    claim_transition,
)
from .receipts import (
    ReceiptError, build_claim_evidence, build_stage_receipt, canonical_bytes,
    validate_stage_receipt,
)
from .schema import (
    WorkflowDocument,
    WorkflowError,
    behavior_payload,
    document_sha256,
    parse_workflow,
)


ZERO_HASH = "0" * 64
_MAX_EVENT_LOG_BYTES = 16 * 1024 * 1024
_MAX_REVISION = 2**31 - 1
_MAX_WARNING_CODES = 256
_MAX_WARNING_CODE_LENGTH = 256
_EXTERNAL_PRODUCER = "external"
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: set[tuple[str, int, int]] = set()
_EVENT_FIELDS = frozenset(
    {
        "event_seq",
        "run_id",
        "semantic_sha256",
        "event_type",
        "payload",
        "previous_event_hash",
        "event_hash",
    }
)
_SELECTION_FIELDS = frozenset(
    {
        "mode",
        "selection_revision",
        "workflow_id",
        "semantic_revision",
        "semantic_sha256",
        "acknowledged_warning_codes",
        "acknowledged_semantic_sha256",
        "acknowledged_at",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "run_status",
        "last_applied_event_seq",
        "last_applied_event_hash",
        "plan_sha256",
        "state",
    }
)


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_bytes(value: object, *, newline: bool = False) -> bytes:
    data = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return data + (b"\n" if newline else b"")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _strict_json_loads(value: str | bytes) -> object:
    parsed = json.loads(
        value,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
    )
    stack: list[tuple[object, int]] = [(parsed, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > 128:
            raise ValueError("JSON nesting exceeds the supported depth")
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return parsed


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (NodeStatus, EdgeStatus)):
        return value.value
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _exact_mapping(value: object, fields: frozenset[str], code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise StoreError(code, "persisted object has missing or unknown fields")
    return value


def _plain_string(value: object, code: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise StoreError(code, "persisted string field is invalid")
    return value


def _lower_sha256(value: object, code: str, *, allow_empty: bool = False) -> str:
    item = _plain_string(value, code, allow_empty=allow_empty)
    if item or not allow_empty:
        if len(item) != 64 or any(char not in "0123456789abcdef" for char in item):
            raise StoreError(code, "persisted SHA-256 field is invalid")
    return item


def _run_id(value: object, code: str) -> str:
    item = _plain_string(value, code)
    if (
        len(item) > 200
        or item in {".", ".."}
        or any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in item)
    ):
        raise StoreError(code, "run ID is not safe for durable storage")
    return item


def _integer(
    value: object,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_REVISION,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise StoreError(code, "persisted integer field is invalid")
    return value


def _warning_codes(
    value: object,
    code: str,
    *,
    require_canonical: bool,
) -> tuple[str, ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, (list, tuple))
        or len(value) > _MAX_WARNING_CODES
    ):
        raise StoreError(code, "warning codes must be a bounded explicit sequence")
    normalized: list[str] = []
    for item in value:
        parsed = _plain_string(item, code)
        if len(parsed) > _MAX_WARNING_CODE_LENGTH or any(
            unicodedata.category(char) == "Cc" for char in parsed
        ):
            raise StoreError(code, "warning code is not bounded normalized text")
        normalized.append(parsed)
    result = tuple(normalized)
    canonical = tuple(sorted(set(result)))
    if require_canonical and result != canonical:
        raise StoreError(code, "warning codes are not sorted and unique")
    return canonical


@dataclass(frozen=True)
class Selection:
    mode: str
    selection_revision: int = 0
    workflow_id: str = ""
    semantic_revision: int = 0
    semantic_sha256: str = ""
    acknowledged_warning_codes: tuple[str, ...] = ()
    acknowledged_semantic_sha256: str = ""
    acknowledged_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "selection_revision": self.selection_revision,
            "workflow_id": self.workflow_id,
            "semantic_revision": self.semantic_revision,
            "semantic_sha256": self.semantic_sha256,
            "acknowledged_warning_codes": list(self.acknowledged_warning_codes),
            "acknowledged_semantic_sha256": self.acknowledged_semantic_sha256,
            "acknowledged_at": self.acknowledged_at,
        }

    def acknowledges(self, plan: CompiledPlan, required_warning_codes: Sequence[str]) -> bool:
        try:
            required = _warning_codes(
                required_warning_codes,
                "selection.invalid",
                require_canonical=False,
            )
        except StoreError:
            return False
        return (
            self.mode == "custom"
            and self.workflow_id == plan.workflow_id
            and self.semantic_revision == plan.semantic_revision
            and self.semantic_sha256 == plan.semantic_sha256
            and self.acknowledged_semantic_sha256 == plan.semantic_sha256
            and self.acknowledged_warning_codes == required
        )

    @classmethod
    def from_payload(cls, value: object) -> "Selection":
        item = _exact_mapping(value, _SELECTION_FIELDS, "selection.invalid")
        mode = _plain_string(item["mode"], "selection.invalid")
        if mode not in {"official", "custom"}:
            raise StoreError("selection.invalid", "selection mode is invalid")
        revision = _integer(item["selection_revision"], "selection.invalid")
        semantic_revision = _integer(item["semantic_revision"], "selection.invalid")
        workflow_id = _plain_string(item["workflow_id"], "selection.invalid", allow_empty=True)
        semantic_sha256 = _lower_sha256(item["semantic_sha256"], "selection.invalid", allow_empty=True)
        acknowledged_hash = _lower_sha256(
            item["acknowledged_semantic_sha256"], "selection.invalid", allow_empty=True
        )
        acknowledged_at = _plain_string(item["acknowledged_at"], "selection.invalid", allow_empty=True)
        normalized_codes = _warning_codes(
            item["acknowledged_warning_codes"],
            "selection.invalid",
            require_canonical=True,
        )
        if mode == "custom":
            if (
                not workflow_id
                or semantic_revision < 1
                or len(semantic_sha256) != 64
                or acknowledged_hash != semantic_sha256
                or not acknowledged_at
            ):
                raise StoreError("selection.invalid", "custom selection binding is incomplete")
            try:
                parsed_time = datetime.fromisoformat(acknowledged_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise StoreError("selection.invalid", "selection acknowledgement time is invalid") from exc
            if not acknowledged_at.endswith("Z") or parsed_time.tzinfo is None:
                raise StoreError("selection.invalid", "selection acknowledgement time is not UTC")
        elif any((workflow_id, semantic_revision, semantic_sha256, normalized_codes, acknowledged_hash, acknowledged_at)):
            raise StoreError("selection.invalid", "official selection must not retain a custom binding")
        return cls(
            mode,
            revision,
            workflow_id,
            semantic_revision,
            semantic_sha256,
            normalized_codes,
            acknowledged_hash,
            acknowledged_at,
        )


@dataclass(frozen=True)
class WorkflowEvent:
    event_seq: int
    run_id: str
    semantic_sha256: str
    event_type: str
    payload: Mapping[str, object]
    previous_event_hash: str
    event_hash: str

    def to_payload(self) -> dict[str, object]:
        return {
            "event_seq": self.event_seq,
            "run_id": self.run_id,
            "semantic_sha256": self.semantic_sha256,
            "event_type": self.event_type,
            "payload": _json_value(self.payload),
            "previous_event_hash": self.previous_event_hash,
            "event_hash": self.event_hash,
        }

    @classmethod
    def create(
        cls,
        *,
        event_seq: int,
        run_id: str,
        semantic_sha256: str,
        event_type: str,
        payload: Mapping[str, object],
        previous_event_hash: str,
    ) -> "WorkflowEvent":
        base = {
            "event_seq": event_seq,
            "run_id": run_id,
            "semantic_sha256": semantic_sha256,
            "event_type": event_type,
            "payload": _json_value(payload),
            "previous_event_hash": previous_event_hash,
        }
        return cls(
            event_seq,
            run_id,
            semantic_sha256,
            event_type,
            _freeze_json(base["payload"]),
            previous_event_hash,
            _sha256(base),
        )

    @classmethod
    def from_payload(cls, value: object) -> "WorkflowEvent":
        item = _exact_mapping(value, _EVENT_FIELDS, "events.invalid_event")
        event_seq = _integer(item["event_seq"], "events.invalid_event", minimum=1)
        run_id = _run_id(item["run_id"], "events.invalid_event")
        semantic_sha256 = _lower_sha256(item["semantic_sha256"], "events.invalid_event")
        event_type = _plain_string(item["event_type"], "events.invalid_event")
        previous = _lower_sha256(item["previous_event_hash"], "events.invalid_event")
        event_hash = _lower_sha256(item["event_hash"], "events.invalid_event")
        payload = item["payload"]
        if not isinstance(payload, Mapping):
            raise StoreError("events.invalid_event", "event payload must be an object")
        return cls(
            event_seq,
            run_id,
            semantic_sha256,
            event_type,
            _freeze_json(payload),
            previous,
            event_hash,
        )

    def hash_is_valid(self) -> bool:
        value = self.to_payload()
        asserted = value.pop("event_hash")
        return asserted == _sha256(value)


def _validated_event(
    *,
    event_seq: int,
    run_id: str,
    semantic_sha256: str,
    event_type: str,
    payload: Mapping[str, object],
    previous_event_hash: str,
) -> WorkflowEvent:
    try:
        created = WorkflowEvent.create(
            event_seq=event_seq,
            run_id=run_id,
            semantic_sha256=semantic_sha256,
            event_type=event_type,
            payload=payload,
            previous_event_hash=previous_event_hash,
        )
        encoded = _canonical_bytes(created.to_payload(), newline=True)
        decoded = _strict_json_loads(encoded)
        restored = WorkflowEvent.from_payload(decoded)
    except StoreError:
        raise
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError(
            "events.invalid_event", "event envelope is not bounded canonical JSON"
        ) from exc
    if len(encoded) > MAX_EVENT_BYTES:
        raise StoreError(
            "events.input_too_large", "complete event line exceeds the bounded size"
        )
    if (
        not restored.hash_is_valid()
        or _canonical_bytes(restored.to_payload(), newline=True) != encoded
    ):
        raise StoreError(
            "events.invalid_event", "event envelope does not round-trip canonically"
        )
    return restored


def _validate_lifecycle_step(
    event: WorkflowEvent,
    previous_status: str | None,
) -> str:
    status = event.payload.get("run_status")
    if status not in {"active", "stopped", "archived"}:
        raise StoreError(
            "events.lifecycle_invalid", "runtime event has an invalid durable status"
        )
    if event.event_seq == 1:
        if (
            previous_status is not None
            or event.event_type != "run_started"
            or status != "active"
        ):
            raise StoreError(
                "events.lifecycle_invalid",
                "event 1 must start an active run",
            )
        return status
    if previous_status is None or event.event_type == "run_started":
        raise StoreError(
            "events.lifecycle_invalid", "run_started is valid only for event 1"
        )
    if status == "archived":
        raise StoreError(
            "events.lifecycle_invalid", "no runtime event authorizes archived status"
        )
    if previous_status in {"stopped", "archived"} and status == "active":
        raise StoreError(
            "events.lifecycle_invalid", "a stopped or archived run cannot become active"
        )
    if previous_status == "active" and status == "stopped":
        if event.event_type != "run_stopped":
            raise StoreError(
                "events.lifecycle_invalid", "only run_stopped may stop an active run"
            )
    elif event.event_type == "run_stopped":
        raise StoreError(
            "events.lifecycle_invalid", "run_stopped must change active to stopped"
        )
    return status


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    code: str
    state: RunState | None = None
    archived_tail: Path | None = None


@dataclass(frozen=True)
class StorePaths:
    base: Path
    lock: Path
    selection: Path
    workflow: Path
    revisions: Path
    audit_events: Path
    run_dir: Path
    plan: Path
    state: Path
    events: Path
    artifacts: Path
    summary: Path
    recovery: Path
    archived_runs: Path
    receipts: Path


def _document_data(document: WorkflowDocument) -> dict[str, object]:
    return {
        "schema_version": document.schema_version,
        "workflow_id": document.workflow_id,
        "document_revision": document.document_revision,
        "semantic_revision": document.semantic_revision,
        "derived_from": _json_value(document.derived_from),
        "max_parallelism": document.max_parallelism,
        "external_inputs": list(document.external_inputs),
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
                "display_name": node.display_name,
                "entry": node.entry,
                "enabled": node.enabled,
                "skill_ref": node.skill_ref,
                "validator_ref": node.validator_ref,
                "origin_projection_node_id": node.origin_projection_node_id,
                "inputs": list(node.inputs),
                "outputs": list(node.outputs),
                "outcomes": [] if node.type in {"condition", "join"} else list(node.outcomes),
                "write_scopes": list(node.write_scopes),
                "failure_policy": node.failure_policy,
                "condition_cases": _json_value(node.condition_cases),
                "join_mode": node.join_mode,
            }
            for node in document.nodes
        ],
        "edges": [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "trigger": edge.trigger,
                "output_map": dict(edge.output_map),
            }
            for edge in document.edges
        ],
        "ui": _json_value(document.ui),
    }


def _skill_data(value: SkillIdentity | None) -> object:
    if value is None:
        return None
    return {
        "catalog_id": value.catalog_id,
        "root": str(value.root),
        "relative_path": value.relative_path,
        "skill_sha256": value.skill_sha256,
        "tree_sha256": value.tree_sha256,
        "locked": value.locked,
    }


def _validator_data(value: ValidatorIdentity | None) -> object:
    if value is None:
        return None
    return {
        "validator_id": value.validator_id,
        "script": str(value.script),
        "sha256": value.sha256,
        "adapter": value.adapter,
        "input_schema": value.input_schema,
        "control_tags": list(value.control_tags),
        "outcomes": list(value.outcomes),
    }


def _plan_data(plan: CompiledPlan) -> dict[str, object]:
    return {
        "schema_version": "compiled-plan-v1",
        "workflow_id": plan.workflow_id,
        "semantic_revision": plan.semantic_revision,
        "document_sha256": plan.document_sha256,
        "semantic_sha256": plan.semantic_sha256,
        "external_inputs": list(plan.external_inputs),
        "nodes": {
            node_id: {
                "id": node.id,
                "type": node.type,
                "entry": node.entry,
                "skill": _skill_data(node.skill),
                "validator": _validator_data(node.validator),
                "inputs": list(node.inputs),
                "outputs": list(node.outputs),
                "outcomes": list(node.outcomes),
                "write_scopes": list(node.write_scopes),
                "failure_policy": node.failure_policy,
                "condition_cases": _json_value(node.condition_cases),
                "join_mode": node.join_mode,
            }
            for node_id, node in sorted(plan.nodes.items())
        },
        "edges": {
            edge_id: {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "trigger": edge.trigger,
                "output_map": dict(sorted(edge.output_map.items())),
            }
            for edge_id, edge in sorted(plan.edges.items())
        },
        "incoming": {key: list(value) for key, value in sorted(plan.incoming.items())},
        "outgoing": {key: list(value) for key, value in sorted(plan.outgoing.items())},
        "topological_order": list(plan.topological_order),
        "max_parallelism": plan.max_parallelism,
    }


def _string_list(value: object, code: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise StoreError(code, "persisted string list is invalid")
    return tuple(_plain_string(item, code) for item in value)


def _skill_from_data(value: object) -> SkillIdentity | None:
    if value is None:
        return None
    fields = frozenset({"catalog_id", "root", "relative_path", "skill_sha256", "tree_sha256", "locked"})
    item = _exact_mapping(value, fields, "plan.invalid")
    if type(item["locked"]) is not bool:
        raise StoreError("plan.invalid", "persisted Skill lock flag is invalid")
    return SkillIdentity(
        _plain_string(item["catalog_id"], "plan.invalid"),
        Path(_plain_string(item["root"], "plan.invalid")),
        _plain_string(item["relative_path"], "plan.invalid"),
        _plain_string(item["skill_sha256"], "plan.invalid"),
        _plain_string(item["tree_sha256"], "plan.invalid"),
        item["locked"],
    )


def _validator_from_data(value: object) -> ValidatorIdentity | None:
    if value is None:
        return None
    fields = frozenset(
        {"validator_id", "script", "sha256", "adapter", "input_schema", "control_tags", "outcomes"}
    )
    item = _exact_mapping(value, fields, "plan.invalid")
    return ValidatorIdentity(
        _plain_string(item["validator_id"], "plan.invalid"),
        Path(_plain_string(item["script"], "plan.invalid")),
        _plain_string(item["sha256"], "plan.invalid"),
        _plain_string(item["adapter"], "plan.invalid"),
        _plain_string(item["input_schema"], "plan.invalid"),
        _string_list(item["control_tags"], "plan.invalid"),
        _string_list(item["outcomes"], "plan.invalid"),
    )


def _plan_from_data(value: object) -> CompiledPlan:
    fields = frozenset(
        {
            "schema_version",
            "workflow_id",
            "semantic_revision",
            "document_sha256",
            "semantic_sha256",
            "external_inputs",
            "nodes",
            "edges",
            "incoming",
            "outgoing",
            "topological_order",
            "max_parallelism",
        }
    )
    item = _exact_mapping(value, fields, "plan.invalid")
    if item["schema_version"] != "compiled-plan-v1":
        raise StoreError("plan.invalid", "compiled plan version is invalid")
    raw_nodes = item["nodes"]
    raw_edges = item["edges"]
    if not isinstance(raw_nodes, Mapping) or not isinstance(raw_edges, Mapping):
        raise StoreError("plan.invalid", "compiled plan topology is invalid")
    node_fields = frozenset(
        {
            "id", "type", "entry", "skill", "validator", "inputs", "outputs", "outcomes",
            "write_scopes", "failure_policy", "condition_cases", "join_mode",
        }
    )
    nodes: dict[str, CompiledNode] = {}
    for node_id, raw in raw_nodes.items():
        node = _exact_mapping(raw, node_fields, "plan.invalid")
        if node_id != node["id"] or type(node["entry"]) is not bool:
            raise StoreError("plan.invalid", "compiled node identity is invalid")
        cases = node["condition_cases"]
        if not isinstance(cases, list) or not all(isinstance(case, Mapping) for case in cases):
            raise StoreError("plan.invalid", "compiled condition cases are invalid")
        nodes[str(node_id)] = CompiledNode(
            str(node["id"]),
            _plain_string(node["type"], "plan.invalid"),
            node["entry"],
            _skill_from_data(node["skill"]),
            _validator_from_data(node["validator"]),
            _string_list(node["inputs"], "plan.invalid"),
            _string_list(node["outputs"], "plan.invalid"),
            _string_list(node["outcomes"], "plan.invalid"),
            _string_list(node["write_scopes"], "plan.invalid"),
            _plain_string(node["failure_policy"], "plan.invalid"),
            tuple(_freeze_json(case) for case in cases),
            _plain_string(node["join_mode"], "plan.invalid"),
        )
    edge_fields = frozenset({"id", "source", "target", "trigger", "output_map"})
    edges: dict[str, CompiledEdge] = {}
    for edge_id, raw in raw_edges.items():
        edge = _exact_mapping(raw, edge_fields, "plan.invalid")
        output_map = edge["output_map"]
        if edge_id != edge["id"] or not isinstance(output_map, Mapping) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in output_map.items()
        ):
            raise StoreError("plan.invalid", "compiled edge identity is invalid")
        edges[str(edge_id)] = CompiledEdge(
            str(edge["id"]),
            _plain_string(edge["source"], "plan.invalid"),
            _plain_string(edge["target"], "plan.invalid"),
            _plain_string(edge["trigger"], "plan.invalid"),
            MappingProxyType(dict(output_map)),
        )
    incoming = item["incoming"]
    outgoing = item["outgoing"]
    if not isinstance(incoming, Mapping) or not isinstance(outgoing, Mapping):
        raise StoreError("plan.invalid", "compiled adjacency is invalid")
    plan = CompiledPlan(
        _plain_string(item["workflow_id"], "plan.invalid"),
        _integer(item["semantic_revision"], "plan.invalid", minimum=1),
        _lower_sha256(item["document_sha256"], "plan.invalid"),
        _lower_sha256(item["semantic_sha256"], "plan.invalid"),
        _string_list(item["external_inputs"], "plan.invalid"),
        MappingProxyType(nodes),
        MappingProxyType(edges),
        MappingProxyType({str(key): _string_list(items, "plan.invalid") for key, items in incoming.items()}),
        MappingProxyType({str(key): _string_list(items, "plan.invalid") for key, items in outgoing.items()}),
        _string_list(item["topological_order"], "plan.invalid"),
        _integer(item["max_parallelism"], "plan.invalid", minimum=1),
    )
    if set(plan.nodes) != set(plan.incoming) or set(plan.nodes) != set(plan.outgoing):
        raise StoreError("plan.invalid", "compiled plan node and adjacency keys differ")
    if len(plan.topological_order) != len(plan.nodes) or set(plan.topological_order) != set(plan.nodes):
        raise StoreError("plan.invalid", "compiled topological order is not a node permutation")
    seen_incoming: set[str] = set()
    seen_outgoing: set[str] = set()
    for node_id in plan.nodes:
        for edge_id in plan.incoming[node_id]:
            edge = plan.edges.get(edge_id)
            if edge is None or edge.target != node_id or edge_id in seen_incoming:
                raise StoreError("plan.invalid", "compiled incoming adjacency is inconsistent")
            seen_incoming.add(edge_id)
        for edge_id in plan.outgoing[node_id]:
            edge = plan.edges.get(edge_id)
            if edge is None or edge.source != node_id or edge_id in seen_outgoing:
                raise StoreError("plan.invalid", "compiled outgoing adjacency is inconsistent")
            seen_outgoing.add(edge_id)
    if seen_incoming != set(plan.edges) or seen_outgoing != set(plan.edges):
        raise StoreError("plan.invalid", "compiled adjacency does not cover every edge")
    return plan


def _validated_plan_data(plan: CompiledPlan) -> tuple[dict[str, object], CompiledPlan, str]:
    try:
        raw = _plan_data(plan)
        encoded = _canonical_bytes(raw)
        if len(encoded) > MAX_JSON_BYTES:
            raise StoreError("plan.invalid", "compiled plan exceeds the bounded material size")
        restored = _plan_from_data(raw)
    except (RecursionError, TypeError, ValueError, StoreError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError("plan.invalid", "compiled plan exceeds the supported nesting depth") from exc
    if encoded != _canonical_bytes(_plan_data(restored)):
        raise StoreError("plan.invalid", "compiled plan does not round-trip canonically")
    return raw, restored, _sha256(raw)


def _state_data(state: RunState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "workflow_id": state.workflow_id,
        "semantic_sha256": state.semantic_sha256,
        "nodes": {
            node_id: {
                "status": runtime.status.value,
                "attempt": runtime.attempt,
                "outcome": runtime.outcome,
                "claim_token_hash": runtime.claim_token_hash,
                "selected_inputs": dict(runtime.selected_inputs),
                "outputs": dict(runtime.outputs),
                "auxiliary_outputs": {
                    key: list(items) for key, items in runtime.auxiliary_outputs.items()
                },
                "winner_edge_id": runtime.winner_edge_id,
            }
            for node_id, runtime in sorted(state.nodes.items())
        },
        "edges": {
            edge_id: {
                "status": runtime.status.value,
                "selected_output_map": dict(runtime.selected_output_map),
            }
            for edge_id, runtime in sorted(state.edges.items())
        },
        "artifacts": {
            artifact_id: {
                "artifact_id": artifact.artifact_id,
                "path": artifact.path,
                "sha256": artifact.sha256,
                "state": artifact.state,
                "producer_node_id": artifact.producer_node_id,
                "producer_attempt": artifact.producer_attempt,
            }
            for artifact_id, artifact in sorted(state.artifacts.items())
        },
        "decisions": dict(state.decisions),
        "project_booleans": dict(state.project_booleans),
    }


def _string_map(value: object, code: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise StoreError(code, "persisted string mapping is invalid")
    return MappingProxyType(
        {
            _plain_string(key, code): _plain_string(item, code)
            for key, item in value.items()
        }
    )


def _state_from_data(value: object) -> RunState:
    fields = frozenset(
        {"run_id", "workflow_id", "semantic_sha256", "nodes", "edges", "artifacts", "decisions", "project_booleans"}
    )
    item = _exact_mapping(value, fields, "snapshot.invalid")
    raw_nodes = item["nodes"]
    raw_edges = item["edges"]
    raw_artifacts = item["artifacts"]
    if not isinstance(raw_nodes, Mapping) or not isinstance(raw_edges, Mapping) or not isinstance(raw_artifacts, Mapping):
        raise StoreError("snapshot.invalid", "runtime collections are invalid")
    node_fields = frozenset(
        {"status", "attempt", "outcome", "claim_token_hash", "selected_inputs", "outputs", "auxiliary_outputs", "winner_edge_id"}
    )
    nodes: dict[str, NodeRuntime] = {}
    for node_id, raw in raw_nodes.items():
        runtime = _exact_mapping(raw, node_fields, "snapshot.invalid")
        auxiliary = runtime["auxiliary_outputs"]
        if not isinstance(auxiliary, Mapping):
            raise StoreError("snapshot.invalid", "runtime auxiliary outputs are invalid")
        try:
            status = NodeStatus(runtime["status"])
        except (TypeError, ValueError) as exc:
            raise StoreError("snapshot.invalid", "runtime node status is invalid") from exc
        nodes[str(node_id)] = NodeRuntime(
            status,
            _integer(runtime["attempt"], "snapshot.invalid"),
            _plain_string(runtime["outcome"], "snapshot.invalid", allow_empty=True),
            _lower_sha256(
                runtime["claim_token_hash"], "snapshot.invalid", allow_empty=True
            ),
            _string_map(runtime["selected_inputs"], "snapshot.invalid"),
            _string_map(runtime["outputs"], "snapshot.invalid"),
            MappingProxyType(
                {str(key): _string_list(items, "snapshot.invalid") for key, items in auxiliary.items()}
            ),
            _plain_string(runtime["winner_edge_id"], "snapshot.invalid", allow_empty=True),
        )
    edge_fields = frozenset({"status", "selected_output_map"})
    edges: dict[str, EdgeRuntime] = {}
    for edge_id, raw in raw_edges.items():
        runtime = _exact_mapping(raw, edge_fields, "snapshot.invalid")
        try:
            status = EdgeStatus(runtime["status"])
        except (TypeError, ValueError) as exc:
            raise StoreError("snapshot.invalid", "runtime edge status is invalid") from exc
        edges[str(edge_id)] = EdgeRuntime(status, _string_map(runtime["selected_output_map"], "snapshot.invalid"))
    artifact_fields = frozenset(
        {"artifact_id", "path", "sha256", "state", "producer_node_id", "producer_attempt"}
    )
    artifacts: dict[str, ArtifactRuntime] = {}
    for artifact_id, raw in raw_artifacts.items():
        artifact = _exact_mapping(raw, artifact_fields, "snapshot.invalid")
        parsed_artifact_id = _plain_string(artifact_id, "snapshot.invalid")
        if parsed_artifact_id != artifact["artifact_id"]:
            raise StoreError("snapshot.invalid", "runtime artifact identity is invalid")
        artifact_state = _plain_string(artifact["state"], "snapshot.invalid")
        if artifact_state not in {"verified", "stale"}:
            raise StoreError("snapshot.invalid", "runtime artifact state is invalid")
        artifacts[parsed_artifact_id] = ArtifactRuntime(
            _plain_string(artifact["artifact_id"], "snapshot.invalid"),
            _plain_string(artifact["path"], "snapshot.invalid"),
            _lower_sha256(artifact["sha256"], "snapshot.invalid"),
            artifact_state,
            _plain_string(artifact["producer_node_id"], "snapshot.invalid", allow_empty=True),
            _integer(artifact["producer_attempt"], "snapshot.invalid"),
        )
    decisions = item["decisions"]
    project_booleans = item["project_booleans"]
    if not isinstance(decisions, Mapping) or not isinstance(project_booleans, Mapping):
        raise StoreError("snapshot.invalid", "runtime facts are invalid")
    try:
        return RunState(
            _run_id(item["run_id"], "snapshot.invalid"),
            _plain_string(item["workflow_id"], "snapshot.invalid"),
            _lower_sha256(item["semantic_sha256"], "snapshot.invalid"),
            MappingProxyType(nodes),
            MappingProxyType(edges),
            MappingProxyType(artifacts),
            MappingProxyType(dict(decisions)),
            MappingProxyType(dict(project_booleans)),
        )
    except (TypeError, ValueError, WorkflowError) as exc:
        raise StoreError("snapshot.invalid", str(exc)) from exc


def _validated_state_data(state: RunState) -> tuple[dict[str, object], RunState]:
    raw = _state_data(state)
    try:
        restored = _state_from_data(raw)
    except RecursionError as exc:
        raise StoreError("snapshot.invalid", "runtime state exceeds the supported nesting depth") from exc
    if _canonical_bytes(raw) != _canonical_bytes(_state_data(restored)):
        raise StoreError("snapshot.invalid", "runtime state does not round-trip canonically")
    return raw, restored


def _snapshot_data(
    state: RunState,
    event_seq: int,
    event_hash: str,
    *,
    run_status: str,
    plan_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": "run-snapshot-v1",
        "run_status": run_status,
        "last_applied_event_seq": event_seq,
        "last_applied_event_hash": event_hash,
        "plan_sha256": plan_sha256,
        "state": _state_data(state),
    }


def _parse_snapshot(value: object) -> tuple[RunState, int, str, str, str]:
    item = _exact_mapping(value, _SNAPSHOT_FIELDS, "snapshot.invalid")
    if item["schema_version"] != "run-snapshot-v1":
        raise StoreError("snapshot.invalid", "runtime snapshot version is invalid")
    run_status = _plain_string(item["run_status"], "snapshot.invalid")
    if run_status not in {"active", "stopped", "archived"}:
        raise StoreError("snapshot.invalid", "runtime snapshot status is invalid")
    sequence = _integer(item["last_applied_event_seq"], "snapshot.invalid", minimum=1)
    event_hash = _lower_sha256(item["last_applied_event_hash"], "snapshot.invalid")
    plan_sha256 = _lower_sha256(item["plan_sha256"], "snapshot.invalid")
    state = _state_from_data(item["state"])
    _validated_state_data(state)
    return state, sequence, event_hash, run_status, plan_sha256


def _validate_state_binding(plan: CompiledPlan, state: RunState) -> None:
    if (
        state.workflow_id != plan.workflow_id
        or state.semantic_sha256 != plan.semantic_sha256
        or set(state.nodes) != set(plan.nodes)
        or set(state.edges) != set(plan.edges)
    ):
        raise StoreError(
            "snapshot.plan_mismatch",
            "runtime state does not match the compiled plan",
        )


def _validate_artifact_authority(plan: CompiledPlan, state: RunState) -> None:
    for artifact_id, artifact in state.artifacts.items():
        if artifact.producer_node_id == _EXTERNAL_PRODUCER:
            valid = (
                artifact_id in plan.external_inputs
                and artifact.producer_attempt == 0
            )
        else:
            producer = plan.nodes.get(artifact.producer_node_id)
            runtime = state.nodes.get(artifact.producer_node_id)
            valid = (
                producer is not None
                and runtime is not None
                and artifact_id in producer.outputs
                and artifact.producer_attempt > 0
                and artifact.producer_attempt == runtime.attempt
                and runtime.outputs.get(artifact_id) == artifact.path
                and (
                    artifact.state != "verified"
                    or runtime.status is NodeStatus.SUCCEEDED
                )
            )
        if not valid:
            raise StoreError(
                "artifact.authority_invalid",
                f"artifact receipt has no compiled producer authority: {artifact_id}",
            )


def _validate_edge_and_winner_state(plan: CompiledPlan, state: RunState) -> None:
    """Bind live routes to source outputs and a frozen, explicit join edge."""
    for edge_id, runtime in state.edges.items():
        edge = plan.edges[edge_id]
        source = state.nodes[edge.source]
        if runtime.status is EdgeStatus.SATISFIED:
            if source.status is not NodeStatus.SUCCEEDED or edge.trigger != source.outcome:
                raise StoreError("edge.authority_invalid", f"satisfied edge has no successful source: {edge_id}")
            expected = _edge_outputs(plan, edge, source.outputs)
            if runtime.selected_output_map != expected:
                raise StoreError("edge.authority_invalid", f"edge output map differs from its source: {edge_id}")
        elif runtime.selected_output_map:
            raise StoreError("edge.authority_invalid", f"non-satisfied edge retains an output route: {edge_id}")
    for node_id, runtime in state.nodes.items():
        node = plan.nodes[node_id]
        is_any = node.type == "join" and node.join_mode == "any_success"
        if not is_any:
            if runtime.winner_edge_id:
                raise StoreError("join.winner_invalid", f"non-any-success node has a winner: {node_id}")
            continue
        winner = runtime.winner_edge_id
        if winner and winner not in plan.incoming[node_id]:
            raise StoreError("join.winner_invalid", f"winner is not an incoming edge: {node_id}")
        if runtime.status in {NodeStatus.READY, NodeStatus.SUCCEEDED}:
            if not winner or state.edges[winner].status is not EdgeStatus.SATISFIED:
                raise StoreError("join.winner_invalid", f"ready or completed join has no live winner: {node_id}")
            selected = state.edges[winner].selected_output_map
            if runtime.selected_inputs != selected or (
                runtime.status is NodeStatus.SUCCEEDED and runtime.outputs != selected
            ):
                raise StoreError("join.winner_invalid", f"join output differs from its frozen winner: {node_id}")
        elif winner and runtime.status is not NodeStatus.STALE:
            raise StoreError("join.winner_invalid", f"frozen join cannot silently return to pending: {node_id}")
        if runtime.status is NodeStatus.STALE and runtime.selected_inputs:
            raise StoreError("join.winner_invalid", f"stale join retains a selected input route: {node_id}")
        if runtime.status is NodeStatus.STALE and any(
            state.edges[edge_id].status is EdgeStatus.SATISFIED
            for edge_id in plan.outgoing[node_id]
        ):
            raise StoreError("join.winner_invalid", f"stale join retains a live outgoing route: {node_id}")


def _validate_completion_transition(
    plan: CompiledPlan,
    previous: RunState,
    current: RunState,
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    """Only a scheduler-equivalent result/control event can mint a live edge."""
    if event_type == "node_result_recorded":
        changed = [
            node_id for node_id in plan.nodes
            if previous.nodes[node_id].status is NodeStatus.RUNNING
            and current.nodes[node_id].status in {
                NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED
            }
        ]
        if len(changed) != 1:
            raise StoreError("events.invalid_completion", "receipted result needs one running task completion")
        node_id = changed[0]
        node = plan.nodes[node_id]
        after = current.nodes[node_id]
        if node.type != "task":
            raise StoreError("events.invalid_completion", "node_result source is not a task")
        receipt_artifacts = [
            artifact for artifact in current.artifacts.values()
            if artifact.producer_node_id == node_id and artifact.producer_attempt == after.attempt
        ]
        result = {
            "node_id": node_id,
            "attempt": after.attempt,
            "status": "succeeded" if after.status is NodeStatus.SUCCEEDED else "failed",
            "outcome": after.outcome,
            "outputs": dict(after.outputs) if after.status is NodeStatus.SUCCEEDED else {},
            "artifacts": receipt_artifacts if after.status is NodeStatus.SUCCEEDED else [],
        }
        try:
            expected = result_transition(plan, previous, result)
        except WorkflowError as exc:
            raise StoreError("events.invalid_completion", str(exc)) from exc
        if _state_data(expected) != _state_data(current):
            raise StoreError("events.invalid_completion", "node_result differs from the scheduler transition")
    elif event_type in {"condition_selected", "join_succeeded"}:
        if not {"node_id", "outcome", "edge_updates"}.issubset(payload):
            raise StoreError("events.invalid_completion", "control completion lacks declared transition fields")
        node_id = payload["node_id"]
        if not isinstance(node_id, str) or node_id not in plan.nodes:
            raise StoreError("events.invalid_completion", "control completion names an unknown node")
        node = plan.nodes[node_id]
        runtime = previous.nodes[node_id]
        expected_type = "condition_selected" if node.type == "condition" else "join_succeeded"
        if node.type not in {"condition", "join"} or event_type != expected_type or runtime.status is not NodeStatus.READY:
            raise StoreError("events.invalid_completion", "control completion has the wrong ready source")
        outcome = (
            evaluate_condition(node.condition_cases, condition_facts(previous))
            if node.type == "condition" else "succeeded"
        )
        if payload["outcome"] != outcome:
            raise StoreError("events.invalid_completion", "control outcome differs from its declared evaluation")
        expected, transition = _complete_control(plan, previous, node_id, outcome)
        if payload["edge_updates"] != {key: value.value for key, value in transition.edge_updates.items()} or _state_data(expected) != _state_data(current):
            raise StoreError("events.invalid_completion", "control completion differs from the scheduler transition")


def _validate_edge_and_winner_transition(
    plan: CompiledPlan,
    previous: RunState | None,
    current: RunState,
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    _validate_edge_and_winner_state(plan, current)
    if previous is None:
        if _state_data(current) != _state_data(initial_run(plan, current.run_id)):
            raise StoreError("events.invalid_completion", "run start differs from the scheduler's initial state")
        if any(runtime.winner_edge_id for runtime in current.nodes.values()) or any(
            runtime.status is EdgeStatus.SATISFIED for runtime in current.edges.values()
        ):
            raise StoreError("join.winner_invalid", "run start cannot contain completed routes")
        return
    newly_satisfied = {
        edge_id for edge_id, runtime in current.edges.items()
        if runtime.status is EdgeStatus.SATISFIED
        and previous.edges[edge_id].status is not EdgeStatus.SATISFIED
    }
    newly_succeeded = {
        node_id for node_id, runtime in current.nodes.items()
        if runtime.status is NodeStatus.SUCCEEDED
        and previous.nodes[node_id].status is not NodeStatus.SUCCEEDED
    }
    if newly_succeeded and event_type not in {"node_result_recorded", "condition_selected", "join_succeeded"}:
        raise StoreError("events.invalid_completion", "non-completion event cannot finish a node")
    for edge_id, runtime in current.edges.items():
        before = previous.edges[edge_id]
        if before.status is EdgeStatus.SATISFIED and runtime.status is EdgeStatus.SATISFIED and before != runtime:
            raise StoreError("edge.authority_invalid", f"live edge map changed without a new completion: {edge_id}")
        if (
            before.status is EdgeStatus.SATISFIED
            and runtime.status is not EdgeStatus.SATISFIED
            and current.nodes[plan.edges[edge_id].source].status is NodeStatus.SUCCEEDED
        ):
            raise StoreError("edge.authority_invalid", f"completed source lost its satisfied route: {edge_id}")
    if newly_satisfied and event_type not in {"node_result_recorded", "condition_selected", "join_succeeded"}:
        raise StoreError("events.invalid_completion", "non-completion event cannot create a satisfied edge")
    if event_type in {"node_result_recorded", "condition_selected", "join_succeeded"}:
        _validate_completion_transition(plan, previous, current, event_type, payload)
    for node_id, runtime in current.nodes.items():
        node = plan.nodes[node_id]
        if node.type != "join" or node.join_mode != "any_success":
            continue
        before = previous.nodes[node_id]
        if before.winner_edge_id:
            if runtime.winner_edge_id != before.winner_edge_id:
                raise StoreError("join.winner_invalid", f"frozen winner changed: {node_id}")
        elif runtime.winner_edge_id:
            incoming_new = set(plan.incoming[node_id]) & newly_satisfied
            if incoming_new != {runtime.winner_edge_id}:
                raise StoreError("join.winner_invalid", f"join did not select a unique first satisfaction: {node_id}")
        elif runtime.status in {NodeStatus.READY, NodeStatus.SUCCEEDED}:
            raise StoreError("join.winner_invalid", f"join became ready without a winner: {node_id}")


EdgeWitnesses = dict[str, dict[str, ArtifactRuntime]]


def validate_node_evidence_delta(
    plan: CompiledPlan,
    before: RunState | None,
    after: RunState,
    event_type: str,
    payload: Mapping[str, object],
    prior_events: Sequence[WorkflowEvent],
    witnesses: EdgeWitnesses,
) -> None:
    """Check claims/completions across every event name, both commit and replay."""
    if before is None:
        return
    derived = None
    for node_id, node in plan.nodes.items():
        if node.type not in {"task", "validator"}:
            continue
        earlier, current = before.nodes[node_id], after.nodes[node_id]
        if current.status is NodeStatus.SKIPPED and earlier.status not in {NodeStatus.RUNNING, NodeStatus.SKIPPED}:
            if event_type not in {"node_result_recorded", "condition_selected", "join_succeeded"}:
                if event_type not in {"readiness_refreshed", "artifact_registered", "fact_recorded", "decision_recorded"}:
                    raise StoreError("events.invalid_completion", "event cannot exclude a task branch")
                if derived is None:
                    derived = refresh_ready(plan, replace(before, artifacts=after.artifacts,
                                                          decisions=after.decisions, project_booleans=after.project_booleans))
                if current != derived.nodes[node_id]:
                    raise StoreError("events.invalid_completion", "skipped task is not a derived branch exclusion")
        if current.status is NodeStatus.FAILED and earlier.status is not NodeStatus.FAILED:
            if earlier.status is not NodeStatus.RUNNING or event_type != "node_result_recorded":
                raise StoreError("events.invalid_completion", "execution failure requires a receipted running attempt")
        if (earlier.status is current.status and earlier.status in {
                NodeStatus.RUNNING, NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED
            } and earlier != current):
            raise StoreError("events.invalid_evidence", "an existing attempt's frozen evidence changed")
        if earlier.status is NodeStatus.RUNNING and current.status is NodeStatus.BLOCKED and event_type != "recovery_running_blocked":
            raise StoreError("events.invalid_evidence", "only recovery may block an interrupted attempt")
        if earlier.attempt != current.attempt and event_type != "node_claimed":
            raise StoreError("events.invalid_claim", "only a claim may advance an attempt")
    for artifact_id, artifact in after.artifacts.items():
        earlier = before.artifacts.get(artifact_id)
        if (artifact != earlier and artifact.producer_node_id != _EXTERNAL_PRODUCER
                and artifact.state == "verified" and event_type != "node_result_recorded"):
            raise StoreError("artifact.authority_invalid", "only a receipted result may introduce verified produced evidence")
    claims = [node_id for node_id in plan.nodes if after.nodes[node_id].status is NodeStatus.RUNNING
              and (before.nodes[node_id].status is not NodeStatus.RUNNING
                   or before.nodes[node_id].attempt != after.nodes[node_id].attempt
                   or before.nodes[node_id].claim_token_hash != after.nodes[node_id].claim_token_hash)]
    terminals = [node_id for node_id in plan.nodes if before.nodes[node_id].status is NodeStatus.RUNNING
                 and after.nodes[node_id].status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED}]
    try:
        if claims or event_type == "node_claimed":
            if len(claims) != 1 or event_type != "node_claimed":
                raise StoreError("events.invalid_claim", "running attempt requires a dedicated claim event")
            node_id = claims[0]
            _lower_sha256(after.nodes[node_id].claim_token_hash, "events.invalid_claim")
            claim = payload.get("claim_evidence")
            if not isinstance(claim, Mapping):
                raise StoreError("events.invalid_claim", "claim evidence is missing")
            started = claim.get("started_at")
            if not isinstance(started, str) or not started.endswith("Z"):
                raise StoreError("events.invalid_claim", "claim start timestamp is invalid")
            try:
                datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError as exc:
                raise StoreError("events.invalid_claim", "claim start timestamp is invalid") from exc
            expected = claim_transition(plan, before, node_id, "evidence-verification")
            nodes = dict(expected.nodes)
            nodes[node_id] = replace(nodes[node_id], claim_token_hash=after.nodes[node_id].claim_token_hash)
            expected = replace(expected, nodes=nodes)
            if expected != after or _json_value(claim) != build_claim_evidence(plan, after, node_id, witnesses, started):
                raise StoreError("events.invalid_claim", "claim differs from scheduler or frozen input evidence")
        if terminals or event_type == "node_result_recorded":
            if len(terminals) != 1 or event_type != "node_result_recorded":
                raise StoreError("events.invalid_completion", "terminal attempt requires a receipted completion event")
            node_id = terminals[0]
            receipt = validate_stage_receipt(payload.get("receipt"))
            digest = _lower_sha256(payload.get("result_sha256"), "events.invalid_completion")
            claim_seq = payload.get("claim_event_seq")
            matching = [event for event in prior_events if event.event_type == "node_claimed"
                        and event.payload.get("claim_evidence", {}).get("node_id") == node_id
                        and event.payload.get("claim_evidence", {}).get("attempt") == after.nodes[node_id].attempt]
            if len(matching) != 1 or type(claim_seq) is not int or matching[0].event_seq != claim_seq:
                raise StoreError("events.invalid_completion", "completion lacks one matching claim")
            if any(event.event_type == "node_result_recorded" and event.payload.get("claim_event_seq") == claim_seq for event in prior_events):
                raise StoreError("events.invalid_completion", "attempt already completed")
            claim = matching[0].payload["claim_evidence"]
            if before.nodes[node_id].claim_token_hash != claim["claim_token_sha256"]:
                raise StoreError("events.invalid_completion", "claim token hash differs from running attempt")
            expected = build_stage_receipt(plan, after, node_id, claim, summary=receipt["summary"],
                                           uncertainties=receipt["uncertainties"], completed_at=receipt["completed_at"], error=receipt["error"])
            if receipt != expected or not digest:
                raise StoreError("events.invalid_completion", "receipt differs from claim and post-state")
    except (ReceiptError, WorkflowError, KeyError, TypeError, ValueError) as exc:
        raise StoreError("events.invalid_evidence", str(exc)) from exc


def _edge_witnesses_after_step(
    plan: CompiledPlan,
    previous: RunState | None,
    current: RunState,
    prior: EdgeWitnesses,
) -> EdgeWitnesses:
    """Derive historical source receipts only at the edge's completion event.

    The registry is a *current* view: a later branch may legally replace the
    same logical ID.  Retained edges therefore keep the earlier event receipt.
    """
    result: EdgeWitnesses = {}
    for edge_id, runtime in current.edges.items():
        if runtime.status is not EdgeStatus.SATISFIED:
            continue
        if previous is not None and previous.edges[edge_id].status is EdgeStatus.SATISFIED:
            if edge_id not in prior:
                raise StoreError("edge.witness_missing", f"live edge lacks historical proof: {edge_id}")
            result[edge_id] = dict(prior[edge_id])
            continue
        edge = plan.edges[edge_id]
        source_node = plan.nodes[edge.source]
        source = current.nodes[edge.source]
        proofs: dict[str, ArtifactRuntime] = {}
        for output_id, path in source.outputs.items():
            target_id = edge.output_map.get(output_id, output_id)
            if runtime.selected_output_map.get(target_id) != path:
                continue
            if source_node.type == "task":
                artifact = current.artifacts.get(output_id)
                if (
                    artifact is None
                    or artifact.state != "verified"
                    or artifact.path != path
                    or artifact.producer_node_id != edge.source
                    or artifact.producer_attempt != source.attempt
                ):
                    raise StoreError("edge.witness_missing", f"task edge has no completion hash witness: {edge_id}")
                proof = artifact
            elif source_node.type == "join":
                incoming_ids = (
                    (source.winner_edge_id,)
                    if source_node.join_mode == "any_success"
                    else plan.incoming[edge.source]
                )
                matches = [
                    prior[incoming_id][output_id]
                    for incoming_id in incoming_ids
                    if incoming_id in prior
                    and prior[incoming_id].get(output_id) is not None
                    and current.edges[incoming_id].selected_output_map.get(output_id) == path
                ]
                # Fan-out/reconvergence may carry the same original receipt on
                # multiple routes. Only distinct full provenance is ambiguous.
                if len(set(matches)) != 1:
                    raise StoreError("edge.witness_missing", f"join edge cannot trace one incoming receipt: {edge_id}")
                proof = matches[0]
            else:
                raise StoreError("edge.witness_missing", f"file-carrying edge lacks a supported source receipt: {edge_id}")
            if target_id in proofs and proofs[target_id] != proof:
                raise StoreError("edge.witness_missing", f"edge has conflicting source receipts: {edge_id}")
            proofs[target_id] = proof
        if set(proofs) != set(runtime.selected_output_map):
            raise StoreError("edge.witness_missing", f"file-carrying edge lacks historical hash proof: {edge_id}")
        result[edge_id] = proofs
    return result


def _preflight_json_output(value: object, code: str) -> object:
    try:
        encoded = _canonical_bytes(value, newline=True)
        decoded = _strict_json_loads(encoded)
    except StoreError:
        raise
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreError(code, "output material is not bounded canonical JSON") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise StoreError(code, "output material exceeds the bounded JSON size")
    if _canonical_bytes(decoded, newline=True) != encoded:
        raise StoreError(code, "output material does not round-trip canonically")
    return decoded


def _projection_material(
    state: RunState,
    run_status: str,
) -> tuple[bytes, bytes]:
    artifacts = {
        "schema_version": "artifact-projection-v1",
        "run_id": state.run_id,
        "semantic_sha256": state.semantic_sha256,
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "path": item.path,
                "sha256": item.sha256,
                "state": item.state,
                "producer_node_id": item.producer_node_id,
                "producer_attempt": item.producer_attempt,
            }
            for _key, item in sorted(state.artifacts.items())
        ],
    }
    decoded_artifacts = _preflight_json_output(
        artifacts, "projection.input_too_large"
    )
    artifact_bytes = _canonical_bytes(decoded_artifacts, newline=True)
    lines = [
        "# Custom Workflow Run",
        "",
        f"- Run: `{state.run_id}`",
        f"- Workflow: `{state.workflow_id}`",
        f"- Status: `{run_status}`",
        f"- Semantic SHA-256: `{state.semantic_sha256}`",
        "",
        "## Nodes",
        "",
    ]
    lines.extend(
        f"- `{node_id}`: `{runtime.status.value}` (attempt {runtime.attempt})"
        for node_id, runtime in sorted(state.nodes.items())
    )
    lines.extend(["", "## Artifacts", ""])
    if state.artifacts:
        lines.extend(
            f"- `{artifact_id}`: `{artifact.state}` at `{artifact.path}`"
            for artifact_id, artifact in sorted(state.artifacts.items())
        )
    else:
        lines.append("- None recorded.")
    summary_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    if len(summary_bytes) > MAX_JSON_BYTES:
        raise StoreError(
            "projection.input_too_large", "summary projection exceeds the bounded size"
        )
    return artifact_bytes, summary_bytes


class WorkflowStore:
    def __init__(self, project_root: Path | str) -> None:
        supplied = Path(project_root).expanduser()
        if supplied.is_symlink():
            raise StoreError("path.unsafe", "project root must not be a symlink")
        try:
            root = supplied.resolve(strict=True)
        except OSError as exc:
            raise StoreError("path.unsafe", "project root must exist") from exc
        if not root.is_dir():
            raise StoreError("path.unsafe", "project root must be a directory")
        self.project_root = root
        root_stat = root.stat(follow_symlinks=False)
        self._project_root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._active_lease_token: object | None = None
        self._active_lease_owner: tuple[int, int] | None = None
        self._active_lock_handle: Any | None = None
        self._active_lock_identity: tuple[int, int] | None = None
        self._active_lease_invalidated = False
        base = root / ".research/custom-workflow"
        run_dir = base / "active-run"
        self.paths = StorePaths(
            base=base,
            lock=base / ".lock",
            selection=base / "selection.json",
            workflow=base / "workflow.json",
            revisions=base / "revisions",
            audit_events=base / "activation-events.jsonl",
            run_dir=run_dir,
            plan=run_dir / "plan.json",
            state=run_dir / "state.json",
            events=run_dir / "events.jsonl",
            artifacts=run_dir / "artifacts.json",
            summary=run_dir / "summary.md",
            recovery=run_dir / "recovery",
            archived_runs=base / "runs",
            receipts=base / "receipts",
        )

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise StoreError("path.unsafe", "store path escapes the project") from exc

    def _checked(self, path: Path) -> Path:
        try:
            return resolve_project_path(self.project_root, self._relative(path))
        except PathSafetyError as exc:
            raise StoreError("path.unsafe", str(exc)) from exc

    def _ensure_directory(self, path: Path) -> Path:
        try:
            return ensure_project_directory(self.project_root, self._relative(path))
        except PathSafetyError as exc:
            raise StoreError("path.unsafe", str(exc)) from exc

    def _ensure_base(self) -> None:
        self._ensure_directory(self.paths.base)

    def _ensure_locked_directory(self, path: Path) -> Path:
        self._assert_active_lease()
        result = self._ensure_directory(path)
        self._assert_active_lease()
        return result

    @contextmanager
    def _lock(self, *, timeout: float = 20.0) -> Iterator[object]:
        owner = (os.getpid(), threading.get_ident())
        registry_key = (str(self.project_root), owner[0], owner[1])
        with _LOCK_REGISTRY_GUARD:
            if registry_key in _LOCK_REGISTRY:
                raise StoreError("store.lock_reentrant", "workflow lock is already held by this thread")
        self._ensure_base()
        lock_path = self._checked(self.paths.lock)
        lock_existed = lock_path.exists() or lock_path.is_symlink()
        if lock_existed:
            existing_lock = lock_path.lstat()
            if not stat.S_ISREG(existing_lock.st_mode) or existing_lock.st_nlink != 1:
                raise StoreError("path.unsafe", "project lock is not a single-name regular file")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise StoreError("path.unsafe", "could not open project lock safely") from exc
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        lock_acquired = False
        registered = False
        try:
            opened_lock = os.fstat(handle.fileno())
            named_lock = lock_path.lstat()
            if (
                not stat.S_ISREG(named_lock.st_mode)
                or not stat.S_ISREG(opened_lock.st_mode)
                or named_lock.st_nlink != 1
                or opened_lock.st_nlink != 1
                or (opened_lock.st_dev, opened_lock.st_ino)
                != (named_lock.st_dev, named_lock.st_ino)
            ):
                raise StoreError("path.unsafe", "project lock path changed while opening")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            if not lock_existed:
                _fsync_directory(lock_path.parent)
            started = time.monotonic()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                    break
                except (OSError, BlockingIOError) as exc:
                    if time.monotonic() - started >= timeout:
                        raise StoreError("store.lock_timeout", "timed out waiting for workflow lock") from exc
                    time.sleep(0.05)
            self._checked(self.paths.lock)
            named_lock = lock_path.lstat()
            if (
                named_lock.st_nlink != 1
                or (opened_lock.st_dev, opened_lock.st_ino) != (named_lock.st_dev, named_lock.st_ino)
            ):
                raise StoreError("path.unsafe", "project lock path changed while acquiring")
            token = object()
            with _LOCK_REGISTRY_GUARD:
                if registry_key in _LOCK_REGISTRY:
                    raise StoreError("store.lock_reentrant", "workflow lock lease was duplicated")
                _LOCK_REGISTRY.add(registry_key)
                registered = True
            self._active_lease_token = token
            self._active_lease_owner = owner
            self._active_lock_handle = handle
            self._active_lock_identity = (opened_lock.st_dev, opened_lock.st_ino)
            self._active_lease_invalidated = False
            yield token
        finally:
            self._active_lease_token = None
            self._active_lease_owner = None
            self._active_lock_handle = None
            self._active_lock_identity = None
            self._active_lease_invalidated = False
            if registered:
                with _LOCK_REGISTRY_GUARD:
                    _LOCK_REGISTRY.discard(registry_key)
            try:
                if lock_acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _assert_active_lease(
        self,
        token: object | None = None,
        owner: tuple[int, int] | None = None,
    ) -> None:
        current_owner = (os.getpid(), threading.get_ident())
        expected_owner = self._active_lease_owner
        if (
            self._active_lease_token is None
            or expected_owner is None
            or self._active_lock_handle is None
            or self._active_lock_identity is None
            or current_owner != expected_owner
            or (token is not None and token is not self._active_lease_token)
            or (owner is not None and owner != expected_owner)
        ):
            raise StoreError(
                "store.lock_required",
                "authoritative workflow access requires the current lock lease",
            )
        if self._active_lease_invalidated:
            raise StoreError(
                "store.lock_invalidated", "the named workflow lock lease was invalidated"
            )
        try:
            root_stat = self.project_root.stat(follow_symlinks=False)
            opened = os.fstat(self._active_lock_handle.fileno())
            named = self.paths.lock.lstat()
            named_attributes = getattr(named, "st_file_attributes", 0)
            opened_attributes = getattr(opened, "st_file_attributes", 0)
            valid = (
                stat.S_ISDIR(root_stat.st_mode)
                and (root_stat.st_dev, root_stat.st_ino)
                == self._project_root_identity
                and stat.S_ISREG(opened.st_mode)
                and stat.S_ISREG(named.st_mode)
                and opened.st_nlink == 1
                and named.st_nlink == 1
                and not (
                    named_attributes
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                and not (
                    opened_attributes
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                and (opened.st_dev, opened.st_ino) == self._active_lock_identity
                and (named.st_dev, named.st_ino) == self._active_lock_identity
            )
        except (OSError, ValueError):
            valid = False
        if not valid:
            self._active_lease_invalidated = True
            raise StoreError(
                "store.lock_invalidated",
                "the acquired lock handle no longer matches the named workflow lock",
            )

    def _lease_is_valid(self, token: object, owner: tuple[int, int]) -> bool:
        try:
            self._assert_active_lease(token, owner)
        except StoreError:
            return False
        return True

    def _transaction(self, lease: object) -> "WorkflowTransaction":
        return WorkflowTransaction(self, _lease_token=lease)

    def _read_json(self, path: Path, code: str) -> object:
        self._assert_active_lease()
        checked = self._checked(path)
        try:
            value = checked.lstat()
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise StoreError(code, f"persisted material is not a regular file: {path}")
            if value.st_size > MAX_JSON_BYTES:
                raise StoreError("store.input_too_large", f"persisted JSON exceeds size limit: {path}")
            raw = checked.read_bytes()
            if len(raw) > MAX_JSON_BYTES:
                raise StoreError("store.input_too_large", f"persisted JSON exceeds size limit: {path}")
            parsed = _strict_json_loads(raw.decode("utf-8"))
            self._assert_active_lease()
            return parsed
        except StoreError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise StoreError(code, f"persisted JSON is unreadable: {path}") from exc

    def _atomic_json(self, path: Path, value: object) -> None:
        self._assert_active_lease()
        self._checked(path)
        try:
            atomic_write_json(path, value)
        except PathSafetyError as exc:
            raise StoreError("path.unsafe", str(exc)) from exc
        self._assert_active_lease()

    def _atomic_bytes(self, path: Path, value: bytes) -> None:
        self._assert_active_lease()
        checked = self._checked(path)
        parent = self._checked(checked.parent)
        parent_stat = parent.stat(follow_symlinks=False)
        parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
        target_identity: tuple[int, int, int, int] | None = None
        if checked.exists() or checked.is_symlink():
            target_stat = checked.lstat()
            if not stat.S_ISREG(target_stat.st_mode):
                raise StoreError("path.unsafe", "atomic byte target is not a regular file")
            target_identity = (
                target_stat.st_dev,
                target_stat.st_ino,
                target_stat.st_size,
                target_stat.st_mtime_ns,
            )
        temporary = parent / f".{checked.name}.tmp-{os.getpid()}-{time.time_ns()}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            self._checked(path)
            current_parent = parent.stat(follow_symlinks=False)
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise StoreError("path.unsafe", "atomic byte parent changed during write")
            if target_identity is None:
                if checked.exists() or checked.is_symlink():
                    raise StoreError("path.unsafe", "atomic byte target appeared during write")
            else:
                current_target = checked.lstat()
                current_identity = (
                    current_target.st_dev,
                    current_target.st_ino,
                    current_target.st_size,
                    current_target.st_mtime_ns,
                )
                if current_identity != target_identity:
                    raise StoreError("path.unsafe", "atomic byte target changed during write")
            os.replace(temporary, checked)
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            self._assert_active_lease()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass

    def _atomic_text(self, path: Path, value: str) -> None:
        self._atomic_bytes(path, value.encode("utf-8"))

    def _append_event(self, path: Path, event: WorkflowEvent) -> None:
        self._assert_active_lease()
        try:
            append_event(path, event)
        except PathSafetyError as exc:
            message = str(exc)
            code = (
                "events.input_too_large"
                if "bounded canonical line size" in message
                else "path.unsafe"
            )
            raise StoreError(code, message) from exc
        self._assert_active_lease()

    @staticmethod
    def _selection_identity(selection: Selection) -> bytes:
        return _canonical_bytes(selection.to_payload())

    def _journal_history_unlocked(self) -> tuple[Selection, ...] | None:
        journal_exists = self.paths.audit_events.exists() or self.paths.audit_events.is_symlink()
        if not journal_exists:
            return None
        events, _tail, _prefix = self._read_event_log(
            self.paths.audit_events, allow_truncated=False
        )
        if not events:
            raise StoreError("selection.journal_invalid", "activation journal is empty")
        history: list[Selection] = []
        for expected_revision, event in enumerate(events, start=1):
            if set(event.payload) != {"selection"}:
                raise StoreError("selection.journal_invalid", "activation journal payload is invalid")
            try:
                candidate = Selection.from_payload(event.payload["selection"])
            except StoreError as exc:
                raise StoreError("selection.journal_invalid", str(exc)) from exc
            expected_type = (
                "selection_activated" if candidate.mode == "custom" else "selection_deactivated"
            )
            expected_semantic = (
                candidate.semantic_sha256
                if candidate.mode == "custom"
                else (history[-1].semantic_sha256 if history else ZERO_HASH)
            )
            if (
                event.event_seq != expected_revision
                or event.run_id != f"selection-{expected_revision}"
                or candidate.selection_revision != expected_revision
                or event.event_type != expected_type
                or event.semantic_sha256 != (expected_semantic or ZERO_HASH)
            ):
                raise StoreError("selection.journal_invalid", "activation journal revision is discontinuous")
            history.append(candidate)
        return tuple(history)

    def _selection_unlocked(self) -> Selection:
        self._checked(self.paths.selection)
        selection_exists = self.paths.selection.exists() or self.paths.selection.is_symlink()
        journal_exists = self.paths.audit_events.exists() or self.paths.audit_events.is_symlink()
        if not selection_exists and not journal_exists:
            return Selection("official")
        projected: Selection | None = None
        if selection_exists:
            try:
                projected = Selection.from_payload(
                    self._read_json(self.paths.selection, "selection.invalid")
                )
            except StoreError as exc:
                if exc.code in {"path.unsafe", "store.input_too_large"}:
                    raise
                raise StoreError("selection.invalid", str(exc)) from exc
        history = self._journal_history_unlocked()
        if history is None:
            raise StoreError(
                "selection.journal_conflict",
                "selection exists without its authoritative activation journal",
            )
        tip = history[-1]
        if projected is not None and projected.selection_revision < tip.selection_revision:
            boundary = (
                Selection("official")
                if projected.selection_revision == 0
                else history[projected.selection_revision - 1]
            )
            if self._selection_identity(projected) != self._selection_identity(boundary):
                raise StoreError(
                    "selection.journal_conflict",
                    "selection projection conflicts with its journal boundary",
                )
        if projected is None or projected.selection_revision < tip.selection_revision:
            self._atomic_json(self.paths.selection, tip.to_payload())
            return tip
        if (
            projected.selection_revision > tip.selection_revision
            or self._selection_identity(projected) != self._selection_identity(tip)
        ):
            raise StoreError(
                "selection.journal_conflict",
                "selection projection conflicts with the activation journal tip",
            )
        return projected

    def read_selection(self) -> Selection:
        self._checked(self.paths.selection)
        if (
            not self.paths.selection.exists()
            and not self.paths.selection.is_symlink()
            and not self.paths.audit_events.exists()
            and not self.paths.audit_events.is_symlink()
        ):
            return Selection("official")
        with self._lock():
            return self._selection_unlocked()

    def _validate_revision_snapshots(self) -> None:
        if not self.paths.revisions.exists():
            return
        self._checked(self.paths.revisions)
        for path in sorted(self.paths.revisions.glob("*.json")):
            try:
                value = self._read_json(path, "store.revision_snapshot_collision")
                document = parse_workflow(value)
                expected = f"{document.semantic_revision}-{document_sha256(document)}.json"
                if path.name != expected or _canonical_bytes(_document_data(document), newline=True) != path.read_bytes():
                    raise StoreError(
                        "store.revision_snapshot_collision",
                        f"immutable semantic snapshot does not match its identity: {path}",
                    )
            except (WorkflowError, OSError, StoreError) as exc:
                if isinstance(exc, StoreError) and exc.code == "store.revision_snapshot_collision":
                    raise
                raise StoreError(
                    "store.revision_snapshot_collision",
                    f"immutable semantic snapshot is ambiguous: {path}",
                ) from exc

    def load_draft(self) -> WorkflowDocument:
        with self._lock():
            self._checked(self.paths.workflow)
            if not self.paths.workflow.exists() and not self.paths.workflow.is_symlink():
                raise StoreError("store.draft_missing", "custom workflow draft does not exist")
            try:
                return parse_workflow(self._read_json(self.paths.workflow, "store.draft_invalid"))
            except WorkflowError as exc:
                raise StoreError("store.draft_invalid", str(exc)) from exc

    def save_draft(
        self,
        document: WorkflowDocument,
        *,
        expected_document_revision: int,
    ) -> WorkflowDocument:
        _integer(
            expected_document_revision,
            "store.invalid_revision",
            maximum=_MAX_REVISION - 1,
        )
        with self._lock():
            self._ensure_locked_directory(self.paths.revisions)
            self._validate_revision_snapshots()
            previous: WorkflowDocument | None = None
            if self.paths.workflow.exists() or self.paths.workflow.is_symlink():
                try:
                    previous = parse_workflow(self._read_json(self.paths.workflow, "store.draft_invalid"))
                except WorkflowError as exc:
                    raise StoreError("store.draft_invalid", str(exc)) from exc
            current_revision = 0 if previous is None else previous.document_revision
            if expected_document_revision != current_revision:
                raise StoreError("store.revision_conflict", "draft document revision is stale")
            behavior_changed = previous is None or _canonical_bytes(
                behavior_payload(previous)
            ) != _canonical_bytes(behavior_payload(document))
            semantic_revision = (
                (0 if previous is None else previous.semantic_revision) + 1
                if behavior_changed
                else previous.semantic_revision
            )
            payload = _document_data(document)
            payload["document_revision"] = current_revision + 1
            payload["semantic_revision"] = semantic_revision
            try:
                normalized = parse_workflow(payload)
            except WorkflowError as exc:
                raise StoreError("store.draft_invalid", str(exc)) from exc
            if behavior_changed:
                revision_path = self.paths.revisions / f"{semantic_revision}-{document_sha256(normalized)}.json"
                expected_bytes = _canonical_bytes(_document_data(normalized), newline=True)
                if revision_path.exists() or revision_path.is_symlink():
                    try:
                        existing = self._checked(revision_path).read_bytes()
                    except OSError as exc:
                        raise StoreError("store.revision_snapshot_collision", "could not inspect revision snapshot") from exc
                    if existing != expected_bytes:
                        raise StoreError(
                            "store.revision_snapshot_collision",
                            "immutable semantic revision path already has different bytes",
                        )
                else:
                    self._atomic_json(revision_path, _document_data(normalized))
            self._atomic_json(self.paths.workflow, _document_data(normalized))
            return normalized

    def _active_snapshot_unlocked(self) -> tuple[RunState, int, str, str, str] | None:
        if not self.paths.state.exists() and not self.paths.state.is_symlink():
            return None
        return _parse_snapshot(self._read_json(self.paths.state, "snapshot.invalid"))

    def _reject_active_run(self) -> None:
        if not self.paths.run_dir.exists() and not self.paths.run_dir.is_symlink():
            return
        if not self.paths.state.exists() and not self.paths.state.is_symlink():
            raise StoreError("run.recovery_required", "partial active-run material requires recovery")
        material = self._validated_run_material(
            allow_truncated=False,
            require_no_suffix=True,
        )
        _plan, _state, _seq, _hash, run_status, _events, _tail, _prefix, _witnesses = material
        if run_status == "active":
            raise StoreError("run.already_active", "a custom workflow run is already active")

    def _audit_append(self, event_type: str, semantic_sha256: str, payload: Mapping[str, object]) -> WorkflowEvent:
        events, _tail, _prefix = self._read_event_log(self.paths.audit_events, allow_truncated=False)
        sequence = events[-1].event_seq + 1 if events else 1
        if set(payload) != {"selection"}:
            raise StoreError("selection.journal_invalid", "activation journal payload is invalid")
        selected = Selection.from_payload(payload["selection"])
        if selected.selection_revision != sequence:
            raise StoreError("selection.journal_conflict", "selection revision does not follow journal tip")
        previous = events[-1].event_hash if events else ZERO_HASH
        event = _validated_event(
            event_seq=sequence,
            run_id=f"selection-{sequence}",
            semantic_sha256=semantic_sha256 or ZERO_HASH,
            event_type=event_type,
            payload=payload,
            previous_event_hash=previous,
        )
        self._checked(self.paths.audit_events)
        self._append_event(self.paths.audit_events, event)
        return event

    def activate_custom(
        self,
        plan: CompiledPlan,
        *,
        high_risk_warning_codes: Sequence[str],
        acknowledged_warning_codes: Sequence[str],
    ) -> Selection:
        try:
            required = _warning_codes(
                high_risk_warning_codes,
                "activation.acknowledgement_mismatch",
                require_canonical=False,
            )
            acknowledged = _warning_codes(
                acknowledged_warning_codes,
                "activation.acknowledgement_mismatch",
                require_canonical=False,
            )
        except (TypeError, StoreError) as exc:
            raise StoreError(
                "activation.acknowledgement_mismatch",
                "warning acknowledgement codes are invalid",
            ) from exc
        if required != acknowledged or len(acknowledged) != len(
            tuple(acknowledged_warning_codes)
        ):
            raise StoreError(
                "activation.acknowledgement_mismatch",
                "activation requires the exact current high-risk warning-code set",
            )
        with self._lock():
            self._reject_active_run()
            _raw_plan, plan, _plan_sha256 = _validated_plan_data(plan)
            self._validate_revision_snapshots()
            if not self.paths.workflow.exists() and not self.paths.workflow.is_symlink():
                raise StoreError("activation.draft_mismatch", "activation requires a saved workflow draft")
            try:
                latest = parse_workflow(
                    self._read_json(self.paths.workflow, "store.draft_invalid")
                )
            except WorkflowError as exc:
                raise StoreError("store.draft_invalid", str(exc)) from exc
            latest_hash = document_sha256(latest)
            revision_path = self.paths.revisions / f"{latest.semantic_revision}-{latest_hash}.json"
            if (
                plan.workflow_id != latest.workflow_id
                or plan.semantic_revision != latest.semantic_revision
                or plan.document_sha256 != latest_hash
                or not revision_path.exists()
                or revision_path.is_symlink()
            ):
                raise StoreError(
                    "activation.draft_mismatch",
                    "compiled plan does not name the latest saved behavior revision",
                )
            previous = self._selection_unlocked()
            selected = Selection(
                "custom",
                previous.selection_revision + 1,
                plan.workflow_id,
                plan.semantic_revision,
                plan.semantic_sha256,
                required,
                plan.semantic_sha256,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            self._audit_append("selection_activated", plan.semantic_sha256, {"selection": selected.to_payload()})
            self._atomic_json(self.paths.selection, selected.to_payload())
            return selected

    def deactivate_custom(self) -> Selection:
        with self._lock() as lease:
            previous = self._selection_unlocked()
            if self.paths.state.exists() or self.paths.state.is_symlink():
                material = self._validated_run_material(
                    allow_truncated=False,
                    require_no_suffix=True,
                )
                plan, state, sequence, event_hash, run_status, _events, _tail, _prefix, witnesses = material
                if run_status == "active":
                    transaction = self._transaction(lease)
                    transaction._set_loaded(
                        plan,
                        state,
                        sequence,
                        event_hash,
                        run_status,
                        _events,
                        witnesses,
                    )
                    transaction._commit_event("run_stopped", state, {}, run_status="stopped")
            selected = Selection("official", previous.selection_revision + 1)
            self._audit_append(
                "selection_deactivated",
                previous.semantic_sha256 or ZERO_HASH,
                {"selection": selected.to_payload()},
            )
            self._atomic_json(self.paths.selection, selected.to_payload())
            return selected

    def read_audit_events(self) -> tuple[WorkflowEvent, ...]:
        if not self.paths.audit_events.exists() and not self.paths.audit_events.is_symlink():
            return ()
        with self._lock():
            events, _tail, _prefix = self._read_event_log(self.paths.audit_events, allow_truncated=False)
            return tuple(events)

    def _archive_old_run(self, state: RunState) -> None:
        self._assert_active_lease()
        self._ensure_locked_directory(self.paths.archived_runs)
        target = self.paths.archived_runs / f"{state.run_id}-{time.time_ns()}"
        self._checked(target)
        self._checked(self.paths.run_dir)
        if target.exists() or target.is_symlink():
            raise StoreError("run.archive_failed", "run archive target already exists")
        source_stat = self.paths.run_dir.lstat()
        if not stat.S_ISDIR(source_stat.st_mode):
            raise StoreError("path.unsafe", "active run material is not a plain directory")
        try:
            os.replace(self.paths.run_dir, target)
        except OSError as exc:
            raise StoreError("run.archive_failed", "could not preserve the previous run") from exc
        first_error: OSError | None = None
        for parent in dict.fromkeys((self.paths.run_dir.parent, target.parent)):
            try:
                _fsync_directory(parent)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise StoreError("run.archive_failed", "could not sync preserved run parents") from first_error
        self._assert_active_lease()

    def start_run(self, plan: CompiledPlan, run_id: str) -> RunState:
        normalized_run_id = _run_id(run_id, "run.invalid_id")
        raw_plan, plan, plan_sha256 = _validated_plan_data(plan)
        raw_plan = _preflight_json_output(raw_plan, "plan.invalid")
        state = initial_run(plan, normalized_run_id)
        state_data, state = _validated_state_data(state)
        _validate_state_binding(plan, state)
        _validate_artifact_authority(plan, state)
        event = _validated_event(
            event_seq=1,
            run_id=normalized_run_id,
            semantic_sha256=plan.semantic_sha256,
            event_type="run_started",
            payload={
                "state": state_data,
                "run_status": "active",
                "plan_sha256": plan_sha256,
            },
            previous_event_hash=ZERO_HASH,
        )
        _validate_lifecycle_step(event, None)
        snapshot = _snapshot_data(
            state,
            1,
            event.event_hash,
            run_status="active",
            plan_sha256=plan_sha256,
        )
        snapshot = _preflight_json_output(snapshot, "snapshot.input_too_large")
        projections = _projection_material(state, "active")
        with self._lock():
            run_material_exists = self.paths.run_dir.exists() or self.paths.run_dir.is_symlink()
            if run_material_exists and not (
                self.paths.state.exists() or self.paths.state.is_symlink()
            ):
                raise StoreError("run.recovery_required", "partial active-run material requires recovery")
            if self.paths.state.exists() or self.paths.state.is_symlink():
                material = self._validated_run_material(
                    allow_truncated=False,
                    require_no_suffix=True,
                )
                _old_plan, old_state, _seq, _hash, run_status, _events, _tail, _prefix, _witnesses = material
                if run_status == "active":
                    raise StoreError("run.already_active", "a custom workflow run is already active")
                self._archive_old_run(old_state)
            self._ensure_locked_directory(self.paths.run_dir)
            self._atomic_json(self.paths.plan, raw_plan)
            self._append_event(self.paths.events, event)
            self._atomic_json(self.paths.state, snapshot)
            self._write_projections(state, "active", prepared=projections)
            return state

    @contextmanager
    def locked_run(self) -> Iterator["WorkflowTransaction"]:
        with self._lock() as lease:
            transaction = self._transaction(lease)
            try:
                yield transaction
            finally:
                transaction._deactivate()

    def _read_event_log(
        self, path: Path, *, allow_truncated: bool
    ) -> tuple[list[WorkflowEvent], bytes | None, bytes]:
        self._assert_active_lease()
        if not path.exists() and not path.is_symlink():
            return [], None, b""
        checked = self._checked(path)
        try:
            inspected = checked.lstat()
            if not stat.S_ISREG(inspected.st_mode) or inspected.st_nlink != 1:
                raise StoreError("path.unsafe", "event log is not a single-name regular file")
            if inspected.st_size > _MAX_EVENT_LOG_BYTES:
                raise StoreError("events.input_too_large", "event log exceeds the bounded input size")
            raw = checked.read_bytes()
            if len(raw) > _MAX_EVENT_LOG_BYTES:
                raise StoreError("events.input_too_large", "event log exceeds the bounded input size")
        except OSError as exc:
            raise StoreError("events.read_error", "could not read event log") from exc
        tail: bytes | None = None
        prefix = raw
        if raw and not raw.endswith(b"\n"):
            split = raw.rfind(b"\n")
            tail = raw[split + 1 :]
            prefix = raw[: split + 1]
            if len(tail) > MAX_EVENT_BYTES:
                raise StoreError("events.input_too_large", "event tail exceeds the bounded input size")
            if not allow_truncated:
                raise StoreError("events.truncated_tail", "event log has an unframed final line")
        events: list[WorkflowEvent] = []
        by_sequence: dict[int, WorkflowEvent] = {}
        expected = 1
        last_hash = ZERO_HASH
        for line in prefix.splitlines():
            if len(line) + 1 > MAX_EVENT_BYTES:
                raise StoreError("events.input_too_large", "event line exceeds the bounded input size")
            try:
                value = _strict_json_loads(line.decode("utf-8"))
            except StoreError:
                raise
            except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
                raise StoreError("events.invalid_json", "event log contains invalid terminated JSON") from exc
            if not isinstance(value, Mapping):
                raise StoreError("events.invalid_event", "event line must be an object")
            raw_sequence = value.get("event_seq")
            if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int) or raw_sequence < 1:
                raise StoreError("events.invalid_event", "event sequence is invalid")
            prior = by_sequence.get(raw_sequence)
            if prior is None and raw_sequence != expected:
                raise StoreError("events.sequence_gap", "event log sequence is not continuous")
            try:
                event = WorkflowEvent.from_payload(value)
            except StoreError as exc:
                if prior is not None:
                    raise StoreError(
                        "events.duplicate_conflict",
                        "event sequence has conflicting canonical content",
                    ) from exc
                raise
            if not event.hash_is_valid():
                raise StoreError("events.hash_mismatch", "event content does not match its hash")
            prior = by_sequence.get(event.event_seq)
            if prior is not None:
                if _canonical_bytes(event.to_payload()) == _canonical_bytes(prior.to_payload()):
                    continue
                raise StoreError("events.duplicate_conflict", "event sequence has conflicting canonical content")
            if event.previous_event_hash != last_hash:
                raise StoreError("events.hash_mismatch", "event previous hash does not match the chain")
            events.append(event)
            by_sequence[event.event_seq] = event
            expected += 1
            last_hash = event.event_hash
        self._assert_active_lease()
        return events, tail, prefix

    def _validated_run_material(
        self,
        *,
        allow_truncated: bool,
        require_no_suffix: bool,
    ) -> tuple[CompiledPlan, RunState, int, str, str, list[WorkflowEvent], bytes | None, bytes, EdgeWitnesses]:
        if not self.paths.state.exists() and not self.paths.state.is_symlink():
            raise StoreError("run.not_found", "there is no active custom workflow run")
        raw_plan = self._read_json(self.paths.plan, "plan.invalid")
        plan = _plan_from_data(raw_plan)
        if _canonical_bytes(raw_plan) != _canonical_bytes(_plan_data(plan)):
            raise StoreError("plan.invalid", "persisted plan is not canonical codec output")
        plan_sha256 = _sha256(raw_plan)
        state, sequence, event_hash, run_status, snapshot_plan_sha256 = _parse_snapshot(
            self._read_json(self.paths.state, "snapshot.invalid")
        )
        if snapshot_plan_sha256 != plan_sha256:
            raise StoreError("plan.digest_mismatch", "compiled plan differs from snapshot binding")
        events, tail, prefix = self._read_event_log(self.paths.events, allow_truncated=allow_truncated)
        if (
            not events
            or events[0].event_type != "run_started"
            or events[0].payload.get("plan_sha256") != plan_sha256
        ):
            raise StoreError("plan.digest_mismatch", "compiled plan differs from first-event binding")
        _validate_state_binding(plan, state)
        for artifact in state.artifacts.values():
            try:
                resolve_project_path(self.project_root, artifact.path)
            except PathSafetyError as exc:
                raise StoreError("path.unsafe", "runtime artifact path is not project-contained") from exc
        _validate_artifact_authority(plan, state)
        previous_status: str | None = None
        previous_state: RunState | None = None
        witnesses: EdgeWitnesses = {}
        boundary_witnesses: EdgeWitnesses | None = None
        for event in events:
            previous_state, previous_status = self._replay_candidate(
                plan,
                event,
                state.run_id,
                previous_status,
                previous_state,
                witnesses,
                events[:event.event_seq - 1],
            )
            if event.event_seq == sequence:
                boundary_witnesses = {
                    edge_id: dict(proofs) for edge_id, proofs in witnesses.items()
                }
        boundary = next((event for event in events if event.event_seq == sequence), None)
        if boundary is None or boundary_witnesses is None or boundary.event_hash != event_hash:
            raise StoreError("snapshot.boundary_mismatch", "snapshot event boundary is not in the verified chain")
        boundary_state = boundary.payload.get("state")
        boundary_status = boundary.payload.get("run_status")
        if (
            not isinstance(boundary_state, Mapping)
            or _canonical_bytes(_state_data(state)) != _canonical_bytes(boundary_state)
            or run_status != boundary_status
        ):
            raise StoreError("snapshot.boundary_mismatch", "snapshot content differs from its boundary event")
        if require_no_suffix and any(event.event_seq > sequence for event in events):
            raise StoreError("recovery.required", "verified event suffix must be recovered before mutation")
        if require_no_suffix and (
            self._artifact_drift_ids(state) or self._witness_drift_producers(boundary_witnesses)
        ):
            raise StoreError("recovery.required", "artifact bytes must be recovered before mutation")
        self._receipt_material(events, require_existing=require_no_suffix)
        return plan, state, sequence, event_hash, run_status, events, tail, prefix, boundary_witnesses

    def _replay_candidate(
        self,
        plan: CompiledPlan,
        event: WorkflowEvent,
        expected_run_id: str | None,
        previous_status: str | None,
        previous_state: RunState | None,
        witnesses: EdgeWitnesses,
        prior_events: Sequence[WorkflowEvent] = (),
    ) -> tuple[RunState, str]:
        raw_state = event.payload.get("state")
        if not isinstance(raw_state, Mapping):
            raise StoreError("events.unreplayable", "event does not contain a replayable state")
        run_status = _validate_lifecycle_step(event, previous_status)
        try:
            state = _state_from_data(raw_state)
            encoded, state = _validated_state_data(state)
        except StoreError as exc:
            raise StoreError("events.unreplayable", str(exc)) from exc
        if _canonical_bytes(encoded) != _canonical_bytes(raw_state):
            raise StoreError("events.unreplayable", "event state is not canonical codec output")
        if state.run_id != event.run_id or (
            expected_run_id is not None and state.run_id != expected_run_id
        ):
            raise StoreError("events.unreplayable", "event state does not match the compiled run")
        if event.semantic_sha256 != plan.semantic_sha256:
            raise StoreError("events.semantic_mismatch", "event belongs to another semantic plan")
        try:
            _validate_state_binding(plan, state)
        except StoreError as exc:
            raise StoreError("events.unreplayable", str(exc)) from exc
        for artifact in state.artifacts.values():
            try:
                resolve_project_path(self.project_root, artifact.path)
            except PathSafetyError as exc:
                raise StoreError("events.unreplayable", "event artifact path is unsafe") from exc
        _validate_artifact_authority(plan, state)
        validate_node_evidence_delta(plan, previous_state, state, event.event_type, event.payload, prior_events, witnesses)
        _validate_edge_and_winner_transition(
            plan, previous_state, state, event.event_type, event.payload
        )
        self._validate_artifact_source_transition(
            plan, previous_state, state, event.event_type
        )
        next_witnesses = _edge_witnesses_after_step(
            plan, previous_state, state, witnesses
        )
        witnesses.clear()
        witnesses.update(next_witnesses)
        return state, run_status

    def _receipt_material(
        self, events: Sequence[WorkflowEvent], *, require_existing: bool = False,
    ) -> dict[Path, bytes]:
        material = {}
        for event in events:
            if event.event_type != "node_result_recorded":
                continue
            try:
                receipt = validate_stage_receipt(event.payload.get("receipt"))
            except ReceiptError as exc:
                raise StoreError("receipt.invalid", str(exc)) from exc
            path = self.paths.receipts / receipt["run_id"] / f'{receipt["node_id"]}-attempt-{receipt["attempt"]}.json'
            self._checked(path)
            raw = canonical_bytes(receipt) + b"\n"
            if path in material:
                raise StoreError("receipt.conflict", "duplicate attempt receipt")
            material[path] = raw
            if path.exists():
                if path.read_bytes() != raw:
                    raise StoreError("receipt.conflict", "receipt projection differs from committed event")
            elif require_existing:
                raise StoreError("recovery.required", "committed receipt projection is missing")
        if events:
            directory = self.paths.receipts / events[0].run_id
            self._checked(directory)
            if directory.exists():
                for path in directory.iterdir():
                    self._checked(path)
                    if path not in material:
                        raise StoreError("receipt.orphan", "receipt file lacks a committed completion event")
        return material

    def _write_receipts(self, material: Mapping[Path, bytes]) -> bool:
        changed = False
        for path, raw in material.items():
            self._checked(path)
            if not path.exists():
                self._ensure_locked_directory(path.parent)
                self._atomic_bytes(path, raw)
                changed = True
        return changed

    def _write_projections(
        self,
        state: RunState,
        run_status: str,
        *,
        prepared: tuple[bytes, bytes] | None = None,
    ) -> None:
        artifact_bytes, summary_bytes = (
            _projection_material(state, run_status) if prepared is None else prepared
        )
        self._atomic_bytes(self.paths.artifacts, artifact_bytes)
        self._atomic_bytes(self.paths.summary, summary_bytes)

    def read_run_events(self) -> tuple[WorkflowEvent, ...]:
        with self._lock():
            _plan, _state, _seq, _hash, _status, events, _tail, _prefix, _witnesses = (
                self._validated_run_material(
                    allow_truncated=False,
                    require_no_suffix=False,
                )
            )
            return tuple(events)

    def _archive_truncated_tail(self, tail: bytes, prefix: bytes) -> Path:
        self._ensure_locked_directory(self.paths.recovery)
        target = self.paths.recovery / f"events-truncated-{time.time_ns()}.jsonl"
        self._atomic_bytes(target, tail)
        self._atomic_bytes(self.paths.events, prefix)
        return target

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _artifact_drift_ids(self, state: RunState) -> tuple[str, ...]:
        drifted: list[str] = []
        for artifact_id, artifact in sorted(state.artifacts.items()):
            if artifact.state != "verified":
                continue
            try:
                path = resolve_project_path(self.project_root, artifact.path)
                if not path.is_file() or self._hash_file(path) != artifact.sha256:
                    drifted.append(artifact_id)
            except (OSError, PathSafetyError):
                drifted.append(artifact_id)
        return tuple(drifted)

    def _witness_drift_producers(self, witnesses: EdgeWitnesses) -> tuple[str, ...]:
        drifted: set[str] = set()
        for edge_proofs in witnesses.values():
            for proof in edge_proofs.values():
                try:
                    path = resolve_project_path(self.project_root, proof.path)
                    if not path.is_file() or self._hash_file(path) != proof.sha256:
                        drifted.add(proof.producer_node_id)
                except (OSError, PathSafetyError):
                    drifted.add(proof.producer_node_id)
        return tuple(sorted(drifted))

    def _require_verified_artifact_bytes(self, state: RunState) -> None:
        drifted = self._artifact_drift_ids(state)
        if drifted:
            raise StoreError(
                "artifact.verification_failed",
                f"verified artifact bytes do not match recorded hashes: {drifted[0]}",
            )

    def _require_artifact_paths_contained(self, state: RunState) -> None:
        for artifact in state.artifacts.values():
            try:
                resolve_project_path(self.project_root, artifact.path)
            except PathSafetyError as exc:
                raise StoreError(
                    "path.unsafe", "runtime artifact path is not project-contained"
                ) from exc

    @staticmethod
    def _predicate_mentions_artifact(value: object, artifact_id: str) -> bool:
        if isinstance(value, Mapping):
            if value.get("op") == "artifact_state_is" and value.get("artifact") == artifact_id:
                return True
            return any(WorkflowStore._predicate_mentions_artifact(item, artifact_id) for item in value.values())
        if isinstance(value, (tuple, list)):
            return any(WorkflowStore._predicate_mentions_artifact(item, artifact_id) for item in value)
        return False

    def _validate_artifact_source_transition(
        self,
        plan: CompiledPlan,
        previous: RunState | None,
        current: RunState,
        event_type: str,
    ) -> None:
        if previous is None:
            return
        authorized_invalidation: set[str] = set()
        for artifact_id, earlier in previous.artifacts.items():
            replacement = current.artifacts.get(artifact_id)
            if replacement is None:
                raise StoreError(
                    "artifact.authority_invalid",
                    f"artifact provenance cannot be removed: {artifact_id}",
                )
            if replacement.producer_node_id != _EXTERNAL_PRODUCER:
                continue
            if earlier == replacement:
                continue
            if (
                earlier.producer_node_id == _EXTERNAL_PRODUCER
                and earlier.path == replacement.path
                and earlier.sha256 == replacement.sha256
                and replacement.state == "stale"
                and event_type == "artifacts_marked_stale"
            ):
                continue
            if event_type != "artifact_registered" or replacement.state != "verified":
                raise StoreError(
                    "artifact.authority_invalid",
                    f"changed external artifact needs explicit re-registration: {artifact_id}",
                )

            affected = set()
            if earlier.producer_node_id != _EXTERNAL_PRODUCER:
                affected.add(earlier.producer_node_id)
            for node_id, node in plan.nodes.items():
                if artifact_id in node.inputs or any(
                    self._predicate_mentions_artifact(case, artifact_id)
                    for case in node.condition_cases
                ):
                    affected.add(node_id)
            pending = list(affected)
            while pending:
                for edge_id in plan.outgoing[pending.pop()]:
                    target = plan.edges[edge_id].target
                    target_node = plan.nodes[target]
                    if (
                        target_node.type == "join"
                        and target_node.join_mode == "any_success"
                        and previous.nodes[target].winner_edge_id
                        and previous.nodes[target].winner_edge_id != edge_id
                    ):
                        continue
                    if target not in affected:
                        affected.add(target)
                        pending.append(target)
            authorized_invalidation.update(affected)
            independently_ready = dict(current.nodes)
            for node_id in affected:
                runtime = independently_ready[node_id]
                if runtime.status is NodeStatus.READY:
                    independently_ready[node_id] = replace(
                        runtime, selected_inputs=MappingProxyType({})
                    )
            refreshed: RunState | None = None
            for node_id in affected:
                before = previous.nodes[node_id]
                after = current.nodes[node_id]
                if before.status is NodeStatus.RUNNING or after.status is NodeStatus.RUNNING:
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"external re-registration cannot continue running work: {node_id}",
                    )
                unattempted_exclusion = (
                    before.status is NodeStatus.SKIPPED
                    and before.attempt == 0
                    and not before.outcome
                    and not before.claim_token_hash
                    and not before.selected_inputs
                    and not before.outputs
                    and not before.auxiliary_outputs
                    and bool(plan.incoming[node_id])
                )
                unchanged_exclusion = unattempted_exclusion and all(
                    previous.edges[edge_id] == current.edges[edge_id]
                    and current.edges[edge_id].status is EdgeStatus.INACTIVE
                    and previous.nodes[plan.edges[edge_id].source]
                    == current.nodes[plan.edges[edge_id].source]
                    for edge_id in plan.incoming[node_id]
                )
                if unattempted_exclusion:
                    valid_status = (
                        after == before if unchanged_exclusion
                        else after == replace(before, status=NodeStatus.PENDING)
                    )
                elif before.status in {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED, NodeStatus.STALE}:
                    valid_status = after.status is NodeStatus.STALE
                elif before.status in {NodeStatus.PENDING, NodeStatus.READY}:
                    valid_status = after.status in {NodeStatus.PENDING, NodeStatus.READY} or (
                        before.status is NodeStatus.READY
                        and plan.nodes[node_id].type == "join"
                        and plan.nodes[node_id].join_mode == "any_success"
                        and bool(before.winner_edge_id)
                        and after.status is NodeStatus.STALE
                    )
                else:
                    valid_status = after.status in {before.status, NodeStatus.STALE}
                if not valid_status:
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"external re-registration must invalidate completed evidence: {node_id}",
                    )
                for edge_id in plan.outgoing[node_id]:
                    edge = current.edges[edge_id]
                    if unchanged_exclusion:
                        valid_edge = (
                            edge == previous.edges[edge_id]
                            and edge.status is EdgeStatus.INACTIVE
                            and not edge.selected_output_map
                        )
                    else:
                        valid_edge = edge.status is EdgeStatus.WAITING and not edge.selected_output_map
                    if not valid_edge:
                        raise StoreError(
                            "artifact.authority_invalid",
                            f"external re-registration must reset old output route: {edge_id}",
                        )
                if after.status is NodeStatus.PENDING and after.selected_inputs:
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"pending work retains superseded inputs: {node_id}",
                    )
                if after.status is NodeStatus.READY:
                    if refreshed is None:
                        refreshed = refresh_ready(
                            plan, replace(
                                current, nodes=MappingProxyType(independently_ready)
                            )
                        )
                    expected = refreshed.nodes[node_id]
                    if (
                        expected.status is not NodeStatus.READY
                        or expected.selected_inputs != after.selected_inputs
                    ):
                        raise StoreError(
                            "artifact.authority_invalid",
                            f"ready work retains superseded inputs: {node_id}",
                        )
                if after.status in {NodeStatus.FAILED, NodeStatus.BLOCKED} and after.selected_inputs:
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"unfinished work retains superseded inputs: {node_id}",
                    )
            for sibling_id, sibling in previous.artifacts.items():
                if sibling_id == artifact_id:
                    continue
                present_sibling = current.artifacts.get(sibling_id)
                if (
                    (sibling.producer_node_id, sibling.producer_attempt)
                    == (earlier.producer_node_id, earlier.producer_attempt)
                    or sibling.producer_node_id in affected
                ) and (present_sibling is None or present_sibling.state != "stale"):
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"external re-registration must stale related evidence: {sibling_id}",
                    )
        if event_type == "artifact_registered":
            for node_id, runtime in current.nodes.items():
                if (
                    runtime.status is NodeStatus.STALE
                    and previous.nodes[node_id].status is not NodeStatus.STALE
                    and node_id not in authorized_invalidation
                ):
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"registration staled an unrelated source: {node_id}",
                    )
            for edge_id, runtime in current.edges.items():
                if (
                    previous.edges[edge_id].status is EdgeStatus.SATISFIED
                    and runtime.status is not EdgeStatus.SATISFIED
                    and plan.edges[edge_id].source not in authorized_invalidation
                ):
                    raise StoreError(
                        "artifact.authority_invalid",
                        f"registration removed an unrelated route: {edge_id}",
                    )

    def _mark_drift(
        self,
        plan: CompiledPlan,
        state: RunState,
        drifted: Sequence[str],
        historical_producers: Sequence[str] = (),
    ) -> RunState:
        artifacts = dict(state.artifacts)
        direct: set[str] = set()
        producers: set[str] = set(historical_producers)
        for artifact_id, artifact in tuple(artifacts.items()):
            if artifact.producer_node_id in producers and artifact.state != "stale":
                artifacts[artifact_id] = replace(artifact, state="stale")
        for artifact_id in drifted:
            artifact = artifacts[artifact_id]
            if artifact.producer_node_id in plan.nodes:
                producers.add(artifact.producer_node_id)
                producer_key = (artifact.producer_node_id, artifact.producer_attempt)
                for sibling_id, sibling in tuple(artifacts.items()):
                    if (sibling.producer_node_id, sibling.producer_attempt) == producer_key:
                        artifacts[sibling_id] = replace(sibling, state="stale")
            else:
                artifacts[artifact_id] = replace(artifact, state="stale")
        stale_artifact_ids = {
            artifact_id for artifact_id, artifact in artifacts.items() if artifact.state == "stale"
        }
        for artifact_id in stale_artifact_ids:
            for node_id, node in plan.nodes.items():
                if artifact_id in node.inputs or any(
                    self._predicate_mentions_artifact(case, artifact_id)
                    for case in node.condition_cases
                ):
                    direct.add(node_id)
        updated = replace(state, artifacts=MappingProxyType(artifacts))
        if producers:
            nodes = dict(updated.nodes)
            for node_id in producers:
                nodes[node_id] = replace(nodes[node_id], status=NodeStatus.STALE)
            updated = replace(updated, nodes=MappingProxyType(nodes))
            updated = mark_descendants_stale(plan, updated, tuple(sorted(producers)))
        if direct:
            nodes = dict(updated.nodes)
            for node_id in direct:
                nodes[node_id] = replace(nodes[node_id], status=NodeStatus.STALE)
            updated = replace(updated, nodes=MappingProxyType(nodes))
            updated = mark_descendants_stale(plan, updated, tuple(sorted(direct)))
        return updated

    def recover(self) -> RecoveryResult:
        with self._lock() as lease:
            archived: Path | None = None
            changed = False
            state_exists = self.paths.state.exists() or self.paths.state.is_symlink()
            plan_exists = self.paths.plan.exists() or self.paths.plan.is_symlink()
            events_exist = self.paths.events.exists() or self.paths.events.is_symlink()
            run_dir_exists = self.paths.run_dir.exists() or self.paths.run_dir.is_symlink()
            if not state_exists and not plan_exists and not events_exist:
                if run_dir_exists:
                    return RecoveryResult("blocked", "recovery.incomplete_run")
                return RecoveryResult("clean", "recovery.no_run")
            if not plan_exists or not events_exist:
                return RecoveryResult("blocked", "recovery.incomplete_run")

            if state_exists:
                try:
                    material = self._validated_run_material(
                        allow_truncated=True,
                        require_no_suffix=False,
                    )
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code)
                plan, state, sequence, event_hash, run_status, events, tail, prefix, witnesses = material
                plan_sha256 = _sha256(_plan_data(plan))
                expected_run_id: str | None = state.run_id
                suffix = [event for event in events if event.event_seq > sequence]
                previous_status: str | None = run_status
                previous_state: RunState | None = state
            else:
                try:
                    raw_plan = self._read_json(self.paths.plan, "plan.invalid")
                    plan = _plan_from_data(raw_plan)
                    if _canonical_bytes(raw_plan) != _canonical_bytes(_plan_data(plan)):
                        raise StoreError("plan.invalid", "persisted plan is not canonical codec output")
                    plan_sha256 = _sha256(raw_plan)
                    events, tail, prefix = self._read_event_log(
                        self.paths.events, allow_truncated=True
                    )
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code)
                if (
                    not events
                    or events[0].event_type != "run_started"
                    or events[0].payload.get("plan_sha256") != plan_sha256
                ):
                    return RecoveryResult("blocked", "plan.digest_mismatch")
                state = None
                sequence = 0
                event_hash = ZERO_HASH
                run_status = "active"
                expected_run_id = None
                suffix = events
                previous_status = None
                previous_state = None
                witnesses = {}

            # Validate the entire replay suffix before any tail archive, log
            # truncation, snapshot replacement, or projection regeneration.
            try:
                for event in suffix:
                    candidate, next_status = self._replay_candidate(
                        plan, event, expected_run_id, previous_status, previous_state, witnesses,
                        events[:event.event_seq - 1],
                    )
                    if expected_run_id is None:
                        expected_run_id = candidate.run_id
                    state = candidate
                    sequence = event.event_seq
                    event_hash = event.event_hash
                    run_status = next_status
                    previous_status = next_status
                    previous_state = candidate
            except StoreError as exc:
                return RecoveryResult("blocked", exc.code, state)
            if state is None:
                return RecoveryResult("blocked", "recovery.incomplete_run")
            replayed = bool(suffix)

            try:
                receipt_material = self._receipt_material(events)
            except StoreError as exc:
                return RecoveryResult("blocked", exc.code, state)

            if tail is not None:
                try:
                    archived = self._archive_truncated_tail(tail, prefix)
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code, state)
                changed = True
            if not state_exists or replayed:
                try:
                    self._atomic_json(
                        self.paths.state,
                        _snapshot_data(
                            state,
                            sequence,
                            event_hash,
                            run_status=run_status,
                            plan_sha256=plan_sha256,
                        ),
                    )
                    self._write_projections(state, run_status)
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code, state, archived)
                changed = True

            drifted = list(self._artifact_drift_ids(state))
            historical_producers = self._witness_drift_producers(witnesses)
            if drifted or historical_producers:
                stale = self._mark_drift(plan, state, drifted, historical_producers)
                transaction = self._transaction(lease)
                transaction._set_loaded(plan, state, sequence, event_hash, run_status, events, witnesses)
                transaction._commit_event(
                    "artifacts_marked_stale",
                    stale,
                    {
                        "artifact_ids": list(drifted),
                        "witness_producer_ids": list(historical_producers),
                    },
                    run_status=run_status,
                )
                state = stale
                sequence = transaction._event_seq
                event_hash = transaction._event_hash
                witnesses = transaction._witnesses
                changed = True

            running = [
                node_id for node_id, runtime in state.nodes.items() if runtime.status is NodeStatus.RUNNING
            ]
            if running:
                nodes = dict(state.nodes)
                for node_id in running:
                    nodes[node_id] = replace(nodes[node_id], status=NodeStatus.BLOCKED)
                blocked = replace(state, nodes=MappingProxyType(nodes))
                transaction = self._transaction(lease)
                transaction._set_loaded(plan, state, sequence, event_hash, run_status, events, witnesses)
                transaction._commit_event(
                    "recovery_running_blocked",
                    blocked,
                    {"node_ids": sorted(running)},
                    run_status=run_status,
                )
                return RecoveryResult(
                    "blocked",
                    "recovery.running_work_uncertain",
                    blocked,
                    archived,
                )
            try:
                changed = self._write_receipts(receipt_material) or changed
                self._write_projections(state, run_status)
            except StoreError as exc:
                return RecoveryResult("blocked", exc.code, state, archived)
            if drifted or historical_producers:
                return RecoveryResult("recovered", "recovery.artifact_drift", state, archived)
            if changed:
                return RecoveryResult("recovered", "recovery.replayed", state, archived)
            return RecoveryResult("clean", "recovery.clean", state, archived)


class WorkflowTransaction:
    """One lock-scoped runtime transaction used by the Task 7 service layer."""

    def __init__(self, store: WorkflowStore, *, _lease_token: object | None = None) -> None:
        owner = (os.getpid(), threading.get_ident())
        if _lease_token is None or not store._lease_is_valid(_lease_token, owner):
            raise StoreError(
                "store.transaction_invalid",
                "transactions can only be created by an active locked_run() lease",
            )
        self.store = store
        self._lease_token = _lease_token
        self._lease_owner = owner
        self._lease_active = True
        self._plan: CompiledPlan | None = None
        self._plan_sha256 = ""
        self._state: RunState | None = None
        self._event_seq = 0
        self._event_hash = ZERO_HASH
        self._run_status = "active"
        self._events: list[WorkflowEvent] = []
        self._witnesses: EdgeWitnesses = {}

    def _deactivate(self) -> None:
        self._lease_active = False

    def _require_lease(self) -> None:
        current = (os.getpid(), threading.get_ident())
        if current != self._lease_owner:
            raise StoreError(
                "store.transaction_wrong_owner",
                "transaction used outside its owning process or thread",
            )
        if not self._lease_active:
            raise StoreError("store.transaction_inactive", "transaction lease is no longer active")
        try:
            self.store._assert_active_lease(self._lease_token, self._lease_owner)
        except StoreError as exc:
            if exc.code == "store.lock_invalidated":
                raise
            raise StoreError(
                "store.transaction_inactive", "transaction lease is no longer active"
            ) from exc

    def _set_loaded(
        self,
        plan: CompiledPlan,
        state: RunState,
        sequence: int,
        event_hash: str,
        run_status: str,
        events: Sequence[WorkflowEvent] = (),
        witnesses: EdgeWitnesses | None = None,
    ) -> None:
        self._require_lease()
        self._plan = plan
        self._plan_sha256 = _sha256(_plan_data(plan))
        self._state = state
        self._event_seq = sequence
        self._event_hash = event_hash
        self._run_status = run_status
        self._events = list(events)
        self._witnesses = {} if witnesses is None else {
            key: dict(value) for key, value in witnesses.items()
        }

    def load_active_run(self) -> tuple[CompiledPlan, RunState]:
        self._require_lease()
        material = self.store._validated_run_material(
            allow_truncated=False,
            require_no_suffix=True,
        )
        plan, state, sequence, event_hash, run_status, events, _tail, _prefix, witnesses = material
        if run_status != "active":
            raise StoreError("run.not_active", "custom workflow run is not active")
        self._set_loaded(plan, state, sequence, event_hash, run_status, events, witnesses)
        return plan, state

    def events(self, event_type: str | None = None) -> tuple[WorkflowEvent, ...]:
        """Return the verified lock-scoped event view for receipt/idempotency checks."""

        self._require_loaded()
        if event_type is None:
            return tuple(self._events)
        return tuple(event for event in self._events if event.event_type == event_type)

    def _require_loaded(self) -> tuple[CompiledPlan, RunState]:
        self._require_lease()
        if self._plan is None or self._state is None:
            raise StoreError("store.transaction_not_loaded", "load_active_run() must be called first")
        return self._plan, self._state

    @staticmethod
    def _validate_binding(plan: CompiledPlan, state: RunState) -> None:
        _validate_state_binding(plan, state)

    def _prepare_event(
        self,
        event_type: str,
        updated_state: RunState,
        payload: Mapping[str, object],
        *,
        run_status: str,
        event_seq: int,
        previous_event_hash: str,
        previous_status: str,
        witnesses: EdgeWitnesses | None = None,
        previous_state: RunState | None = None,
    ) -> tuple[WorkflowEvent, RunState, EdgeWitnesses]:
        plan, loaded_current = self._require_loaded()
        current = loaded_current if previous_state is None else previous_state
        self._validate_binding(plan, updated_state)
        state_data, updated_state = _validated_state_data(updated_state)
        self.store._require_verified_artifact_bytes(updated_state)
        self.store._require_artifact_paths_contained(updated_state)
        _validate_artifact_authority(plan, updated_state)
        validate_node_evidence_delta(plan, current, updated_state, event_type, payload,
                                     self._events, self._witnesses if witnesses is None else witnesses)
        _validate_edge_and_winner_transition(
            plan, current, updated_state, event_type, payload
        )
        self.store._validate_artifact_source_transition(
            plan, current, updated_state, event_type
        )
        next_witnesses = _edge_witnesses_after_step(
            plan,
            current,
            updated_state,
            self._witnesses if witnesses is None else witnesses,
        )
        if self.store._witness_drift_producers(next_witnesses):
            raise StoreError("artifact.verification_failed", "live edge witness bytes have changed")
        if updated_state.run_id != current.run_id:
            raise StoreError(
                "snapshot.run_mismatch", "updated runtime state belongs to another run"
            )
        event_payload = dict(payload)
        if "state" in event_payload or "run_status" in event_payload:
            raise StoreError(
                "events.reserved_payload", "transition payload uses a reserved field"
            )
        event_payload["state"] = state_data
        event_payload["run_status"] = run_status
        event = _validated_event(
            event_seq=event_seq,
            run_id=updated_state.run_id,
            semantic_sha256=plan.semantic_sha256,
            event_type=event_type,
            payload=event_payload,
            previous_event_hash=previous_event_hash,
        )
        _validate_lifecycle_step(event, previous_status)
        return event, updated_state, next_witnesses

    def _preflight_snapshot_and_projections(
        self,
        state: RunState,
        event: WorkflowEvent,
        run_status: str,
    ) -> tuple[object, tuple[bytes, bytes]]:
        snapshot = _snapshot_data(
            state,
            event.event_seq,
            event.event_hash,
            run_status=run_status,
            plan_sha256=self._plan_sha256,
        )
        return (
            _preflight_json_output(snapshot, "snapshot.input_too_large"),
            _projection_material(state, run_status),
        )

    def _commit_event(
        self,
        event_type: str,
        updated_state: RunState,
        payload: Mapping[str, object],
        *,
        run_status: str | None = None,
        replace_snapshot: bool = True,
    ) -> WorkflowEvent:
        self._require_loaded()
        status = self._run_status if run_status is None else run_status
        event, updated_state, next_witnesses = self._prepare_event(
            event_type,
            updated_state,
            payload,
            run_status=status,
            event_seq=self._event_seq + 1,
            previous_event_hash=self._event_hash,
            previous_status=self._run_status,
        )
        snapshot: object | None = None
        projections: tuple[bytes, bytes] | None = None
        if replace_snapshot:
            snapshot, projections = self._preflight_snapshot_and_projections(
                updated_state, event, status
            )
        receipt_material = self.store._receipt_material([*self._events, event])
        try:
            self.store._append_event(self.store.paths.events, event)
        except Exception:
            self._lease_active = False
            raise
        self._event_seq = event.event_seq
        self._event_hash = event.event_hash
        self._state = updated_state
        self._run_status = status
        self._events.append(event)
        self._witnesses = next_witnesses
        if replace_snapshot:
            try:
                self.store._atomic_json(self.store.paths.state, snapshot)
                self.store._write_projections(
                    updated_state, status, prepared=projections
                )
                self.store._write_receipts(receipt_material)
            except Exception:
                self._lease_active = False
                raise
        return event

    def commit_transition(
        self,
        event_type: str,
        updated_state: RunState,
        payload: Mapping[str, object] | None = None,
    ) -> WorkflowEvent:
        return self._commit_event(event_type, updated_state, {} if payload is None else payload)

    def commit_receipted_transition(self, event_type, updated_state, receipt, *, result_sha256, claim_event_seq):
        if event_type != "node_result_recorded":
            raise StoreError("events.invalid_completion", "unsupported receipt event type")
        return self._commit_event(event_type, updated_state, {
            "receipt": receipt, "result_sha256": result_sha256, "claim_event_seq": claim_event_seq,
        })

    def input_witnesses(self):
        """Return an immutable copy of the verified, lock-scoped route evidence."""
        self._require_loaded()
        return MappingProxyType({edge_id: MappingProxyType(dict(items)) for edge_id, items in self._witnesses.items()})

    @staticmethod
    def _apply_control(
        plan: CompiledPlan,
        state: RunState,
        transition: ControlTransition,
    ) -> RunState:
        if transition.node_id not in plan.nodes:
            raise StoreError("control.invalid_transition", "control transition names an unknown node")
        node = plan.nodes[transition.node_id]
        runtime = state.nodes[transition.node_id]
        expected_type = "condition_selected" if node.type == "condition" else "join_succeeded"
        if (
            node.type not in {"condition", "join"}
            or runtime.status is not NodeStatus.READY
            or transition.event_type != expected_type
            or set(transition.edge_updates) != set(plan.outgoing[transition.node_id])
        ):
            raise StoreError("control.invalid_transition", "control transition does not match current state")
        nodes = dict(state.nodes)
        edges = dict(state.edges)
        outputs = runtime.selected_inputs if node.type == "join" else MappingProxyType({})
        nodes[transition.node_id] = replace(
            runtime,
            status=NodeStatus.SUCCEEDED,
            outcome=transition.outcome,
            outputs=MappingProxyType(dict(outputs)),
        )
        for edge_id, status in transition.edge_updates.items():
            edge = plan.edges[edge_id]
            selected: dict[str, str] = {}
            if status is EdgeStatus.SATISFIED:
                target = plan.nodes[edge.target]
                accepted = set(target.inputs)
                if target.type == "join":
                    accepted.update(target.outputs)
                selected = {
                    edge.output_map.get(output_id, output_id): path
                    for output_id, path in outputs.items()
                    if edge.output_map.get(output_id, output_id) in accepted
                }
            edges[edge_id] = EdgeRuntime(status, MappingProxyType(selected))
        return refresh_ready(
            plan,
            replace(state, nodes=MappingProxyType(nodes), edges=MappingProxyType(edges)),
        )

    def commit_control_transitions(
        self,
        transitions: Sequence[ControlTransition],
        final_state: RunState,
    ) -> tuple[WorkflowEvent, ...]:
        plan, current = self._require_loaded()
        if not transitions:
            self._validate_binding(plan, final_state)
            _raw_final, normalized_final = _validated_state_data(final_state)
            _validate_artifact_authority(plan, normalized_final)
            self.store._require_verified_artifact_bytes(normalized_final)
            self.store._require_artifact_paths_contained(normalized_final)
            if _canonical_bytes(_state_data(current)) != _canonical_bytes(_state_data(final_state)):
                raise StoreError("control.final_state_mismatch", "empty control transition set changed state")
            return ()
        prepared: list[tuple[WorkflowEvent, RunState]] = []
        intermediate = current
        witnesses = {key: dict(value) for key, value in self._witnesses.items()}
        next_sequence = self._event_seq
        next_hash = self._event_hash
        next_status = self._run_status
        for transition in transitions:
            before_transition = intermediate
            intermediate = self._apply_control(plan, intermediate, transition)
            event, intermediate, witnesses = self._prepare_event(
                transition.event_type,
                intermediate,
                {
                    "node_id": transition.node_id,
                    "outcome": transition.outcome,
                    "edge_updates": {
                        key: value.value
                        for key, value in sorted(transition.edge_updates.items())
                    },
                },
                run_status=self._run_status,
                event_seq=next_sequence + 1,
                previous_event_hash=next_hash,
                previous_status=next_status,
                witnesses=witnesses,
                previous_state=before_transition,
            )
            prepared.append((event, intermediate))
            next_sequence = event.event_seq
            next_hash = event.event_hash
            next_status = self._run_status
        self._validate_binding(plan, final_state)
        _raw_final, normalized_final = _validated_state_data(final_state)
        _validate_artifact_authority(plan, normalized_final)
        self.store._require_verified_artifact_bytes(normalized_final)
        self.store._require_artifact_paths_contained(normalized_final)
        if _canonical_bytes(_state_data(intermediate)) != _canonical_bytes(_state_data(final_state)):
            raise StoreError("control.final_state_mismatch", "control transitions do not produce supplied final state")
        final_event = prepared[-1][0]
        snapshot, projections = self._preflight_snapshot_and_projections(
            normalized_final,
            final_event,
            self._run_status,
        )
        committed: list[WorkflowEvent] = []
        try:
            for event, event_state in prepared:
                self.store._append_event(self.store.paths.events, event)
                self._event_seq = event.event_seq
                self._event_hash = event.event_hash
                self._state = event_state
                self._events.append(event)
                committed.append(event)
            self._witnesses = witnesses
            self.store._atomic_json(self.store.paths.state, snapshot)
            self.store._write_projections(
                normalized_final,
                self._run_status,
                prepared=projections,
            )
        except Exception:
            self._lease_active = False
            raise
        self._state = normalized_final
        return tuple(committed)
