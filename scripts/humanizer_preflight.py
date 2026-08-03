#!/usr/bin/env python3
"""Fail-closed preflight for the prose-only humanizer stage.

The workflow deliberately ships no universal LaTeX/DOCX/PDF parser.  This
command therefore returns ``ready`` only after a supplied adapter has produced
machine-readable evidence for the *specific* input: an immutable copy, a
protected-span manifest, a passing claim/evidence diff, an integrity receipt,
and an adapter self-test.  It never rewrites the canonical manuscript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


FORMAT_ALIASES = {
    "md": "markdown",
    "markdown": "markdown",
    "tex": "latex",
    "latex": "latex",
    "docx": "docx",
    "word": "docx",
    "pdf": "pdf",
}
KNOWN_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".tex": "latex", ".docx": "docx", ".pdf": "pdf"}
REQUIRED_OPERATIONS = ("extract_protected", "reassemble", "verify_protected", "verify_claims", "rollback")
ADAPTER_SCHEMA_VERSION = 2
PROTECTED_KINDS = {
    "math", "code", "table", "figure_caption", "citation", "bibliography", "url", "doi", "quote",
    "technical_term", "data", "number", "unit", "statistical_qualifier", "negative_finding",
    "modality", "negation", "scope", "frontmatter", "label",
}
CLAIM_PROTECTED_FIELDS = (
    "entities", "numeric_units", "modality", "negation", "scope",
    "statistical_qualifiers", "citations", "equations_labels",
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class PreflightError(ValueError):
    pass


class JSONArgumentParser(argparse.ArgumentParser):
    """Make even malformed invocations fail closed with machine-readable JSON."""

    def error(self, message: str) -> None:  # pragma: no cover - exercised through CLI
        print(json.dumps({
            "input": "",
            "format": "unknown",
            "status": "blocked",
            "reason": message,
            "canonical_mutation_allowed": False,
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)


def _attach_kill_on_close_job(process: subprocess.Popen[str]) -> tuple[object, object] | None:
    """Attach the self-test to an OS job so descendants cannot outlive it."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel32.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    return job, kernel32


def _close_kill_job(job_state: tuple[object, object] | None) -> None:
    if job_state is not None:
        _job, kernel32 = job_state
        kernel32.CloseHandle(_job)


def _kill_posix_process_group(process: subprocess.Popen[str] | None) -> None:
    if process is None or os.name == "nt":
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _force_terminate_windows_tree(process: subprocess.Popen[str] | None) -> None:
    """Best-effort bounded fallback when a Windows Job Object cannot be attached."""
    if process is None or os.name != "nt":
        return
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        return
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    if not taskkill.is_file():
        return
    try:
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _terminate_self_test_process(
    process: subprocess.Popen[str] | None,
    job_state: tuple[object, object] | None,
) -> None:
    """Terminate the complete self-test tree before waiting on its pipes."""
    _kill_posix_process_group(process)
    if process is not None and process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    if job_state is not None:
        _close_kill_job(job_state)
    elif os.name == "nt":
        _force_terminate_windows_tree(process)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_manifest_hash(data: dict[str, Any]) -> str:
    """Hash the manifest definition without its self-referential mapping binding."""
    canonical = dict(data)
    canonical.pop("mapping_receipt", None)
    canonical.pop("mapping_receipt_sha256", None)
    return sha256_text(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def verify_file_hash(path: Path, expected: Any, label: str) -> None:
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise PreflightError(f"{label} must contain a sha256:<64-hex> hash")
    if not path.is_file():
        raise PreflightError(f"{label} path does not exist or is not a file: {path}")
    if sha256_file(path) != expected:
        raise PreflightError(f"{label} hash does not match: {path}")


def nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreflightError(f"{label} must be a non-empty string")
    return value.strip()


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PreflightError(f"{label} does not exist or is not a file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{label} is not readable UTF-8 JSON: {path}") from exc
    if not isinstance(data, dict):
        raise PreflightError(f"{label} must contain a JSON object")
    return data


def detect_format(path: Path, requested: str) -> str:
    suffix_format = KNOWN_SUFFIXES.get(path.suffix.lower())
    requested_format = FORMAT_ALIASES.get(requested.lower().lstrip("."), requested.lower().lstrip(".")) if requested else ""
    if requested_format and suffix_format and requested_format != suffix_format:
        raise PreflightError(
            f"format override conflicts with input suffix: {path.suffix} implies {suffix_format}, requested {requested_format}"
        )
    fmt = requested_format or suffix_format or "unknown"
    if fmt not in set(FORMAT_ALIASES.values()):
        raise PreflightError(f"unsupported or unknown manuscript format: {fmt}")
    return fmt


def verify_integrity_report(path: Path, input_hash: str, candidate_hash: str, manifest_hash: str, claim_hash: str) -> None:
    if not path.is_file():
        raise PreflightError(f"integrity report does not exist or is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PreflightError(f"integrity report is not readable UTF-8 text: {path}") from exc
    def require_field(name: str, expected: str | None = None) -> None:
        matches = re.findall(rf"(?m)^\s*{re.escape(name)}\s*:\s*(.*?)\s*$", text)
        if len(matches) != 1 or not matches[0]:
            raise PreflightError(f"integrity report must contain exactly one non-empty {name} field")
        if expected is not None and matches[0] != expected:
            raise PreflightError(f"integrity report {name} does not match the bound evidence")

    require_field("validation_status", "pass")
    require_field("input_sha256", input_hash)
    require_field("candidate_sha256", candidate_hash)
    require_field("validity_status", "clear")
    require_field("schema_version", "1")
    require_field("protected_manifest_sha256", manifest_hash)
    require_field("claim_diff_sha256", claim_hash)
    require_field("receipt_id")


def verify_immutable_copy(input_path: Path, copy_path: Path, input_hash: str) -> None:
    if input_path == copy_path:
        raise PreflightError("immutable copy must be a separate path from the canonical input")
    if not copy_path.is_file():
        raise PreflightError(f"immutable copy does not exist or is not a file: {copy_path}")
    if sha256_file(copy_path) != input_hash:
        raise PreflightError("immutable copy hash does not match the canonical input")


def verify_manifest(
    path: Path,
    fmt: str,
    input_path: Path,
    input_hash: str,
    candidate_path: Path,
    candidate_hash: str,
) -> tuple[str | None, Path | None]:
    data = load_json(path, "protected manifest")
    if data.get("schema_version") != 1 or data.get("format") != fmt:
        raise PreflightError("protected manifest schema_version/format does not match the selected format")
    if data.get("input_sha256") != input_hash:
        raise PreflightError("protected manifest input_sha256 does not match the canonical input")
    offset_unit = data.get("offset_unit")
    if offset_unit not in {"utf8_byte", "adapter"}:
        raise PreflightError("protected manifest offset_unit must be utf8_byte or adapter")
    mapping_hash: str | None = None
    mapping_path: Path | None = None
    mapping: dict[str, Any] | None = None
    if offset_unit == "adapter":
        if data.get("mapping_input_sha256") != input_hash or not nonempty(data.get("mapping_receipt"), "adapter mapping_receipt"):
            raise PreflightError("adapter offsets require an input-bound mapping receipt")
        mapping_value = data["mapping_receipt"]
        mapping_candidate = Path(mapping_value).expanduser()
        if mapping_candidate.is_absolute() or not mapping_candidate.parts or ".." in mapping_candidate.parts or any(char in mapping_value for char in "*?[]"):
            raise PreflightError("adapter mapping_receipt must be a safe relative path")
        mapping_path = (path.parent / mapping_candidate).resolve()
        try:
            mapping_path.relative_to(path.parent.resolve())
        except ValueError as exc:
            raise PreflightError("adapter mapping_receipt escapes the manifest directory") from exc
        verify_file_hash(mapping_path, data.get("mapping_receipt_sha256"), "adapter mapping receipt")
        mapping = load_json(mapping_path, "adapter mapping receipt")
        if (
            mapping.get("status") != "pass"
            or mapping.get("input_sha256") != input_hash
            or mapping.get("candidate_sha256") != candidate_hash
            or mapping.get("protected_manifest_sha256") != canonical_manifest_hash(data)
            or mapping.get("offset_mapping_verified") is not True
            or mapping.get("candidate_protected_verified") is not True
        ):
            raise PreflightError("adapter mapping receipt is not a passing receipt for this input")
        mapping_hash = data.get("mapping_receipt_sha256")
    spans = data.get("spans")
    if not isinstance(spans, list):
        raise PreflightError("protected manifest spans must be a list")
    if mapping is not None and mapping.get("protected_span_count") != len(spans):
        raise PreflightError("adapter mapping receipt protected_span_count does not match the manifest")
    seen: set[str] = set()
    previous_end = 0
    candidate_previous_end = 0
    input_bytes = input_path.read_bytes()
    candidate_bytes = candidate_path.read_bytes()
    candidate_text = candidate_path.read_text(encoding="utf-8") if fmt in {"markdown", "latex"} else None
    for index, span in enumerate(spans):
        if not isinstance(span, dict):
            raise PreflightError(f"protected manifest span {index} is not an object")
        span_id = nonempty(span.get("id"), f"protected span {index} id")
        if span_id in seen:
            raise PreflightError(f"duplicate protected span id: {span_id}")
        seen.add(span_id)
        kind = nonempty(span.get("kind"), f"protected span {span_id} kind")
        if kind not in PROTECTED_KINDS:
            raise PreflightError(f"unsupported protected span kind: {kind}")
        exact_text = span.get("exact_text")
        if not isinstance(exact_text, str) or not exact_text:
            raise PreflightError(f"protected span {span_id} exact_text must be non-empty")
        canonical_hash = span.get("canonical_hash")
        if canonical_hash != sha256_text(exact_text) or not isinstance(canonical_hash, str) or not SHA256_RE.fullmatch(canonical_hash):
            raise PreflightError(f"protected span {span_id} canonical_hash does not match exact_text")
        offsets = span.get("source_offset")
        if not isinstance(offsets, list) or len(offsets) != 2 or not all(isinstance(value, int) and not isinstance(value, bool) for value in offsets):
            raise PreflightError(f"protected span {span_id} source_offset must be [start, end] integers")
        start, end = offsets
        if start < 0 or end < start or end > len(input_bytes):
            raise PreflightError(f"protected span {span_id} source_offset is invalid")
        if start < previous_end:
            raise PreflightError(f"protected span {span_id} overlaps or is out of order")
        previous_end = end
        if offset_unit == "utf8_byte":
            try:
                observed = input_bytes[start:end].decode("utf-8")
            except (UnicodeDecodeError, IndexError) as exc:
                raise PreflightError(f"protected span {span_id} is outside valid UTF-8 input offsets") from exc
            if observed != exact_text:
                raise PreflightError(f"protected span {span_id} exact_text does not match input offsets")
        candidate_exact_text = span.get("candidate_exact_text")
        candidate_span_hash = span.get("candidate_span_sha256")
        if not isinstance(candidate_exact_text, str) or not candidate_exact_text:
            raise PreflightError(f"protected span {span_id} candidate_exact_text must be non-empty")
        if candidate_span_hash != sha256_text(candidate_exact_text) or not isinstance(candidate_span_hash, str) or not SHA256_RE.fullmatch(candidate_span_hash):
            raise PreflightError(f"protected span {span_id} candidate_span_sha256 does not match candidate_exact_text")
        if candidate_exact_text != exact_text:
            raise PreflightError(f"protected span {span_id} candidate text changes protected content")
        candidate_grounded = candidate_exact_text in candidate_text if candidate_text is not None else mapping_path is not None
        if not candidate_grounded:
            raise PreflightError(f"protected span {span_id} candidate text is not grounded in the candidate")
        candidate_offsets = span.get("candidate_offset")
        if not isinstance(candidate_offsets, list) or len(candidate_offsets) != 2 or not all(isinstance(value, int) and not isinstance(value, bool) for value in candidate_offsets):
            raise PreflightError(f"protected span {span_id} candidate_offset must be [start, end] integers")
        candidate_start, candidate_end = candidate_offsets
        if candidate_start < 0 or candidate_end < candidate_start or candidate_end > len(candidate_bytes) or candidate_start < candidate_previous_end:
            raise PreflightError(f"protected span {span_id} candidate_offset is invalid")
        candidate_previous_end = candidate_end
        if offset_unit == "utf8_byte" and candidate_bytes[candidate_start:candidate_end].decode("utf-8") != candidate_exact_text:
            raise PreflightError(f"protected span {span_id} candidate_offset does not match candidate text")
    return mapping_hash, mapping_path


def verify_claim_diff(path: Path, fmt: str, input_hash: str, candidate_hash: str, input_path: Path, candidate_path: Path) -> Path:
    data = load_json(path, "claim/evidence diff")
    if data.get("schema_version") != 1 or data.get("format") != fmt:
        raise PreflightError("claim/evidence diff schema_version/format does not match the selected format")
    if data.get("input_sha256") != input_hash or data.get("original_sha256") != input_hash or data.get("candidate_sha256") != candidate_hash or data.get("status") != "pass":
        raise PreflightError("claim/evidence diff must be a passing diff for this exact input")
    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        raise PreflightError("claim/evidence diff claims must be a non-empty list")
    if data.get("claim_count") != len(claims):
        raise PreflightError("claim/evidence diff claim_count does not match the claims list")
    verifier_receipt = data.get("verifier_receipt")
    if not isinstance(verifier_receipt, dict) or verifier_receipt.get("status") != "pass" or verifier_receipt.get("input_sha256") != input_hash or verifier_receipt.get("candidate_sha256") != candidate_hash or not nonempty(verifier_receipt.get("verifier_id"), "verifier_receipt.verifier_id"):
        raise PreflightError("claim/evidence diff lacks an independent candidate-bound verifier receipt")
    inventory = [{"claim_id": claim.get("claim_id"), "old_hash": claim.get("old_hash"), "new_hash": claim.get("new_hash")} for claim in claims if isinstance(claim, dict)]
    inventory_hash = sha256_text(json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if data.get("claim_inventory_sha256") != inventory_hash:
        raise PreflightError("claim/evidence diff claim inventory hash does not match")
    verifier_path_value = data.get("verifier_receipt_path")
    if not isinstance(verifier_path_value, str) or not verifier_path_value.strip():
        raise PreflightError("claim/evidence diff must bind a separate verifier_receipt_path")
    verifier_path = Path(verifier_path_value).expanduser()
    if verifier_path.is_absolute() or not verifier_path.parts or ".." in verifier_path.parts or any(char in verifier_path_value for char in "*?[]"):
        raise PreflightError("claim/evidence verifier receipt path must be safely relative")
    verifier_path = (path.parent / verifier_path).resolve()
    try:
        verifier_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise PreflightError("claim/evidence verifier receipt escapes the diff directory") from exc
    verify_file_hash(verifier_path, data.get("verifier_receipt_sha256"), "claim/evidence verifier receipt")
    independent = load_json(verifier_path, "claim/evidence verifier receipt")
    if independent.get("status") != "pass" or independent.get("format") != fmt or independent.get("input_sha256") != input_hash or independent.get("candidate_sha256") != candidate_hash or independent.get("claim_inventory_sha256") != inventory_hash or not nonempty(independent.get("verifier_id"), "independent verifier_id") or independent.get("verifier_id") != verifier_receipt.get("verifier_id"):
        raise PreflightError("claim/evidence verifier receipt is not bound to this input, candidate, and inventory")
    independent_refs = independent.get("evidence_refs")
    if not isinstance(independent_refs, list) or not independent_refs:
        raise PreflightError("claim/evidence verifier receipt evidence_refs must be non-empty")
    verified_claims = independent.get("verified_claims")
    if not isinstance(verified_claims, list) or not verified_claims:
        raise PreflightError("claim/evidence verifier receipt must list verified claims")
    verified_by_id = {item.get("claim_id"): item for item in verified_claims if isinstance(item, dict)}
    if len(verified_by_id) != len(verified_claims):
        raise PreflightError("claim/evidence verifier receipt has duplicate or malformed claim IDs")
    grounding_mode = data.get("grounding_mode", "text")
    if grounding_mode not in {"text", "adapter"} or (fmt in {"docx", "pdf"} and grounding_mode != "adapter"):
        raise PreflightError("binary formats require adapter-grounded claim excerpts")
    input_text = input_path.read_text(encoding="utf-8") if grounding_mode == "text" else None
    candidate_text = candidate_path.read_text(encoding="utf-8") if grounding_mode == "text" else None
    seen: set[str] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise PreflightError(f"claim {index} is not an object")
        claim_id = nonempty(claim.get("claim_id"), f"claim {index} claim_id")
        if claim_id in seen:
            raise PreflightError(f"duplicate claim_id: {claim_id}")
        seen.add(claim_id)
        independent_claim = verified_by_id.get(claim_id)
        if claim.get("status") != "pass" or claim.get("semantic_checks") is not True or not isinstance(independent_claim, dict) or independent_claim.get("semantic_checks") is not True:
            raise PreflightError(f"claim {claim_id} is not semantically verified")
        if independent_claim.get("old_hash") != claim.get("old_hash") or independent_claim.get("new_hash") != claim.get("new_hash"):
            raise PreflightError(f"claim {claim_id} independent verifier hashes do not match the diff")
        independent_claim_refs = independent_claim.get("evidence_refs")
        if not isinstance(independent_claim_refs, list) or not independent_claim_refs:
            raise PreflightError(f"claim {claim_id} independent evidence_refs must be non-empty")
        evidence_refs = claim.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise PreflightError(f"claim {claim_id} evidence_refs must be a non-empty list")
        for field in ("old_excerpt", "new_excerpt", "verifier"):
            if not isinstance(claim.get(field), str) or not claim[field].strip():
                raise PreflightError(f"claim {claim_id} lacks non-empty {field}")
        if grounding_mode == "text" and (claim["old_excerpt"] not in input_text or claim["new_excerpt"] not in candidate_text):
            raise PreflightError(f"claim {claim_id} excerpts are not grounded in the input/candidate files")
        if grounding_mode == "adapter" and (verifier_receipt.get("grounded_excerpts") is not True or independent.get("grounded_excerpts") is not True):
            raise PreflightError(f"claim {claim_id} lacks adapter-grounded excerpt evidence")
        protected_fields = claim.get("protected_fields")
        if not isinstance(protected_fields, list) or any(not isinstance(item, str) for item in protected_fields) or not set(CLAIM_PROTECTED_FIELDS).issubset(set(protected_fields)):
            raise PreflightError(f"claim {claim_id} lacks the complete protected-field inventory")
        for inventory_field in CLAIM_PROTECTED_FIELDS:
            value = claim.get(inventory_field)
            if not isinstance(value, list):
                raise PreflightError(f"claim {claim_id} lacks list field: {inventory_field}")
        ledger = claim.get("protected_field_ledger")
        if not isinstance(ledger, dict):
            raise PreflightError(f"claim {claim_id} lacks a protected-field verification ledger")
        for inventory_field in CLAIM_PROTECTED_FIELDS:
            entry = ledger.get(inventory_field)
            if not isinstance(entry, dict) or entry.get("status") != "verified" or not isinstance(entry.get("count"), int) or isinstance(entry.get("count"), bool) or entry.get("count") < 0:
                raise PreflightError(f"claim {claim_id} protected field {inventory_field} is not independently verified")
            if entry.get("count") != len(claim.get(inventory_field, [])):
                raise PreflightError(f"claim {claim_id} protected field {inventory_field} ledger count does not match the inventory")
        independent_fields = independent_claim.get("protected_fields")
        independent_ledger = independent_claim.get("protected_field_ledger")
        if not isinstance(independent_fields, list) or not set(CLAIM_PROTECTED_FIELDS).issubset(set(independent_fields)) or not isinstance(independent_ledger, dict):
            raise PreflightError(f"claim {claim_id} independent verifier lacks the complete protected-field ledger")
        for inventory_field in CLAIM_PROTECTED_FIELDS:
            independent_entry = independent_ledger.get(inventory_field)
            if not isinstance(independent_entry, dict) or independent_entry.get("status") != "verified" or independent_entry.get("count") != len(claim.get(inventory_field, [])):
                raise PreflightError(f"claim {claim_id} independent protected field {inventory_field} is not verified")
        for field, excerpt_field in (("old_hash", "old_excerpt"), ("new_hash", "new_excerpt")):
            value = claim.get(field)
            if value != sha256_text(claim[excerpt_field]) or not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise PreflightError(f"claim {claim_id} {field} does not match its excerpt")
    if set(verified_by_id) != seen:
        raise PreflightError("claim/evidence verifier receipt claim IDs do not exactly match the diff")
    return verifier_path


def verify_humanizer_skill(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise PreflightError(f"humanizer skill is not installed at: {path}")
    if path.name.lower() != "skill.md":
        raise PreflightError("humanizer skill override must point to a SKILL.md file")
    skill_hash = sha256_file(path)
    text = path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^name:\s*humanizer\s*$", text):
        raise PreflightError("selected skill is not the humanizer package")
    match = re.search(r"(?m)^\s+version:\s*[\"']?([^\"'\s]+)", text)
    if not match:
        raise PreflightError("humanizer skill metadata.version is missing")
    return skill_hash, match.group(1)


def verify_adapter_contract(
    path: Path,
    fmt: str,
    input_hash: str,
    input_path: Path,
    candidate: Path,
    candidate_hash: str,
    immutable_copy: Path,
    protected_manifest: Path,
    claim_diff: Path,
    integrity_report: Path,
    humanizer_skill_hash: str,
    humanizer_version: str,
    humanizer_skill_path: Path,
    mapping_receipt_hash: str | None,
    mapping_receipt_path: Path | None,
    claim_verifier_path: Path,
) -> None:
    data = load_json(path, "adapter contract")
    if data.get("schema_version") != ADAPTER_SCHEMA_VERSION or data.get("format") != fmt:
        raise PreflightError("adapter contract schema_version/format does not match the selected format")
    adapter_id = nonempty(data.get("adapter_id"), "adapter_id")
    contract_id = nonempty(data.get("contract_id"), "contract_id")
    nonempty(data.get("adapter_version"), "adapter_version")
    nonempty(data.get("parser_version"), "parser_version")
    entrypoint = Path(nonempty(data.get("entrypoint"), "entrypoint")).expanduser().resolve()
    verify_file_hash(entrypoint, data.get("entrypoint_sha256"), "adapter entrypoint")
    contract_root = path.parent.parent if path.parent.name == ".research" else path.parent
    try:
        entrypoint.relative_to(contract_root.resolve())
    except ValueError as exc:
        raise PreflightError("adapter entrypoint must stay inside the project workspace") from exc
    if data.get("humanizer_skill_sha256") != humanizer_skill_hash or data.get("humanizer_version") != humanizer_version:
        raise PreflightError("adapter contract is not bound to the selected humanizer skill hash/version")
    operations = data.get("operations")
    if not isinstance(operations, dict) or not all(operations.get(name) is True for name in REQUIRED_OPERATIONS):
        raise PreflightError("adapter contract does not prove all required operations")
    kinds = data.get("protected_kinds")
    if not isinstance(kinds, list) or any(not isinstance(item, str) for item in kinds) or not PROTECTED_KINDS.issubset(set(kinds)):
        raise PreflightError("adapter contract does not declare every protected-span kind")
    if data.get("network_scope") != "none":
        raise PreflightError("adapter contract must set network_scope: none for the local self-test")
    for capability in ("bounded_chunks", "immutable_original"):
        if data.get(capability) is not True:
            raise PreflightError(f"adapter contract lacks required capability: {capability}")
    for field, expected_path in (
        ("immutable_copy", immutable_copy),
        ("protected_manifest", protected_manifest),
        ("claim_diff", claim_diff),
        ("integrity_report", integrity_report),
        ("candidate", candidate),
    ):
        declared = Path(nonempty(data.get(field), field)).expanduser().resolve()
        if declared != expected_path.resolve():
            raise PreflightError(f"adapter contract {field} is not bound to the preflight input")
        verify_file_hash(declared, data.get(f"{field}_sha256"), f"adapter contract {field}")
    rollback_target = Path(nonempty(data.get("rollback_target"), "rollback_target")).expanduser().resolve()
    verify_file_hash(rollback_target, data.get("rollback_target_sha256"), "adapter rollback_target")
    self_test_path = Path(nonempty(data.get("self_test_report"), "self_test_report")).expanduser().resolve()
    verify_file_hash(self_test_path, data.get("self_test_report_sha256"), "adapter self-test report")
    self_test = load_json(self_test_path, "adapter self-test report")
    if self_test.get("schema_version") != ADAPTER_SCHEMA_VERSION or self_test.get("status") != "pass":
        raise PreflightError("adapter self-test report is not passing")
    if self_test.get("adapter_id") != adapter_id or self_test.get("format") != fmt:
        raise PreflightError("adapter self-test identity does not match the contract")
    if self_test.get("contract_id") != contract_id or self_test.get("entrypoint_sha256") != data.get("entrypoint_sha256"):
        raise PreflightError("adapter self-test is not bound to this contract and entrypoint")
    if self_test.get("tested_input_sha256") != input_hash:
        raise PreflightError("adapter self-test was not run on this exact input")
    if self_test.get("tested_candidate_sha256") != candidate_hash:
        raise PreflightError("adapter self-test was not run on this exact candidate")
    if self_test.get("humanizer_skill_sha256") != humanizer_skill_hash or self_test.get("humanizer_version") != humanizer_version:
        raise PreflightError("adapter self-test is not bound to the selected humanizer skill")
    if self_test.get("rollback_target_sha256") != data.get("rollback_target_sha256") or self_test.get("rollback_verified") is not True:
        raise PreflightError("adapter self-test did not prove the rollback target")
    if mapping_receipt_hash is not None and self_test.get("mapping_receipt_sha256") != mapping_receipt_hash:
        raise PreflightError("adapter self-test did not bind the adapter mapping receipt")
    if self_test.get("canonical_mutation_allowed") is not False:
        raise PreflightError("adapter self-test must prove canonical_mutation_allowed: false")
    if self_test.get("offset_mapping_verified") is not True:
        raise PreflightError("adapter self-test must prove offset_mapping_verified: true")
    if not all(self_test.get(name) is True for name in REQUIRED_OPERATIONS):
        raise PreflightError("adapter self-test did not pass every required operation")
    command = data.get("self_test_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise PreflightError("adapter contract self_test_command must be a non-empty argument list")
    required_placeholders = {"{input}", "{candidate}", "{contract_id}", "{self_test_report}"}
    if not required_placeholders.issubset(set(command)) or "--self-test" not in command:
        raise PreflightError("adapter self-test command must include --self-test and all bound path placeholders")
    replacements = {"{input}": str(input_path), "{candidate}": str(candidate), "{contract_id}": contract_id, "{self_test_report}": str(self_test_path)}
    command = [replacements.get(item, item) for item in command]
    if len(command) < 2:
        raise PreflightError("adapter self-test command must invoke an interpreter and the hashed entrypoint")
    try:
        interpreter = Path(command[0]).expanduser().resolve()
        invoked_entrypoint = Path(command[1]).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise PreflightError("adapter self-test command contains an invalid executable path") from exc
    if interpreter != Path(sys.executable).resolve() or invoked_entrypoint != entrypoint:
        raise PreflightError("adapter self-test command must invoke the current Python interpreter and the hashed entrypoint")
    if any(item in {"-c", "-m", "--eval", "--exec"} for item in command[2:]):
        raise PreflightError("adapter self-test command cannot evaluate inline code or launch another module")
    if any("\x00" in item for item in command):
        raise PreflightError("adapter self-test command contains a NUL byte")
    bound_paths = [path, input_path, candidate, immutable_copy, protected_manifest, claim_diff, integrity_report, rollback_target, self_test_path, entrypoint, humanizer_skill_path]
    if mapping_receipt_path is not None:
        bound_paths.append(mapping_receipt_path)
    bound_paths.append(claim_verifier_path)
    before_hashes = {bound: sha256_file(bound) for bound in bound_paths}
    safe_env = {name: os.environ[name] for name in ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "TEMP", "TMP") if name in os.environ}
    safe_env.update({"PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"})
    with tempfile.TemporaryDirectory(prefix="humanizer-self-test-") as probe_dir:
        probe_input = Path(probe_dir) / input_path.name
        probe_candidate = Path(probe_dir) / candidate.name
        shutil.copyfile(input_path, probe_input)
        shutil.copyfile(candidate, probe_candidate)
        command = [str(probe_input) if item == str(input_path) else str(probe_candidate) if item == str(candidate) else item for item in command]
        process_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            process_kwargs["start_new_session"] = True
        process: subprocess.Popen[str] | None = None
        job_state: tuple[object, object] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=str(probe_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                shell=False,
                env=safe_env,
                **process_kwargs,
            )
            job_state = _attach_kill_on_close_job(process)
            try:
                stdout, _stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired as exc:
                _terminate_self_test_process(process, job_state)
                job_state = None
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired as still_running:
                    raise PreflightError("adapter self-test descendants could not be terminated") from still_running
                raise PreflightError("adapter self-test exceeded the 10-second bound") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            _terminate_self_test_process(process, job_state)
            job_state = None
            raise PreflightError(f"adapter self-test could not execute: {exc}") from exc
        finally:
            _kill_posix_process_group(process)
            _close_kill_job(job_state)
        if process is None:
            raise PreflightError("adapter self-test process was not created")
        if process.returncode != 0:
            raise PreflightError(f"adapter self-test exited with status {process.returncode}")
        completed_stdout = stdout
    for bound, expected_hash in before_hashes.items():
        if not bound.is_file() or sha256_file(bound) != expected_hash:
            raise PreflightError(f"adapter self-test mutated a bound artifact: {bound}")
    if mapping_receipt_path is not None:
        verify_file_hash(mapping_receipt_path, mapping_receipt_hash, "adapter mapping receipt after self-test")
        mapping_after = load_json(mapping_receipt_path, "adapter mapping receipt after self-test")
        if mapping_after.get("status") != "pass" or mapping_after.get("input_sha256") != input_hash or mapping_after.get("offset_mapping_verified") is not True:
            raise PreflightError("adapter mapping receipt is not a passing post-self-test receipt")
    try:
        runtime_report = json.loads(completed_stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PreflightError("adapter self-test stdout must be a JSON object") from exc
    if not isinstance(runtime_report, dict) or runtime_report.get("status") != "pass":
        raise PreflightError("adapter self-test stdout is not passing")
    for field in ("contract_id", "entrypoint_sha256", "tested_input_sha256", "tested_candidate_sha256"):
        if runtime_report.get(field) != self_test.get(field):
            raise PreflightError(f"adapter self-test stdout does not match report field: {field}")
    if runtime_report.get("rollback_target_sha256") != data.get("rollback_target_sha256"):
        raise PreflightError("adapter self-test stdout does not bind the rollback target")
    if runtime_report.get("canonical_mutation_allowed") is not False or runtime_report.get("offset_mapping_verified") is not True:
        raise PreflightError("adapter self-test stdout did not prove mutation and offset safeguards")
    if runtime_report.get("humanizer_skill_sha256") != humanizer_skill_hash or runtime_report.get("humanizer_version") != humanizer_version:
        raise PreflightError("adapter self-test stdout is not bound to the selected humanizer skill")
    if not all(runtime_report.get(name) is True for name in REQUIRED_OPERATIONS):
        raise PreflightError("adapter self-test stdout did not pass every required operation")
    for field in ("schema_version", "status", "adapter_id", "format", "contract_id", "entrypoint_sha256", "tested_input_sha256", "tested_candidate_sha256", "humanizer_skill_sha256", "humanizer_version", "rollback_target_sha256", "rollback_verified", "canonical_mutation_allowed", "offset_mapping_verified", *REQUIRED_OPERATIONS):
        if runtime_report.get(field) != self_test.get(field):
            raise PreflightError(f"adapter self-test stdout does not match report field: {field}")
    if runtime_report.get("self_test_report_sha256") != data.get("self_test_report_sha256"):
        raise PreflightError("adapter self-test stdout does not prove report provenance")


def main() -> int:
    parser = JSONArgumentParser(description="Fail-closed preflight for a safe humanizer invocation")
    parser.add_argument("--input", required=True)
    parser.add_argument("--candidate", required=True, help="immutable humanized candidate produced by the adapter")
    parser.add_argument("--format", default="")
    parser.add_argument("--adapter-contract", required=True)
    parser.add_argument("--immutable-copy", required=True)
    parser.add_argument("--protected-manifest", required=True)
    parser.add_argument("--claim-diff", required=True)
    parser.add_argument("--integrity-report", required=True)
    parser.add_argument("--humanizer-skill", default="")
    args = parser.parse_args()

    try:
        path = Path(args.input).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({
            "input": str(args.input),
            "format": "unknown",
            "status": "blocked",
            "reason": str(exc),
            "canonical_mutation_allowed": False,
        }, ensure_ascii=False, indent=2))
        return 1
    result = {
        "input": str(path),
        "format": "unknown",
        "status": "blocked",
        "reason": "",
        "canonical_mutation_allowed": False,
    }
    try:
        if not path.is_file():
            raise PreflightError("input file does not exist or is not a file")
        fmt = detect_format(path, args.format)
        result["format"] = fmt
        input_hash = sha256_file(path)
        candidate = Path(args.candidate).expanduser().resolve()
        if candidate == path or not candidate.is_file():
            raise PreflightError("candidate must be a separate existing file from the canonical input")
        candidate_hash = sha256_file(candidate)
        if args.humanizer_skill:
            humanizer_candidates = [Path(args.humanizer_skill).expanduser().resolve()]
        else:
            humanizer_candidates = [
                Path(__file__).resolve().parents[2] / "humanizer" / "SKILL.md",
                Path.home() / ".codex" / "skills" / "humanizer" / "SKILL.md",
            ]
        humanizer_skill = next((candidate for candidate in humanizer_candidates if candidate.is_file()), None)
        if humanizer_skill is None:
            raise PreflightError("humanizer skill is not installed at any configured location")
        humanizer_skill_hash, humanizer_version = verify_humanizer_skill(humanizer_skill)
        immutable_path = Path(args.immutable_copy).expanduser().resolve()
        manifest_path = Path(args.protected_manifest).expanduser().resolve()
        claim_path = Path(args.claim_diff).expanduser().resolve()
        integrity_path = Path(args.integrity_report).expanduser().resolve()
        verify_immutable_copy(path, immutable_path, input_hash)
        mapping_receipt_hash, mapping_receipt_path = verify_manifest(manifest_path, fmt, path, input_hash, candidate, candidate_hash)
        claim_verifier_path = verify_claim_diff(claim_path, fmt, input_hash, candidate_hash, path, candidate)
        verify_integrity_report(integrity_path, input_hash, candidate_hash, sha256_file(manifest_path), sha256_file(claim_path))
        verify_adapter_contract(
            Path(args.adapter_contract).expanduser().resolve(),
            fmt,
            input_hash,
            path,
            candidate,
            candidate_hash,
            immutable_path,
            manifest_path,
            claim_path,
            integrity_path,
            humanizer_skill_hash,
            humanizer_version,
            humanizer_skill,
            mapping_receipt_hash,
            mapping_receipt_path,
            claim_verifier_path,
        )
        result["status"] = "ready"
        result["reason"] = "adapter handshake and all pre-edit evidence checks passed; canonical mutation remains forbidden"
    except (OSError, UnicodeError, ValueError, RuntimeError, PreflightError) as exc:
        result["reason"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
