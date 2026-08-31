"""Immutable, versioned custom-workflow documents and semantic hashing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = "paper-workflow-custom-v1"
NODE_TYPES = frozenset({"task", "condition", "join", "validator"})

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$")
_PROJECTION_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_POLICIES = frozenset({"block", "skip_branch"})
_JOIN_MODES = frozenset({"all_active", "any_success"})
_VALIDATOR_OUTCOMES = frozenset({"pass", "fail", "blocked"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "document_revision",
        "semantic_revision",
        "derived_from",
        "max_parallelism",
        "external_inputs",
        "nodes",
        "edges",
        "ui",
    }
)
_NODE_FIELDS = frozenset(
    {
        "id",
        "type",
        "display_name",
        "entry",
        "enabled",
        "skill_ref",
        "validator_ref",
        "origin_projection_node_id",
        "inputs",
        "outputs",
        "outcomes",
        "write_scopes",
        "failure_policy",
        "condition_cases",
        "join_mode",
    }
)
_EDGE_FIELDS = frozenset({"id", "source", "target", "trigger", "output_map"})
_DERIVED_FROM_FIELDS = frozenset({"projection_id", "projection_sha256"})
_UI_FIELDS = frozenset({"positions"})
_POSITION_FIELDS = frozenset({"x", "y"})
_CONDITION_CASE_FIELDS = frozenset({"outcome", "when"})

_MAX_SCALAR = 4_000
_MAX_ITEMS = 1_000
_MAX_PARALLELISM = 64
_MAX_JSON_DEPTH = 32
_MAX_COORDINATE = 1_000_000


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str, *, node_id: str = "", edge_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.node_id = node_id
        self.edge_id = edge_id


@dataclass(frozen=True)
class WorkflowIssue:
    severity: str
    code: str
    message: str
    node_id: str = ""
    edge_id: str = ""


@dataclass(frozen=True)
class NodeSpec:
    id: str
    type: str
    display_name: str
    entry: bool
    enabled: bool
    skill_ref: str | None
    validator_ref: str | None
    origin_projection_node_id: str | None
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    outcomes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    failure_policy: str
    condition_cases: tuple[Mapping[str, Any], ...]
    join_mode: str


@dataclass(frozen=True)
class EdgeSpec:
    id: str
    source: str
    target: str
    trigger: str
    output_map: Mapping[str, str]


@dataclass(frozen=True)
class WorkflowDocument:
    schema_version: str
    workflow_id: str
    document_revision: int
    semantic_revision: int
    derived_from: Mapping[str, Any] | None
    max_parallelism: int
    external_inputs: tuple[str, ...]
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    ui: Mapping[str, Any] = field(compare=False, hash=False)


def _fail(code: str, message: str, *, node_id: str = "", edge_id: str = "") -> None:
    raise WorkflowError(code, message, node_id=node_id, edge_id=edge_id)


def _object(value: object, label: str, *, node_id: str = "", edge_id: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        _fail("schema.invalid_object", f"{label} must be an object", node_id=node_id, edge_id=edge_id)
    if not all(isinstance(key, str) for key in value):
        _fail("schema.invalid_object_key", f"{label} keys must be strings", node_id=node_id, edge_id=edge_id)
    return value


def _exact_fields(
    value: object,
    allowed: frozenset[str],
    label: str,
    *,
    unknown_code: str,
    node_id: str = "",
    edge_id: str = "",
) -> dict[str, object]:
    item = _object(value, label, node_id=node_id, edge_id=edge_id)
    unknown = sorted(set(item) - allowed)
    if unknown:
        _fail(
            unknown_code,
            f"{label} contains unknown field: {unknown[0]}",
            node_id=node_id,
            edge_id=edge_id,
        )
    missing = sorted(allowed - set(item))
    if missing:
        _fail(
            "schema.missing_field",
            f"{label} is missing required field: {missing[0]}",
            node_id=node_id,
            edge_id=edge_id,
        )
    return item


def _scalar(value: object, label: str, *, node_id: str = "", edge_id: str = "") -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SCALAR:
        _fail("schema.invalid_scalar", f"{label} must be a non-empty string up to {_MAX_SCALAR} characters", node_id=node_id, edge_id=edge_id)
    if value != value.strip() or any(ord(char) < 32 for char in value):
        _fail("schema.invalid_scalar", f"{label} contains surrounding whitespace or a control character", node_id=node_id, edge_id=edge_id)
    return value


def _identifier(value: object, label: str, *, node_id: str = "", edge_id: str = "") -> str:
    item = _scalar(value, label, node_id=node_id, edge_id=edge_id)
    if not _IDENTIFIER_RE.fullmatch(item):
        _fail("schema.invalid_identifier", f"{label} must be a normalized identifier", node_id=node_id, edge_id=edge_id)
    return item


def _optional_identifier(value: object, label: str, *, node_id: str = "") -> str | None:
    if value is None:
        return None
    return _identifier(value, label, node_id=node_id)


def _projection_identifier(value: object, label: str) -> str:
    item = _scalar(value, label)
    if not _PROJECTION_IDENTIFIER_RE.fullmatch(item):
        _fail(
            "schema.invalid_projection_id",
            f"{label} must be a normalized versioned projection identifier",
        )
    return item


def _boolean(value: object, label: str, *, node_id: str = "") -> bool:
    if not isinstance(value, bool):
        _fail("schema.invalid_boolean", f"{label} must be true or false", node_id=node_id)
    return value


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail("schema.invalid_integer", f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _identifier_list(value: object, label: str, *, node_id: str = "") -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("schema.invalid_list", f"{label} must be a list with at most {_MAX_ITEMS} items", node_id=node_id)
    items = tuple(_identifier(item, label, node_id=node_id) for item in value)
    if len(items) != len(set(items)):
        _fail("schema.duplicate_identifier", f"{label} must not contain duplicate identifiers", node_id=node_id)
    return items


def _freeze_json(value: object, label: str, *, depth: int = 0, node_id: str = "") -> object:
    if depth > _MAX_JSON_DEPTH:
        _fail("schema.json_too_deep", f"{label} exceeds the maximum nesting depth", node_id=node_id)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _scalar(value, label, node_id=node_id)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("schema.invalid_number", f"{label} must be finite", node_id=node_id)
        return value
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            _fail("schema.invalid_list", f"{label} has too many items", node_id=node_id)
        return tuple(_freeze_json(item, label, depth=depth + 1, node_id=node_id) for item in value)
    if isinstance(value, dict):
        if len(value) > _MAX_ITEMS:
            _fail("schema.invalid_object", f"{label} has too many fields", node_id=node_id)
        frozen: dict[str, object] = {}
        for key, item in value.items():
            key_value = _scalar(key, f"{label} key", node_id=node_id)
            frozen[key_value] = _freeze_json(item, label, depth=depth + 1, node_id=node_id)
        return MappingProxyType(frozen)
    _fail("schema.invalid_json_value", f"{label} contains a value outside JSON", node_id=node_id)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail("schema.duplicate_json_key", f"JSON object contains duplicate key: {key}")
        value[key] = item
    return value


def _derived_from(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    item = _exact_fields(
        value,
        _DERIVED_FROM_FIELDS,
        "derived_from",
        unknown_code="schema.unknown_derived_from_field",
    )
    projection_id = _projection_identifier(item["projection_id"], "derived_from.projection_id")
    projection_sha256 = _scalar(item["projection_sha256"], "derived_from.projection_sha256")
    if not _SHA256_RE.fullmatch(projection_sha256):
        _fail("schema.invalid_projection_hash", "derived_from.projection_sha256 must be a lowercase SHA-256 hex digest")
    return MappingProxyType({"projection_id": projection_id, "projection_sha256": projection_sha256})


def _condition_cases(value: object, node_id: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("schema.invalid_list", "condition_cases must be a list", node_id=node_id)
    cases: list[Mapping[str, Any]] = []
    outcomes: set[str] = set()
    for index, case in enumerate(value):
        item = _exact_fields(
            case,
            _CONDITION_CASE_FIELDS,
            f"condition_cases[{index}]",
            unknown_code="schema.unknown_condition_case_field",
            node_id=node_id,
        )
        outcome = _identifier(item["outcome"], "condition case outcome", node_id=node_id)
        if outcome == "default":
            _fail("schema.reserved_outcome", "condition case outcome 'default' is reserved for the fallback edge", node_id=node_id)
        if outcome in outcomes:
            _fail("schema.duplicate_condition_outcome", "condition case outcomes must be unique", node_id=node_id)
        outcomes.add(outcome)
        when = _freeze_json(item["when"], "condition case predicate", node_id=node_id)
        if not isinstance(when, Mapping):
            _fail("schema.invalid_condition_case", "condition case predicate must be an object", node_id=node_id)
        cases.append(MappingProxyType({"outcome": outcome, "when": when}))
    return tuple(cases)


def _node(value: object, derived_from: Mapping[str, Any] | None) -> NodeSpec:
    preliminary = _object(value, "node")
    node_id = _identifier(preliminary.get("id"), "node.id") if "id" in preliminary else ""
    item = _exact_fields(
        preliminary,
        _NODE_FIELDS,
        "node",
        unknown_code="schema.unknown_node_field",
        node_id=node_id,
    )
    node_id = _identifier(item["id"], "node.id", node_id=node_id)
    node_type = _scalar(item["type"], "node.type", node_id=node_id)
    if node_type not in NODE_TYPES:
        _fail("schema.invalid_node_type", f"node.type must be one of {sorted(NODE_TYPES)}", node_id=node_id)
    display_name = _scalar(item["display_name"], "node.display_name", node_id=node_id)
    entry = _boolean(item["entry"], "node.entry", node_id=node_id)
    enabled = _boolean(item["enabled"], "node.enabled", node_id=node_id)
    skill_ref = _optional_identifier(item["skill_ref"], "node.skill_ref", node_id=node_id)
    validator_ref = _optional_identifier(item["validator_ref"], "node.validator_ref", node_id=node_id)
    origin_projection_node_id = _optional_identifier(
        item["origin_projection_node_id"], "node.origin_projection_node_id", node_id=node_id
    )
    if origin_projection_node_id is not None and derived_from is None:
        _fail(
            "schema.provenance_without_derived_from",
            "node.origin_projection_node_id requires derived_from projection ID and hash",
            node_id=node_id,
        )
    inputs = _identifier_list(item["inputs"], "node.inputs", node_id=node_id)
    outputs = _identifier_list(item["outputs"], "node.outputs", node_id=node_id)
    declared_outcomes = _identifier_list(item["outcomes"], "node.outcomes", node_id=node_id)
    write_scopes = _identifier_list(item["write_scopes"], "node.write_scopes", node_id=node_id)
    failure_policy = _scalar(item["failure_policy"], "node.failure_policy", node_id=node_id)
    if failure_policy not in _FAILURE_POLICIES:
        _fail("schema.invalid_failure_policy", "node.failure_policy is not supported", node_id=node_id)
    condition_cases = _condition_cases(item["condition_cases"], node_id)
    join_mode = _scalar(item["join_mode"], "node.join_mode", node_id=node_id)
    if join_mode not in _JOIN_MODES:
        _fail("schema.invalid_join_mode", "node.join_mode is not supported", node_id=node_id)

    if node_type == "task":
        if validator_ref is not None:
            _fail("schema.task_validator_forbidden", "task nodes cannot bind a validator", node_id=node_id)
        if "succeeded" not in declared_outcomes:
            _fail("schema.task_succeeded_required", "task outcomes must include succeeded", node_id=node_id)
        if condition_cases:
            _fail("schema.task_condition_cases_forbidden", "task nodes cannot declare condition cases", node_id=node_id)
        if join_mode != "all_active":
            _fail("schema.task_join_mode_forbidden", "task nodes cannot select a join mode", node_id=node_id)
        outcomes = declared_outcomes
    elif node_type == "validator":
        if skill_ref is not None:
            _fail("schema.validator_skill_forbidden", "validator nodes cannot bind a Skill", node_id=node_id)
        if set(declared_outcomes) != _VALIDATOR_OUTCOMES:
            _fail("schema.invalid_validator_outcomes", "validator outcomes must be pass, fail, and blocked", node_id=node_id)
        if condition_cases:
            _fail("schema.validator_condition_cases_forbidden", "validator nodes cannot declare condition cases", node_id=node_id)
        if join_mode != "all_active":
            _fail("schema.validator_join_mode_forbidden", "validator nodes cannot select a join mode", node_id=node_id)
        outcomes = declared_outcomes
    elif node_type == "condition":
        if skill_ref is not None:
            _fail("schema.control_skill_forbidden", "control nodes cannot bind a Skill", node_id=node_id)
        if validator_ref is not None:
            _fail("schema.control_validator_forbidden", "control nodes cannot bind a validator", node_id=node_id)
        if declared_outcomes:
            _fail("schema.condition_outcomes_derived", "condition outcomes must be derived from condition cases", node_id=node_id)
        if not condition_cases:
            _fail("schema.condition_cases_required", "condition nodes require at least one named case", node_id=node_id)
        if join_mode != "all_active":
            _fail("schema.condition_join_mode_forbidden", "condition nodes cannot select a join mode", node_id=node_id)
        outcomes = tuple(case["outcome"] for case in condition_cases) + ("default",)
    else:
        if skill_ref is not None:
            _fail("schema.control_skill_forbidden", "control nodes cannot bind a Skill", node_id=node_id)
        if validator_ref is not None:
            _fail("schema.control_validator_forbidden", "control nodes cannot bind a validator", node_id=node_id)
        if declared_outcomes:
            _fail("schema.join_outcomes_forbidden", "join nodes do not declare custom outcomes", node_id=node_id)
        if condition_cases:
            _fail("schema.join_condition_cases_forbidden", "join nodes cannot declare condition cases", node_id=node_id)
        outcomes = ("succeeded",)

    return NodeSpec(
        id=node_id,
        type=node_type,
        display_name=display_name,
        entry=entry,
        enabled=enabled,
        skill_ref=skill_ref,
        validator_ref=validator_ref,
        origin_projection_node_id=origin_projection_node_id,
        inputs=inputs,
        outputs=outputs,
        outcomes=outcomes,
        write_scopes=write_scopes,
        failure_policy=failure_policy,
        condition_cases=condition_cases,
        join_mode=join_mode,
    )


def _edge(value: object) -> EdgeSpec:
    preliminary = _object(value, "edge")
    edge_id = _identifier(preliminary.get("id"), "edge.id") if "id" in preliminary else ""
    item = _exact_fields(
        preliminary,
        _EDGE_FIELDS,
        "edge",
        unknown_code="schema.unknown_edge_field",
        edge_id=edge_id,
    )
    edge_id = _identifier(item["id"], "edge.id", edge_id=edge_id)
    source = _identifier(item["source"], "edge.source", edge_id=edge_id)
    target = _identifier(item["target"], "edge.target", edge_id=edge_id)
    trigger = _identifier(item["trigger"], "edge.trigger", edge_id=edge_id)
    output_map = _object(item["output_map"], "edge.output_map", edge_id=edge_id)
    if len(output_map) > _MAX_ITEMS:
        _fail("schema.invalid_object", "edge.output_map has too many fields", edge_id=edge_id)
    frozen_output_map: dict[str, str] = {}
    for source_output, target_input in output_map.items():
        source_id = _identifier(source_output, "edge.output_map source", edge_id=edge_id)
        target_id = _identifier(target_input, "edge.output_map target", edge_id=edge_id)
        frozen_output_map[source_id] = target_id
    return EdgeSpec(
        id=edge_id,
        source=source,
        target=target,
        trigger=trigger,
        output_map=MappingProxyType(frozen_output_map),
    )


def _ui(value: object) -> Mapping[str, Any]:
    item = _exact_fields(value, _UI_FIELDS, "ui", unknown_code="schema.unknown_ui_field")
    positions = _object(item["positions"], "ui.positions")
    if len(positions) > _MAX_ITEMS:
        _fail("schema.invalid_object", "ui.positions has too many fields")
    frozen_positions: dict[str, object] = {}
    for node_id, position in positions.items():
        node_key = _identifier(node_id, "ui.positions node ID")
        coordinate = _exact_fields(
            position,
            _POSITION_FIELDS,
            f"ui.positions.{node_key}",
            unknown_code="schema.unknown_position_field",
        )
        frozen_coordinate: dict[str, float | int] = {}
        for axis in ("x", "y"):
            number = coordinate[axis]
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                _fail("schema.invalid_coordinate", f"ui.positions.{node_key}.{axis} must be finite")
            if isinstance(number, int) and abs(number) > _MAX_COORDINATE:
                _fail("schema.invalid_coordinate", f"ui.positions.{node_key}.{axis} exceeds the canvas limit")
            if isinstance(number, float) and (not math.isfinite(number) or abs(number) > _MAX_COORDINATE):
                _fail("schema.invalid_coordinate", f"ui.positions.{node_key}.{axis} exceeds the canvas limit")
            frozen_coordinate[axis] = number
        frozen_positions[node_key] = MappingProxyType(frozen_coordinate)
    return MappingProxyType({"positions": MappingProxyType(frozen_positions)})


def parse_workflow(value: object) -> WorkflowDocument:
    """Parse a JSON-compatible workflow document into immutable schema values."""
    item = _exact_fields(
        value,
        _TOP_LEVEL_FIELDS,
        "workflow",
        unknown_code="schema.unknown_top_level_field",
    )
    schema_version = _scalar(item["schema_version"], "schema_version")
    if schema_version != SCHEMA_VERSION:
        _fail("schema.unsupported_version", f"schema_version must be {SCHEMA_VERSION}")
    workflow_id = _identifier(item["workflow_id"], "workflow_id")
    document_revision = _integer(item["document_revision"], "document_revision", minimum=0, maximum=2_147_483_647)
    semantic_revision = _integer(item["semantic_revision"], "semantic_revision", minimum=0, maximum=2_147_483_647)
    derived_from = _derived_from(item["derived_from"])
    max_parallelism = _integer(item["max_parallelism"], "max_parallelism", minimum=1, maximum=_MAX_PARALLELISM)
    external_inputs = _identifier_list(item["external_inputs"], "external_inputs")

    raw_nodes = item["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes or len(raw_nodes) > _MAX_ITEMS:
        _fail("schema.invalid_nodes", "nodes must be a non-empty list within the size limit")
    nodes = tuple(_node(node, derived_from) for node in raw_nodes)
    node_ids = [node.id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        _fail("schema.duplicate_node_id", "node IDs must be unique")

    raw_edges = item["edges"]
    if not isinstance(raw_edges, list) or len(raw_edges) > _MAX_ITEMS:
        _fail("schema.invalid_edges", "edges must be a list within the size limit")
    edges = tuple(_edge(edge) for edge in raw_edges)
    edge_ids = [edge.id for edge in edges]
    if len(edge_ids) != len(set(edge_ids)):
        _fail("schema.duplicate_edge_id", "edge IDs must be unique")

    return WorkflowDocument(
        schema_version=schema_version,
        workflow_id=workflow_id,
        document_revision=document_revision,
        semantic_revision=semantic_revision,
        derived_from=derived_from,
        max_parallelism=max_parallelism,
        external_inputs=external_inputs,
        nodes=nodes,
        edges=edges,
        ui=_ui(item["ui"]),
    )


def load_workflow(path: Path | str) -> WorkflowDocument:
    """Load and structurally validate one UTF-8 workflow JSON document."""
    source = Path(path)
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except OSError as exc:
        raise WorkflowError("schema.read_error", f"could not read workflow document: {source}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError("schema.invalid_json", f"workflow document is not valid JSON: {source}") from exc
    return parse_workflow(value)


def behavior_payload(document: WorkflowDocument) -> dict[str, object]:
    """Return behavior-bearing fields in deterministic, JSON-ready form."""
    return {
        "schema_version": document.schema_version,
        "workflow_id": document.workflow_id,
        "derived_from": _json_value(document.derived_from),
        "max_parallelism": document.max_parallelism,
        "external_inputs": list(document.external_inputs),
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
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
            for node in sorted(document.nodes, key=lambda item: item.id)
        ],
        "edges": [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "trigger": edge.trigger,
                "output_map": dict(sorted(edge.output_map.items())),
            }
            for edge in sorted(document.edges, key=lambda item: item.id)
        ],
    }


def document_payload(document: WorkflowDocument) -> dict[str, object]:
    """Return the revision-bound semantic payload used for document identity."""
    return {"semantic_revision": document.semantic_revision, **behavior_payload(document)}


def document_sha256(document: WorkflowDocument) -> str:
    """Return the canonical SHA-256 for persisted workflow behavior."""
    raw = json.dumps(document_payload(document), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
