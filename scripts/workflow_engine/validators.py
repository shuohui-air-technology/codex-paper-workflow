"""Fixed custom-validator adapters; no workflow-controlled executable or flags.

The future manager passes a compiled validator node and frozen claim input
records (id/source_id/path/sha256). ``build_validator_argv`` validates their
identity, role mapping, project containment and current bytes. ``run_validator``
repeats those checks at execution entry and returns a bounded domain result or
an execution failure. The manager owns claim/relock authority and receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .catalog import CatalogError, load_validator_registry
from .compiler import CompiledNode
from .fs import PathSafetyError, resolve_project_path


MAX_STREAM_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 120.0
_FIXED = {
    "experiment-contract": ("experiment_contract_v1", "scripts/experiment_contract_validator.py", ("contract",)),
    "figure-contract": ("figure_contract_v1", "scripts/figure_contract_validator.py", ("receipt",)),
    "final-edit-receipt": ("final_edit_receipt_v1", "scripts/final_edit_receipt_validator.py", ("receipt",)),
    "paper-section": ("paper_section_v1", "scripts/paper_section_validator.py", ("file",)),
}
_PAPER_CHOICES = {
    "phase": frozenset({"body", "abstract", "final"}),
    "paper_type": frozenset({"empirical", "theoretical", "review", "protocol"}),
    "language": frozenset({"en", "zh"}),
    "method_profile": frozenset({"method-first", "data-first"}),
    "validity_status": frozenset({"pending", "clear", "blocked"}),
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_EXPERIMENT_FIELDS = (
    "objective", "metric", "direction", "baseline", "budget", "max_runs",
    "max_wall_time", "stop_conditions", "data_code_scope", "network_scope",
    "write_and_commit_policy", "report_destination", "validation_status",
    "approved_by", "user_confirmation", "stage_receipt", "stage_receipt_sha256",
    "validity_status",
)
_STRUCTURAL_LITERAL = frozenset({
    "missing required title heading",
    "abstract must be drafted only after the body is complete",
    "abstract phase requires a non-empty abstract",
    "Discussion section has no body content",
    "integrated Discussion function requires a Results or Analysis section body",
    "Discussion heading may be omitted only with an explicitly integrated discussion function",
    "method-first profile cannot locate a Results section after Methods",
    "data-first profile requires a Methods section",
})
_EVIDENCE_LITERAL = frozenset({
    "validity_status: blocked prevents section advancement",
    "final phase requires an independent semantic receipt",
    "semantic receipt must be readable UTF-8 JSON",
    "semantic receipt must contain status: pass",
    "semantic receipt must contain schema_version: 1",
    "semantic receipt must contain a verifier_id",
    "semantic receipt must contain a verifier_receipt_sha256",
    "semantic verifier receipt hash does not match",
    "semantic verifier receipt must contain status: pass",
    "semantic verifier identity does not match",
    "semantic verifier receipt manuscript_sha256 does not match",
    "semantic verifier receipt paper_type/language does not match",
    "semantic verifier receipt profile does not match",
    "semantic verifier receipt evidence_refs must be non-empty",
    "semantic verifier receipt must contain an independent checks object",
    "semantic verifier receipt must be a non-empty relative path",
    "semantic verifier receipt must be a safe relative path",
    "semantic verifier receipt resolves outside its receipt directory",
    "semantic verifier receipt sections must be a list",
    "semantic verifier receipt sections do not cover the required paper sections",
    "semantic receipt paper_type/language does not match the validator invocation",
    "semantic receipt method_profile does not match the validator invocation",
    "semantic receipt discussion_integrated does not match the validator invocation",
    "semantic receipt must contain validity_status: clear",
    "semantic receipt manuscript_sha256 does not match this manuscript",
    "semantic receipt evidence_refs must be a non-empty list",
    "semantic receipt sections must be a list of section names",
    "semantic receipt sections do not cover the required paper sections",
})
_SECTIONS = frozenset({"abstract", "keywords", "introduction", "methods", "results", "discussion", "conclusion", "references", "appendix"})
_DISCUSSION_GROUPS = frozenset({"interpretation/comparison", "application boundary", "limitations"})


class ValidatorError(ValueError):
    """Stable, bounded adapter/runner failure for a future manager receipt."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message[:400])
        self.code = code


@dataclass(frozen=True)
class ValidatorResult:
    """Normalized outcome; ``None`` means execution failure, never domain fail."""

    outcome: str | None
    summary: str
    error_code: str | None = None
    error_message: str | None = None


def _execution_failure(code: str, message: str) -> ValidatorResult:
    return ValidatorResult(None, "Validator execution failed.", code, message[:400])


def _regular_hash(path: Path) -> str:
    try:
        before = path.lstat()
        reparse = getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or reparse:
            raise ValidatorError("validator.unsafe_path", "Validator input is not a plain regular file.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValidatorError("validator.unsafe_path", "Validator input changed during open.")
                digest = hashlib.sha256()
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                after = os.fstat(handle.fileno())
                if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValidatorError("validator.input_changed", "Validator input changed during hashing.")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return digest.hexdigest()
    except ValidatorError:
        raise
    except (OSError, ValueError) as exc:
        raise ValidatorError("validator.unsafe_path", "Validator input cannot be read safely.") from exc


def _plain_root(value: Path, label: str) -> Path:
    """Reject linked/reparse ancestors before canonicalizing a trusted root."""
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        raise ValidatorError("validator.unsafe_path", f"{label} must be an absolute path.")
    try:
        for component in (*reversed(supplied.parents), supplied):
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValidatorError("validator.unsafe_path", f"{label} uses a link or reparse point.")
        resolved = supplied.resolve(strict=True)
        if not resolved.is_dir():
            raise ValidatorError("validator.unsafe_path", f"{label} is not a directory.")
        return resolved
    except ValidatorError:
        raise
    except OSError as exc:
        raise ValidatorError("validator.unsafe_path", f"{label} cannot be inspected safely.") from exc


def _checked_identity(node: CompiledNode, repository_root: Path) -> Path:
    if node.type != "validator" or node.validator is None or node.validator.validator_id == "humanizer-preflight":
        raise ValidatorError("validator.unavailable", "Validator is unavailable for custom execution.")
    identity = node.validator
    fixed = _FIXED.get(identity.validator_id)
    if fixed is None:
        raise ValidatorError("validator.unknown_id", "Validator has no fixed custom adapter.")
    adapter, relative, _ = fixed
    root = _plain_root(Path(repository_root), "Repository root")
    try:
        registry_path = resolve_project_path(root, "references/workflows/validator-registry.v1.json")
        if not registry_path.is_file() or registry_path.is_symlink():
            raise ValidatorError("validator.identity_changed", "Validator registry is unsafe.")
        registered = load_validator_registry(registry_path, root)[identity.validator_id]
        script = resolve_project_path(root, relative)
    except (CatalogError, PathSafetyError, OSError, KeyError) as exc:
        raise ValidatorError("validator.identity_changed", "Validator registry or script identity changed.") from exc
    if (identity.adapter != adapter or identity.input_schema != adapter or
            identity != registered or identity.script != script):
        raise ValidatorError("validator.identity_changed", "Compiled validator identity differs from the registry.")
    return script


def validate_validator_identity(node: CompiledNode, repository_root: Path) -> Path:
    """Confirm one compiled validator still matches its fixed registry entry."""
    return _checked_identity(node, repository_root)


def _checked_roles(node: CompiledNode, claim_inputs: Sequence[Mapping[str, object]], project_root: Path) -> dict[str, Path]:
    identity = node.validator
    assert identity is not None
    config = node.validator_config
    if not isinstance(config, Mapping) or set(config) != {"input_roles", "options"}:
        raise ValidatorError("validator.invalid_form", "Compiled validator form is incomplete.")
    roles, options = config["input_roles"], config["options"]
    if not isinstance(roles, Mapping) or not isinstance(options, Mapping):
        raise ValidatorError("validator.invalid_form", "Compiled validator form is invalid.")
    expected = set(_FIXED[identity.validator_id][2])
    if identity.validator_id == "paper-section":
        if set(options) != set(_PAPER_CHOICES) | {"discussion_integrated"}:
            raise ValidatorError("validator.invalid_form", "Paper validator options must be explicit.")
        for name, choices in _PAPER_CHOICES.items():
            if type(options[name]) is not str or options[name] not in choices:
                raise ValidatorError("validator.invalid_form", "Paper validator option is invalid.")
        if type(options["discussion_integrated"]) is not bool:
            raise ValidatorError("validator.invalid_form", "Paper discussion option is invalid.")
        if options["phase"] == "final":
            expected.add("semantic_receipt")
    elif options:
        raise ValidatorError("validator.invalid_form", "This validator has no options.")
    if set(roles) != expected or not all(type(value) is str and value for value in roles.values()):
        raise ValidatorError("validator.invalid_form", "Validator roles do not match its fixed form.")
    ids = tuple(roles.values())
    if len(set(ids)) != len(ids) or set(ids) != set(node.inputs) or len(node.inputs) != len(ids) or node.outputs:
        raise ValidatorError("validator.invalid_form", "Validator roles must match all declared inputs and no outputs.")
    if len(claim_inputs) != len(ids):
        raise ValidatorError("validator.input_mismatch", "Claim inputs do not match the compiled roles.")
    by_id: dict[str, Mapping[str, object]] = {}
    for claim in claim_inputs:
        if not isinstance(claim, Mapping) or set(claim) != {"id", "source_id", "path", "sha256"}:
            raise ValidatorError("validator.input_mismatch", "Claim input record is invalid.")
        artifact_id = claim["id"]
        if type(artifact_id) is not str or artifact_id in by_id or artifact_id not in ids:
            raise ValidatorError("validator.input_mismatch", "Claim input ID does not match a role.")
        if type(claim["source_id"]) is not str or not claim["source_id"]:
            raise ValidatorError("validator.input_mismatch", "Claim source ID is invalid.")
        if type(claim["path"]) is not str:
            raise ValidatorError("validator.input_mismatch", "Claim input path must be project-relative text.")
        by_id[artifact_id] = claim
    if set(by_id) != set(ids):
        raise ValidatorError("validator.input_mismatch", "Claim input IDs are incomplete.")
    resolved: dict[str, Path] = {}
    for role, artifact_id in roles.items():
        claim = by_id[artifact_id]
        if type(claim["sha256"]) is not str or not _HEX.fullmatch(claim["sha256"]):
            raise ValidatorError("validator.input_mismatch", "Claim input hash is invalid.")
        try:
            path = resolve_project_path(project_root, claim["path"])
        except (PathSafetyError, TypeError, ValueError) as exc:
            raise ValidatorError("validator.unsafe_path", "Claim input path is unsafe.") from exc
        if _regular_hash(path) != claim["sha256"]:
            raise ValidatorError("validator.input_changed", "Claim input bytes changed.")
        resolved[role] = path
    return resolved


def _argv(node: CompiledNode, roles: Mapping[str, Path], project_root: Path, script: Path) -> tuple[str, ...]:
    validator_id = node.validator.validator_id  # type: ignore[union-attr]
    if validator_id == "experiment-contract":
        args = ("--contract", str(roles["contract"]))
    elif validator_id == "figure-contract":
        args = ("--receipt", str(roles["receipt"]), "--project-root", str(project_root))
    elif validator_id == "final-edit-receipt":
        args = ("--receipt", str(roles["receipt"]))
    else:
        options = node.validator_config["options"]  # type: ignore[index]
        args = ("--file", str(roles["file"]), "--phase", options["phase"],
                "--paper-type", options["paper_type"], "--language", options["language"],
                "--method-profile", options["method_profile"], "--validity-status", options["validity_status"])
        if options["discussion_integrated"]:
            args += ("--discussion-integrated",)
        if options["phase"] == "final":
            args += ("--semantic-receipt", str(roles["semantic_receipt"]))
    return (sys.executable, str(script), *args)


def build_validator_argv(node: CompiledNode, claim_inputs: Sequence[Mapping[str, object]],
                         project_root: Path, repository_root: Path) -> tuple[str, ...]:
    """Return fixed argv after checking compiled identity and frozen claim bytes."""
    script = _checked_identity(node, repository_root)
    root = _plain_root(Path(project_root), "Project root")
    roles = _checked_roles(node, claim_inputs, root)
    return _argv(node, roles, root, script)


def _capture_bounded(argv: Sequence[str], cwd: Path, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes, bytes]:
    """Drain each child pipe concurrently, killing and reaping on limit/deadline."""
    env = {name: value for name in ("PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL")
           if (value := os.environ.get(name)) is not None}
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   start_new_session=os.name == "posix")
    except OSError as exc:
        raise ValidatorError("validator.launch_failed", "Validator process could not start.") from exc
    buffers = [bytearray(), bytearray()]
    over_limit = threading.Event()

    def drain(pipe, buffer: bytearray) -> None:
        try:
            while True:
                # BufferedReader.read(n) waits for n bytes or EOF. A live child
                # can exceed the cap by one byte yet stall here until timeout.
                # os.read returns currently available pipe bytes promptly.
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    break
                remaining = MAX_STREAM_BYTES + 1 - len(buffer)
                buffer.extend(chunk[:remaining])
                if len(buffer) > MAX_STREAM_BYTES:
                    over_limit.set()
                    return
        except OSError:
            over_limit.set()

    assert process.stdout is not None and process.stderr is not None
    threads = [threading.Thread(target=drain, args=(process.stdout, buffers[0]), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, buffers[1]), daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    reason: str | None = None
    try:
        while any(thread.is_alive() for thread in threads) or process.poll() is None:
            if over_limit.is_set():
                reason = "validator.output_limit"
                break
            if time.monotonic() >= deadline:
                reason = "validator.timeout"
                break
            for thread in threads:
                thread.join(.01)
        if reason is not None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
        process.wait()
    finally:
        process.stdout.close()
        process.stderr.close()
        for thread in threads:
            thread.join(timeout=1)
        if process.poll() is None:
            process.kill()
            process.wait()
    if reason == "validator.output_limit" or over_limit.is_set():
        raise ValidatorError("validator.output_limit", "Validator stdout or stderr exceeded 1 MiB.")
    if reason == "validator.timeout":
        raise ValidatorError("validator.timeout", "Validator exceeded its time limit.")
    return process.returncode, bytes(buffers[0]), bytes(buffers[1])


def _strict_json(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_STREAM_BYTES:
        raise ValidatorError("validator.invalid_output", "Validator returned no bounded JSON object.")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValidatorError("validator.invalid_output", "Validator returned invalid JSON.") from exc
    if not isinstance(value, dict) or len(encoded) > MAX_STREAM_BYTES:
        raise ValidatorError("validator.invalid_output", "Validator output is not one bounded JSON object.")
    return value


def _string_list(value: object, *, nonempty: bool = False) -> bool:
    return isinstance(value, list) and (not nonempty or bool(value)) and all(type(item) is str and bool(item) for item in value)


def _paper_error_kind(message: str) -> str | None:
    if message in _EVIDENCE_LITERAL:
        return "blocked"
    for name in ("discussion_function", "conclusion_function", "abstract_consistency"):
        if message in {f"semantic verifier receipt must contain {name}: pass", f"semantic receipt must contain {name}: pass"}:
            return "blocked"
    if message in _STRUCTURAL_LITERAL:
        return "fail"
    if re.fullmatch(r"(?:missing required section|final manuscript missing required section|required section has no body content): (?:abstract|introduction|methods|results|conclusion|references)", message):
        return "fail"
    if re.fullmatch(r"section order invalid: [a-z]+ must precede [a-z]+", message):
        match = re.fullmatch(r"section order invalid: ([a-z]+) must precede ([a-z]+)", message)
        assert match is not None
        order = ("abstract", "keywords", "introduction", "methods", "results", "discussion", "conclusion", "references", "appendix")
        if (match[1], match[2]) in set(zip(order, order[1:])):
            return "fail"
    prefix = "Discussion function lacks: "
    if message.startswith(prefix):
        groups = message[len(prefix):].split(", ")
        if groups and len(groups) == len(set(groups)) and set(groups) <= _DISCUSSION_GROUPS:
            return "fail"
    return None


def normalize_validator_output(node: CompiledNode, returncode: int, stdout: bytes,
                               role_paths: Mapping[str, Path]) -> ValidatorResult:
    """Classify only documented JSON/exit-code pairs from the four fixed scripts."""
    try:
        result = _strict_json(stdout)
    except ValidatorError as exc:
        return _execution_failure(exc.code, str(exc))
    validator_id = node.validator.validator_id if node.validator is not None else ""
    if returncode not in (0, 1):
        return _execution_failure("validator.returncode", "Validator returned an unsupported exit code.")
    errors = result.get("errors")
    if not _string_list(errors):
        return _execution_failure("validator.invalid_shape", "Validator errors field is invalid.")
    if validator_id in {"experiment-contract", "figure-contract", "final-edit-receipt"}:
        status = result.get("status")
        if validator_id == "experiment-contract":
            if result.get("contract") != str(role_paths.get("contract")):
                return _execution_failure("validator.path_mismatch", "Experiment result names another contract.")
            expected_pass = {"status", "contract", "errors", "validated_fields", "project_root", "scope_paths_pending"}
            expected_blocked = {"status", "contract", "errors"}
            contract = role_paths["contract"]
            expected_root = contract.parent.parent if contract.parent.name == ".research" else contract.parent
            if status == "pass" and (set(result) != expected_pass or result.get("validated_fields") != list(_EXPERIMENT_FIELDS) or
                                     not _string_list(result.get("scope_paths_pending")) or result.get("project_root") != str(expected_root)):
                return _execution_failure("validator.invalid_shape", "Experiment pass shape is invalid.")
            if status == "blocked" and set(result) != expected_blocked:
                return _execution_failure("validator.invalid_shape", "Experiment blocked shape is invalid.")
        elif validator_id == "figure-contract":
            if set(result) != {"status", "errors"}:
                return _execution_failure("validator.invalid_shape", "Figure result shape is invalid.")
        else:
            if set(result) != {"status", "receipt", "mode", "errors"} or result.get("receipt") != str(role_paths.get("receipt")) or not (result["mode"] is None or type(result["mode"]) is str):
                return _execution_failure("validator.invalid_shape", "Final-edit result shape or receipt binding is invalid.")
            if status == "pass" and result["mode"] not in {"revise", "audit", "learn"}:
                return _execution_failure("validator.invalid_shape", "Final-edit pass mode is invalid.")
        if status == "pass" and returncode == 0 and not errors:
            return ValidatorResult("pass", "Validator passed.")
        if status == "blocked" and returncode == 1 and errors:
            return ValidatorResult("blocked", "Validator reported blocked evidence.")
        return _execution_failure("validator.contradiction", "Validator status and exit code disagree.")
    if validator_id == "paper-section":
        if set(result) != {"valid", "errors", "warnings", "sections", "file"} or type(result["valid"]) is not bool or result["file"] != str(role_paths.get("file")) or not _string_list(result["warnings"]) or not _string_list(result["sections"]):
            return _execution_failure("validator.invalid_shape", "Paper result shape or file binding is invalid.")
        if not all(section in _SECTIONS for section in result["sections"]):
            return _execution_failure("validator.invalid_shape", "Paper sections are invalid.")
        if result["valid"] is True and returncode == 0 and not errors:
            return ValidatorResult("pass", "Paper structure passed.")
        if result["valid"] is False and returncode == 1 and errors:
            kinds = [_paper_error_kind(error) for error in errors]
            if any(kind is None for kind in kinds):
                return _execution_failure("validator.unknown_error", "Paper validator returned an unknown error rule.")
            return ValidatorResult("blocked" if "blocked" in kinds else "fail",
                                   "Paper evidence is blocked." if "blocked" in kinds else "Paper structure failed.")
        return _execution_failure("validator.contradiction", "Paper validity and exit code disagree.")
    return _execution_failure("validator.unknown_id", "Validator has no fixed normalizer.")


def run_validator(node: CompiledNode, claim_inputs: Sequence[Mapping[str, object]],
                  project_root: Path, repository_root: Path) -> ValidatorResult:
    """Run one trusted validator; raise on prelaunch claim/identity errors."""
    # Identity and hashes are checked immediately before launch, including
    # legacy/direct Humanizer attempts (which fail before any process exists).
    argv = build_validator_argv(node, claim_inputs, project_root, repository_root)
    root = _plain_root(Path(project_root), "Project root")
    roles = _checked_roles(node, claim_inputs, root)
    try:
        returncode, stdout, _stderr = _capture_bounded(argv, root)
    except ValidatorError as exc:
        return _execution_failure(exc.code, str(exc))
    return normalize_validator_output(node, returncode, stdout, roles)
