"""Strict, bounded codecs for custom node results and event-owned receipts.

Filesystem containment and declaration/claim agreement are checked by the
manager/store; these pure codecs never confer completion authority by themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Mapping


INVOCATION_SCHEMA = "node-invocation-v1"
RESULT_SCHEMA = "node-result-v1"
RECEIPT_SCHEMA = "stage-receipt-v2"
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_TEXT = 4000
MAX_ITEMS = 1000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,255}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_RESULT_FIELDS = frozenset({
    "schema_version", "run_id", "node_id", "attempt", "idempotency_token",
    "status", "outcome", "summary", "artifacts", "uncertainties",
})
_RECEIPT_FIELDS = frozenset({
    "schema_version", "workflow_id", "semantic_revision", "semantic_sha256",
    "run_id", "node_id", "node_type", "attempt", "claim_token_sha256",
    "resolved_identity", "input_artifacts", "output_artifacts", "status", "outcome",
    "started_at", "completed_at", "summary", "uncertainties", "error",
})


class ReceiptError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str) -> None:
    raise ReceiptError("receipt.invalid", message)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            _fail("JSON object keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def canonical_bytes(value: object) -> bytes:
    try:
        raw = json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ReceiptError("receipt.invalid", "invalid JSON value") from exc
    if len(raw) > MAX_ENVELOPE_BYTES:
        _fail("envelope exceeds 1 MiB")
    return raw


def _decode(value: object) -> dict[str, object]:
    if isinstance(value, (str, bytes)):
        try:
            raw = value.encode("utf-8") if isinstance(value, str) else value
            if len(raw) > MAX_ENVELOPE_BYTES:
                _fail("envelope exceeds 1 MiB")
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _fail("nonfinite JSON number"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ReceiptError("receipt.invalid", "malformed JSON envelope") from exc
    raw = canonical_bytes(value)
    normalized = json.loads(raw)
    if not isinstance(normalized, dict):
        _fail("envelope must be an object")
    return normalized


def _exact(value: object, keys: set[str] | frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail("object fields do not match the protocol")
    return value


def _text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT or (not empty and not value.strip()) or "\x00" in value:
        _fail("expected bounded text")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("invalid identifier")
    return value


def _positive(value: object) -> None:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        _fail("expected positive bounded integer")


def _hash(value: object) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("expected lowercase SHA-256")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        _fail("timestamp must be UTC with Z suffix")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError("receipt.invalid", "invalid timestamp") from exc


def _path(value: object) -> str:
    text = _text(value)
    parts = text.split("/")
    if PurePosixPath(text).is_absolute() or "\\" in text or ":" in text or any(part in {"", ".", ".."} for part in parts) or any(ord(char) < 32 for char in text):
        _fail("path must be a normalized project-relative path")
    return text


def _artifacts(value: object, *, inputs: bool = False, hashed: bool = True) -> None:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        _fail("artifacts must be a bounded list")
    keys = {"id", "path"} | ({"sha256"} if hashed else set()) | ({"source_id"} if inputs else set())
    ids = []
    for artifact in value:
        item = _exact(artifact, keys)
        ids.append(_identifier(item["id"]))
        _path(item["path"])
        if hashed:
            _hash(item["sha256"])
        if inputs:
            _identifier(item["source_id"])
    if len(set(ids)) != len(ids) or (hashed and ids != sorted(ids)):
        _fail("artifact IDs must be unique and receipt artifacts canonically ordered")


def _report(value: dict[str, object]) -> None:
    _text(value["summary"])
    uncertainties = value["uncertainties"]
    if not isinstance(uncertainties, list) or len(uncertainties) > MAX_ITEMS:
        _fail("uncertainties must be a bounded list")
    for item in uncertainties:
        _text(item)
    error = value.get("error")
    if error is not None:
        _exact(error, {"code", "message"})
        _identifier(error["code"])
        _text(error["message"])


def parse_result(value: object) -> dict[str, object]:
    result = _decode(value)
    if set(result) not in (_RESULT_FIELDS, _RESULT_FIELDS | {"error"}):
        _fail("result fields do not match node-result-v1")
    if result["schema_version"] != RESULT_SCHEMA:
        _fail("unsupported result schema")
    for key in ("run_id", "node_id"):
        _identifier(result[key])
    _positive(result["attempt"])
    _text(result["idempotency_token"])
    _text(result["outcome"], empty=True)
    _artifacts(result["artifacts"], hashed=False)
    _report(result)
    if result["status"] == "succeeded":
        if not result["outcome"] or result.get("error") is not None:
            _fail("successful result requires an outcome and no execution error")
    elif result["status"] == "failed":
        if result["outcome"] or result["artifacts"] or result.get("error") is None:
            _fail("failed result requires error and no output/outcome")
    else:
        _fail("result status must be succeeded or failed")
    return result


def canonical_result_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(parse_result(value))).hexdigest()


def _identity(identity: object, node_type: str) -> None:
    if node_type == "task":
        _exact(identity, {"kind", "catalog_id", "relative_path", "skill_sha256", "tree_sha256", "locked"})
        if identity["kind"] != "skill" or type(identity["locked"]) is not bool:
            _fail("invalid Skill identity")
        _identifier(identity["catalog_id"])
        _path(identity["relative_path"])
        _hash(identity["skill_sha256"])
        tree_hash = identity["tree_sha256"]
        if not isinstance(tree_hash, str) or not tree_hash.startswith("sha256:"):
            _fail("invalid tree hash")
        _hash(tree_hash[7:])
    elif node_type == "validator":
        _exact(identity, {"kind", "validator_id", "script", "sha256", "adapter", "input_schema", "outcomes"})
        if identity["kind"] != "validator":
            _fail("invalid validator identity")
        for key in ("validator_id", "adapter", "input_schema"):
            _identifier(identity[key])
        _path(identity["script"])
        _hash(identity["sha256"])
        if identity["outcomes"] != ["pass", "fail", "blocked"]:
            _fail("invalid validator outcomes")
    else:
        _fail("receipt node type must be task or validator")


def validate_stage_receipt(value: object) -> dict[str, object]:
    receipt = _exact(_decode(value), _RECEIPT_FIELDS)
    if receipt["schema_version"] != RECEIPT_SCHEMA:
        _fail("unsupported receipt schema")
    for key in ("workflow_id", "run_id", "node_id"):
        _identifier(receipt[key])
    for key in ("attempt", "semantic_revision"):
        _positive(receipt[key])
    for key in ("semantic_sha256", "claim_token_sha256"):
        _hash(receipt[key])
    identity = receipt["resolved_identity"]
    _identity(identity, receipt["node_type"])
    _artifacts(receipt["input_artifacts"], inputs=True)
    _artifacts(receipt["output_artifacts"])
    _report(receipt)
    _text(receipt["status"])
    _text(receipt["outcome"], empty=True)
    if receipt["status"] == "succeeded":
        if not receipt["outcome"] or receipt["error"] is not None:
            _fail("successful receipt needs outcome and no error")
        if receipt["node_type"] == "validator" and receipt["outcome"] not in identity["outcomes"]:
            _fail("validator outcome is undeclared")
    elif receipt["status"] in {"failed", "skipped"}:
        if receipt["outcome"] or receipt["output_artifacts"] or receipt["error"] is None:
            _fail("execution failure needs error and no outputs/outcome")
    else:
        _fail("invalid receipt terminal status")
    times = [_timestamp(receipt[key]) for key in ("started_at", "completed_at")]
    if times[1] < times[0]:
        _fail("completion precedes claim")
    return receipt


def validate_invocation(value: object) -> dict[str, object]:
    invocation = _exact(_decode(value), {
        "schema_version", "run_id", "semantic_sha256", "node_id", "attempt",
        "claim_token_sha256", "resolved_identity", "input_artifacts", "expected_outputs",
        "outcomes", "started_at", "idempotency_token", "allowed_project_root",
    })
    if invocation["schema_version"] != INVOCATION_SCHEMA:
        _fail("unsupported invocation schema")
    for key in ("run_id", "node_id"):
        _identifier(invocation[key])
    _positive(invocation["attempt"])
    for key in ("semantic_sha256", "claim_token_sha256"):
        _hash(invocation[key])
    token = _text(invocation["idempotency_token"])
    if hashlib.sha256(token.encode("utf-8")).hexdigest() != invocation["claim_token_sha256"]:
        _fail("invocation token differs from claim hash")
    _identity(invocation["resolved_identity"], "task")
    _artifacts(invocation["input_artifacts"], inputs=True)
    for key in ("expected_outputs", "outcomes"):
        values = invocation[key]
        if not isinstance(values, list) or len(values) > MAX_ITEMS:
            _fail("invocation declarations must be bounded lists")
        for item in values:
            _identifier(item)
        if len(set(values)) != len(values):
            _fail("invocation declarations must be unique")
    if "succeeded" not in invocation["outcomes"]:
        _fail("task invocation must declare succeeded outcome")
    _timestamp(invocation["started_at"])
    if not Path(_text(invocation["allowed_project_root"])).is_absolute():
        _fail("invocation project root must be absolute")
    return invocation


def build_invocation(claim: Mapping[str, object], token: str, project_root: Path) -> dict[str, object]:
    return validate_invocation({"schema_version": INVOCATION_SCHEMA, **claim,
                                "idempotency_token": token, "allowed_project_root": str(project_root)})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolved_identity(node) -> dict[str, object]:
    if node.type == "task" and node.skill is not None:
        skill = node.skill
        return {"kind": "skill", "catalog_id": skill.catalog_id, "relative_path": skill.relative_path,
                "skill_sha256": skill.skill_sha256, "tree_sha256": skill.tree_sha256, "locked": skill.locked}
    if node.type == "validator" and node.validator is not None:
        validator = node.validator
        root = Path(__file__).resolve().parents[2]
        try:
            script = validator.script.relative_to(root).as_posix()
        except ValueError as exc:
            raise ReceiptError("receipt.identity_invalid", "validator is outside the installed repository") from exc
        return {"kind": "validator", "validator_id": validator.validator_id, "script": script,
                "sha256": validator.sha256, "adapter": validator.adapter,
                "input_schema": validator.input_schema, "outcomes": list(validator.outcomes)}
    raise ReceiptError("receipt.identity_invalid", "node lacks a resolved executable identity")


def input_evidence(plan, state, node_id, witnesses) -> list[dict[str, str]]:
    """Select historical edge lineage, never a later same-ID registry pointer."""
    from .scheduler import _producer_edges_for_input
    result = []
    for artifact_id in sorted(plan.nodes[node_id].inputs):
        edges = _producer_edges_for_input(plan, node_id, artifact_id)
        if edges:
            artifact = witnesses.get(edges[0], {}).get(artifact_id) if len(edges) == 1 else None
        else:
            artifact = state.artifacts.get(artifact_id)
        path = state.nodes[node_id].selected_inputs.get(artifact_id)
        if artifact is None or artifact.state != "verified" or artifact.path != path:
            raise ReceiptError("receipt.input_unverified", f"input lacks selected verified lineage: {artifact_id}")
        result.append({"id": artifact_id, "source_id": artifact.artifact_id, "path": path, "sha256": artifact.sha256})
    return result


def build_claim_evidence(plan, state, node_id, witnesses, started_at) -> dict[str, object]:
    _timestamp(started_at)
    runtime = state.nodes[node_id]
    return {
        "run_id": state.run_id, "semantic_sha256": plan.semantic_sha256,
        "node_id": node_id, "attempt": runtime.attempt,
        "claim_token_sha256": runtime.claim_token_hash,
        "resolved_identity": resolved_identity(plan.nodes[node_id]),
        "input_artifacts": input_evidence(plan, state, node_id, witnesses),
        "expected_outputs": list(plan.nodes[node_id].outputs),
        "outcomes": list(plan.nodes[node_id].outcomes), "started_at": started_at,
    }


def build_stage_receipt(plan, state, node_id, claim, *, summary, uncertainties,
                        completed_at, error=None) -> dict[str, object]:
    runtime = state.nodes[node_id]
    outputs = [
        {"id": artifact_id, "path": state.artifacts[artifact_id].path, "sha256": state.artifacts[artifact_id].sha256}
        for artifact_id in sorted(plan.nodes[node_id].outputs)
    ] if runtime.status.value == "succeeded" else []
    return validate_stage_receipt({
        "schema_version": RECEIPT_SCHEMA, "workflow_id": plan.workflow_id,
        "semantic_revision": plan.semantic_revision, "semantic_sha256": plan.semantic_sha256,
        "run_id": state.run_id, "node_id": node_id, "node_type": plan.nodes[node_id].type,
        "attempt": runtime.attempt, "claim_token_sha256": claim["claim_token_sha256"],
        "resolved_identity": claim["resolved_identity"], "input_artifacts": claim["input_artifacts"],
        "output_artifacts": outputs, "status": runtime.status.value, "outcome": runtime.outcome,
        "started_at": claim["started_at"], "completed_at": completed_at,
        "summary": summary, "uncertainties": uncertainties, "error": error,
    })
