"""Conservative server-derived control coverage warnings."""

from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence

from .catalog import ValidatorIdentity
from .schema import EdgeSpec, NodeSpec, WorkflowIssue


_PROTECTED_SCOPES = frozenset({"canonical_manuscript"})


def _warning(code: str, message: str, *, node_id: str = "") -> WorkflowIssue:
    return WorkflowIssue("warning", code, message, node_id=node_id)


def _binding_compatible(node: NodeSpec, projected: Mapping[str, object]) -> bool:
    kind = projected.get("projection_kind")
    if kind == "gate":
        suggestions = projected.get("suggested_validator_ids")
        return (
            node.type == "validator"
            and isinstance(suggestions, list)
            and node.validator_ref in suggestions
        )
    suggestions = projected.get("suggested_skill_ids")
    return (
        node.type == "task"
        and isinstance(suggestions, list)
        and node.skill_ref in suggestions
    )


def _shape_compatible(node: NodeSpec, projected: Mapping[str, object]) -> bool:
    outputs = tuple(projected.get("outputs", ()))
    projected_scopes = tuple(projected.get("write_scopes", ()))
    expected_scopes = tuple(
        dict.fromkeys(
            outputs
            + tuple(scope for scope in projected_scopes if scope in _PROTECTED_SCOPES)
        )
    )
    return (
        tuple(projected.get("inputs", ())) == node.inputs
        and outputs == node.outputs
        and expected_scopes == node.write_scopes
    )


def _projected_node_type(projected: Mapping[str, object]) -> str | None:
    kind = projected.get("projection_kind")
    if kind == "gate":
        return "validator"
    if kind in {"task", "orchestrator", "delivery"}:
        return "task"
    return None


def _safe_trigger(projected: Mapping[str, object]) -> str | None:
    return "pass" if projected.get("projection_kind") == "gate" else (
        "succeeded" if _projected_node_type(projected) == "task" else None
    )


def _mapping_semantics(
    outputs: Sequence[object],
    inputs: Sequence[object],
    output_map: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    target_inputs = {item for item in inputs if isinstance(item, str)}
    transfers = []
    for source_output in outputs:
        if not isinstance(source_output, str):
            continue
        target_input = output_map.get(source_output, source_output)
        if target_input in target_inputs:
            transfers.append((source_output, target_input))
    return tuple(sorted(transfers))


def _adjacency_compatible(
    node: NodeSpec,
    origins: Mapping[str, NodeSpec],
    edges: Sequence[EdgeSpec],
    projected_nodes: Mapping[str, Mapping[str, object]],
    projected_edges: Sequence[Mapping[str, object]],
) -> bool:
    actual = [edge for edge in edges if edge.source == node.id or edge.target == node.id]
    reverse_origins = {custom.id: origin for origin, custom in origins.items()}
    actual_signatures: list[tuple[object, ...]] = []
    for edge in actual:
        source_origin = reverse_origins.get(edge.source)
        target_origin = reverse_origins.get(edge.target)
        projected_source = projected_nodes.get(source_origin or "")
        projected_target = projected_nodes.get(target_origin or "")
        source = origins.get(source_origin or "")
        target = origins.get(target_origin or "")
        if (
            source_origin is None
            or target_origin is None
            or projected_source is None
            or projected_target is None
            or source is None
            or target is None
            or source.type != _projected_node_type(projected_source)
            or target.type != _projected_node_type(projected_target)
            or not _shape_compatible(source, projected_source)
            or not _shape_compatible(target, projected_target)
        ):
            return False
        actual_signatures.append(
            (
                source_origin,
                target_origin,
                edge.trigger,
                _mapping_semantics(source.outputs, target.inputs, edge.output_map),
            )
        )

    expected_signatures: list[tuple[object, ...]] = []
    for projected_edge in projected_edges:
        source_origin = projected_edge.get("source")
        target_origin = projected_edge.get("target")
        if node.origin_projection_node_id not in {source_origin, target_origin}:
            continue
        projected_source = projected_nodes.get(str(source_origin))
        projected_target = projected_nodes.get(str(target_origin))
        if projected_source is None or projected_target is None:
            return False
        trigger = _safe_trigger(projected_source)
        if trigger is None:
            return False
        expected_signatures.append(
            (
                source_origin,
                target_origin,
                trigger,
                _mapping_semantics(
                    projected_source.get("outputs", ()),
                    projected_target.get("inputs", ()),
                    {},
                ),
            )
        )
    return Counter(actual_signatures) == Counter(expected_signatures)


def control_risk_warnings(
    nodes: Sequence[NodeSpec],
    edges: Sequence[EdgeSpec],
    validators: Mapping[str, ValidatorIdentity],
    projection: Mapping[str, object],
) -> tuple[WorkflowIssue, ...]:
    """Compare fixed projection controls with verified custom coverage."""
    projected_nodes_value = projection.get("nodes")
    projected_edges_value = projection.get("edges")
    tags_value = projection.get("control_tags")
    if not isinstance(projected_nodes_value, list) or not isinstance(tags_value, list):
        return ()
    projected_nodes = {
        item["id"]: item
        for item in projected_nodes_value
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    projected_edges = tuple(
        item
        for item in projected_edges_value or ()
        if isinstance(item, Mapping)
        and isinstance(item.get("source"), str)
        and isinstance(item.get("target"), str)
    )
    origins = {
        node.origin_projection_node_id: node
        for node in nodes
        if node.origin_projection_node_id is not None
    }
    fixed_coverage = {
        tag
        for node in nodes
        if node.type == "validator" and node.validator_ref in validators
        for tag in validators[node.validator_ref].control_tags
    }
    compatible_origins = {
        origin
        for origin, node in origins.items()
        if origin in projected_nodes
        and _binding_compatible(node, projected_nodes[origin])
        and _shape_compatible(node, projected_nodes[origin])
        and _adjacency_compatible(
            node, origins, edges, projected_nodes, projected_edges
        )
    }
    warnings: list[WorkflowIssue] = []
    for tag in sorted(item for item in tags_value if isinstance(item, str)):
        if tag in fixed_coverage:
            continue
        providers = {
            origin
            for origin, projected in projected_nodes.items()
            if tag in projected.get("control_tags", ())
        }
        if providers & compatible_origins:
            continue
        claimed = providers & origins.keys()
        if claimed:
            node_id = origins[sorted(claimed)[0]].id
            warnings.append(
                _warning(
                    f"risk.control_replaced.{tag}",
                    f"projected {tag} control was materially changed",
                    node_id=node_id,
                )
            )
        else:
            warnings.append(
                _warning(
                    f"risk.control_removed.{tag}",
                    f"projected {tag} control is not covered by the enabled custom graph",
                )
            )
    return tuple(warnings)
