#!/usr/bin/env python3
"""Fail-closed validator for a bounded autoresearch handoff contract.

The validator accepts JSON or a small flat YAML subset without PyYAML. It
validates control evidence, scope containment, and a confirmed stage receipt;
contract prose cannot grant permissions by adding unknown keys.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "objective",
    "metric",
    "direction",
    "baseline",
    "budget",
    "max_runs",
    "max_wall_time",
    "stop_conditions",
    "data_code_scope",
    "network_scope",
    "write_and_commit_policy",
    "report_destination",
    "validation_status",
    "approved_by",
    "user_confirmation",
    "stage_receipt",
    "stage_receipt_sha256",
    "validity_status",
)
ALLOWED_NETWORK_SCOPE = {"none", "local-only"}
ALLOWED_WRITE_POLICY = {"no_auto_commit", "external_user_only", "local-stage-receipt-only"}
ALLOWED_DIRECTIONS = {"maximize", "minimize"}
MAX_RUNS = 1000
MAX_BUDGET = 1_000_000_000.0
MAX_WALL_TIME = 7 * 24 * 60 * 60
MAX_RECEIPT_HORIZON = timedelta(days=90)
UNBOUNDED_TERMS = ("never", "indefinite", "forever", "unlimited", "without limit")


class ContractError(ValueError):
    pass


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - CLI misuse
        print(json.dumps({"status": "blocked", "errors": [message]}, ensure_ascii=False, indent=2))
        raise SystemExit(1)


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value in {"null", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith(("[", "{", '"', "'")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError):
                pass
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value.strip('"\'')


def load_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"contract does not exist or is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"contract is not readable UTF-8 text: {path}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = {}
        lines = text.splitlines()
        line_number = 0
        while line_number < len(lines):
            line = lines[line_number]
            line_number += 1
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped == "---":
                continue
            if line[:1].isspace() or ":" not in line:
                raise ContractError(f"unsupported YAML structure at line {line_number}")
            key, raw = line.split(":", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ContractError(f"invalid contract field at line {line_number}: {key}")
            if raw.strip():
                value[key] = _scalar(raw)
                continue
            items: list[Any] = []
            while line_number < len(lines) and lines[line_number].lstrip().startswith("-"):
                item_line = lines[line_number]
                line_number += 1
                items.append(_scalar(item_line.lstrip()[1:]))
            value[key] = items
    if not isinstance(value, dict):
        raise ContractError("contract must contain an object/map")
    return value


def _nonempty(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, dict)) and not value):
        raise ContractError(f"missing or empty required field: {key}")


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _project_root(contract_path: Path) -> Path:
    return contract_path.parent.parent if contract_path.parent.name == ".research" else contract_path.parent


def _scope_hash(data: dict[str, Any]) -> str:
    scope = {
        key: data[key]
        for key in (
            "objective",
            "metric",
            "direction",
            "baseline",
            "budget",
            "max_runs",
            "max_wall_time",
            "stop_conditions",
            "data_code_scope",
            "network_scope",
            "write_and_commit_policy",
            "report_destination",
            "validation_status",
            "approved_by",
            "user_confirmation",
            "validity_status",
        )
    }
    encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_stage_receipt(data: dict[str, Any], project_root: Path) -> list[str]:
    errors: list[str] = []
    receipt_value = data.get("stage_receipt")
    if not isinstance(receipt_value, str) or not receipt_value.strip():
        return ["stage_receipt must be a relative receipt file path"]
    receipt_path = Path(receipt_value)
    if receipt_path.is_absolute() or ".." in receipt_path.parts or any(char in receipt_value for char in "*?[]"):
        return ["stage_receipt must stay inside the project root and cannot contain traversal/globs"]
    receipt_path = (project_root / receipt_path).resolve()
    try:
        receipt_path.relative_to(project_root.resolve())
    except ValueError:
        return ["stage_receipt resolves outside the project root"]
    if not receipt_path.is_file():
        return [f"stage_receipt does not exist: {receipt_path}"]
    if data.get("stage_receipt_sha256") != _sha256_file(receipt_path):
        errors.append("stage_receipt_sha256 does not match the receipt file")
    try:
        receipt = load_contract(receipt_path)
    except ContractError as exc:
        return errors + [str(exc)]
    for key, expected in (
        ("status", "confirmed"),
        ("approved_by", "orchestrator"),
        ("user_confirmation", "recorded"),
        ("validity_status", "clear"),
    ):
        if receipt.get(key) != expected:
            errors.append(f"stage receipt must contain {key}: {expected}")
    if receipt.get("scope_hash") != _scope_hash(data):
        errors.append("stage receipt scope_hash does not bind this contract")
    expires_at = receipt.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at.strip():
        errors.append("stage receipt must contain an expires_at timestamp")
    else:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if expiry <= now:
                errors.append("stage receipt is expired")
            elif expiry - now > MAX_RECEIPT_HORIZON:
                errors.append("stage receipt expires too far in the future; renew confirmation within 90 days")
        except ValueError:
            errors.append("stage receipt expires_at is not an ISO-8601 timestamp")
    return errors


def validate_contract(data: dict[str, Any], contract_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_FIELDS:
        try:
            _nonempty(data, key)
        except ContractError as exc:
            errors.append(str(exc))
    if errors:
        return errors

    if data["direction"] not in ALLOWED_DIRECTIONS:
        errors.append("direction must be maximize or minimize")
    if not isinstance(data["max_runs"], int) or isinstance(data["max_runs"], bool) or not 0 < data["max_runs"] <= MAX_RUNS:
        errors.append("max_runs must be a positive integer within the finite run limit")
    for key in ("budget", "max_wall_time"):
        limit = MAX_BUDGET if key == "budget" else MAX_WALL_TIME
        value = data[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or not 0 < value <= limit:
            errors.append(f"{key} must be a finite positive number within the bounded limit")
    conditions = data["stop_conditions"]
    if not isinstance(conditions, list) or not conditions or any(not isinstance(item, str) or not item.strip() for item in conditions):
        errors.append("stop_conditions must be a non-empty list of finite textual conditions")
    elif any(term in item.lower() for item in conditions for term in UNBOUNDED_TERMS):
        errors.append("stop_conditions cannot contain an unbounded condition")

    scopes = data["data_code_scope"]
    if not isinstance(scopes, list) or not scopes or any(not isinstance(item, str) or not item.strip() for item in scopes):
        errors.append("data_code_scope must be a non-empty list of relative paths")
    elif contract_path is not None:
        project_root = _project_root(contract_path).resolve()
        for item in scopes:
            candidate = Path(item)
            if not candidate.parts:
                errors.append("data_code_scope cannot grant the entire project root")
                continue
            if candidate.is_absolute() or ".." in candidate.parts or any(char in item for char in "*?[]"):
                errors.append(f"data_code_scope path is not a safe relative path: {item}")
                continue
            resolved = (project_root / candidate).resolve()
            try:
                resolved.relative_to(project_root)
            except ValueError:
                errors.append(f"data_code_scope path escapes the project root: {item}")
                continue
            if resolved.exists() and item.endswith(("/", "\\")) and not resolved.is_dir():
                errors.append(f"directory-like data_code_scope path is not a directory: {item}")
            elif not resolved.exists():
                # A fresh experiment project may not yet contain its approved
                # directory scopes.  Permit only an explicitly directory-like
                # path (trailing slash); the runner may provision that exact
                # path after validation.  Missing file paths remain blocked so
                # a typo cannot silently become a new artifact.
                if not item.endswith(("/", "\\")):
                    errors.append(f"data_code_scope file path does not exist: {item}")

    if data["network_scope"] not in ALLOWED_NETWORK_SCOPE:
        errors.append("network_scope must be none or local-only; external access requires a separate integration")
    if data["write_and_commit_policy"] not in ALLOWED_WRITE_POLICY:
        errors.append("write_and_commit_policy must prohibit automatic commits")
    if data["report_destination"] != "local-only":
        errors.append("report_destination must be local-only")
    if data["validation_status"] != "pass":
        errors.append("validation_status must be pass")
    if data["approved_by"] != "orchestrator":
        errors.append("approved_by must be orchestrator")
    if data["user_confirmation"] != "recorded":
        errors.append("user_confirmation must be recorded")
    if data["validity_status"] != "clear":
        errors.append("validity_status must be clear")
    if contract_path is not None and not errors:
        errors.extend(_validate_stage_receipt(data, _project_root(contract_path).resolve()))
    return errors


def main() -> int:
    parser = JSONArgumentParser(description="Validate a bounded autoresearch contract")
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()
    try:
        path = Path(args.contract).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "blocked", "contract": str(args.contract), "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    result: dict[str, Any] = {"status": "blocked", "contract": str(path), "errors": []}
    try:
        data = load_contract(path)
        errors = validate_contract(data, path)
        if errors:
            result["errors"] = errors
        else:
            result["status"] = "pass"
            result["validated_fields"] = list(REQUIRED_FIELDS)
            result["project_root"] = str(_project_root(path).resolve())
            project_root = _project_root(path).resolve()
            pending: list[str] = []
            for item in data.get("data_code_scope", []):
                resolved = (project_root / Path(item)).resolve()
                if not resolved.exists() and item.endswith(("/", "\\")):
                    pending.append(item)
            result["scope_paths_pending"] = pending
    except (OSError, UnicodeError, ValueError, RuntimeError, ContractError) as exc:
        result["errors"] = [str(exc)]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
