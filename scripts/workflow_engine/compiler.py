"""Activation-boundary validation and deterministic workflow DAG compilation."""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from .catalog import CatalogResult, SkillIdentity, ValidatorIdentity
from .conditions import validate_predicate
from .risk import control_risk_warnings
from .schema import (
    EdgeSpec,
    NodeSpec,
    WorkflowDocument,
    WorkflowError,
    WorkflowIssue,
    document_payload,
    document_sha256,
)


_PROJECTION_SCHEMA = "paper-workflow-studio-projection-v1"
_PROTECTED_SCOPES = frozenset({"canonical_manuscript"})


@dataclass(frozen=True)
class CompiledNode:
    id: str
    type: str
    entry: bool
    skill: SkillIdentity | None
    validator: ValidatorIdentity | None
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    outcomes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    failure_policy: str
    condition_cases: tuple[Mapping[str, object], ...]
    join_mode: str


@dataclass(frozen=True)
class CompiledEdge:
    id: str
    source: str
    target: str
    trigger: str
    output_map: Mapping[str, str]


@dataclass(frozen=True)
class CompiledPlan:
    workflow_id: str
    semantic_revision: int
    document_sha256: str
    semantic_sha256: str
    nodes: Mapping[str, CompiledNode]
    edges: Mapping[str, CompiledEdge]
    incoming: Mapping[str, tuple[str, ...]]
    outgoing: Mapping[str, tuple[str, ...]]
    topological_order: tuple[str, ...]
    max_parallelism: int


@dataclass(frozen=True)
class CompileResult:
    plan: CompiledPlan | None
    errors: tuple[WorkflowIssue, ...]
    warnings: tuple[WorkflowIssue, ...]


def _issue(
    code: str,
    message: str,
    *,
    node_id: str = "",
    edge_id: str = "",
) -> WorkflowIssue:
    return WorkflowIssue("error", code, message, node_id=node_id, edge_id=edge_id)


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _projection_nodes(projection: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    value = projection.get("nodes")
    if not isinstance(value, list):
        return {}
    return {
        item["id"]: item
        for item in value
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _validate_projection(
    document: WorkflowDocument,
    projection: Mapping[str, object],
    nodes: Sequence[NodeSpec],
) -> list[WorkflowIssue]:
    errors: list[WorkflowIssue] = []
    if projection.get("schema_version") != _PROJECTION_SCHEMA:
        return [_issue("projection.invalid", "official projection schema is invalid")]
    projected_nodes = _projection_nodes(projection)
    derived = document.derived_from
    if derived is not None:
        if derived.get("projection_id") != projection.get("projection_id"):
            errors.append(_issue("projection.id_mismatch", "derived projection ID does not match"))
        if derived.get("projection_sha256") != _canonical_sha256(projection):
            errors.append(_issue("projection.hash_mismatch", "derived projection hash does not match"))
    origins: dict[str, str] = {}
    for node in nodes:
        origin = node.origin_projection_node_id
        if origin is None:
            continue
        if derived is None:
            errors.append(
                _issue(
                    "projection.provenance_missing",
                    "projection node provenance requires derived_from",
                    node_id=node.id,
                )
            )
        if origin not in projected_nodes:
            errors.append(
                _issue(
                    "projection.unknown_node",
                    f"projection node provenance does not resolve: {origin}",
                    node_id=node.id,
                )
            )
        if origin in origins:
            errors.append(
                _issue(
                    "projection.duplicate_origin",
                    f"projection node provenance is used more than once: {origin}",
                    node_id=node.id,
                )
            )
        else:
            origins[origin] = node.id
    return errors


def _topological_order(
    nodes: Mapping[str, NodeSpec],
    edges: Sequence[EdgeSpec],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    incoming_lists = {node_id: [] for node_id in sorted(nodes)}
    outgoing_lists = {node_id: [] for node_id in sorted(nodes)}
    indegree = {node_id: 0 for node_id in sorted(nodes)}
    for edge in sorted(edges, key=lambda item: item.id):
        incoming_lists[edge.target].append(edge.id)
        outgoing_lists[edge.source].append(edge.id)
        indegree[edge.target] += 1
    ready = [node_id for node_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    edge_by_id = {edge.id: edge for edge in edges}
    order: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        order.append(node_id)
        targets = sorted(
            (edge_by_id[edge_id].target, edge_id) for edge_id in outgoing_lists[node_id]
        )
        for target, _edge_id in targets:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    incoming = {key: tuple(sorted(value)) for key, value in incoming_lists.items()}
    outgoing = {key: tuple(sorted(value)) for key, value in outgoing_lists.items()}
    return tuple(order), incoming, outgoing


def _reachable(start: Sequence[str], adjacency: Mapping[str, Sequence[str]]) -> set[str]:
    seen = set(start)
    pending = list(start)
    while pending:
        node_id = pending.pop()
        for other in adjacency.get(node_id, ()):
            if other not in seen:
                seen.add(other)
                pending.append(other)
    return seen


def _identity_payload(
    compiled_nodes: Mapping[str, CompiledNode],
) -> dict[str, object]:
    skills: list[dict[str, object]] = []
    validators: list[dict[str, object]] = []
    for node_id, node in sorted(compiled_nodes.items()):
        if node.skill is not None:
            skills.append(
                {
                    "node_id": node_id,
                    "catalog_id": node.skill.catalog_id,
                    "relative_path": node.skill.relative_path,
                    "skill_sha256": node.skill.skill_sha256,
                    "tree_sha256": node.skill.tree_sha256,
                    "locked": node.skill.locked,
                }
            )
        if node.validator is not None:
            validators.append(
                {
                    "node_id": node_id,
                    "validator_id": node.validator.validator_id,
                    "adapter": node.validator.adapter,
                    "sha256": node.validator.sha256,
                    "input_schema": node.validator.input_schema,
                    "outcomes": list(node.validator.outcomes),
                }
            )
    return {"skills": skills, "validators": validators}


def _semantic_hash(
    document: WorkflowDocument,
    compiled_nodes: Mapping[str, CompiledNode],
    compiled_edges: Mapping[str, CompiledEdge],
    incoming: Mapping[str, tuple[str, ...]],
    outgoing: Mapping[str, tuple[str, ...]],
    order: tuple[str, ...],
) -> str:
    topology = {
        "nodes": sorted(compiled_nodes),
        "edges": [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "trigger": edge.trigger,
                "output_map": dict(sorted(edge.output_map.items())),
            }
            for edge in sorted(compiled_edges.values(), key=lambda item: item.id)
        ],
        "incoming": {key: list(incoming[key]) for key in sorted(incoming)},
        "outgoing": {key: list(outgoing[key]) for key in sorted(outgoing)},
        "topological_order": list(order),
    }
    return _canonical_sha256(
        {
            "document": document_payload(document),
            "identities": _identity_payload(compiled_nodes),
            "compiled_topology": topology,
        }
    )


def compile_workflow(
    document: WorkflowDocument,
    catalog: CatalogResult,
    validators: Mapping[str, ValidatorIdentity],
    projection: Mapping[str, object],
) -> CompileResult:
    """Validate an immutable document and return a canonical plan or blocking issues."""
    errors = list(catalog.errors)
    warnings = list(catalog.warnings)

    enabled = {node.id: node for node in document.nodes if node.enabled}
    disabled_ids = {node.id for node in document.nodes if not node.enabled}
    edges = tuple(
        edge
        for edge in document.edges
        if edge.source not in disabled_ids and edge.target not in disabled_ids
    )
    errors.extend(_validate_projection(document, projection, document.nodes))
    if not enabled:
        errors.append(_issue("graph.no_enabled_nodes", "workflow has no enabled nodes"))

    valid_edges: list[EdgeSpec] = []
    for edge in sorted(edges, key=lambda item: item.id):
        source = enabled.get(edge.source)
        target = enabled.get(edge.target)
        if source is None:
            errors.append(
                _issue(
                    "graph.unknown_source",
                    f"edge source does not resolve: {edge.source}",
                    edge_id=edge.id,
                )
            )
        if target is None:
            errors.append(
                _issue(
                    "graph.unknown_target",
                    f"edge target does not resolve: {edge.target}",
                    edge_id=edge.id,
                )
            )
        if source is None or target is None:
            continue
        valid_edges.append(edge)
        legal_triggers = (
            frozenset({"succeeded"})
            if source.type in {"task", "join"}
            else frozenset(source.outcomes)
        )
        if edge.trigger not in legal_triggers:
            errors.append(
                _issue(
                    "graph.invalid_trigger",
                    f"edge trigger is not declared by source node: {edge.trigger}",
                    edge_id=edge.id,
                )
            )
        target_artifacts = set(target.inputs)
        if target.type == "join":
            target_artifacts.update(target.outputs)
        for source_output, target_artifact in edge.output_map.items():
            if source_output not in source.outputs:
                errors.append(
                    _issue(
                        "artifact.output_map_source",
                        f"output map source is not declared: {source_output}",
                        edge_id=edge.id,
                    )
                )
            if target_artifact not in target_artifacts:
                errors.append(
                    _issue(
                        "artifact.output_map_target",
                        f"output map target is not declared: {target_artifact}",
                        edge_id=edge.id,
                    )
                )
            if source_output in _PROTECTED_SCOPES and source_output != target_artifact:
                errors.append(
                    _issue(
                        "artifact.protected_scope_renamed",
                        f"protected artifact cannot be renamed: {source_output}",
                        edge_id=edge.id,
                    )
                )

    edge_tuple = tuple(valid_edges)
    order, incoming, outgoing = _topological_order(enabled, edge_tuple)
    if len(order) != len(enabled):
        errors.append(_issue("graph.cycle", "enabled workflow graph contains a cycle"))

    edge_by_id = {edge.id: edge for edge in edge_tuple}
    incoming_sources = {
        node_id: tuple(edge_by_id[edge_id].source for edge_id in incoming[node_id])
        for node_id in enabled
    }
    outgoing_targets = {
        node_id: tuple(edge_by_id[edge_id].target for edge_id in outgoing[node_id])
        for node_id in enabled
    }
    entries = tuple(sorted(node.id for node in enabled.values() if node.entry))
    terminals = tuple(sorted(node_id for node_id in enabled if not outgoing[node_id]))
    for node_id, node in sorted(enabled.items()):
        if not incoming[node_id] and not node.entry:
            errors.append(
                _issue(
                    "graph.entry_required",
                    "zero-incoming enabled node must be an explicit entry",
                    node_id=node_id,
                )
            )
    reachable_from_entries = _reachable(entries, outgoing_targets)
    reaches_terminal = _reachable(terminals, incoming_sources)
    for node_id in sorted(enabled):
        if node_id not in reachable_from_entries:
            errors.append(
                _issue(
                    "graph.unreachable_from_entry",
                    "enabled node is not reachable from an entry",
                    node_id=node_id,
                )
            )
        if node_id not in reaches_terminal:
            errors.append(
                _issue(
                    "graph.no_terminal_path",
                    "enabled node cannot reach a terminal",
                    node_id=node_id,
                )
            )

    for node_id, node in sorted(enabled.items()):
        if node.type == "task":
            if node.skill_ref is None:
                errors.append(
                    _issue(
                        "catalog.skill_required",
                        "task node requires a Skill binding",
                        node_id=node_id,
                    )
                )
            elif node.skill_ref not in catalog.skills:
                errors.append(
                    _issue(
                        "catalog.skill_not_found",
                        f"task Skill identity does not resolve: {node.skill_ref}",
                        node_id=node_id,
                    )
                )
            elif catalog.skills[node.skill_ref].catalog_id != node.skill_ref:
                errors.append(
                    _issue(
                        "catalog.skill_identity_mismatch",
                        "resolved Skill identity does not match its catalog key",
                        node_id=node_id,
                    )
                )
        elif node.type == "validator":
            if node.validator_ref is None:
                errors.append(
                    _issue(
                        "validator.identity_required",
                        "validator node requires a registered identity",
                        node_id=node_id,
                    )
                )
            elif node.validator_ref not in validators:
                errors.append(
                    _issue(
                        "validator.identity_not_found",
                        f"validator identity does not resolve: {node.validator_ref}",
                        node_id=node_id,
                    )
                )
            elif validators[node.validator_ref].validator_id != node.validator_ref:
                errors.append(
                    _issue(
                        "validator.identity_mismatch",
                        "resolved validator identity does not match its registry key",
                        node_id=node_id,
                    )
                )
            elif set(validators[node.validator_ref].outcomes) != set(node.outcomes):
                errors.append(
                    _issue(
                        "validator.outcomes_mismatch",
                        "validator node outcomes do not match its fixed identity",
                        node_id=node_id,
                    )
                )

        if node.type == "condition":
            default_edges = [
                edge_id
                for edge_id in outgoing[node_id]
                if edge_by_id[edge_id].trigger == "default"
            ]
            if len(default_edges) != 1:
                errors.append(
                    _issue(
                        "condition.default_edge",
                        "condition node requires exactly one default edge",
                        node_id=node_id,
                    )
                )
            predecessors = frozenset(incoming_sources[node_id])
            predecessor_outcomes = {
                predecessor: enabled[predecessor].outcomes for predecessor in predecessors
            }
            for case in node.condition_cases:
                try:
                    validate_predicate(
                        case["when"],
                        predecessor_ids=predecessors,
                        predecessor_outcomes=predecessor_outcomes,
                    )
                except WorkflowError as exc:
                    errors.append(
                        _issue(exc.code, str(exc), node_id=node_id)
                    )

        if node.type == "join" and node.join_mode == "any_success":
            if incoming[node_id] and any(
                not edge_by_id[edge_id].output_map for edge_id in incoming[node_id]
            ):
                errors.append(
                    _issue(
                        "join.output_map_required",
                        "any_success join requires an output map on every incoming edge",
                        node_id=node_id,
                    )
                )

        for artifact in node.inputs:
            if artifact in document.external_inputs:
                continue
            produced = False
            for edge_id in incoming[node_id]:
                edge = edge_by_id[edge_id]
                source = enabled[edge.source]
                for output in source.outputs:
                    if edge.output_map.get(output, output) == artifact:
                        produced = True
                        break
                if produced:
                    break
            if not produced:
                errors.append(
                    _issue(
                        "artifact.input_unbound",
                        f"node input has no incoming or external producer: {artifact}",
                        node_id=node_id,
                    )
                )

    reachability = {
        node_id: _reachable((node_id,), outgoing_targets) - {node_id}
        for node_id in enabled
    }
    node_ids = sorted(enabled)
    for index, first_id in enumerate(node_ids):
        for second_id in node_ids[index + 1 :]:
            if second_id in reachability[first_id] or first_id in reachability[second_id]:
                continue
            overlap = sorted(
                set(enabled[first_id].write_scopes) & set(enabled[second_id].write_scopes)
            )
            if overlap:
                errors.append(
                    _issue(
                        "graph.parallel_write_conflict",
                        f"unordered nodes share write scope {overlap[0]}: {first_id}, {second_id}",
                        node_id=first_id,
                    )
                )

    warnings.extend(
        control_risk_warnings(tuple(enabled.values()), edge_tuple, validators, projection)
    )
    if errors:
        return CompileResult(None, tuple(errors), tuple(warnings))

    compiled_nodes = {
        node_id: CompiledNode(
            id=node.id,
            type=node.type,
            entry=node.entry,
            skill=catalog.skills.get(node.skill_ref) if node.skill_ref is not None else None,
            validator=validators.get(node.validator_ref) if node.validator_ref is not None else None,
            inputs=node.inputs,
            outputs=node.outputs,
            outcomes=node.outcomes,
            write_scopes=node.write_scopes,
            failure_policy=node.failure_policy,
            condition_cases=node.condition_cases,
            join_mode=node.join_mode,
        )
        for node_id, node in sorted(enabled.items())
    }
    compiled_edges = {
        edge.id: CompiledEdge(
            id=edge.id,
            source=edge.source,
            target=edge.target,
            trigger=edge.trigger,
            output_map=MappingProxyType(dict(sorted(edge.output_map.items()))),
        )
        for edge in sorted(edge_tuple, key=lambda item: item.id)
    }
    semantic_hash = _semantic_hash(
        document, compiled_nodes, compiled_edges, incoming, outgoing, order
    )
    plan = CompiledPlan(
        workflow_id=document.workflow_id,
        semantic_revision=document.semantic_revision,
        document_sha256=document_sha256(document),
        semantic_sha256=semantic_hash,
        nodes=MappingProxyType(compiled_nodes),
        edges=MappingProxyType(compiled_edges),
        incoming=MappingProxyType(incoming),
        outgoing=MappingProxyType(outgoing),
        topological_order=order,
        max_parallelism=document.max_parallelism,
    )
    return CompileResult(plan, (), tuple(warnings))
