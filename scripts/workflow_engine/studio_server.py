"""Authenticated loopback HTTP service for the local Workflow Studio."""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote_to_bytes, urlsplit

from .catalog import (
    CatalogError,
    discover_skills,
    load_validator_registry,
)
from .schema import WorkflowError, WorkflowIssue, validator_form_metadata
from .store import StoreError
from .validators import ValidatorError
from .fs import PathSafetyError
from scripts.workflow_manager import WorkflowService
from scripts.workflow_engine.receipts import ReceiptError


MAX_JSON_BODY = 2 * 1024 * 1024
MAX_JSON_NESTING = 128
_MAX_IDLE_TIMEOUT = 24 * 60 * 60
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_ROOT = Path(__file__).resolve().parents[2]
_PROJECTION_PATH = _ROOT / "references" / "workflows" / "official-v1.0-studio-projection.json"
_REGISTRY_PATH = _ROOT / "references" / "workflows" / "validator-registry.v1.json"


@dataclass(frozen=True)
class StudioConfig:
    project_root: Path
    asset_root: Path
    host: str = "127.0.0.1"
    port: int = 0
    session_token: str = ""
    csrf_token: str = ""
    idle_timeout_seconds: float = 900
    open_browser: bool = True
    skill_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("Workflow Studio must bind to 127.0.0.1")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("Workflow Studio port is invalid")
        for label, token in (("session", self.session_token), ("CSRF", self.csrf_token)):
            if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
                raise ValueError(f"Workflow Studio {label} token is invalid")
        if (
            isinstance(self.idle_timeout_seconds, bool)
            or not isinstance(self.idle_timeout_seconds, (int, float))
            or not 0 < self.idle_timeout_seconds <= _MAX_IDLE_TIMEOUT
        ):
            raise ValueError("Workflow Studio idle timeout is invalid")
        if not isinstance(self.open_browser, bool):
            raise ValueError("Workflow Studio browser setting is invalid")
        if not isinstance(self.skill_roots, tuple):
            raise ValueError("Workflow Studio Skill roots must be a tuple")


def envelope(*, status: str, data=None, errors=(), warnings=(), wrote_files=False) -> dict:
    """Return the stable API response shape used by the browser client."""
    return {
        "status": status,
        "data": data,
        "errors": list(errors),
        "warnings": list(warnings),
        "wrote_files": bool(wrote_files),
    }


class StudioAPIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        operation: str = "request",
        recovery: str = "Review the request and try again.",
        node_id: str = "",
        edge_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.operation = operation
        self.recovery = recovery
        self.node_id = node_id
        self.edge_id = edge_id


def _is_link(path: Path) -> bool:
    if path.is_symlink() or os.path.islink(path):
        return True
    if os.name == "nt" and path.exists():
        try:
            attributes = path.stat(follow_symlinks=False).st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def _regular_directory(path: Path, label: str) -> Path:
    supplied = Path(path).expanduser()
    if _is_link(supplied):
        raise ValueError(f"{label} must not be a symlink or reparse point")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} must be an existing directory") from exc
    if not resolved.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return resolved


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError("non-finite JSON number")


def _constant_time_equal_ascii(actual: str, expected: str) -> bool:
    """Compare protocol tokens without letting non-ASCII headers escape as 500s."""
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    try:
        return hmac.compare_digest(actual.encode("ascii"), expected.encode("ascii"))
    except UnicodeEncodeError:
        return False


def _json_nesting_is_bounded(raw: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_JSON_NESTING:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                return False
    return True


def _issue(
    code: str,
    message: str,
    *,
    operation: str,
    recovery: str,
    node_id: str = "",
    edge_id: str = "",
) -> dict:
    return {
        "code": str(code)[:128],
        "message": str(message)[:1000],
        "operation": operation,
        "recovery": recovery,
        "node_id": node_id if isinstance(node_id, str) else "",
        "edge_id": edge_id if isinstance(edge_id, str) else "",
    }


def _validation_issue(item: WorkflowIssue, operation: str, project_root: Path, skill_roots) -> dict:
    if isinstance(item, Mapping):
        code = item.get("code", "workflow.invalid")
        raw_message = item.get("message", "Workflow validation reported an issue.")
        node_id = item.get("node_id", "")
        edge_id = item.get("edge_id", "")
    else:
        code = item.code
        raw_message = item.message
        node_id = item.node_id
        edge_id = item.edge_id
    message = str(raw_message)
    for root in (project_root, *_ROOTS(skill_roots)):
        message = message.replace(str(root), "[local path]")
    # Catalog diagnostics can contain a Skill filename outside the selected
    # project roots. Keep their useful error category without exposing it.
    if isinstance(code, str) and code.startswith("catalog."):
        message = {
            "catalog.symlink_in_tree": "A local Skill tree contains a symbolic link or reparse point.",
            "catalog.symlink_escape": "A local Skill directory resolves outside its catalog root.",
            "catalog.ambiguous_skill": "Multiple installed Skills have the same identifier and different contents.",
        }.get(code, "The local Skill catalog contains an invalid entry.")
    recovery = (
        "Correct the highlighted workflow fields and validate again."
        if operation in {"validate", "compile", "activate"}
        else "Review the local Skill installation and retry."
    )
    return _issue(
        code,
        message,
        operation=operation,
        recovery=recovery,
        node_id=node_id,
        edge_id=edge_id,
    )


def _ROOTS(roots):
    for item in roots:
        try:
            yield Path(item).expanduser().resolve()
        except OSError:
            continue


def _safe_metadata(skill_file: Path) -> tuple[str, str]:
    """Read only bounded, plain-text name/description fields from frontmatter."""
    try:
        with skill_file.open("rb") as source:
            raw = source.read(256 * 1024)
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return "", ""
    lines = text.splitlines()[:80]
    if not lines or lines[0] != "---":
        return "", ""
    try:
        end = lines.index("---", 1)
    except ValueError:
        return "", ""
    values = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator and key in {"name", "description"}:
            values[key] = value.strip().strip("\"'")[:500]
    return values.get("name", ""), values.get("description", "")


class StudioApplication:
    """Small API boundary that delegates all workflow authority to the engine."""

    def __init__(self, config: StudioConfig, *, project_root: Path, asset_root: Path) -> None:
        self.config = config
        self.project_root = project_root
        self.asset_root = asset_root
        self.service = WorkflowService(project_root, skill_roots=config.skill_roots)
        self._sensitive_roots = (_ROOT, project_root, asset_root, *_ROOTS(self.service.skill_roots))
        self._mutation_lock = threading.Lock()

    def _safe_error(self, exc: Exception, operation: str) -> tuple[int, dict]:
        code = getattr(exc, "code", "studio.operation_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", code):
            code = "studio.operation_failed"
        message = str(exc)
        for root in self._sensitive_roots:
            message = message.replace(str(root), "[local path]")
        if any(root in message for root in ("/Users/", "/home/", "C:\\", "\\\\")):
            message = "The operation could not be completed because local state failed validation."
        message = message[:1000]
        status_by_code = {
            "activation.acknowledgement_mismatch": 409,
            "activation.draft_mismatch": 409,
            "activation.start_failed": 500,
            "activation.rollback_failed": 500,
            "http.invalid_request": 400,
            "run.already_active": 409,
            "run.not_found": 404,
            "selection.invalid": 409,
            "selection.journal_conflict": 409,
            "selection.journal_invalid": 409,
            "store.draft_missing": 404,
            "store.revision_conflict": 409,
            "studio.activation_conflict": 409,
            "studio.activation_missing": 404,
            "studio.activation_validation_blocked": 422,
        }
        http_status = status_by_code.get(code, 400)
        recovery = "Reload the current workflow state, review the reported issue, and retry."
        if code == "activation.start_failed":
            recovery = "Official mode was restored. Keep the project files and review local custom-run state before retrying."
        elif code == "activation.rollback_failed":
            recovery = "Do not retry activation yet. Preserve the project files and inspect the local workflow selection and run state."
        issue = _issue(
            code,
            message,
            operation=operation,
            recovery=recovery,
        )
        return http_status, envelope(
            status="error",
            errors=(issue,),
            wrote_files=code in {"activation.start_failed", "activation.rollback_failed"},
        )

    def _read_draft_if_present(self):
        path = self.service.store.paths.workflow
        if not path.exists() and not path.is_symlink():
            return None
        return self.service.load_draft()

    def _bootstrap(self) -> dict:
        summary = self.service.summary()
        draft = self._read_draft_if_present()
        return {
            "project_label": self.project_root.name,
            "mode": summary["mode"],
            "active_workflow": (
                {
                    "workflow_id": summary["workflow_id"],
                    "semantic_revision": summary["semantic_revision"],
                    "semantic_sha256": summary["semantic_sha256"],
                    "run_id": summary["run_id"],
                }
                if summary["mode"] == "custom"
                else None
            ),
            "document_revision": 0 if draft is None else draft["document_revision"],
            "csrf_token": self.config.csrf_token,
            "max_json_body_bytes": MAX_JSON_BODY,
        }

    def _catalog(self) -> tuple[dict, list[dict], list[dict]]:
        catalog = discover_skills(self.service.skill_roots, {})
        skills = []
        for catalog_id, identity in sorted(catalog.skills.items()):
            name, description = _safe_metadata(identity.root / identity.relative_path / "SKILL.md")
            skills.append({
                "catalog_id": catalog_id,
                "display_name": name or catalog_id.replace("-", " ").title(),
                "description": description,
                "relative_path": identity.relative_path,
                "skill_sha256": identity.skill_sha256,
                "tree_sha256": identity.tree_sha256,
                "locked": identity.locked,
                "ambiguous": False,
            })
        ambiguous_ids = set()
        for issue in catalog.errors:
            if issue.code == "catalog.ambiguous_skill":
                candidate_id = str(issue.message).rsplit(": ", 1)[-1]
                if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", candidate_id):
                    ambiguous_ids.add(candidate_id)
        for catalog_id in sorted(ambiguous_ids):
            skills.append({
                "catalog_id": catalog_id,
                "display_name": catalog_id.replace("-", " ").title(),
                "description": "",
                "relative_path": "",
                "skill_sha256": "",
                "tree_sha256": "",
                "locked": False,
                "ambiguous": True,
            })
        try:
            validators = load_validator_registry(_REGISTRY_PATH, _ROOT)
        except (CatalogError, OSError) as exc:
            issue = _issue(
                getattr(exc, "code", "validator.invalid_registry"),
                "The bundled validator registry could not be verified.",
                operation="catalog",
                recovery="Verify the installed Orchestrator files and retry.",
            )
            return {"skills": skills, "validators": []}, [issue], []
        validator_forms = validator_form_metadata()
        validator_data = [
            {
                "validator_id": item.validator_id,
                "script": item.script.relative_to(_ROOT).as_posix(),
                "sha256": item.sha256,
                "adapter": item.adapter,
                "input_schema": item.input_schema,
                "control_tags": list(item.control_tags),
                "outcomes": list(item.outcomes),
                **validator_forms.get(item.validator_id, {}),
            }
            for _key, item in sorted(validators.items())
        ]
        errors = [
            _validation_issue(item, "catalog", self.project_root, self.service.skill_roots)
            for item in catalog.errors
        ]
        warnings = [
            _validation_issue(item, "catalog", self.project_root, self.service.skill_roots)
            for item in catalog.warnings
        ]
        return {"skills": skills, "validators": validator_data}, errors, warnings

    def _projection(self) -> dict:
        raw = _PROJECTION_PATH.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        return {"projection": value, "sha256": hashlib.sha256(_canonical_json(value)).hexdigest()}

    @staticmethod
    def _require_keys(body: object, expected: set[str], operation: str) -> Mapping:
        if not isinstance(body, dict) or set(body) != expected:
            raise StudioAPIError(
                "http.invalid_request",
                "Request fields do not match the operation contract.",
                operation=operation,
                recovery="Review the operation fields and try again.",
            )
        return body

    def dispatch(self, method: str, path: str, body: object = None) -> tuple[int, dict, bool]:
        """Dispatch a pre-authenticated HTTP request; return status, payload, shutdown."""
        if (
            (method == "PUT" and path == "/api/workflow")
            or (method == "POST" and path in {"/api/activate", "/api/deactivate"})
        ):
            with self._mutation_lock:
                return self._dispatch_locked(method, path, body)
        return self._dispatch_locked(method, path, body)

    def _dispatch_locked(self, method: str, path: str, body: object = None) -> tuple[int, dict, bool]:
        try:
            if method == "GET" and path == "/api/bootstrap":
                return 200, envelope(status="pass", data=self._bootstrap()), False
            if method == "GET" and path == "/api/catalog":
                data, errors, warnings = self._catalog()
                status = "blocked" if errors else "pass"
                return 200, envelope(status=status, data=data, errors=errors, warnings=warnings), False
            if method == "GET" and path == "/api/projection":
                return 200, envelope(status="pass", data=self._projection()), False
            if method == "GET" and path == "/api/workflow":
                draft = self._read_draft_if_present()
                data = {
                    "workflow": None if draft is None else draft,
                    "document_revision": 0 if draft is None else draft["document_revision"],
                }
                return 200, envelope(status="pass", data=data), False
            if method == "POST" and path == "/api/validate":
                request = self._require_keys(body, {"workflow"}, "validate")
                result = self.service.validate_document(request["workflow"])
                errors = [
                    _validation_issue(item, "validate", self.project_root, self.service.skill_roots)
                    for item in result["errors"]
                ]
                warnings = [
                    _validation_issue(item, "validate", self.project_root, self.service.skill_roots)
                    for item in result["warnings"]
                ]
                data = {
                    "document_sha256": result["document_sha256"],
                    "semantic_sha256": result["semantic_sha256"],
                    "required_warning_codes": result["required_warning_codes"],
                }
                status = "blocked" if errors else "pass"
                return 200, envelope(status=status, data=data, errors=errors, warnings=warnings), False
            if method == "POST" and path == "/api/compile":
                request = self._require_keys(body, {"workflow"}, "compile")
                _document, compiled = self.service._compile(request["workflow"])
                errors = [
                    _validation_issue(item, "compile", self.project_root, self.service.skill_roots)
                    for item in compiled.errors
                ]
                warnings = [
                    _validation_issue(item, "compile", self.project_root, self.service.skill_roots)
                    for item in compiled.warnings
                ]
                if compiled.plan is None or errors:
                    return 200, envelope(status="blocked", errors=errors, warnings=warnings), False
                plan = compiled.plan
                preview = {
                    "workflow_id": plan.workflow_id,
                    "semantic_revision": plan.semantic_revision,
                    "semantic_sha256": plan.semantic_sha256,
                    "topological_order": list(plan.topological_order),
                    "entry_nodes": [node_id for node_id in plan.topological_order if plan.nodes[node_id].entry],
                    "max_parallelism": plan.max_parallelism,
                }
                return 200, envelope(status="pass", data=preview, warnings=warnings), False
            if method == "PUT" and path == "/api/workflow":
                request = self._require_keys(
                    body, {"workflow", "expected_document_revision"}, "save"
                )
                saved = self.service.save_draft(
                    request["workflow"],
                    expected_document_revision=request["expected_document_revision"],
                )
                return 200, envelope(
                    status="pass",
                    data={
                        "workflow": saved,
                        "document_revision": saved["document_revision"],
                        "semantic_revision": saved["semantic_revision"],
                    },
                    wrote_files=True,
                ), False
            if method == "POST" and path == "/api/activate":
                request = self._require_keys(
                    body,
                    {
                        "workflow_id",
                        "expected_document_revision",
                        "semantic_sha256",
                        "acknowledged_warning_codes",
                    },
                    "activate",
                )
                draft = self._read_draft_if_present()
                if draft is None:
                    raise StudioAPIError(
                        "studio.activation_missing",
                        "There is no saved workflow to activate.",
                        http_status=404,
                        operation="activate",
                        recovery="Save the current workflow and validate it before activation.",
                    )
                if (
                    draft["workflow_id"] != request["workflow_id"]
                    or not isinstance(request["workflow_id"], str)
                    or draft["document_revision"] != request["expected_document_revision"]
                    or type(request["expected_document_revision"]) is not int
                    or request["expected_document_revision"] < 0
                ):
                    raise StudioAPIError(
                        "studio.activation_conflict",
                        "The saved workflow changed since the activation preview.",
                        http_status=409,
                        operation="activate",
                        recovery="Reload the latest saved workflow, validate it, and review its warnings.",
                    )
                validation = self.service.validate_document(draft)
                if validation["status"] == "blocked":
                    errors = [
                        _validation_issue(item, "activate", self.project_root, self.service.skill_roots)
                        for item in validation["errors"]
                    ]
                    warnings = [
                        _validation_issue(item, "activate", self.project_root, self.service.skill_roots)
                        for item in validation["warnings"]
                    ]
                    return 200, envelope(status="blocked", errors=errors, warnings=warnings), False
                if validation["semantic_sha256"] != request["semantic_sha256"]:
                    raise StudioAPIError(
                        "studio.activation_conflict",
                        "The workflow semantic hash changed since validation.",
                        http_status=409,
                        operation="activate",
                        recovery="Validate the current saved workflow again before activation.",
                    )
                acknowledgements = request["acknowledged_warning_codes"]
                if not isinstance(acknowledgements, list) or not all(
                    isinstance(item, str) for item in acknowledgements
                ):
                    raise StudioAPIError(
                        "http.invalid_request",
                        "Warning acknowledgements must be a list of codes.",
                        operation="activate",
                    )
                if len(set(acknowledgements)) != len(acknowledgements):
                    raise StudioAPIError(
                        "http.invalid_request",
                        "Warning acknowledgement codes must be unique.",
                        operation="activate",
                    )
                if sorted(acknowledgements) != validation["required_warning_codes"]:
                    raise StudioAPIError(
                        "activation.acknowledgement_mismatch",
                        "The acknowledged warning set is not the current validation result.",
                        http_status=409,
                        operation="activate",
                        recovery="Review the current warnings and submit their exact codes.",
                    )
                activated = self.service.activate(
                    draft,
                    acknowledged_warning_codes=acknowledgements,
                    expected_document_revision=request["expected_document_revision"],
                )
                updated_draft = self.service.load_draft()
                return 200, envelope(
                    status="pass",
                    data={
                        "selection": activated["selection"],
                        "run_id": activated["run_id"],
                        "document_revision": updated_draft["document_revision"],
                    },
                    warnings=[
                        _validation_issue(item, "activate", self.project_root, self.service.skill_roots)
                        for item in validation["warnings"]
                    ],
                    wrote_files=True,
                ), False
            if method == "POST" and path == "/api/deactivate":
                self._require_keys(body, set(), "deactivate")
                result = self.service.deactivate()
                return 200, envelope(status="pass", data=result, wrote_files=True), False
            if method == "POST" and path == "/api/shutdown":
                self._require_keys(body, set(), "shutdown")
                return 200, envelope(
                    status="pass", data={"shutdown_requested": True}
                ), True
            known_paths = {
                "/api/bootstrap", "/api/catalog", "/api/projection", "/api/workflow",
                "/api/validate", "/api/compile", "/api/activate", "/api/deactivate",
                "/api/shutdown",
            }
            if path not in known_paths:
                return self._error("http.route_not_found", "API route was not found.", 404, "route")
            return self._error(
                "http.method_not_allowed",
                "HTTP method is not allowed for this API route.",
                405,
                "route",
            )
        except StudioAPIError as exc:
            issue = _issue(
                exc.code, str(exc), operation=exc.operation, recovery=exc.recovery,
                node_id=exc.node_id, edge_id=exc.edge_id,
            )
            return exc.http_status, envelope(status="error", errors=(issue,)), False
        except (WorkflowError, StoreError, ReceiptError, ValidatorError, PathSafetyError, CatalogError) as exc:
            status, payload = self._safe_error(exc, path.rsplit("/", 1)[-1])
            return status, payload, False
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            status, payload = self._safe_error(exc, path.rsplit("/", 1)[-1])
            return status, payload, False
        except Exception:
            issue = _issue(
                "studio.internal_error",
                "The Studio operation failed unexpectedly.",
                operation=path.rsplit("/", 1)[-1],
                recovery="Reload Studio. If the problem persists, review local diagnostics without sharing private project files.",
            )
            return 500, envelope(status="error", errors=(issue,)), False

    @staticmethod
    def _error(code: str, message: str, status: int, operation: str) -> tuple[int, dict, bool]:
        return status, envelope(
            status="error",
            errors=(
                _issue(
                    code,
                    message,
                    operation=operation,
                    recovery="Check the route and try again.",
                ),
            ),
        ), False

    def error(self, code: str, message: str, status: int, operation: str) -> tuple[int, dict, bool]:
        return self._error(code, message, status, operation)

    def static_file(self, raw_path: str) -> tuple[bytes, str] | None:
        try:
            decoded = unquote_to_bytes(raw_path).decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        if "\x00" in decoded or "\\" in decoded:
            return None
        if decoded == "/" or decoded == "":
            parts = ("index.html",)
        else:
            trimmed = decoded.lstrip("/")
            parts = tuple(trimmed.split("/"))
            if not parts or any(part in {"", ".", ".."} for part in parts):
                return None
        payload = self._read_static_parts(parts)
        if payload is None:
            return None
        content_type = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        return payload, content_type

    def _read_static_parts(self, parts: tuple[str, ...]) -> bytes | None:
        """Open bundled assets without following replaceable path components."""
        if (
            os.open in getattr(os, "supports_dir_fd", set())
            and hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
        ):
            descriptors = []
            try:
                directory_fd = os.open(
                    self.asset_root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                descriptors.append(directory_fd)
                for part in parts[:-1]:
                    directory_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    descriptors.append(directory_fd)
                file_fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                descriptors.append(file_fd)
                inspected = os.fstat(file_fd)
                if not stat.S_ISREG(inspected.st_mode) or inspected.st_nlink != 1:
                    return None
                with os.fdopen(os.dup(file_fd), "rb") as handle:
                    return handle.read()
            except OSError:
                return None
            finally:
                for descriptor in reversed(descriptors):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

        candidate = self.asset_root
        for part in parts:
            candidate = candidate / part
            if _is_link(candidate):
                return None
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.asset_root)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(resolved, flags)
            try:
                inspected = os.fstat(descriptor)
                if not stat.S_ISREG(inspected.st_mode) or inspected.st_nlink != 1:
                    return None
                with os.fdopen(os.dup(descriptor), "rb") as handle:
                    return handle.read()
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return None


class _StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, handler, application: StudioApplication, idle_timeout_seconds: float):
        self.application = application
        self.idle_timeout_seconds = idle_timeout_seconds
        self.request_timeout_seconds = _DEFAULT_REQUEST_TIMEOUT_SECONDS
        self._last_activity = time.monotonic()
        self._activity_lock = threading.Lock()
        self._active_requests = 0
        self._shutdown_lock = threading.Lock()
        self._shutdown_requested = False
        self._request_timeout_lock = threading.Lock()
        self._request_timeout_timers: dict[socket.socket, threading.Timer] = {}
        super().__init__(address, handler)

    def get_request(self):
        request, client_address = super().get_request()
        # A client that connects but never sends an HTTP request must not pin
        # an active-request slot or make server_close() wait forever.
        request.settimeout(self.request_timeout_seconds)
        timer = threading.Timer(
            self.request_timeout_seconds,
            self._expire_request,
            args=(request,),
        )
        timer.daemon = True
        with self._request_timeout_lock:
            self._request_timeout_timers[request] = timer
        timer.start()
        return request, client_address

    def _expire_request(self, request: socket.socket) -> None:
        with self._request_timeout_lock:
            timer = self._request_timeout_timers.pop(request, None)
        if timer is None:
            return
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish_request_input(self, request: socket.socket) -> None:
        """Stop the absolute read deadline once this request's body is complete."""
        with self._request_timeout_lock:
            timer = self._request_timeout_timers.pop(request, None)
        if timer is not None:
            timer.cancel()

    def _cancel_request_timeout(self, request: socket.socket) -> None:
        self.finish_request_input(request)

    def mark_activity(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def process_request(self, request, client_address) -> None:
        with self._activity_lock:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._cancel_request_timeout(request)
            with self._activity_lock:
                self._active_requests -= 1
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._cancel_request_timeout(request)
            with self._activity_lock:
                self._active_requests -= 1

    def serve_forever(self, poll_interval: float = 0.1) -> None:
        super().serve_forever(poll_interval=poll_interval)

    def request_shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True
        threading.Thread(target=self.shutdown, name="workflow-studio-shutdown", daemon=True).start()

    def service_actions(self) -> None:
        with self._activity_lock:
            idle_for = time.monotonic() - self._last_activity
            has_active_requests = self._active_requests > 0
        if not has_active_requests and idle_for >= self.idle_timeout_seconds:
            self.request_shutdown()


class _StudioRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PaperWorkflowStudio"
    sys_version = ""

    @property
    def studio_server(self) -> _StudioHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format, *args):
        # Request targets and headers can contain credentials; never log them.
        return

    def handle(self):
        try:
            super().handle()
        except (OSError, TimeoutError):
            # Socket read timeouts and peer disconnects are ordinary local
            # client failures. In particular, makefile.readline() may wrap a
            # timeout as OSError("cannot read from timed out object").
            return

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_HEAD(self):
        self._handle(head_only=True)

    def do_DELETE(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's default error page includes implementation
        # details; use the same bounded JSON error contract for parser failures.
        payload = self.studio_server.application.error(
            "http.protocol_error",
            "The HTTP request could not be parsed or is not supported.",
            int(code),
            "http",
        )[1]
        self._write_json(int(code), payload)

    def _write_headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _write_json(self, status: int, payload: dict) -> None:
        try:
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raw = b'{"data":null,"errors":[],"status":"error","warnings":[],"wrote_files":false}'
            status = 500
        self._write_headers(status, "application/json; charset=utf-8", len(raw))
        if self.command != "HEAD":
            self.wfile.write(raw)
            self.wfile.flush()

    def _authorization_ok(self) -> bool:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            return False
        header = values[0]
        expected = "Bearer " + self.studio_server.application.config.session_token
        return bool(header) and _constant_time_equal_ascii(header, expected)

    def _read_json_body(self, operation: str) -> tuple[int, object] | tuple[None, object]:
        if self.headers.get_all("Transfer-Encoding", []):
            return 400, self.studio_server.application.error(
                "http.transfer_encoding_unsupported",
                "Chunked request bodies are not accepted.", 400, operation,
            )[1]
        length_headers = self.headers.get_all("Content-Length", [])
        if not length_headers:
            return 411, self.studio_server.application.error(
                "http.content_length_required",
                "A bounded Content-Length is required.", 411, operation,
            )[1]
        if len(length_headers) != 1:
            return 400, self.studio_server.application.error(
                "http.invalid_content_length",
                "Only one Content-Length header is accepted.", 400, operation,
            )[1]
        raw_length = length_headers[0]
        if not raw_length.isascii() or not raw_length.isdigit():
            return 400, self.studio_server.application.error(
                "http.invalid_content_length",
                "Content-Length must be a non-negative integer.", 400, operation,
            )[1]
        if len(raw_length) > 10:
            return 413, self.studio_server.application.error(
                "http.body_too_large",
                "Request body exceeds the configured size limit.", 413, operation,
            )[1]
        length = int(raw_length)
        if length > MAX_JSON_BODY:
            return 413, self.studio_server.application.error(
                "http.body_too_large",
                "Request body exceeds the configured size limit.", 413, operation,
            )[1]
        content_type_headers = self.headers.get_all("Content-Type", [])
        if len(content_type_headers) != 1:
            return 415, self.studio_server.application.error(
                "http.invalid_content_type",
                "Exactly one application/json Content-Type is required.", 415, operation,
            )[1]
        content_type_parts = [item.strip().lower() for item in content_type_headers[0].split(";")]
        supported_parameters = {"charset=utf-8", 'charset="utf-8"'}
        if (
            content_type_parts[0] != "application/json"
            or any(parameter not in supported_parameters for parameter in content_type_parts[1:])
        ):
            return 415, self.studio_server.application.error(
                "http.invalid_content_type",
                "State-changing requests must use application/json.", 415, operation,
            )[1]
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("short request body")
            if not _json_nesting_is_bounded(raw):
                raise ValueError("JSON nesting exceeds the configured limit")
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_nonfinite,
            )
            if not isinstance(value, dict):
                raise ValueError("JSON request must be an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return 400, self.studio_server.application.error(
                "http.invalid_json",
                "Request body must be a UTF-8 JSON object without duplicate keys.", 400, operation,
            )[1]
        except (OSError, TimeoutError):
            return 408, self.studio_server.application.error(
                "http.request_timeout",
                "The request body was not received before the local request timeout.",
                408,
                operation,
            )[1]
        return None, value

    def _handle(self, *, head_only: bool = False) -> None:
        expected_host = f"127.0.0.1:{self.studio_server.server_address[1]}"
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1 or host_headers[0] != expected_host:
            self._write_json(*self.studio_server.application.error(
                "http.invalid_host", "Request Host does not match the bound loopback address.", 403, "host"
            )[:2])
            return

        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._write_json(*self.studio_server.application.error(
                "http.invalid_path", "Request path is invalid.", 400, "route"
            )[:2])
            return
        if parsed.scheme or parsed.netloc or parsed.query:
            self._write_json(*self.studio_server.application.error(
                "http.invalid_path", "Absolute request targets and query strings are not accepted.", 400, "route"
            )[:2])
            return

        path = parsed.path
        is_api = path == "/api" or path.startswith("/api/")
        if is_api:
            if not self._authorization_ok():
                self._write_json(*self.studio_server.application.error(
                    "http.invalid_session", "A valid Studio session token is required.", 401, "authentication"
                )[:2])
                return
            self.studio_server.mark_activity()
            if self.command != "GET" and self.command != "HEAD":
                origin_headers = self.headers.get_all("Origin", [])
                origin = origin_headers[0] if len(origin_headers) == 1 else ""
                expected_origin = f"http://127.0.0.1:{self.studio_server.server_address[1]}"
                if not origin_headers:
                    self._write_json(*self.studio_server.application.error(
                        "http.origin_required", "State-changing requests require an exact Origin.", 403, "origin"
                    )[:2])
                    return
                if len(origin_headers) != 1 or origin != expected_origin:
                    self._write_json(*self.studio_server.application.error(
                        "http.invalid_origin", "Request Origin does not match the Studio origin.", 403, "origin"
                    )[:2])
                    return
                csrf_headers = self.headers.get_all("X-Workflow-CSRF", [])
                csrf = csrf_headers[0] if len(csrf_headers) == 1 else ""
                if len(csrf_headers) != 1 or not csrf or not _constant_time_equal_ascii(
                    csrf, self.studio_server.application.config.csrf_token
                ):
                    self._write_json(*self.studio_server.application.error(
                        "http.invalid_csrf", "A valid CSRF token is required.", 403, "csrf"
                    )[:2])
                    return
                status, body = self._read_json_body(path.rsplit("/", 1)[-1])
                self.studio_server.finish_request_input(self.connection)
                if status is not None:
                    self._write_json(status, body)
                    return
            else:
                body = None
                self.studio_server.finish_request_input(self.connection)
            status, result, shutdown = self.studio_server.application.dispatch(
                self.command, path, body
            )
            self._write_json(status, result)
            if shutdown:
                self.studio_server.request_shutdown()
            return

        if self.command not in {"GET", "HEAD"}:
            self.studio_server.finish_request_input(self.connection)
            self._write_json(*self.studio_server.application.error(
                "http.method_not_allowed", "Static resources are available only with GET.", 405, "static"
            )[:2])
            return
        self.studio_server.finish_request_input(self.connection)
        result = self.studio_server.application.static_file(path)
        if result is None:
            self._write_json(*self.studio_server.application.error(
                "http.asset_not_found", "Requested static resource was not found.", 404, "static"
            )[:2])
            return
        payload, content_type = result
        self._write_headers(200, content_type, len(payload))
        if not head_only:
            self.wfile.write(payload)
            self.wfile.flush()


def create_server(config: StudioConfig) -> ThreadingHTTPServer:
    """Validate immutable launch inputs, bind loopback, and return a ready server."""
    project_root = _regular_directory(config.project_root, "Project root")
    asset_root = _regular_directory(config.asset_root, "Studio asset root")
    index = asset_root / "index.html"
    if _is_link(index) or not index.is_file():
        raise ValueError("Studio asset root must contain a regular index.html")
    application = StudioApplication(config, project_root=project_root, asset_root=asset_root)
    return _StudioHTTPServer(
        ("127.0.0.1", config.port),
        _StudioRequestHandler,
        application,
        float(config.idle_timeout_seconds),
    )


def serve(config: StudioConfig) -> None:
    """Serve one Studio session until idle, explicitly shut down, or interrupted."""
    server = create_server(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
