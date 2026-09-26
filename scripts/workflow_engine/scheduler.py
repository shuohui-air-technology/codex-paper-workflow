"""Pure, immutable runtime transitions for compiled custom workflows."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from .compiler import CompiledEdge, CompiledNode, CompiledPlan
from .conditions import ConditionFacts, evaluate_condition
from .schema import WorkflowError


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_FACT_ITEMS = 1_000
_MAX_FACT_STRING = 4_000
_MAX_DECISION_INTEGER = 2**63 - 1


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    STALE = "stale"


class EdgeStatus(str, Enum):
    WAITING = "waiting"
    SATISFIED = "satisfied"
    INACTIVE = "inactive"
    FAILED = "failed"


def _string_map(value: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return MappingProxyType({} if value is None else dict(value))


def _aux_map(
    value: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType(
        {} if value is None else {key: tuple(items) for key, items in value.items()}
    )


def _fact_name(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_FACT_STRING
        or any(ord(char) < 32 for char in value)
    ):
        _fail(code, "runtime fact name must be a bounded normalized string")
    return value


def _decision_map(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or len(value) > _MAX_FACT_ITEMS:
        _fail("runtime.invalid_decision", "decisions must be a bounded mapping")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        name = _fact_name(key, "runtime.invalid_decision")
        if item is None or isinstance(item, bool):
            normalized[name] = item
        elif isinstance(item, str) and len(item) <= _MAX_FACT_STRING:
            normalized[name] = item
        elif isinstance(item, int) and not isinstance(item, bool) and abs(item) <= _MAX_DECISION_INTEGER:
            normalized[name] = item
        elif isinstance(item, float) and math.isfinite(item):
            normalized[name] = item
        else:
            _fail(
                "runtime.invalid_decision",
                "decision values must be bounded finite JSON scalars",
            )
    return MappingProxyType(normalized)


def _project_boolean_map(value: object) -> Mapping[str, bool]:
    if not isinstance(value, Mapping) or len(value) > _MAX_FACT_ITEMS:
        _fail("runtime.invalid_project_fact", "project facts must be a bounded mapping")
    normalized: dict[str, bool] = {}
    for key, item in value.items():
        name = _fact_name(key, "runtime.invalid_project_fact")
        if type(item) is not bool:
            _fail("runtime.invalid_project_fact", "project fact values must be booleans")
        normalized[name] = item
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class EdgeRuntime:
    status: EdgeStatus
    selected_output_map: Mapping[str, str] = field(default_factory=lambda: _string_map())

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_output_map", _string_map(self.selected_output_map))


@dataclass(frozen=True)
class ArtifactRuntime:
    artifact_id: str
    path: str
    sha256: str
    state: str
    producer_node_id: str
    producer_attempt: int


@dataclass(frozen=True)
class NodeRuntime:
    status: NodeStatus
    attempt: int = 0
    outcome: str = ""
    claim_token_hash: str = ""
    selected_inputs: Mapping[str, str] = field(default_factory=lambda: _string_map())
    outputs: Mapping[str, str] = field(default_factory=lambda: _string_map())
    auxiliary_outputs: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _aux_map()
    )
    winner_edge_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_inputs", _string_map(self.selected_inputs))
        object.__setattr__(self, "outputs", _string_map(self.outputs))
        object.__setattr__(self, "auxiliary_outputs", _aux_map(self.auxiliary_outputs))


@dataclass(frozen=True)
class RunState:
    run_id: str
    workflow_id: str
    semantic_sha256: str
    nodes: Mapping[str, NodeRuntime]
    edges: Mapping[str, EdgeRuntime]
    artifacts: Mapping[str, ArtifactRuntime]
    decisions: Mapping[str, object]
    project_booleans: Mapping[str, bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", MappingProxyType(dict(self.nodes)))
        object.__setattr__(self, "edges", MappingProxyType(dict(self.edges)))
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        object.__setattr__(self, "decisions", _decision_map(self.decisions))
        object.__setattr__(self, "project_booleans", _project_boolean_map(self.project_booleans))


@dataclass(frozen=True)
class ControlTransition:
    node_id: str
    event_type: str
    outcome: str
    edge_updates: Mapping[str, EdgeStatus]

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_updates", MappingProxyType(dict(self.edge_updates)))


def _fail(code: str, message: str, *, node_id: str = "", edge_id: str = "") -> None:
    raise WorkflowError(code, message, node_id=node_id, edge_id=edge_id)


def _validate_binding(plan: CompiledPlan, state: RunState) -> None:
    if (
        state.workflow_id != plan.workflow_id
        or state.semantic_sha256 != plan.semantic_sha256
        or set(state.nodes) != set(plan.nodes)
        or set(state.edges) != set(plan.edges)
    ):
        _fail("runtime.plan_mismatch", "runtime snapshot does not match the compiled plan")


def _replace_state(
    state: RunState,
    *,
    nodes: Mapping[str, NodeRuntime] | None = None,
    edges: Mapping[str, EdgeRuntime] | None = None,
    artifacts: Mapping[str, ArtifactRuntime] | None = None,
) -> RunState:
    return replace(
        state,
        nodes=MappingProxyType(dict(state.nodes if nodes is None else nodes)),
        edges=MappingProxyType(dict(state.edges if edges is None else edges)),
        artifacts=MappingProxyType(dict(state.artifacts if artifacts is None else artifacts)),
    )


def _producer_edges_for_input(
    plan: CompiledPlan, node_id: str, artifact_id: str
) -> tuple[str, ...]:
    producers: list[str] = []
    for edge_id in plan.incoming[node_id]:
        edge = plan.edges[edge_id]
        source = plan.nodes[edge.source]
        if any(edge.output_map.get(output, output) == artifact_id for output in source.outputs):
            producers.append(edge_id)
    return tuple(producers)


def _resolve_node_inputs(
    plan: CompiledPlan,
    state: RunState,
    node_id: str,
) -> tuple[Mapping[str, str], bool]:
    node = plan.nodes[node_id]
    selected: dict[str, str] = {}
    for artifact_id in node.inputs:
        producer_edges = _producer_edges_for_input(plan, node_id, artifact_id)
        if len(producer_edges) > 1:
            _fail(
                "runtime.ambiguous_input",
                f"compiled node input has multiple producers: {artifact_id}",
                node_id=node_id,
            )
        if producer_edges:
            edge_runtime = state.edges[producer_edges[0]]
            value = edge_runtime.selected_output_map.get(artifact_id)
            if edge_runtime.status is not EdgeStatus.SATISFIED or value is None:
                return _string_map(selected), False
            selected[artifact_id] = value
            continue
        if artifact_id not in plan.external_inputs:
            _fail(
                "runtime.input_unbound",
                f"compiled node input has no producer: {artifact_id}",
                node_id=node_id,
            )
        artifact = state.artifacts.get(artifact_id)
        if artifact is None or artifact.state != "verified":
            return _string_map(selected), False
        selected[artifact_id] = artifact.path
    return _string_map(selected), True


def _join_inputs(
    plan: CompiledPlan,
    state: RunState,
    node_id: str,
) -> tuple[Mapping[str, str], bool]:
    node = plan.nodes[node_id]
    runtime = state.nodes[node_id]
    satisfied = [
        edge_id
        for edge_id in plan.incoming[node_id]
        if state.edges[edge_id].status is EdgeStatus.SATISFIED
    ]
    if node.join_mode == "any_success":
        if runtime.winner_edge_id:
            if runtime.winner_edge_id not in satisfied:
                return _string_map(), False
            satisfied = [runtime.winner_edge_id]
        elif len(satisfied) != 1:
            if len(satisfied) > 1:
                _fail("runtime.ambiguous_winner", "simultaneous first join satisfactions have no unique winner", node_id=node_id)
            return _string_map(), False
    selected: dict[str, str] = {}
    for edge_id in satisfied:
        for artifact_id, path in state.edges[edge_id].selected_output_map.items():
            previous = selected.get(artifact_id)
            if previous is not None and previous != path:
                return _string_map(), False
            selected[artifact_id] = path
    if any(artifact_id not in selected for artifact_id in node.outputs):
        return _string_map(selected), False
    return _string_map(selected), True


def _set_outgoing_status(
    plan: CompiledPlan,
    edges: dict[str, EdgeRuntime],
    node_id: str,
    status: EdgeStatus,
) -> None:
    for edge_id in plan.outgoing[node_id]:
        current = edges[edge_id]
        edges[edge_id] = EdgeRuntime(
            status,
            current.selected_output_map if status is EdgeStatus.SATISFIED else _string_map(),
        )


def initial_run(plan: CompiledPlan, run_id: str) -> RunState:
    if not isinstance(run_id, str) or not run_id or run_id != run_id.strip():
        _fail("runtime.invalid_run_id", "run ID must be a non-empty normalized string")
    state = RunState(
        run_id=run_id,
        workflow_id=plan.workflow_id,
        semantic_sha256=plan.semantic_sha256,
        nodes=MappingProxyType(
            {node_id: NodeRuntime(NodeStatus.PENDING) for node_id in sorted(plan.nodes)}
        ),
        edges=MappingProxyType(
            {edge_id: EdgeRuntime(EdgeStatus.WAITING) for edge_id in sorted(plan.edges)}
        ),
        artifacts=MappingProxyType({}),
        decisions=MappingProxyType({}),
        project_booleans=MappingProxyType({}),
    )
    return refresh_ready(plan, state)


def refresh_ready(plan: CompiledPlan, state: RunState) -> RunState:
    """Recompute derived ready, blocked, and recursively skipped states."""
    _validate_binding(plan, state)
    nodes = dict(state.nodes)
    edges = dict(state.edges)

    changed = True
    while changed:
        changed = False
        snapshot = _replace_state(state, nodes=nodes, edges=edges)
        for node_id in plan.topological_order:
            runtime = nodes[node_id]
            # A claimed attempt blocked by recovery needs its own explicit retry.
            # Refreshing another branch must not silently reopen that attempt.
            if runtime.status in {
                NodeStatus.RUNNING,
                NodeStatus.SUCCEEDED,
                NodeStatus.FAILED,
                NodeStatus.SKIPPED,
                NodeStatus.STALE,
            } or (runtime.status is NodeStatus.BLOCKED and runtime.attempt > 0):
                continue
            node = plan.nodes[node_id]
            incoming_ids = plan.incoming[node_id]
            incoming_statuses = [edges[edge_id].status for edge_id in incoming_ids]

            if incoming_ids and all(status is EdgeStatus.INACTIVE for status in incoming_statuses):
                updated = replace(runtime, status=NodeStatus.SKIPPED, selected_inputs=_string_map())
                if updated != runtime:
                    nodes[node_id] = updated
                    changed = True
                before = dict(edges)
                _set_outgoing_status(plan, edges, node_id, EdgeStatus.INACTIVE)
                changed = changed or edges != before
                continue

            active = [status for status in incoming_statuses if status is not EdgeStatus.INACTIVE]
            if node.type == "join" and node.join_mode == "any_success":
                winner = runtime.winner_edge_id
                satisfied_ids = [edge_id for edge_id in incoming_ids if edges[edge_id].status is EdgeStatus.SATISFIED]
                if winner and edges[winner].status is not EdgeStatus.SATISFIED:
                    dependencies_ready = False
                    next_status = NodeStatus.STALE
                elif not winner and len(satisfied_ids) > 1:
                    _fail("runtime.ambiguous_winner", "simultaneous first join satisfactions have no unique winner", node_id=node_id)
                elif satisfied_ids:
                    dependencies_ready = True
                    if not winner:
                        winner = satisfied_ids[0]
                elif any(status is EdgeStatus.WAITING for status in active) or not active:
                    dependencies_ready = False
                    next_status = NodeStatus.PENDING
                else:
                    dependencies_ready = False
                    next_status = NodeStatus.BLOCKED
            elif any(status is EdgeStatus.FAILED for status in active):
                dependencies_ready = False
                next_status = NodeStatus.BLOCKED
            elif active and all(status is EdgeStatus.SATISFIED for status in active):
                dependencies_ready = True
            elif not incoming_ids:
                dependencies_ready = True
            else:
                dependencies_ready = False
                next_status = NodeStatus.PENDING

            selected: Mapping[str, str] = _string_map()
            inputs_ready = True
            if dependencies_ready:
                if node.type == "join":
                    selected, inputs_ready = _join_inputs(plan, snapshot, node_id)
                else:
                    selected, inputs_ready = _resolve_node_inputs(plan, snapshot, node_id)
                next_status = NodeStatus.READY if inputs_ready else NodeStatus.PENDING

            updated = replace(
                runtime,
                status=next_status,
                selected_inputs=selected,
                winner_edge_id=winner if node.type == "join" and node.join_mode == "any_success" else runtime.winner_edge_id,
            )
            if updated != runtime:
                nodes[node_id] = updated
                changed = True

            before = dict(edges)
            if next_status is NodeStatus.BLOCKED:
                _set_outgoing_status(plan, edges, node_id, EdgeStatus.FAILED)
            elif next_status is NodeStatus.STALE:
                _set_outgoing_status(plan, edges, node_id, EdgeStatus.WAITING)
            elif runtime.status is NodeStatus.BLOCKED and next_status in {
                NodeStatus.PENDING,
                NodeStatus.READY,
            }:
                for edge_id in plan.outgoing[node_id]:
                    if edges[edge_id].status is EdgeStatus.FAILED:
                        edges[edge_id] = EdgeRuntime(EdgeStatus.WAITING)
            changed = changed or edges != before

    return _replace_state(state, nodes=nodes, edges=edges)


def ready_node_ids(plan: CompiledPlan, state: RunState) -> tuple[str, ...]:
    _validate_binding(plan, state)
    return tuple(
        node_id
        for node_id in plan.topological_order
        if state.nodes[node_id].status is NodeStatus.READY
    )


def condition_facts(state: RunState) -> ConditionFacts:
    return ConditionFacts(
        node_statuses=MappingProxyType(
            {node_id: runtime.status.value for node_id, runtime in state.nodes.items()}
        ),
        node_outcomes=MappingProxyType(
            {node_id: runtime.outcome for node_id, runtime in state.nodes.items()}
        ),
        decisions=MappingProxyType(dict(state.decisions)),
        artifact_states=MappingProxyType(
            {artifact_id: artifact.state for artifact_id, artifact in state.artifacts.items()}
        ),
        project_booleans=MappingProxyType(dict(state.project_booleans)),
    )


def _edge_outputs(
    plan: CompiledPlan,
    edge: CompiledEdge,
    outputs: Mapping[str, str],
) -> Mapping[str, str]:
    target = plan.nodes[edge.target]
    accepted = set(target.inputs)
    if target.type == "join":
        accepted.update(target.outputs)
    selected = {
        edge.output_map.get(output_id, output_id): path
        for output_id, path in outputs.items()
        if edge.output_map.get(output_id, output_id) in accepted
    }
    return _string_map(selected)


def _complete_control(
    plan: CompiledPlan,
    state: RunState,
    node_id: str,
    outcome: str,
) -> tuple[RunState, ControlTransition]:
    nodes = dict(state.nodes)
    edges = dict(state.edges)
    runtime = nodes[node_id]
    node = plan.nodes[node_id]
    outputs = runtime.selected_inputs if node.type == "join" else _string_map()
    nodes[node_id] = replace(
        runtime,
        status=NodeStatus.SUCCEEDED,
        outcome=outcome,
        outputs=_string_map(outputs),
    )
    updates: dict[str, EdgeStatus] = {}
    for edge_id in plan.outgoing[node_id]:
        edge = plan.edges[edge_id]
        status = EdgeStatus.SATISFIED if edge.trigger == outcome else EdgeStatus.INACTIVE
        selected = _edge_outputs(plan, edge, outputs) if status is EdgeStatus.SATISFIED else _string_map()
        edges[edge_id] = EdgeRuntime(status, selected)
        updates[edge_id] = status
    updated = refresh_ready(plan, _replace_state(state, nodes=nodes, edges=edges))
    transition = ControlTransition(
        node_id=node_id,
        event_type="condition_selected" if node.type == "condition" else "join_succeeded",
        outcome=outcome,
        edge_updates=MappingProxyType(dict(sorted(updates.items()))),
    )
    return updated, transition


def stabilize_control_nodes(
    plan: CompiledPlan, state: RunState
) -> tuple[RunState, tuple[ControlTransition, ...]]:
    current = refresh_ready(plan, state)
    transitions: list[ControlTransition] = []
    while True:
        advanced = False
        for node_id in plan.topological_order:
            node = plan.nodes[node_id]
            runtime = current.nodes[node_id]
            if runtime.status is not NodeStatus.READY or node.type not in {"condition", "join"}:
                continue
            if node.type == "condition":
                outcome = evaluate_condition(node.condition_cases, condition_facts(current))
            else:
                outcome = "succeeded"
            current, transition = _complete_control(plan, current, node_id, outcome)
            transitions.append(transition)
            advanced = True
            break
        if not advanced:
            return current, tuple(transitions)


def claim_transition(
    plan: CompiledPlan, state: RunState, node_id: str, token: str
) -> RunState:
    _validate_binding(plan, state)
    if node_id not in plan.nodes:
        _fail("runtime.unknown_node", f"node does not exist: {node_id}", node_id=node_id)
    if plan.nodes[node_id].type != "task":
        _fail("runtime.node_type", "only task nodes may be claimed", node_id=node_id)
    runtime = state.nodes[node_id]
    if runtime.status is not NodeStatus.READY:
        _fail("runtime.node_not_ready", "node is not ready to claim", node_id=node_id)
    if not isinstance(token, str) or not token:
        _fail("runtime.invalid_claim_token", "claim token must be a non-empty string", node_id=node_id)
    running_count = sum(
        item.status is NodeStatus.RUNNING for item in state.nodes.values()
    )
    if running_count >= plan.max_parallelism:
        _fail("runtime.parallelism_exceeded", "maximum workflow parallelism reached")
    nodes = dict(state.nodes)
    nodes[node_id] = replace(
        runtime,
        status=NodeStatus.RUNNING,
        attempt=runtime.attempt + 1,
        claim_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    return _replace_state(state, nodes=nodes)


def validator_claim_transition(
    plan: CompiledPlan, state: RunState, node_id: str, token: str
) -> RunState:
    """Claim a ready validator without granting task-result authority."""
    _validate_binding(plan, state)
    if node_id not in plan.nodes:
        _fail("runtime.unknown_node", f"node does not exist: {node_id}", node_id=node_id)
    if plan.nodes[node_id].type != "validator":
        _fail("runtime.node_type", "only validator nodes may be claimed", node_id=node_id)
    runtime = state.nodes[node_id]
    if runtime.status is not NodeStatus.READY:
        _fail("runtime.node_not_ready", "node is not ready to claim", node_id=node_id)
    if not isinstance(token, str) or not token:
        _fail("runtime.invalid_claim_token", "claim token must be a non-empty string", node_id=node_id)
    if sum(item.status is NodeStatus.RUNNING for item in state.nodes.values()) >= plan.max_parallelism:
        _fail("runtime.parallelism_exceeded", "maximum workflow parallelism reached")
    nodes = dict(state.nodes)
    nodes[node_id] = replace(
        runtime, status=NodeStatus.RUNNING, attempt=runtime.attempt + 1,
        claim_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    return _replace_state(state, nodes=nodes)


def _validate_outputs(node: CompiledNode, value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(path, str) and path
        for key, path in value.items()
    ):
        _fail("runtime.invalid_outputs", "result outputs must map declared IDs to paths")
    outputs = dict(value)
    if set(outputs) != set(node.outputs):
        _fail("runtime.invalid_outputs", "successful result outputs must match declarations")
    return _string_map(outputs)


def _validate_artifacts(
    node: CompiledNode,
    node_id: str,
    attempt: int,
    value: object,
) -> tuple[ArtifactRuntime, ...]:
    if not isinstance(value, (tuple, list)):
        _fail("runtime.invalid_artifact", "result artifacts must be a sequence")
    artifacts: list[ArtifactRuntime] = []
    seen: set[str] = set()
    for artifact in value:
        if (
            not isinstance(artifact, ArtifactRuntime)
            or artifact.artifact_id not in node.outputs
            or artifact.artifact_id in seen
            or artifact.producer_node_id != node_id
            or artifact.producer_attempt != attempt
            or artifact.state != "verified"
            or not artifact.path
            or not _SHA256_RE.fullmatch(artifact.sha256)
        ):
            _fail("runtime.invalid_artifact", "artifact receipt does not match the running result")
        seen.add(artifact.artifact_id)
        artifacts.append(artifact)
    return tuple(artifacts)


def _record_late_join_outputs(
    plan: CompiledPlan,
    nodes: dict[str, NodeRuntime],
    edges: Mapping[str, EdgeRuntime],
) -> None:
    for node_id in plan.topological_order:
        node = plan.nodes[node_id]
        runtime = nodes[node_id]
        if (
            node.type != "join"
            or node.join_mode != "any_success"
            or runtime.status not in {NodeStatus.READY, NodeStatus.SUCCEEDED}
        ):
            continue
        auxiliary = {key: list(values) for key, values in runtime.auxiliary_outputs.items()}
        for edge_id in plan.incoming[node_id]:
            if edge_id == runtime.winner_edge_id:
                continue
            edge_runtime = edges[edge_id]
            if edge_runtime.status is not EdgeStatus.SATISFIED:
                continue
            for artifact_id, path in edge_runtime.selected_output_map.items():
                if runtime.selected_inputs.get(artifact_id) == path:
                    continue
                values = auxiliary.setdefault(artifact_id, [])
                if path not in values:
                    values.append(path)
        nodes[node_id] = replace(runtime, auxiliary_outputs=_aux_map(auxiliary))


def result_transition(
    plan: CompiledPlan, state: RunState, result: Mapping[str, object]
) -> RunState:
    _validate_binding(plan, state)
    if not isinstance(result, Mapping) or set(result) != {
        "node_id", "attempt", "status", "outcome", "outputs", "artifacts"
    }:
        _fail("runtime.invalid_result", "result receipt has invalid fields")
    node_id = result["node_id"]
    if not isinstance(node_id, str) or node_id not in plan.nodes:
        _fail("runtime.unknown_node", "result node does not exist")
    node = plan.nodes[node_id]
    if node.type != "task":
        _fail("runtime.node_type", "ordinary result transition is task-only", node_id=node_id)
    runtime = state.nodes[node_id]
    if runtime.status is not NodeStatus.RUNNING:
        _fail("runtime.node_not_running", "result node is not running", node_id=node_id)
    attempt = result["attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt != runtime.attempt:
        _fail("runtime.stale_attempt", "result attempt is not current", node_id=node_id)
    status = result["status"]
    if status not in {"succeeded", "failed"}:
        _fail("runtime.invalid_result_status", "result status must be succeeded or failed")
    outcome = result["outcome"]

    nodes = dict(state.nodes)
    edges = dict(state.edges)
    artifacts = dict(state.artifacts)
    if status == "succeeded":
        if not isinstance(outcome, str) or outcome not in node.outcomes:
            _fail("runtime.invalid_outcome", "result outcome is not declared", node_id=node_id)
        outputs = _validate_outputs(node, result["outputs"])
        receipts = _validate_artifacts(node, node_id, attempt, result["artifacts"])
        for artifact in receipts:
            if outputs[artifact.artifact_id] != artifact.path:
                _fail("runtime.invalid_artifact", "artifact path does not match its output")
        nodes[node_id] = replace(
            runtime,
            status=NodeStatus.SUCCEEDED,
            outcome=outcome,
            outputs=outputs,
        )
        for edge_id in plan.outgoing[node_id]:
            edge = plan.edges[edge_id]
            edge_status = EdgeStatus.SATISFIED if edge.trigger == outcome else EdgeStatus.INACTIVE
            selected = _edge_outputs(plan, edge, outputs) if edge_status is EdgeStatus.SATISFIED else _string_map()
            edges[edge_id] = EdgeRuntime(edge_status, selected)
        for artifact in receipts:
            artifacts[artifact.artifact_id] = artifact
    else:
        failure_outputs = result["outputs"]
        failure_artifacts = result["artifacts"]
        if (
            type(outcome) is not str
            or outcome != ""
            or not isinstance(failure_outputs, Mapping)
            or bool(failure_outputs)
            or not isinstance(failure_artifacts, (tuple, list))
            or bool(failure_artifacts)
        ):
            _fail(
                "runtime.invalid_failure",
                "failed result requires an empty outcome, output map, and artifact sequence",
            )
        if node.failure_policy == "skip_branch":
            nodes[node_id] = replace(runtime, status=NodeStatus.SKIPPED, outcome="")
            _set_outgoing_status(plan, edges, node_id, EdgeStatus.INACTIVE)
        else:
            nodes[node_id] = replace(runtime, status=NodeStatus.FAILED, outcome="")
            _set_outgoing_status(plan, edges, node_id, EdgeStatus.FAILED)

    _record_late_join_outputs(plan, nodes, edges)
    return refresh_ready(plan, _replace_state(state, nodes=nodes, edges=edges, artifacts=artifacts))


def validator_result_transition(
    plan: CompiledPlan, state: RunState, result: Mapping[str, object]
) -> RunState:
    """Record a domain outcome or an execution failure; validators produce no files."""
    _validate_binding(plan, state)
    if not isinstance(result, Mapping) or set(result) != {
        "node_id", "attempt", "status", "outcome", "outputs", "artifacts"
    }:
        _fail("runtime.invalid_result", "validator result has invalid fields")
    node_id = result["node_id"]
    if not isinstance(node_id, str) or node_id not in plan.nodes:
        _fail("runtime.unknown_node", "result node does not exist")
    node = plan.nodes[node_id]
    if node.type != "validator":
        _fail("runtime.node_type", "validator result transition is validator-only", node_id=node_id)
    runtime = state.nodes[node_id]
    if runtime.status is not NodeStatus.RUNNING:
        _fail("runtime.node_not_running", "validator is not running", node_id=node_id)
    if type(result["attempt"]) is not int or result["attempt"] != runtime.attempt:
        _fail("runtime.stale_attempt", "validator attempt is not current", node_id=node_id)
    if type(result["status"]) is not str or result["status"] not in {"succeeded", "failed"}:
        _fail("runtime.invalid_result_status", "validator status must be succeeded or failed")
    if not isinstance(result["outputs"], Mapping) or result["outputs"] or not isinstance(result["artifacts"], (list, tuple)) or result["artifacts"]:
        _fail("runtime.invalid_outputs", "validator results cannot produce file outputs")
    nodes = dict(state.nodes)
    edges = dict(state.edges)
    if result["status"] == "succeeded":
        outcome = result["outcome"]
        if not isinstance(outcome, str) or outcome not in node.outcomes or outcome not in {"pass", "fail", "blocked"}:
            _fail("runtime.invalid_outcome", "validator outcome is not declared", node_id=node_id)
        nodes[node_id] = replace(runtime, status=NodeStatus.SUCCEEDED, outcome=outcome, outputs=_string_map())
        for edge_id in plan.outgoing[node_id]:
            edge_status = EdgeStatus.SATISFIED if plan.edges[edge_id].trigger == outcome else EdgeStatus.INACTIVE
            edges[edge_id] = EdgeRuntime(edge_status)
    else:
        if result["outcome"] != "" or type(result["outcome"]) is not str:
            _fail("runtime.invalid_failure", "execution failure has no domain outcome")
        nodes[node_id] = replace(runtime, status=NodeStatus.FAILED, outcome="")
        if node.failure_policy == "skip_branch":
            _set_outgoing_status(plan, edges, node_id, EdgeStatus.INACTIVE)
        else:
            _set_outgoing_status(plan, edges, node_id, EdgeStatus.FAILED)
    _record_late_join_outputs(plan, nodes, edges)
    return refresh_ready(plan, _replace_state(state, nodes=nodes, edges=edges))


def retry_transition(plan: CompiledPlan, state: RunState, node_id: str) -> RunState:
    _validate_binding(plan, state)
    if node_id not in plan.nodes:
        _fail("runtime.unknown_node", f"node does not exist: {node_id}", node_id=node_id)
    if plan.nodes[node_id].type != "task":
        _fail("runtime.node_type", "only task nodes may be retried", node_id=node_id)
    runtime = state.nodes[node_id]
    if runtime.status not in {NodeStatus.FAILED, NodeStatus.BLOCKED}:
        _fail("runtime.invalid_retry", "only failed or blocked nodes may be retried", node_id=node_id)
    nodes = dict(state.nodes)
    edges = dict(state.edges)
    nodes[node_id] = replace(
        runtime,
        status=NodeStatus.PENDING,
        outcome="",
        claim_token_hash="",
        outputs=_string_map(),
        auxiliary_outputs=_aux_map(),
    )
    pending = [node_id]
    descendants: set[str] = set()
    while pending:
        current = pending.pop()
        for edge_id in plan.outgoing[current]:
            target = plan.edges[edge_id].target
            if target not in descendants:
                descendants.add(target)
                pending.append(target)
    for current in descendants:
        if nodes[current].status is NodeStatus.BLOCKED:
            nodes[current] = replace(nodes[current], status=NodeStatus.PENDING)
    for current in {node_id, *descendants}:
        for edge_id in plan.outgoing[current]:
            if edges[edge_id].status is EdgeStatus.FAILED:
                edges[edge_id] = EdgeRuntime(EdgeStatus.WAITING)
    return refresh_ready(plan, _replace_state(state, nodes=nodes, edges=edges))


def validator_retry_transition(plan: CompiledPlan, state: RunState, node_id: str) -> RunState:
    """Retry only an interrupted or failed validator execution."""
    _validate_binding(plan, state)
    if node_id not in plan.nodes:
        _fail("runtime.unknown_node", f"node does not exist: {node_id}", node_id=node_id)
    if plan.nodes[node_id].type != "validator":
        _fail("runtime.node_type", "only validator nodes may be retried", node_id=node_id)
    runtime = state.nodes[node_id]
    if (runtime.status not in {NodeStatus.FAILED, NodeStatus.BLOCKED}
            or runtime.attempt < 1 or not runtime.claim_token_hash):
        _fail("runtime.invalid_retry", "only failed or interrupted validators may be retried", node_id=node_id)
    nodes = dict(state.nodes)
    edges = dict(state.edges)
    nodes[node_id] = replace(runtime, status=NodeStatus.PENDING, outcome="", claim_token_hash="",
                             outputs=_string_map(), auxiliary_outputs=_aux_map())
    pending = [node_id]
    descendants: set[str] = set()
    while pending:
        current = pending.pop()
        for edge_id in plan.outgoing[current]:
            target = plan.edges[edge_id].target
            if target not in descendants:
                descendants.add(target)
                pending.append(target)
    revived = {node_id}
    for current in descendants:
        if (nodes[current].attempt == 0
                and nodes[current].status in {NodeStatus.BLOCKED, NodeStatus.SKIPPED}):
            nodes[current] = replace(nodes[current], status=NodeStatus.PENDING)
            revived.add(current)
    for current in revived:
        for edge_id in plan.outgoing[current]:
            if edges[edge_id].status in {EdgeStatus.FAILED, EdgeStatus.INACTIVE}:
                edges[edge_id] = EdgeRuntime(EdgeStatus.WAITING)
    return refresh_ready(plan, _replace_state(state, nodes=nodes, edges=edges))


def mark_descendants_stale(
    plan: CompiledPlan,
    state: RunState,
    changed_node_ids: Sequence[str],
) -> RunState:
    _validate_binding(plan, state)
    unknown = sorted(set(changed_node_ids) - set(plan.nodes))
    if unknown:
        _fail("runtime.unknown_node", f"node does not exist: {unknown[0]}", node_id=unknown[0])
    descendants: set[str] = set()
    pending = list(changed_node_ids)
    while pending:
        current = pending.pop()
        for edge_id in plan.outgoing[current]:
            target = plan.edges[edge_id].target
            target_node = plan.nodes[target]
            if (
                target_node.type == "join"
                and target_node.join_mode == "any_success"
                and state.nodes[target].winner_edge_id
                and state.nodes[target].winner_edge_id != edge_id
            ):
                continue
            if target not in descendants and target not in changed_node_ids:
                descendants.add(target)
                pending.append(target)
    nodes = dict(state.nodes)
    edges = dict(state.edges)
    artifacts = dict(state.artifacts)
    for node_id in descendants:
        nodes[node_id] = replace(
            nodes[node_id],
            status=NodeStatus.STALE,
            outcome="",
            selected_inputs=_string_map(),
            auxiliary_outputs=_aux_map(),
            claim_token_hash="",
        )
    for node_id in set(changed_node_ids) | descendants:
        _set_outgoing_status(plan, edges, node_id, EdgeStatus.WAITING)
    for artifact_id, artifact in state.artifacts.items():
        if artifact.producer_node_id in descendants and artifact.state != "stale":
            artifacts[artifact_id] = replace(artifact, state="stale")
    return _replace_state(state, nodes=nodes, edges=edges, artifacts=artifacts)


def _predicate_mentions_any_artifact(value: object, artifact_ids: set[str]) -> bool:
    if isinstance(value, Mapping):
        if value.get("op") == "artifact_state_is" and value.get("artifact") in artifact_ids:
            return True
        return any(_predicate_mentions_any_artifact(item, artifact_ids) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_predicate_mentions_any_artifact(item, artifact_ids) for item in value)
    return False


def register_external_artifacts_transition(
    plan: CompiledPlan,
    state: RunState,
    registrations: Mapping[str, ArtifactRuntime],
) -> RunState:
    """Register verified external inputs and deterministically invalidate old lineage.

    A newly registered artifact is just as capable of changing a condition as a
    replacement is.  The transition therefore derives its invalidation closure
    from both direct consumers and condition predicates that name every changed
    logical artifact ID.
    """
    _validate_binding(plan, state)
    if not isinstance(registrations, Mapping):
        _fail("runtime.invalid_artifact_registration", "external registrations must be a mapping")

    changed: dict[str, ArtifactRuntime] = {}
    for artifact_id, artifact in registrations.items():
        if (
            not isinstance(artifact_id, str)
            or not isinstance(artifact, ArtifactRuntime)
            or artifact.artifact_id != artifact_id
            or artifact_id not in plan.external_inputs
            or artifact.producer_node_id != "external"
            or artifact.producer_attempt != 0
            or artifact.state != "verified"
        ):
            _fail(
                "runtime.invalid_artifact_registration",
                "registration must name a declared, verified external input",
            )
        if state.artifacts.get(artifact_id) != artifact:
            changed[artifact_id] = artifact

    if not changed:
        return state

    affected: set[str] = set()
    changed_ids = set(changed)
    for artifact_id in changed_ids:
        earlier = state.artifacts.get(artifact_id)
        if earlier is not None and earlier.producer_node_id != "external":
            affected.add(earlier.producer_node_id)

    def reads_registry_input(node_id: str, artifact_ids: set[str]) -> bool:
        return any(
            artifact_id in plan.nodes[node_id].inputs
            and not _producer_edges_for_input(plan, node_id, artifact_id)
            for artifact_id in artifact_ids
        )

    for node_id, node in plan.nodes.items():
        if reads_registry_input(node_id, changed_ids):
            affected.add(node_id)
        if node.type == "condition" and any(
            _predicate_mentions_any_artifact(case, changed_ids)
            for case in node.condition_cases
        ):
            affected.add(node_id)

    pending = list(affected)
    while pending:
        current = pending.pop()
        stale_outputs = {
            artifact_id
            for artifact_id, artifact in state.artifacts.items()
            if artifact_id not in changed_ids
            and artifact.producer_node_id == current
            and artifact.state != "stale"
        }
        if stale_outputs:
            for target, node in plan.nodes.items():
                if target in affected:
                    continue
                if reads_registry_input(target, stale_outputs) or (
                    node.type == "condition"
                    and any(
                        _predicate_mentions_any_artifact(case, stale_outputs)
                        for case in node.condition_cases
                    )
                ):
                    affected.add(target)
                    pending.append(target)
        for edge_id in plan.outgoing[current]:
            target = plan.edges[edge_id].target
            target_node = plan.nodes[target]
            if (
                target_node.type == "join"
                and target_node.join_mode == "any_success"
                and state.nodes[target].winner_edge_id
                and state.nodes[target].winner_edge_id != edge_id
            ):
                continue
            if target not in affected:
                affected.add(target)
                pending.append(target)

    running = sorted(
        node_id for node_id in affected
        if state.nodes[node_id].status is NodeStatus.RUNNING
    )
    if running:
        _fail(
            "runtime.artifact_registration_running",
            f"external registration cannot invalidate running work: {running[0]}",
            node_id=running[0],
        )

    # Keep a skipped zero-attempt branch only when every inactive incoming
    # route is still controlled by an unaffected predecessor.
    unchanged_exclusions = {
        node_id
        for node_id in affected
        if state.nodes[node_id].status is NodeStatus.SKIPPED
        and state.nodes[node_id].attempt == 0
        and bool(plan.incoming[node_id])
        and all(
            state.edges[edge_id].status is EdgeStatus.INACTIVE
            and plan.edges[edge_id].source not in affected
            for edge_id in plan.incoming[node_id]
        )
    }

    nodes = dict(state.nodes)
    edges = dict(state.edges)
    artifacts = dict(state.artifacts)
    artifacts.update(changed)

    for node_id in affected:
        if node_id in unchanged_exclusions:
            continue
        runtime = state.nodes[node_id]
        node = plan.nodes[node_id]
        frozen_any_success = (
            node.type == "join"
            and node.join_mode == "any_success"
            and bool(runtime.winner_edge_id)
        )
        if runtime.status in {
            NodeStatus.SUCCEEDED,
            NodeStatus.FAILED,
            NodeStatus.BLOCKED,
            NodeStatus.STALE,
        } or (runtime.status is NodeStatus.SKIPPED and runtime.attempt > 0) or frozen_any_success:
            nodes[node_id] = replace(
                runtime,
                status=NodeStatus.STALE,
                outcome="",
                selected_inputs=_string_map(),
                auxiliary_outputs=_aux_map(),
                claim_token_hash="",
            )
        elif runtime.status is NodeStatus.SKIPPED:
            nodes[node_id] = replace(
                runtime,
                status=NodeStatus.PENDING,
                selected_inputs=_string_map(),
            )
        else:
            nodes[node_id] = replace(
                runtime,
                status=NodeStatus.PENDING,
                selected_inputs=_string_map(),
            )
        if node_id not in unchanged_exclusions:
            _set_outgoing_status(plan, edges, node_id, EdgeStatus.WAITING)

    # Outputs from invalidated producers are no longer current evidence.  Do
    # not stale unrelated external inputs: they share the same synthetic
    # producer identity but are independent registrations.
    for artifact_id, artifact in state.artifacts.items():
        if (
            artifact_id not in changed_ids
            and artifact.producer_node_id in affected
            and artifact.state != "stale"
        ):
            artifacts[artifact_id] = replace(artifact, state="stale")

    updated = _replace_state(
        state,
        nodes=nodes,
        edges=edges,
        artifacts=artifacts,
    )
    return refresh_ready(plan, updated)


def rerun_stale_transition(
    plan: CompiledPlan,
    state: RunState,
    node_id: str,
) -> tuple[RunState, tuple[str, ...]]:
    """Explicitly requeue one stale node and its stale, winner-safe dependents.

    Prior outputs and attempt numbers stay as history. Only the next claim starts
    another attempt; stale artifact receipts remain stale until that claim succeeds.
    """
    _validate_binding(plan, state)
    if node_id not in plan.nodes or state.nodes[node_id].status is not NodeStatus.STALE:
        _fail(
            "runtime.invalid_stale_rerun",
            "only a currently stale node may be explicitly rerun",
            node_id=node_id if node_id in plan.nodes else "",
        )

    affected: set[str] = set()
    pending = [node_id]

    def may_invalidate(candidate_id: str, edge_id: str | None) -> bool:
        candidate = plan.nodes[candidate_id]
        runtime = state.nodes[candidate_id]
        if candidate.type != "join" or candidate.join_mode != "any_success":
            return True
        winner = runtime.winner_edge_id
        return not winner or edge_id == winner

    while pending:
        current = pending.pop()
        if current in affected or state.nodes[current].status is not NodeStatus.STALE:
            continue
        runtime = state.nodes[current]
        node = plan.nodes[current]
        if runtime.status is NodeStatus.RUNNING:
            _fail(
                "runtime.stale_rerun_running",
                "stale rerun closure contains running work",
                node_id=current,
            )
        if node.type == "join" and node.join_mode == "any_success" and runtime.winner_edge_id:
            _fail(
                "runtime.stale_join_requires_new_run",
                "a frozen any-success winner cannot be rerun within this run",
                node_id=current,
            )
        if runtime.outcome or runtime.selected_inputs or runtime.auxiliary_outputs or runtime.claim_token_hash:
            _fail(
                "runtime.stale_rerun_requires_recovery",
                "stale live evidence must be quarantined before rerunning",
                node_id=current,
            )
        if any(
            state.edges[edge_id].status is not EdgeStatus.WAITING
            or state.edges[edge_id].selected_output_map
            for edge_id in plan.outgoing[current]
        ):
            _fail(
                "runtime.stale_rerun_requires_recovery",
                "stale outgoing routes must be cut before rerunning",
                node_id=current,
            )
        affected.add(current)

        for edge_id in plan.outgoing[current]:
            target = plan.edges[edge_id].target
            if (
                state.nodes[target].status is NodeStatus.STALE
                and may_invalidate(target, edge_id)
            ):
                pending.append(target)

        stale_outputs = {
            artifact_id
            for artifact_id, artifact in state.artifacts.items()
            if artifact.producer_node_id == current and artifact.state == "stale"
        }
        if stale_outputs:
            for candidate_id, candidate in plan.nodes.items():
                if candidate_id in affected or state.nodes[candidate_id].status is not NodeStatus.STALE:
                    continue
                consumes_output = bool(set(candidate.inputs) & stale_outputs)
                condition_reads_output = candidate.type == "condition" and any(
                    _predicate_mentions_any_artifact(case, stale_outputs)
                    for case in candidate.condition_cases
                )
                if not (consumes_output or condition_reads_output):
                    continue
                if not may_invalidate(candidate_id, None):
                    continue
                pending.append(candidate_id)

    nodes = dict(state.nodes)
    edges = dict(state.edges)
    for affected_id in affected:
        nodes[affected_id] = replace(
            nodes[affected_id],
            status=NodeStatus.PENDING,
            outcome="",
            selected_inputs=_string_map(),
            auxiliary_outputs=_aux_map(),
            claim_token_hash="",
        )
        _set_outgoing_status(plan, edges, affected_id, EdgeStatus.WAITING)

    refreshed = refresh_ready(
        plan,
        _replace_state(state, nodes=nodes, edges=edges),
    )
    return refreshed, tuple(sorted(affected))
