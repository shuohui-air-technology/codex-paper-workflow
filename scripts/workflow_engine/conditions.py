"""Restricted, side-effect-free workflow condition evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .schema import WorkflowError


_NODE_STATUSES = frozenset(
    {"pending", "ready", "running", "succeeded", "failed", "blocked", "skipped", "stale"}
)
_MISSING = object()


@dataclass(frozen=True)
class ConditionFacts:
    node_statuses: Mapping[str, str]
    node_outcomes: Mapping[str, str]
    decisions: Mapping[str, object]
    artifact_states: Mapping[str, str]
    project_booleans: Mapping[str, bool]


def _invalid(message: str) -> None:
    raise WorkflowError("condition.invalid_ast", message)


def _exact(expression: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(expression, Mapping) or set(expression) != fields:
        _invalid("condition operation has invalid fields")
    if not all(isinstance(key, str) for key in expression):
        _invalid("condition operation keys must be strings")
    return expression


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{label} must be a non-empty string")
    return value


def _decision_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    _invalid("decision value must be a finite JSON scalar")


def _json_scalar_equal(left: object, right: object) -> bool:
    if left is _MISSING:
        return False

    def kind(value: object) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            return "string"
        return "invalid"

    left_kind = kind(left)
    return left_kind != "invalid" and left_kind == kind(right) and left == right


def validate_predicate(
    expression: object,
    *,
    predecessor_ids: frozenset[str] | None = None,
    predecessor_outcomes: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    """Validate one serialized predicate without evaluating user-authored code."""
    if not isinstance(expression, Mapping) or not isinstance(expression.get("op"), str):
        _invalid("condition must be a serialized operation")
    operation = expression["op"]
    if operation in {"status_is", "outcome_is"}:
        item = _exact(expression, frozenset({"op", "node", "value"}))
        node_id = _name(item["node"], f"{operation} node")
        value = _name(item["value"], f"{operation} value")
        if predecessor_ids is not None and node_id not in predecessor_ids:
            raise WorkflowError(
                "condition.invalid_reference",
                f"condition node reference is not a direct predecessor: {node_id}",
            )
        if operation == "status_is" and value not in _NODE_STATUSES:
            _invalid(f"unsupported node status: {value}")
        if operation == "outcome_is" and predecessor_outcomes is not None:
            if value not in predecessor_outcomes.get(node_id, ()):
                raise WorkflowError(
                    "condition.invalid_outcome",
                    f"condition references an undeclared predecessor outcome: {node_id}.{value}",
                )
        return
    if operation == "decision_is":
        item = _exact(expression, frozenset({"op", "name", "value"}))
        _name(item["name"], "decision name")
        _decision_value(item["value"])
        return
    if operation == "artifact_state_is":
        item = _exact(expression, frozenset({"op", "artifact", "value"}))
        _name(item["artifact"], "artifact name")
        _name(item["value"], "artifact state")
        return
    if operation == "fact_is":
        item = _exact(expression, frozenset({"op", "name", "value"}))
        _name(item["name"], "project fact name")
        if not isinstance(item["value"], bool):
            _invalid("project fact value must be a boolean")
        return
    if operation in {"all", "any"}:
        item = _exact(expression, frozenset({"op", "args"}))
        arguments = item["args"]
        if not isinstance(arguments, (list, tuple)) or not arguments:
            _invalid(f"{operation} requires a non-empty argument list")
        for argument in arguments:
            validate_predicate(
                argument,
                predecessor_ids=predecessor_ids,
                predecessor_outcomes=predecessor_outcomes,
            )
        return
    if operation == "not":
        item = _exact(expression, frozenset({"op", "arg"}))
        validate_predicate(
            item["arg"],
            predecessor_ids=predecessor_ids,
            predecessor_outcomes=predecessor_outcomes,
        )
        return
    raise WorkflowError(
        "condition.unsupported_op", f"unsupported condition operation: {operation}"
    )


def evaluate_predicate(expression: object, facts: ConditionFacts) -> bool:
    validate_predicate(expression)
    assert isinstance(expression, Mapping)
    operation = expression["op"]
    if operation == "status_is":
        return facts.node_statuses.get(expression["node"]) == expression["value"]
    if operation == "outcome_is":
        return facts.node_outcomes.get(expression["node"]) == expression["value"]
    if operation == "decision_is":
        return _json_scalar_equal(
            facts.decisions.get(expression["name"], _MISSING), expression["value"]
        )
    if operation == "artifact_state_is":
        return facts.artifact_states.get(expression["artifact"]) == expression["value"]
    if operation == "fact_is":
        return facts.project_booleans.get(expression["name"]) is expression["value"]
    if operation == "all":
        return all(evaluate_predicate(item, facts) for item in expression["args"])
    if operation == "any":
        return any(evaluate_predicate(item, facts) for item in expression["args"])
    return not evaluate_predicate(expression["arg"], facts)


def evaluate_condition(cases: tuple[Mapping[str, object], ...], facts: ConditionFacts) -> str:
    """Return the first matching named outcome, otherwise the reserved default."""
    for case in cases:
        if not isinstance(case, Mapping) or set(case) != {"outcome", "when"}:
            _invalid("condition case must contain exactly outcome and when")
        outcome = _name(case["outcome"], "condition outcome")
        validate_predicate(case["when"])
        if evaluate_predicate(case["when"], facts):
            return outcome
    return "default"
