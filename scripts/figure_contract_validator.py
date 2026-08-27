#!/usr/bin/env python3
"""Fail-closed validation for a claim-bearing scientific figure receipt.

This validator checks contract completeness and file provenance. It does not
claim to prove that a plot is scientifically correct; that remains an
independent integrity and domain-review responsibility.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import stat
import struct
import zlib
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


REQUIRED = (
    "schema_version", "figure_id", "figure_kind", "claim_refs", "source_refs", "source_files",
    "figure_plan", "claim_evidence_matrix", "claim_bindings", "code", "transformations",
    "uncertainty", "missing_data_policy", "target", "outputs", "preview", "alt_text",
    "visual_review", "validation_status",
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIGURE_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]*(?![A-Za-z0-9_-])")
RASTER_FORMATS = {"png", "tiff", "tif"}
VECTOR_FORMATS = {"pdf", "svg"}
OUTPUT_FORMATS = VECTOR_FORMATS | RASTER_FORMATS


def _error(message: str) -> ValueError:
    return ValueError(message)


def _is_link(path: Path) -> bool:
    if path.is_symlink() or os.path.islink(path):
        return True
    if os.name == "nt" and path.exists():
        try:
            attrs = path.stat(follow_symlinks=False).st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def _load(path: Path) -> dict[str, Any]:
    if _is_link(path) or not path.is_file():
        raise _error(f"receipt does not exist or is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(f"receipt is not readable UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise _error("receipt must be a JSON object")
    return value


def _safe_relative(project_root: Path, raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise _error(f"{field} must be a non-empty relative path")
    normalised = raw.replace("\\", "/")
    posix = PurePosixPath(normalised)
    windows = PureWindowsPath(normalised)
    if (
        posix.is_absolute() or windows.is_absolute() or windows.drive
        or ".." in posix.parts or any(not part or part == "." for part in posix.parts)
        or any(":" in part for part in posix.parts)
    ):
        raise _error(f"{field} must stay inside project root")
    root = project_root.expanduser().resolve()
    candidate = root.joinpath(*posix.parts)
    current = root
    for component in posix.parts:
        current = current / component
        if _is_link(current):
            raise _error(f"{field} must not use a symlink or reparse point")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _error(f"{field} escapes project root") from exc
    return resolved


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)


def _positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value > 0
    )


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _validate_hashed_artifact(project_root: Path, value: Any, field: str, errors: list[str]) -> Path | None:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        errors.append(f"{field} must be an object with only path and sha256")
        return None
    if not _valid_sha(value.get("sha256")):
        errors.append(f"{field}.sha256 must be a sha256:<64 lowercase hex> value")
    try:
        path = _safe_relative(project_root, value.get("path"), f"{field}.path")
        if not path.is_file():
            errors.append(f"{field}.path does not exist")
        elif _valid_sha(value.get("sha256")) and value["sha256"] != _hash(path):
            errors.append(f"{field} hash mismatch")
        return path
    except (ValueError, OSError) as exc:
        errors.append(str(exc))
        return None


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _normalised_relative(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def _text_tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text))


def _structured_key_has_value(text: str, keys: set[str], value: str) -> bool:
    """Recognise a scalar key/value in JSON or the small YAML subset used here."""
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        parsed = None

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in keys and child == value:
                    return True
                if walk(child):
                    return True
        elif isinstance(node, list):
            return any(walk(child) for child in node)
        return False

    if parsed is not None and walk(parsed):
        return True
    escaped = re.escape(value)
    for key in keys:
        if re.search(rf"(?m)^\s*(?:-\s*)?{re.escape(key)}\s*:\s*['\"]?{escaped}['\"]?\s*(?:#.*)?$", text):
            return True
    return False


def _yaml_scalar(raw: str) -> Any:
    """Parse the small scalar subset used by the frozen figure contracts."""
    value = raw.strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        parts: list[str] = []
        start = 0
        quote = ""
        depth = 0
        for index, character in enumerate(inner):
            if quote:
                if character == quote and (index == 0 or inner[index - 1] != "\\"):
                    quote = ""
            elif character in {"'", '"'}:
                quote = character
            elif character in "[{":
                depth += 1
            elif character in "]}":
                depth -= 1
            elif character == "," and depth == 0:
                parts.append(inner[start:index])
                start = index + 1
        parts.append(inner[start:])
        return [_yaml_scalar(part) for part in parts]
    if value.startswith("{") and value.endswith("}"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                return value
        return parsed
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    lowered = value.casefold()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _strip_yaml_comment(line: str) -> str:
    quote = ""
    for index, character in enumerate(line):
        if quote:
            if character == quote and (index == 0 or line[index - 1] != "\\"):
                quote = ""
        elif character in {"'", '"'}:
            quote = character
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def _parse_small_yaml(text: str) -> Any:
    """Parse deterministic indentation YAML without adding a PyYAML dependency."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError("tabs are not supported in figure contract YAML")
        cleaned = _strip_yaml_comment(raw).lstrip("\ufeff")
        if not cleaned.strip() or cleaned.lstrip().startswith("---"):
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        lines.append((indent, cleaned.strip()))
    if not lines:
        raise ValueError("figure contract is empty")

    def split_key(line: str) -> tuple[str, str] | None:
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or any(character in key for character in "[]{}"):
            return None
        return key, value.strip()

    def parse_node(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] != indent:
            raise ValueError("invalid figure contract indentation")
        is_list = lines[index][1].startswith("-")
        result: Any = [] if is_list else {}
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent != indent:
                raise ValueError("invalid figure contract indentation")
            if is_list:
                if not content.startswith("-"):
                    break
                item_text = content[1:].strip()
                if not item_text:
                    if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                        raise ValueError("empty figure contract list item")
                    value, index = parse_node(index + 1, lines[index + 1][0])
                    result.append(value)
                    continue
                pair = split_key(item_text)
                if pair is None:
                    result.append(_yaml_scalar(item_text))
                    index += 1
                    continue
                key, raw_value = pair
                item: dict[str, Any] = {}
                index += 1
                if raw_value:
                    item[key] = _yaml_scalar(raw_value)
                elif index < len(lines) and lines[index][0] > indent:
                    item[key], index = parse_node(index, lines[index][0])
                else:
                    item[key] = None
                if index < len(lines) and lines[index][0] > indent:
                    extra, index = parse_node(index, lines[index][0])
                    if not isinstance(extra, dict):
                        raise ValueError("list item continuation must be a mapping")
                    for extra_key, extra_value in extra.items():
                        if extra_key in item:
                            raise ValueError(f"duplicate figure contract key: {extra_key}")
                        item[extra_key] = extra_value
                result.append(item)
            else:
                pair = split_key(content)
                if pair is None:
                    raise ValueError("figure contract mapping entry is malformed")
                key, raw_value = pair
                if key in result:
                    raise ValueError(f"duplicate figure contract key: {key}")
                index += 1
                if raw_value:
                    result[key] = _yaml_scalar(raw_value)
                elif index < len(lines) and lines[index][0] > indent:
                    result[key], index = parse_node(index, lines[index][0])
                else:
                    result[key] = None
        return result, index

    value, position = parse_node(0, lines[0][0])
    if position != len(lines):
        raise ValueError("figure contract contains an unsupported YAML construct")
    return value


def _load_structured_contract(path: Path, label: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} is not readable UTF-8 text: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return _parse_small_yaml(text)
        except ValueError as exc:
            raise ValueError(f"{label} is not valid supported JSON/YAML: {exc}") from exc


def _contract_list(value: Any, keys: tuple[str, ...]) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return None


def _entry_id(entry: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _entry_refs(entry: dict[str, Any], keys: tuple[str, ...]) -> set[str] | None:
    for key in keys:
        if key in entry:
            value = entry[key]
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                return None
            return {item.strip() for item in value}
    return None


def _plan_entry_matches(path: Path, figure_id: str, claim_refs: list[str], source_refs: list[str], errors: list[str]) -> None:
    try:
        data = _load_structured_contract(path, "figure_plan")
    except ValueError as exc:
        errors.append(str(exc))
        return
    entries = _contract_list(data, ("figures", "figure_plan", "items"))
    if entries is None:
        errors.append("figure_plan must expose a figures/figure_plan/items list")
        return
    matches = [entry for entry in entries if _entry_id(entry, ("figure_id", "id")) == figure_id]
    if len(matches) != 1:
        errors.append("figure_plan must contain exactly one entry for receipt.figure_id")
        return
    entry = matches[0]
    planned_claims = _entry_refs(entry, ("claim_refs", "claims"))
    planned_sources = _entry_refs(entry, ("source_refs", "sources"))
    if planned_claims is None:
        errors.append("figure_plan entry must define claim_refs or claims")
    elif planned_claims != set(claim_refs):
        errors.append("figure_plan entry claims do not exactly match receipt.claim_refs")
    if planned_sources is None:
        errors.append("figure_plan entry must define source_refs or sources")
    elif planned_sources != set(source_refs):
        errors.append("figure_plan entry sources do not exactly match receipt.source_refs")


def _matrix_relations(path: Path, errors: list[str]) -> dict[str, set[str]]:
    try:
        data = _load_structured_contract(path, "claim_evidence_matrix")
    except ValueError as exc:
        errors.append(str(exc))
        return {}
    entries = _contract_list(data, ("claim_evidence", "claim_bindings", "entries", "claims"))
    if entries is None:
        errors.append("claim_evidence_matrix must expose relational claim entries")
        return {}
    relations: dict[str, set[str]] = {}
    for index, entry in enumerate(entries):
        claim_id = _entry_id(entry, ("claim_id", "claim_ref", "id"))
        sources = _entry_refs(entry, ("source_refs", "sources", "evidence_refs")) if isinstance(entry, dict) else None
        if claim_id is None or sources is None:
            errors.append(f"claim_evidence_matrix entry {index} must define one claim ID and source_refs")
            continue
        if claim_id in relations:
            errors.append(f"claim_evidence_matrix contains duplicate claim entry: {claim_id}")
        relations[claim_id] = sources
    return relations


def _png_metadata(path: Path) -> tuple[int, int, float, float] | None:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    offset = 8
    width = height = None
    dpi_x = dpi_y = None
    saw_ihdr = saw_idat = saw_iend = False
    while offset + 12 <= len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(raw):
            return None
        payload = raw[offset + 8:offset + 8 + length]
        stored_crc = struct.unpack(">I", raw[offset + 8 + length:offset + 12 + length])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != stored_crc:
            return None
        if kind == b"IHDR" and len(payload) == 13 and not saw_ihdr:
            width, height = struct.unpack(">II", payload[:8])
            saw_ihdr = True
        elif kind == b"pHYs" and len(payload) == 9 and payload[8] == 1:
            xppm, yppm = struct.unpack(">II", payload[:8])
            if xppm > 0 and yppm > 0:
                dpi_x, dpi_y = xppm * 0.0254, yppm * 0.0254
        elif kind == b"IDAT" and saw_ihdr:
            saw_idat = True
        elif kind == b"IEND":
            saw_iend = True
            break
        offset = end
    if saw_ihdr and saw_idat and saw_iend and width and height and dpi_x and dpi_y:
        return width, height, dpi_x, dpi_y
    return None


def _tiff_metadata(path: Path) -> tuple[int, int, float, float] | None:
    raw = path.read_bytes()
    if len(raw) < 8 or raw[:2] not in {b"II", b"MM"}:
        return None
    endian = "<" if raw[:2] == b"II" else ">"
    try:
        magic = struct.unpack_from(endian + "H", raw, 2)[0]
        if magic != 42:
            return None
        ifd_offset = struct.unpack_from(endian + "I", raw, 4)[0]
        if ifd_offset + 2 > len(raw):
            return None
        count = struct.unpack_from(endian + "H", raw, ifd_offset)[0]
        if ifd_offset + 2 + count * 12 > len(raw):
            return None
        values: dict[int, Any] = {}
        type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}
        for index in range(count):
            base = ifd_offset + 2 + index * 12
            tag, kind, number = struct.unpack_from(endian + "HHI", raw, base)
            if kind not in type_sizes:
                continue
            size = type_sizes[kind] * number
            location = base + 8 if size <= 4 else struct.unpack_from(endian + "I", raw, base + 8)[0]
            if location + size > len(raw):
                continue
            payload = raw[location:location + size]
            if kind == 3:
                value = struct.unpack_from(endian + ("H" if number == 1 else f"{number}H"), payload)
            elif kind == 4:
                value = struct.unpack_from(endian + ("I" if number == 1 else f"{number}I"), payload)
            elif kind == 5 and number >= 1:
                numerator, denominator = struct.unpack_from(endian + "II", payload)
                value = numerator / denominator if denominator else 0
            else:
                value = payload
            values[tag] = value[0] if isinstance(value, tuple) and len(value) == 1 else value
        width, height = values.get(256), values.get(257)
        xres, yres = values.get(282), values.get(283)
        unit = values.get(296, 2)
        if width and height and xres and yres and unit in {2, 3}:
            if unit == 3:
                xres, yres = xres * 2.54, yres * 2.54
            return int(width), int(height), float(xres), float(yres)
    except (OSError, struct.error, ValueError, ZeroDivisionError):
        return None
    return None


def _raster_metadata(path: Path, fmt: str) -> tuple[int, int, float, float] | None:
    try:
        return _png_metadata(path) if fmt == "png" else _tiff_metadata(path)
    except (OSError, ValueError):
        return None


def _vector_is_decodable(path: Path, fmt: str) -> bool:
    """Reject files that only claim a vector extension without a vector header."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if fmt == "pdf":
        return raw.startswith(b"%PDF-")
    if fmt == "svg":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False
        # Keep this deliberately small and non-XML-evaluating: the contract
        # needs a real SVG root, while full SVG security/rendering is a
        # journal/export-tool responsibility.
        return bool(re.search(r"<svg(?:\s|>)", text, flags=re.IGNORECASE))
    return False


def validate_receipt(receipt: dict[str, Any], project_root: Path, receipt_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return ["receipt must be a JSON object"]
    for field in REQUIRED:
        if field not in receipt:
            errors.append(f"missing required field: {field}")
    if errors:
        return errors
    if receipt["schema_version"] != "figure-receipt-v1":
        errors.append("unsupported schema_version")

    figure_id = receipt["figure_id"]
    if not isinstance(figure_id, str) or not FIGURE_ID_RE.fullmatch(figure_id):
        errors.append("figure_id must match [A-Za-z][A-Za-z0-9_-]{0,63}")
    elif receipt_path is not None:
        root = project_root.expanduser().resolve()
        expected = root / ".research" / "figures" / figure_id / "figure_receipt.json"
        actual = receipt_path.expanduser().resolve()
        if actual != expected:
            errors.append("receipt must be .research/figures/<figure_id>/figure_receipt.json")

    figure_kind = receipt["figure_kind"]
    if not isinstance(figure_kind, str) or figure_kind not in {"data_plot", "image_panel"}:
        errors.append("figure_kind must be data_plot or image_panel")
    for field in ("claim_refs", "source_refs", "transformations"):
        if not _nonempty_string_list(receipt[field]):
            errors.append(f"{field} must be a non-empty list")
    for field in ("claim_refs", "source_refs"):
        if _nonempty_string_list(receipt[field]) and len(receipt[field]) != len(set(receipt[field])):
            errors.append(f"{field} must not contain duplicate IDs")

    plan_path = _validate_hashed_artifact(project_root, receipt["figure_plan"], "figure_plan", errors)
    matrix_path = _validate_hashed_artifact(project_root, receipt["claim_evidence_matrix"], "claim_evidence_matrix", errors)
    bindings = receipt["claim_bindings"]
    binding_sources_by_claim: dict[str, set[str]] = {}
    if not isinstance(bindings, list) or not bindings:
        errors.append("claim_bindings must be a non-empty list")
    else:
        claim_refs = set(receipt["claim_refs"]) if _nonempty_string_list(receipt["claim_refs"]) else set()
        source_refs = set(receipt["source_refs"]) if _nonempty_string_list(receipt["source_refs"]) else set()
        bound_claims: set[str] = set()
        bound_sources: set[str] = set()
        seen_claim_bindings: set[str] = set()
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict) or set(binding) != {"claim_ref", "source_refs"}:
                errors.append(f"claim_bindings[{index}] must include only claim_ref and source_refs")
                continue
            if not isinstance(binding["claim_ref"], str) or binding["claim_ref"] not in claim_refs:
                errors.append(f"claim_bindings[{index}].claim_ref is not listed in claim_refs")
            else:
                if binding["claim_ref"] in seen_claim_bindings:
                    errors.append(f"claim_bindings[{index}].claim_ref is duplicated")
                seen_claim_bindings.add(binding["claim_ref"])
                bound_claims.add(binding["claim_ref"])
            if not _nonempty_string_list(binding["source_refs"]):
                errors.append(f"claim_bindings[{index}].source_refs must be a non-empty list")
            elif not set(binding["source_refs"]).issubset(source_refs):
                errors.append(f"claim_bindings[{index}] references a source outside source_refs")
            else:
                if isinstance(binding.get("claim_ref"), str) and binding["claim_ref"] in claim_refs:
                    binding_sources_by_claim[binding["claim_ref"]] = set(binding["source_refs"])
                bound_sources.update(binding["source_refs"])
        if _nonempty_string_list(receipt["claim_refs"]) and bound_claims != claim_refs:
            errors.append("claim_bindings must cover every claim_ref exactly once")
        if _nonempty_string_list(receipt["source_refs"]) and bound_sources != source_refs:
            errors.append("claim_bindings must cover every source_ref and no extra source")
    for label, path, refs in (("figure_plan", plan_path, receipt["claim_refs"]), ("claim_evidence_matrix", matrix_path, receipt["claim_refs"] + receipt["source_refs"])):
        if path is not None and _nonempty_string_list(refs) and path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"{label} is not readable UTF-8 text: {exc}")
            else:
                tokens = _text_tokens(text)
                missing = [ref for ref in refs if ref not in tokens]
                if missing:
                    errors.append(f"{label} does not contain referenced IDs: {', '.join(missing)}")
    if plan_path is not None and plan_path.is_file() and isinstance(figure_id, str) and FIGURE_ID_RE.fullmatch(figure_id):
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"figure_plan is not readable UTF-8 text: {exc}")
        else:
            if not _structured_key_has_value(plan_text, {"id", "figure_id"}, figure_id):
                errors.append("figure_plan does not contain an entry for receipt.figure_id")
            _plan_entry_matches(plan_path, figure_id, receipt["claim_refs"], receipt["source_refs"], errors)
    matrix_relations: dict[str, set[str]] = {}
    if matrix_path is not None and matrix_path.is_file():
        matrix_relations = _matrix_relations(matrix_path, errors)
        if _nonempty_string_list(receipt["claim_refs"]):
            for claim_ref in receipt["claim_refs"]:
                expected_sources = binding_sources_by_claim.get(claim_ref)
                matrix_sources = matrix_relations.get(claim_ref)
                if matrix_sources is None:
                    errors.append(f"claim_evidence_matrix has no relation row for {claim_ref}")
                elif expected_sources is None or matrix_sources != expected_sources:
                    errors.append(f"claim_evidence_matrix relation for {claim_ref} does not match claim_bindings")

    source_files = receipt["source_files"]
    if not isinstance(source_files, list) or not source_files:
        errors.append("source_files must be a non-empty list")
    else:
        for index, source in enumerate(source_files):
            if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
                errors.append(f"source_files[{index}] must contain only path and sha256")
                continue
            if not _valid_sha(source.get("sha256")):
                errors.append(f"source_files[{index}].sha256 must be a sha256:<64 lowercase hex> value")
            try:
                source_path = _safe_relative(project_root, source.get("path"), f"source_files[{index}].path")
                if not source_path.is_file():
                    errors.append(f"source_files[{index}] does not exist")
                elif _valid_sha(source.get("sha256")) and source["sha256"] != _hash(source_path):
                    errors.append(f"source_files[{index}] hash mismatch")
            except (ValueError, OSError) as exc:
                errors.append(str(exc))

    if not isinstance(receipt["uncertainty"], str) or not receipt["uncertainty"].strip():
        errors.append("uncertainty must explicitly define the estimate or state that it is not applicable")
    if not isinstance(receipt["missing_data_policy"], str) or not receipt["missing_data_policy"].strip():
        errors.append("missing_data_policy is required")

    target = receipt["target"]
    if not isinstance(target, dict):
        errors.append("target must be an object")
        target = {}
    if not isinstance(target.get("venue"), str) or not target["venue"].strip():
        errors.append("target.venue must be a non-empty string")
    if not isinstance(target.get("phase"), str) or not target["phase"].strip():
        errors.append("target.phase must be a non-empty string")
    width = target.get("width_mm")
    if not _positive_number(width):
        errors.append("target.width_mm must be a finite positive number")

    code = receipt["code"]
    if not isinstance(code, dict) or set(code) != {"path", "sha256"}:
        errors.append("code must contain only path and sha256")
    else:
        if not _valid_sha(code.get("sha256")):
            errors.append("code.sha256 must be a sha256:<64 lowercase hex> value")
        try:
            code_path = _safe_relative(project_root, code.get("path"), "code.path")
            if not code_path.is_file():
                errors.append("code.path does not exist")
            elif _valid_sha(code.get("sha256")) and code["sha256"] != _hash(code_path):
                errors.append("code hash mismatch")
        except (ValueError, OSError) as exc:
            errors.append(str(exc))

    outputs = receipt["outputs"]
    output_paths: dict[str, str] = {}
    output_metadata: dict[str, tuple[int, int, float, float] | None] = {}
    found_vector = False
    found_raster = False
    if not isinstance(outputs, list) or not outputs:
        errors.append("outputs must be a non-empty list")
    else:
        for index, output in enumerate(outputs):
            if not isinstance(output, dict) or set(output) != {"path", "format", "sha256"}:
                errors.append(f"outputs[{index}] must contain path, format, and sha256 only")
                continue
            fmt = output.get("format")
            if not isinstance(fmt, str) or fmt.lower() not in OUTPUT_FORMATS:
                errors.append(f"outputs[{index}].format must be pdf, svg, png, or tiff")
                fmt = ""
            else:
                fmt = fmt.lower()
                found_vector |= fmt in VECTOR_FORMATS
                found_raster |= fmt in RASTER_FORMATS
            if not _valid_sha(output.get("sha256")):
                errors.append(f"outputs[{index}].sha256 must be a sha256:<64 lowercase hex> value")
            try:
                output_path = _safe_relative(project_root, output.get("path"), f"outputs[{index}].path")
                if not output_path.is_file():
                    errors.append(f"outputs[{index}] does not exist")
                elif _valid_sha(output.get("sha256")) and output["sha256"] != _hash(output_path):
                    errors.append(f"outputs[{index}] hash mismatch")
                if fmt and output_path.suffix.lower().lstrip(".") not in ({"tif", "tiff"} if fmt in {"tif", "tiff"} else {fmt}):
                    errors.append(f"outputs[{index}] path extension does not match format")
                if fmt:
                    output_paths[_normalised_relative(output["path"])] = fmt
                    if fmt in VECTOR_FORMATS and output_path.is_file() and not _vector_is_decodable(output_path, fmt):
                        errors.append(f"outputs[{index}] is not a decodable {fmt.upper()} vector")
                    if fmt in RASTER_FORMATS:
                        raster_fmt = "png" if fmt == "png" else "tiff"
                        output_metadata[_normalised_relative(output["path"])] = _raster_metadata(output_path, raster_fmt) if output_path.is_file() else None
                        if output_metadata[_normalised_relative(output["path"])] is None:
                            errors.append(f"outputs[{index}] is not a decodable {fmt.upper()} raster")
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
    if figure_kind == "data_plot" and not found_vector:
        errors.append("data_plot requires a PDF or SVG output")
    if figure_kind == "image_panel" and not found_raster:
        errors.append("image_panel requires a publication-grade PNG or TIFF output")

    preview = receipt["preview"]
    if not isinstance(preview, dict) or set(preview) != {"path", "format", "sha256", "rendered_from", "width_px", "height_px", "dpi", "width_mm"}:
        errors.append("preview must include path, format, hash, rendered_from, dimensions, DPI, and width_mm")
    else:
        preview_fmt = preview.get("format")
        if not isinstance(preview_fmt, str) or preview_fmt.lower() not in RASTER_FORMATS:
            errors.append("preview.format must be png or tiff")
        else:
            preview_fmt = preview_fmt.lower()
        if not _valid_sha(preview.get("sha256")):
            errors.append("preview.sha256 must be a sha256:<64 lowercase hex> value")
        for field in ("width_px", "height_px", "dpi", "width_mm"):
            if not _positive_number(preview.get(field)):
                errors.append(f"preview.{field} must be a finite positive number")
        try:
            preview_path = _safe_relative(project_root, preview.get("path"), "preview.path")
            if not preview_path.is_file():
                errors.append("preview.path does not exist")
            elif _valid_sha(preview.get("sha256")) and preview["sha256"] != _hash(preview_path):
                errors.append("preview hash mismatch")
            if preview_fmt and preview_path.suffix.lower().lstrip(".") not in ({"tif", "tiff"} if preview_fmt in {"tif", "tiff"} else {preview_fmt}):
                errors.append("preview path extension does not match format")
            metadata = _raster_metadata(preview_path, "png" if preview_fmt == "png" else "tiff") if preview_fmt and preview_path.is_file() else None
            if metadata is None:
                errors.append("preview is not a decodable PNG/TIFF raster")
            else:
                actual_width, actual_height, actual_dpi_x, actual_dpi_y = metadata
                if preview.get("width_px") != actual_width or preview.get("height_px") != actual_height:
                    errors.append("preview pixel dimensions do not match the rendered file")
                if not _positive_number(preview.get("dpi")) or abs(float(preview["dpi"]) - actual_dpi_x) > max(1.0, actual_dpi_x * 0.01) or abs(actual_dpi_x - actual_dpi_y) > max(1.0, actual_dpi_x * 0.01):
                    errors.append("preview DPI does not match the rendered file metadata")
                computed_width_mm = actual_width / actual_dpi_x * 25.4
                if not _positive_number(preview.get("width_mm")) or abs(float(preview["width_mm"]) - computed_width_mm) > max(0.1, computed_width_mm * 0.01):
                    errors.append("preview.width_mm is inconsistent with pixels and DPI")
            rendered_from = preview.get("rendered_from")
            if not isinstance(rendered_from, str) or not rendered_from.strip():
                errors.append("preview.rendered_from must name an output artifact")
            elif _normalised_relative(rendered_from) not in output_paths:
                errors.append("preview.rendered_from must reference one of outputs")
            elif _normalised_relative(rendered_from) == _normalised_relative(preview["path"]):
                errors.append("preview must be a separate rendered file, not an output alias")
            if _positive_number(width) and _positive_number(preview.get("width_mm")) and abs(float(preview["width_mm"]) - float(width)) > 1e-6:
                errors.append("preview.width_mm must match target.width_mm")
        except (ValueError, OSError) as exc:
            errors.append(str(exc))

    if not isinstance(receipt["alt_text"], str) or len(receipt["alt_text"].strip()) < 10:
        errors.append("alt_text must be a useful description")
    review = receipt["visual_review"]
    if not isinstance(review, dict):
        errors.append("visual_review must be an object")
    else:
        if review.get("status") != "pass":
            errors.append("visual_review.status must be pass")
        if review.get("reviewer_type") != "human" or not isinstance(review.get("reviewer_id"), str) or not review["reviewer_id"].strip():
            errors.append("visual_review must identify a human reviewer")
        if not _valid_timestamp(review.get("reviewed_at")):
            errors.append("visual_review.reviewed_at must be an ISO-8601 timestamp with timezone")
        if not _nonempty_string_list(review.get("checks")):
            errors.append("visual_review.checks must be a non-empty checklist")
        findings = review.get("findings")
        dispositions = review.get("dispositions")
        if not isinstance(findings, list) or not isinstance(dispositions, list):
            errors.append("visual_review must include findings and dispositions lists")
        else:
            finding_ids: set[str] = set()
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict) or set(finding) != {"id", "severity", "summary"}:
                    errors.append(f"visual_review.findings[{index}] must contain id, severity, and summary")
                    continue
                finding_id = finding.get("id")
                severity = finding.get("severity")
                if not isinstance(finding_id, str) or not finding_id.strip() or finding_id in finding_ids:
                    errors.append(f"visual_review.findings[{index}].id must be unique and non-empty")
                else:
                    finding_ids.add(finding_id)
                if severity not in {"critical", "major", "minor", "cosmetic"}:
                    errors.append(f"visual_review.findings[{index}].severity is invalid")
                if not isinstance(finding.get("summary"), str) or not finding["summary"].strip():
                    errors.append(f"visual_review.findings[{index}].summary must be non-empty")
            disposition_ids: set[str] = set()
            allowed_decisions = {"resolved", "accepted_nonblocking", "rejected"}
            for index, disposition in enumerate(dispositions):
                if not isinstance(disposition, dict) or set(disposition) != {"finding_id", "decision", "reason", "evidence"}:
                    errors.append(f"visual_review.dispositions[{index}] must contain finding_id, decision, reason, and evidence")
                    continue
                finding_id = disposition.get("finding_id")
                if not isinstance(finding_id, str) or finding_id not in finding_ids or finding_id in disposition_ids:
                    errors.append(f"visual_review.dispositions[{index}].finding_id must match one finding exactly once")
                else:
                    disposition_ids.add(finding_id)
                if disposition.get("decision") not in allowed_decisions:
                    errors.append(f"visual_review.dispositions[{index}].decision is invalid")
                if not isinstance(disposition.get("reason"), str) or not disposition["reason"].strip():
                    errors.append(f"visual_review.dispositions[{index}].reason must be non-empty")
                if not _nonempty_string_list(disposition.get("evidence")):
                    errors.append(f"visual_review.dispositions[{index}].evidence must be a non-empty list")
            if finding_ids != disposition_ids:
                errors.append("visual_review findings and dispositions must have an exact one-to-one ID mapping")
            for finding in findings:
                if isinstance(finding, dict) and finding.get("severity") in {"critical", "major"}:
                    decision = next((item.get("decision") for item in dispositions if isinstance(item, dict) and item.get("finding_id") == finding.get("id")), None)
                    if decision != "resolved":
                        errors.append(f"visual_review finding {finding.get('id')} is a critical/major unresolved defect")
    if receipt.get("validation_status") != "pass":
        errors.append("validation_status must be pass")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = _load(args.receipt)
        errors = validate_receipt(receipt, args.project_root, args.receipt)
    except Exception as exc:
        errors = [str(exc)]
    result = {"status": "pass" if not errors else "blocked", "errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
