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
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from .catalog import SkillIdentity, ValidatorIdentity
from .compiler import CompiledEdge, CompiledNode, CompiledPlan
from .fs import (
    PathSafetyError,
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
    initial_run,
    mark_descendants_stale,
    refresh_ready,
)
from .schema import (
    WorkflowDocument,
    WorkflowError,
    behavior_payload,
    document_sha256,
    parse_workflow,
)


ZERO_HASH = "0" * 64
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
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
    )


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


def _integer(value: object, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StoreError(code, "persisted integer field is invalid")
    return value


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
        required = tuple(sorted(set(required_warning_codes)))
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
        codes = item["acknowledged_warning_codes"]
        if (
            not isinstance(codes, list)
            or not all(isinstance(code, str) and code for code in codes)
            or codes != sorted(set(codes))
        ):
            raise StoreError("selection.invalid", "selection warning acknowledgement is invalid")
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
        elif any((workflow_id, semantic_revision, semantic_sha256, codes, acknowledged_hash, acknowledged_at)):
            raise StoreError("selection.invalid", "official selection must not retain a custom binding")
        return cls(
            mode,
            revision,
            workflow_id,
            semantic_revision,
            semantic_sha256,
            tuple(codes),
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
        previous = _plain_string(item["previous_event_hash"], "events.invalid_event")
        event_hash = _plain_string(item["event_hash"], "events.invalid_event")
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
                "outcomes": list(node.outcomes),
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
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise StoreError(code, "persisted string list is invalid")
    return tuple(value)


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
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise StoreError(code, "persisted string mapping is invalid")
    return MappingProxyType(dict(value))


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
        {"status", "attempt", "outcome", "claim_token_hash", "selected_inputs", "outputs", "auxiliary_outputs"}
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
            _plain_string(runtime["claim_token_hash"], "snapshot.invalid", allow_empty=True),
            _string_map(runtime["selected_inputs"], "snapshot.invalid"),
            _string_map(runtime["outputs"], "snapshot.invalid"),
            MappingProxyType(
                {str(key): _string_list(items, "snapshot.invalid") for key, items in auxiliary.items()}
            ),
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
        if artifact_id != artifact["artifact_id"]:
            raise StoreError("snapshot.invalid", "runtime artifact identity is invalid")
        artifact_state = _plain_string(artifact["state"], "snapshot.invalid")
        if artifact_state not in {"verified", "stale"}:
            raise StoreError("snapshot.invalid", "runtime artifact state is invalid")
        artifacts[str(artifact_id)] = ArtifactRuntime(
            str(artifact["artifact_id"]),
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
    except WorkflowError as exc:
        raise StoreError("snapshot.invalid", str(exc)) from exc


def _snapshot_data(
    state: RunState,
    event_seq: int,
    event_hash: str,
    *,
    run_status: str,
) -> dict[str, object]:
    return {
        "schema_version": "run-snapshot-v1",
        "run_status": run_status,
        "last_applied_event_seq": event_seq,
        "last_applied_event_hash": event_hash,
        "state": _state_data(state),
    }


def _parse_snapshot(value: object) -> tuple[RunState, int, str, str]:
    item = _exact_mapping(value, _SNAPSHOT_FIELDS, "snapshot.invalid")
    if item["schema_version"] != "run-snapshot-v1":
        raise StoreError("snapshot.invalid", "runtime snapshot version is invalid")
    run_status = _plain_string(item["run_status"], "snapshot.invalid")
    if run_status not in {"active", "stopped", "archived"}:
        raise StoreError("snapshot.invalid", "runtime snapshot status is invalid")
    sequence = _integer(item["last_applied_event_seq"], "snapshot.invalid", minimum=1)
    event_hash = _lower_sha256(item["last_applied_event_hash"], "snapshot.invalid")
    return _state_from_data(item["state"]), sequence, event_hash, run_status


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

    @contextmanager
    def _lock(self, *, timeout: float = 20.0) -> Iterator[None]:
        self._ensure_base()
        lock_path = self._checked(self.paths.lock)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise StoreError("path.unsafe", "could not open project lock safely") from exc
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        lock_acquired = False
        try:
            opened_lock = os.fstat(handle.fileno())
            named_lock = lock_path.lstat()
            if (
                not stat.S_ISREG(named_lock.st_mode)
                or (opened_lock.st_dev, opened_lock.st_ino)
                != (named_lock.st_dev, named_lock.st_ino)
            ):
                raise StoreError("path.unsafe", "project lock path changed while opening")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
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
            if (opened_lock.st_dev, opened_lock.st_ino) != (named_lock.st_dev, named_lock.st_ino):
                raise StoreError("path.unsafe", "project lock path changed while acquiring")
            yield
        finally:
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

    def _read_json(self, path: Path, code: str) -> object:
        checked = self._checked(path)
        try:
            value = checked.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise StoreError(code, f"persisted material is not a regular file: {path}")
            return _strict_json_loads(checked.read_text(encoding="utf-8"))
        except StoreError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise StoreError(code, f"persisted JSON is unreadable: {path}") from exc

    def _atomic_json(self, path: Path, value: object) -> None:
        self._checked(path)
        try:
            atomic_write_json(path, value)
        except PathSafetyError as exc:
            raise StoreError("path.unsafe", str(exc)) from exc

    def _atomic_bytes(self, path: Path, value: bytes) -> None:
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

    def _selection_unlocked(self) -> Selection:
        self._checked(self.paths.selection)
        if not self.paths.selection.exists() and not self.paths.selection.is_symlink():
            return Selection("official")
        try:
            return Selection.from_payload(self._read_json(self.paths.selection, "selection.invalid"))
        except StoreError as exc:
            if exc.code == "path.unsafe":
                raise
            raise StoreError("selection.invalid", str(exc)) from exc

    def read_selection(self) -> Selection:
        self._checked(self.paths.selection)
        if not self.paths.selection.exists() and not self.paths.selection.is_symlink():
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
        with self._lock():
            self._ensure_directory(self.paths.revisions)
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
            behavior_changed = previous is None or behavior_payload(previous) != behavior_payload(document)
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

    def _active_snapshot_unlocked(self) -> tuple[RunState, int, str, str] | None:
        if not self.paths.state.exists() and not self.paths.state.is_symlink():
            return None
        return _parse_snapshot(self._read_json(self.paths.state, "snapshot.invalid"))

    @staticmethod
    def _terminal(state: RunState) -> bool:
        terminal = {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.SKIPPED, NodeStatus.STALE}
        return bool(state.nodes) and all(runtime.status in terminal for runtime in state.nodes.values())

    def _reject_active_run(self) -> None:
        if not self.paths.state.exists() and not self.paths.state.is_symlink():
            return
        material = self._validated_run_material(
            allow_truncated=False,
            require_no_suffix=True,
        )
        _plan, state, _seq, _hash, run_status, _events, _tail, _prefix = material
        if run_status == "active" and not self._terminal(state):
            raise StoreError("run.already_active", "a custom workflow run is already active")

    def _audit_append(self, event_type: str, semantic_sha256: str, payload: Mapping[str, object]) -> WorkflowEvent:
        events, _tail, _prefix = self._read_event_log(self.paths.audit_events, allow_truncated=False)
        sequence = events[-1].event_seq + 1 if events else 1
        previous = events[-1].event_hash if events else ZERO_HASH
        event = WorkflowEvent.create(
            event_seq=sequence,
            run_id=f"selection-{sequence}",
            semantic_sha256=semantic_sha256 or ZERO_HASH,
            event_type=event_type,
            payload=payload,
            previous_event_hash=previous,
        )
        self._checked(self.paths.audit_events)
        try:
            append_event(self.paths.audit_events, event)
        except PathSafetyError as exc:
            raise StoreError("path.unsafe", str(exc)) from exc
        return event

    def activate_custom(
        self,
        plan: CompiledPlan,
        *,
        high_risk_warning_codes: Sequence[str],
        acknowledged_warning_codes: Sequence[str],
    ) -> Selection:
        required = tuple(sorted(set(high_risk_warning_codes)))
        acknowledged = tuple(sorted(set(acknowledged_warning_codes)))
        if required != acknowledged or len(acknowledged) != len(tuple(acknowledged_warning_codes)):
            raise StoreError(
                "activation.acknowledgement_mismatch",
                "activation requires the exact current high-risk warning-code set",
            )
        with self._lock():
            self._reject_active_run()
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
        with self._lock():
            previous = self._selection_unlocked()
            if self.paths.state.exists() or self.paths.state.is_symlink():
                material = self._validated_run_material(
                    allow_truncated=False,
                    require_no_suffix=True,
                )
                plan, state, sequence, event_hash, run_status, _events, _tail, _prefix = material
                if run_status == "active":
                    transaction = WorkflowTransaction(self)
                    transaction._set_loaded(
                        plan,
                        state,
                        sequence,
                        event_hash,
                        run_status,
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
        self._ensure_directory(self.paths.archived_runs)
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

    def start_run(self, plan: CompiledPlan, run_id: str) -> RunState:
        normalized_run_id = _run_id(run_id, "run.invalid_id")
        with self._lock():
            if self.paths.state.exists() or self.paths.state.is_symlink():
                material = self._validated_run_material(
                    allow_truncated=False,
                    require_no_suffix=True,
                )
                _old_plan, old_state, _seq, _hash, run_status, _events, _tail, _prefix = material
                if run_status == "active" and not self._terminal(old_state):
                    raise StoreError("run.already_active", "a custom workflow run is already active")
                self._archive_old_run(old_state)
            self._ensure_directory(self.paths.run_dir)
            state = initial_run(plan, normalized_run_id)
            self._atomic_json(self.paths.plan, _plan_data(plan))
            event = WorkflowEvent.create(
                event_seq=1,
                run_id=normalized_run_id,
                semantic_sha256=plan.semantic_sha256,
                event_type="run_started",
                payload={"state": _state_data(state), "run_status": "active"},
                previous_event_hash=ZERO_HASH,
            )
            try:
                append_event(self.paths.events, event)
            except PathSafetyError as exc:
                raise StoreError("path.unsafe", str(exc)) from exc
            self._atomic_json(self.paths.state, _snapshot_data(state, 1, event.event_hash, run_status="active"))
            self._write_projections(state, "active")
            return state

    @contextmanager
    def locked_run(self) -> Iterator["WorkflowTransaction"]:
        with self._lock():
            transaction = WorkflowTransaction(self)
            yield transaction

    def _read_event_log(
        self, path: Path, *, allow_truncated: bool
    ) -> tuple[list[WorkflowEvent], bytes | None, bytes]:
        if not path.exists() and not path.is_symlink():
            return [], None, b""
        checked = self._checked(path)
        try:
            raw = checked.read_bytes()
        except OSError as exc:
            raise StoreError("events.read_error", "could not read event log") from exc
        tail: bytes | None = None
        prefix = raw
        if raw and not raw.endswith(b"\n"):
            split = raw.rfind(b"\n")
            tail = raw[split + 1 :]
            prefix = raw[: split + 1]
            if not allow_truncated:
                raise StoreError("events.truncated_tail", "event log has an unframed final line")
        events: list[WorkflowEvent] = []
        by_sequence: dict[int, WorkflowEvent] = {}
        expected = 1
        last_hash = ZERO_HASH
        for line in prefix.splitlines():
            try:
                value = _strict_json_loads(line.decode("utf-8"))
            except StoreError:
                raise
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
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
            prior = by_sequence.get(event.event_seq)
            if prior is not None:
                if event.to_payload() == prior.to_payload():
                    continue
                raise StoreError("events.duplicate_conflict", "event sequence has conflicting canonical content")
            if not event.hash_is_valid():
                raise StoreError("events.hash_mismatch", "event content does not match its hash")
            if event.previous_event_hash != last_hash:
                raise StoreError("events.hash_mismatch", "event previous hash does not match the chain")
            events.append(event)
            by_sequence[event.event_seq] = event
            expected += 1
            last_hash = event.event_hash
        return events, tail, prefix

    def _validated_run_material(
        self,
        *,
        allow_truncated: bool,
        require_no_suffix: bool,
    ) -> tuple[CompiledPlan, RunState, int, str, str, list[WorkflowEvent], bytes | None, bytes]:
        if not self.paths.state.exists() and not self.paths.state.is_symlink():
            raise StoreError("run.not_found", "there is no active custom workflow run")
        plan = _plan_from_data(self._read_json(self.paths.plan, "plan.invalid"))
        state, sequence, event_hash, run_status = _parse_snapshot(
            self._read_json(self.paths.state, "snapshot.invalid")
        )
        events, tail, prefix = self._read_event_log(self.paths.events, allow_truncated=allow_truncated)
        for event in events:
            if event.run_id != state.run_id:
                raise StoreError("events.run_mismatch", "event belongs to another run")
            if event.semantic_sha256 != plan.semantic_sha256:
                raise StoreError("events.semantic_mismatch", "event belongs to another semantic plan")
        if (
            state.workflow_id != plan.workflow_id
            or state.semantic_sha256 != plan.semantic_sha256
            or set(state.nodes) != set(plan.nodes)
            or set(state.edges) != set(plan.edges)
        ):
            raise StoreError("snapshot.plan_mismatch", "runtime snapshot does not match compiled plan")
        for artifact in state.artifacts.values():
            try:
                resolve_project_path(self.project_root, artifact.path)
            except PathSafetyError as exc:
                raise StoreError("path.unsafe", "runtime artifact path is not project-contained") from exc
        boundary = next((event for event in events if event.event_seq == sequence), None)
        if boundary is None or boundary.event_hash != event_hash:
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
        if require_no_suffix and self._artifact_drift_ids(state):
            raise StoreError("recovery.required", "artifact bytes must be recovered before mutation")
        return plan, state, sequence, event_hash, run_status, events, tail, prefix

    def _write_projections(self, state: RunState, run_status: str) -> None:
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
        self._atomic_bytes(self.paths.artifacts, _canonical_bytes(artifacts, newline=True))
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
        self._atomic_text(self.paths.summary, "\n".join(lines) + "\n")

    def read_run_events(self) -> tuple[WorkflowEvent, ...]:
        with self._lock():
            events, _tail, _prefix = self._read_event_log(self.paths.events, allow_truncated=False)
            return tuple(events)

    def _archive_truncated_tail(self, tail: bytes, prefix: bytes) -> Path:
        self._ensure_directory(self.paths.recovery)
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

    def _require_verified_artifact_bytes(self, state: RunState) -> None:
        drifted = self._artifact_drift_ids(state)
        if drifted:
            raise StoreError(
                "artifact.verification_failed",
                f"verified artifact bytes do not match recorded hashes: {drifted[0]}",
            )

    @staticmethod
    def _predicate_mentions_artifact(value: object, artifact_id: str) -> bool:
        if isinstance(value, Mapping):
            if value.get("op") == "artifact_state_is" and value.get("artifact") == artifact_id:
                return True
            return any(WorkflowStore._predicate_mentions_artifact(item, artifact_id) for item in value.values())
        if isinstance(value, (tuple, list)):
            return any(WorkflowStore._predicate_mentions_artifact(item, artifact_id) for item in value)
        return False

    def _mark_drift(self, plan: CompiledPlan, state: RunState, drifted: Sequence[str]) -> RunState:
        artifacts = dict(state.artifacts)
        direct: set[str] = set()
        producers: set[str] = set()
        for artifact_id in drifted:
            artifact = artifacts[artifact_id]
            artifacts[artifact_id] = replace(artifact, state="stale")
            if artifact.producer_node_id in plan.nodes:
                producers.add(artifact.producer_node_id)
            for node_id, node in plan.nodes.items():
                if artifact_id in node.inputs or any(
                    self._predicate_mentions_artifact(case, artifact_id)
                    for case in node.condition_cases
                ):
                    direct.add(node_id)
        updated = replace(state, artifacts=MappingProxyType(artifacts))
        if producers:
            updated = mark_descendants_stale(plan, updated, tuple(sorted(producers)))
        if direct:
            nodes = dict(updated.nodes)
            for node_id in direct:
                nodes[node_id] = replace(nodes[node_id], status=NodeStatus.STALE)
            updated = replace(updated, nodes=MappingProxyType(nodes))
            updated = mark_descendants_stale(plan, updated, tuple(sorted(direct)))
        return updated

    def recover(self) -> RecoveryResult:
        with self._lock():
            archived: Path | None = None
            changed = False
            state_exists = self.paths.state.exists() or self.paths.state.is_symlink()
            if not state_exists:
                plan_exists = self.paths.plan.exists() or self.paths.plan.is_symlink()
                events_exist = self.paths.events.exists() or self.paths.events.is_symlink()
                if not plan_exists and not events_exist:
                    return RecoveryResult("clean", "recovery.no_run")
                if not plan_exists or not events_exist:
                    return RecoveryResult("blocked", "recovery.incomplete_run")
                try:
                    plan = _plan_from_data(self._read_json(self.paths.plan, "plan.invalid"))
                    events, tail, prefix = self._read_event_log(
                        self.paths.events, allow_truncated=True
                    )
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code)
                if not events or events[0].event_type != "run_started":
                    return RecoveryResult("blocked", "recovery.incomplete_run")
                state: RunState | None = None
                run_status = "active"
                for event in events:
                    raw_state = event.payload.get("state")
                    next_status = event.payload.get("run_status")
                    if (
                        event.semantic_sha256 != plan.semantic_sha256
                        or not isinstance(raw_state, Mapping)
                        or next_status not in {"active", "stopped", "archived"}
                    ):
                        return RecoveryResult("blocked", "events.unreplayable")
                    try:
                        candidate = _state_from_data(raw_state)
                    except StoreError:
                        return RecoveryResult("blocked", "events.unreplayable")
                    if (
                        candidate.run_id != event.run_id
                        or candidate.workflow_id != plan.workflow_id
                        or candidate.semantic_sha256 != plan.semantic_sha256
                        or set(candidate.nodes) != set(plan.nodes)
                        or set(candidate.edges) != set(plan.edges)
                    ):
                        return RecoveryResult("blocked", "events.unreplayable")
                    if state is not None and candidate.run_id != state.run_id:
                        return RecoveryResult("blocked", "events.run_mismatch")
                    state = candidate
                    run_status = str(next_status)
                assert state is not None
                sequence = events[-1].event_seq
                event_hash = events[-1].event_hash
                if tail is not None:
                    try:
                        archived = self._archive_truncated_tail(tail, prefix)
                    except StoreError as exc:
                        return RecoveryResult("blocked", exc.code, state)
                try:
                    self._atomic_json(
                        self.paths.state,
                        _snapshot_data(state, sequence, event_hash, run_status=run_status),
                    )
                    self._write_projections(state, run_status)
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code, state, archived)
                changed = True
                tail = None
            else:
                try:
                    material = self._validated_run_material(
                        allow_truncated=True,
                        require_no_suffix=False,
                    )
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code)
                plan, state, sequence, event_hash, run_status, events, tail, prefix = material
            if tail is not None:
                try:
                    archived = self._archive_truncated_tail(tail, prefix)
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code, state)
                changed = True
            for event in events:
                if event.event_seq <= sequence:
                    continue
                raw_state = event.payload.get("state")
                next_status = event.payload.get("run_status")
                if not isinstance(raw_state, Mapping) or next_status not in {"active", "stopped", "archived"}:
                    return RecoveryResult("blocked", "events.unreplayable", state, archived)
                try:
                    candidate = _state_from_data(raw_state)
                except StoreError:
                    return RecoveryResult("blocked", "events.unreplayable", state, archived)
                if (
                    candidate.run_id != state.run_id
                    or candidate.workflow_id != plan.workflow_id
                    or candidate.semantic_sha256 != plan.semantic_sha256
                ):
                    return RecoveryResult("blocked", "events.unreplayable", state, archived)
                state = candidate
                sequence = event.event_seq
                event_hash = event.event_hash
                run_status = str(next_status)
                changed = True
            if changed:
                try:
                    self._atomic_json(
                        self.paths.state,
                        _snapshot_data(state, sequence, event_hash, run_status=run_status),
                    )
                    self._write_projections(state, run_status)
                except StoreError as exc:
                    return RecoveryResult("blocked", exc.code, state, archived)

            drifted = list(self._artifact_drift_ids(state))
            if drifted:
                stale = self._mark_drift(plan, state, drifted)
                transaction = WorkflowTransaction(self)
                transaction._set_loaded(plan, state, sequence, event_hash, run_status)
                transaction._commit_event(
                    "artifacts_marked_stale",
                    stale,
                    {"artifact_ids": list(drifted)},
                    run_status=run_status,
                )
                state = stale
                sequence = transaction._event_seq
                event_hash = transaction._event_hash
                changed = True

            running = [
                node_id for node_id, runtime in state.nodes.items() if runtime.status is NodeStatus.RUNNING
            ]
            if running:
                nodes = dict(state.nodes)
                for node_id in running:
                    nodes[node_id] = replace(nodes[node_id], status=NodeStatus.BLOCKED)
                blocked = replace(state, nodes=MappingProxyType(nodes))
                transaction = WorkflowTransaction(self)
                transaction._set_loaded(plan, state, sequence, event_hash, run_status)
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
                self._write_projections(state, run_status)
            except StoreError as exc:
                return RecoveryResult("blocked", exc.code, state, archived)
            if drifted:
                return RecoveryResult("recovered", "recovery.artifact_drift", state, archived)
            if changed:
                return RecoveryResult("recovered", "recovery.replayed", state, archived)
            return RecoveryResult("clean", "recovery.clean", state, archived)


class WorkflowTransaction:
    """One lock-scoped runtime transaction used by the Task 7 service layer."""

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self._plan: CompiledPlan | None = None
        self._state: RunState | None = None
        self._event_seq = 0
        self._event_hash = ZERO_HASH
        self._run_status = "active"
        self._events: list[WorkflowEvent] = []

    def _set_loaded(
        self,
        plan: CompiledPlan,
        state: RunState,
        sequence: int,
        event_hash: str,
        run_status: str,
        events: Sequence[WorkflowEvent] = (),
    ) -> None:
        self._plan = plan
        self._state = state
        self._event_seq = sequence
        self._event_hash = event_hash
        self._run_status = run_status
        self._events = list(events)

    def load_active_run(self) -> tuple[CompiledPlan, RunState]:
        material = self.store._validated_run_material(
            allow_truncated=False,
            require_no_suffix=True,
        )
        plan, state, sequence, event_hash, run_status, events, _tail, _prefix = material
        if run_status != "active":
            raise StoreError("run.not_active", "custom workflow run is not active")
        self._set_loaded(plan, state, sequence, event_hash, run_status, events)
        return plan, state

    def events(self, event_type: str | None = None) -> tuple[WorkflowEvent, ...]:
        """Return the verified lock-scoped event view for receipt/idempotency checks."""

        self._require_loaded()
        if event_type is None:
            return tuple(self._events)
        return tuple(event for event in self._events if event.event_type == event_type)

    def _require_loaded(self) -> tuple[CompiledPlan, RunState]:
        if self._plan is None or self._state is None:
            raise StoreError("store.transaction_not_loaded", "load_active_run() must be called first")
        return self._plan, self._state

    @staticmethod
    def _validate_binding(plan: CompiledPlan, state: RunState) -> None:
        if (
            state.workflow_id != plan.workflow_id
            or state.semantic_sha256 != plan.semantic_sha256
            or set(state.nodes) != set(plan.nodes)
            or set(state.edges) != set(plan.edges)
        ):
            raise StoreError("snapshot.plan_mismatch", "updated runtime state does not match compiled plan")

    def _commit_event(
        self,
        event_type: str,
        updated_state: RunState,
        payload: Mapping[str, object],
        *,
        run_status: str | None = None,
        replace_snapshot: bool = True,
    ) -> WorkflowEvent:
        plan, current = self._require_loaded()
        self._validate_binding(plan, updated_state)
        self.store._require_verified_artifact_bytes(updated_state)
        if updated_state.run_id != current.run_id:
            raise StoreError("snapshot.run_mismatch", "updated runtime state belongs to another run")
        status = self._run_status if run_status is None else run_status
        event_payload = dict(payload)
        if "state" in event_payload or "run_status" in event_payload:
            raise StoreError("events.reserved_payload", "transition payload uses a reserved field")
        event_payload["state"] = _state_data(updated_state)
        event_payload["run_status"] = status
        event = WorkflowEvent.create(
            event_seq=self._event_seq + 1,
            run_id=updated_state.run_id,
            semantic_sha256=plan.semantic_sha256,
            event_type=event_type,
            payload=event_payload,
            previous_event_hash=self._event_hash,
        )
        try:
            append_event(self.store.paths.events, event)
        except PathSafetyError as exc:
            raise StoreError("path.unsafe", str(exc)) from exc
        self._event_seq = event.event_seq
        self._event_hash = event.event_hash
        self._state = updated_state
        self._run_status = status
        self._events.append(event)
        if replace_snapshot:
            self.store._atomic_json(
                self.store.paths.state,
                _snapshot_data(updated_state, self._event_seq, self._event_hash, run_status=status),
            )
            self.store._write_projections(updated_state, status)
        return event

    def commit_transition(
        self,
        event_type: str,
        updated_state: RunState,
        payload: Mapping[str, object] | None = None,
    ) -> WorkflowEvent:
        return self._commit_event(event_type, updated_state, {} if payload is None else payload)

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
            if _state_data(current) != _state_data(final_state):
                raise StoreError("control.final_state_mismatch", "empty control transition set changed state")
            return ()
        events: list[WorkflowEvent] = []
        intermediate = current
        for transition in transitions:
            intermediate = self._apply_control(plan, intermediate, transition)
            event = self._commit_event(
                transition.event_type,
                intermediate,
                {
                    "node_id": transition.node_id,
                    "outcome": transition.outcome,
                    "edge_updates": {
                        key: value.value for key, value in sorted(transition.edge_updates.items())
                    },
                },
                replace_snapshot=False,
            )
            events.append(event)
        if _state_data(intermediate) != _state_data(final_state):
            raise StoreError("control.final_state_mismatch", "control transitions do not produce supplied final state")
        self.store._atomic_json(
            self.store.paths.state,
            _snapshot_data(final_state, self._event_seq, self._event_hash, run_status=self._run_status),
        )
        self.store._write_projections(final_state, self._run_status)
        self._state = final_state
        return tuple(events)
