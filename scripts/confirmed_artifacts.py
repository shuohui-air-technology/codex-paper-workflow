"""Explicitly accepted artifact bundles with immutable evidence and rebuildable views.

Callers acquire the workflow/mode lock before entering this store. The catalog
lock never acquires a workflow or progress lock. Sources are retained because
existing run receipts may still bind their paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .workflow_engine.fs import (
    PathSafetyError,
    _fsync_directory,
    _relative_parts,
    _validate_windows_project_parts,
    atomic_write_json,
    ensure_project_directory,
    hash_project_file,
    read_project_json_object,
    resolve_project_path,
)


REQUEST_SCHEMA = "confirmed-artifact-request-v1"
CATALOG_SCHEMA = "confirmed-artifact-catalog-v1"
MAX_FILES = 128
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_PROVENANCE_BYTES = 64 * 1024
MAX_OPERATIONS = 5000
_BASE = ".research/confirmed-artifacts"
_CATALOG = f"{_BASE}/catalog.json"
_PROJECTION = f"{_BASE}/projection.json"
_CURRENT = "artifacts/current"
_INDEX = "artifacts/INDEX.md"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^v[0-9]{8,}-[0-9a-f]{16}$")
_LOCK_REGISTRY: set[tuple[str, int, int]] = set()
_REGISTRY_LOCK = threading.Lock()


class ConfirmedArtifactError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code, message):
    raise ConfirmedArtifactError(f"confirmed.{code}", message)


def _bytes(value, limit=MAX_CATALOG_BYTES):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConfirmedArtifactError("confirmed.invalid_json", "Metadata must be finite JSON data.") from exc
    if len(raw) > limit:
        _fail("metadata_too_large", "Metadata exceeds its bounded size.")
    return raw


def _clone(value, limit=MAX_CATALOG_BYTES):
    return json.loads(_bytes(value, limit))


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _keys(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _fail("invalid_schema", "Metadata has missing or unknown fields.")


def _identifier(value):
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        _fail("invalid_identifier", "Identifiers must use lowercase letters, digits, underscores or hyphens.")
    _path(value)
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        _fail("invalid_revision", "Catalog revision must be a non-negative integer.")
    return value


def _text(value, limit=4000):
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        _fail("invalid_text", "A bounded, non-empty confirmation or reason is required.")
    return value


def _hash(value):
    if type(value) is not str or not _SHA256.fullmatch(value):
        _fail("invalid_hash", "SHA-256 must contain 64 lowercase hexadecimal characters.")
    return value


def _path(value):
    if type(value) is not str or len(value) > 1024 or unicodedata.normalize("NFC", value) != value:
        _fail("unsafe_path", "Paths must use bounded normalized relative spelling.")
    try:
        parts = _relative_parts(value)
        _validate_windows_project_parts(parts)
    except PathSafetyError as exc:
        raise ConfirmedArtifactError("confirmed.unsafe_path", str(exc)) from exc
    if any(len(part.encode("utf-8")) > 240 for part in parts):
        _fail("unsafe_path", "A path component is too long for portable storage.")
    return value


def _check_collisions(paths):
    seen = set()
    for path in paths:
        key = unicodedata.normalize("NFC", path).casefold()
        if key in seen:
            _fail("duplicate_path", "Duplicate or case-equivalent file paths are forbidden.")
        seen.add(key)
    for key in seen:
        if any("/".join(key.split("/")[:index]) in seen for index in range(1, len(key.split("/")))):
            _fail("duplicate_path", "A file path cannot also be a parent directory.")


def normalize_request(value):
    """Validate and copy a request, retaining all coordination/evidence fields."""
    _bytes(value, MAX_REQUEST_BYTES)
    _keys(value, {
        "schema_version", "operation_id", "artifact_id", "artifact_type",
        "expected_catalog_revision", "entrypoint", "files", "confirmation",
    }, {"expected_progress_sha256", "evidence"})
    if value["schema_version"] != REQUEST_SCHEMA:
        _fail("invalid_schema", "Unsupported confirmation request version.")
    for key in ("operation_id", "artifact_id", "artifact_type"):
        _identifier(value[key])
    _revision(value["expected_catalog_revision"])
    _path(value["entrypoint"])
    _text(value["confirmation"])
    if type(value["files"]) is not list or not 1 <= len(value["files"]) <= MAX_FILES:
        _fail("invalid_files", f"Declare between 1 and {MAX_FILES} bundle files.")
    for item in value["files"]:
        _keys(item, {"source_path", "relative_path", "sha256"}, {"source_artifact_id"})
        _path(item["source_path"])
        source_key = item["source_path"].casefold()
        current_key = _CURRENT.casefold()
        if source_key == _INDEX.casefold() or source_key == current_key or source_key.startswith(current_key + "/"):
            _fail("mutable_projection_source", "Select the original working source or an immutable resolved snapshot, not artifacts/current.")
        _path(item["relative_path"])
        _hash(item["sha256"])
        if "source_artifact_id" in item:
            _identifier(item["source_artifact_id"])
    _check_collisions([item["source_path"] for item in value["files"]])
    _check_collisions([item["relative_path"] for item in value["files"]])
    if value["entrypoint"] not in {item["relative_path"] for item in value["files"]}:
        _fail("invalid_entrypoint", "Entrypoint must name a declared bundle file.")
    if "expected_progress_sha256" in value:
        _hash(value["expected_progress_sha256"])
    if "evidence" in value:
        if type(value["evidence"]) is not list or len(value["evidence"]) > MAX_FILES:
            _fail("invalid_evidence", "Evidence must be a bounded list.")
        for item in value["evidence"]:
            _keys(item, {"path", "sha256"})
            _path(item["path"])
            _hash(item["sha256"])
        _check_collisions([item["path"] for item in value["evidence"]])
    return _clone(value, MAX_REQUEST_BYTES)


validate_request = normalize_request


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_json", "Duplicate JSON fields are forbidden.")
        result[key] = value
    return result


class ConfirmedArtifactStore:
    def __init__(self, project_root):
        supplied = Path(project_root).expanduser()
        if supplied.is_symlink():
            _fail("unsafe_path", "Project root must be a plain directory.")
        try:
            self.root = supplied.resolve(strict=True)
            if not self.root.is_dir():
                _fail("unsafe_path", "Project root must be an existing directory.")
            info = self.root.stat()
            self._root_identity = (info.st_dev, info.st_ino)
        except OSError as exc:
            raise ConfirmedArtifactError("confirmed.unsafe_path", "Project root is unavailable.") from exc
        self.catalog_path = self.root / _CATALOG
        self.index_path = self.root / _INDEX

    def _checked(self, relative):
        try:
            info = self.root.lstat()
            if (info.st_dev, info.st_ino) != self._root_identity or not stat.S_ISDIR(info.st_mode):
                _fail("unsafe_path", "Project root changed during the operation.")
            return resolve_project_path(self.root, _path(relative))
        except (OSError, PathSafetyError) as exc:
            raise ConfirmedArtifactError("confirmed.unsafe_path", str(exc)) from exc

    def _mkdir(self, relative):
        self._checked(relative)
        try:
            return ensure_project_directory(self.root, relative)
        except PathSafetyError as exc:
            raise ConfirmedArtifactError("confirmed.unsafe_path", str(exc)) from exc

    @contextmanager
    def _lock(self, timeout=20):
        key = (str(self.root), os.getpid(), threading.get_ident())
        with _REGISTRY_LOCK:
            if key in _LOCK_REGISTRY:
                _fail("lock_reentrant", "The catalog lock is already held by this thread.")
            _LOCK_REGISTRY.add(key)
        descriptor = None
        acquired = False
        try:
            self._mkdir(_BASE)
            path = self._checked(f"{_BASE}/.lock")
            if path.exists():
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    _fail("unsafe_path", "Catalog lock is not a plain file.")
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0), 0o600)
            opened = os.fstat(descriptor)
            named = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                _fail("unsafe_path", "Catalog lock changed while opening.")
            if opened.st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
                _fsync_directory(path.parent)
            started = time.monotonic()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() - started >= timeout:
                        raise ConfirmedArtifactError("confirmed.lock_timeout", "Catalog is busy; retry later.") from exc
                    time.sleep(0.05)
            current = self._checked(f"{_BASE}/.lock").lstat()
            if current.st_nlink != 1 or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                _fail("unsafe_path", "Catalog lock was replaced while acquiring it.")
            yield
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        if os.name == "nt":
                            import msvcrt
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            with _REGISTRY_LOCK:
                _LOCK_REGISTRY.discard(key)

    def _read_json(self, relative):
        path = self._checked(relative)
        if not path.exists():
            return None
        try:
            if path.lstat().st_size > MAX_CATALOG_BYTES:
                _fail("metadata_too_large", "Metadata exceeds its bounded size.")
            return read_project_json_object(self.root, relative)
        except (OSError, PathSafetyError, ValueError, UnicodeError, RecursionError) as exc:
            raise ConfirmedArtifactError("confirmed.invalid_json", "Metadata must be stable, bounded, plain JSON object data.") from exc

    def _catalog(self):
        value = self._read_json(_CATALOG)
        if value is None:
            return {"schema_version": CATALOG_SCHEMA, "revision": 0, "artifacts": {}, "operations": {}}
        _keys(value, {"schema_version", "revision", "artifacts", "operations"})
        if value["schema_version"] != CATALOG_SCHEMA:
            _fail("invalid_catalog", "Unsupported confirmed-artifact catalog.")
        _revision(value["revision"])
        if type(value["artifacts"]) is not dict or type(value["operations"]) is not dict or len(value["operations"]) > MAX_OPERATIONS:
            _fail("invalid_catalog", "Catalog collections are invalid.")
        if value["revision"] != len(value["operations"]):
            _fail("invalid_catalog", "Catalog revision does not match its operation history.")
        operation_revisions = set()
        latest = {}
        for operation_id, operation in value["operations"].items():
            _identifier(operation_id)
            _keys(operation, {"action", "request_sha256", "artifact_id", "version_id", "catalog_revision"})
            _identifier(operation["artifact_id"])
            _hash(operation["request_sha256"])
            revision = _revision(operation["catalog_revision"])
            if type(operation["action"]) is not str or operation["action"] not in {"accept", "withdraw"} or not 1 <= revision <= value["revision"] or revision in operation_revisions:
                _fail("invalid_catalog", "Catalog operation identity is invalid.")
            operation_revisions.add(revision)
            if operation["version_id"] is not None and (type(operation["version_id"]) is not str or not _VERSION.fullmatch(operation["version_id"])):
                _fail("invalid_catalog", "Catalog version identifier is invalid.")
            artifact = value["artifacts"].get(operation["artifact_id"])
            if type(artifact) is not dict or (operation["action"] == "withdraw") != (operation["version_id"] is None):
                _fail("invalid_catalog", "Operation does not identify a valid artifact state.")
            prior = latest.get(operation["artifact_id"])
            if prior is None or prior[1]["catalog_revision"] < revision:
                latest[operation["artifact_id"]] = (operation_id, operation)
        for artifact_id, artifact in value["artifacts"].items():
            _identifier(artifact_id)
            _keys(artifact, {"artifact_type", "current_version", "versions", "withdrawal"})
            _identifier(artifact["artifact_type"])
            if type(artifact["versions"]) is not dict or not artifact["versions"]:
                _fail("invalid_catalog", "An artifact must contain accepted version metadata.")
            for version_id, version in artifact["versions"].items():
                if type(version_id) is not str or not _VERSION.fullmatch(version_id):
                    _fail("invalid_catalog", "Version path is invalid.")
                _keys(version, {"request", "provenance", "accepted_at", "sizes"})
                request = normalize_request(version["request"])
                if request["artifact_id"] != artifact_id or request["artifact_type"] != artifact["artifact_type"]:
                    _fail("invalid_catalog", "Version does not belong to its artifact.")
                if type(version["provenance"]) is not dict:
                    _fail("invalid_catalog", "Version provenance must be a JSON object.")
                _bytes(version["provenance"], MAX_PROVENANCE_BYTES)
                _text(version["accepted_at"], 64)
                sizes = version["sizes"]
                if type(sizes) is not dict or set(sizes) != {item["relative_path"] for item in request["files"]} or any(type(size) is not int or not 0 <= size <= MAX_FILE_BYTES for size in sizes.values()) or sum(sizes.values()) > MAX_BUNDLE_BYTES:
                    _fail("invalid_catalog", "Version file sizes are invalid.")
                operation = value["operations"].get(request["operation_id"])
                if not operation or operation["action"] != "accept" or operation["artifact_id"] != artifact_id or operation["version_id"] != version_id or operation["request_sha256"] != _digest(request) or request["expected_catalog_revision"] != operation["catalog_revision"] - 1:
                    _fail("invalid_catalog", "Version lacks its matching confirmation operation.")
            current = artifact["current_version"]
            if current is not None and (type(current) is not str or current not in artifact["versions"]):
                _fail("invalid_catalog", "The current version is not in accepted history.")
            latest_id, latest_operation = latest[artifact_id]
            if current != latest_operation["version_id"]:
                _fail("invalid_catalog", "Current selection must match the latest catalog operation; old versions are not a fallback.")
            withdrawal = artifact["withdrawal"]
            if withdrawal is not None:
                _keys(withdrawal, {"operation_id", "reason"})
                _identifier(withdrawal["operation_id"])
                _text(withdrawal["reason"])
                operation = value["operations"].get(withdrawal["operation_id"])
                expected_withdrawal = {"artifact_id": artifact_id, "expected_revision": latest_operation["catalog_revision"] - 1,
                                       "operation_id": latest_id, "reason": withdrawal["reason"], "action": "withdraw"}
                if current is not None or not operation or operation["action"] != "withdraw" or operation["artifact_id"] != artifact_id or withdrawal["operation_id"] != latest_id or operation["request_sha256"] != _digest(expected_withdrawal):
                    _fail("invalid_catalog", "Withdrawal does not match the artifact state.")
            elif current is None:
                _fail("invalid_catalog", "A withdrawn artifact requires its explicit withdrawal record.")
        for operation in value["operations"].values():
            if operation["action"] == "accept" and operation["version_id"] not in value["artifacts"][operation["artifact_id"]]["versions"]:
                _fail("invalid_catalog", "An acceptance operation is missing its version.")
        return value

    def _projection(self):
        value = self._read_json(_PROJECTION)
        if value is None:
            return {"catalog_revision": -1, "files": {}}
        _keys(value, {"catalog_revision", "files"})
        _revision(value["catalog_revision"])
        if type(value["files"]) is not dict:
            _fail("projection_invalid", "Projection ownership metadata is invalid.")
        for path, digest in value["files"].items():
            _path(path)
            _hash(digest)
            if path != _INDEX and not path.startswith(_CURRENT + "/"):
                _fail("projection_invalid", "Projection ownership names an unrelated file.")
        _check_collisions(value["files"])
        return value

    def _snapshot_root(self, artifact_id, version_id):
        return f"{_BASE}/versions/{artifact_id}/{version_id}"

    def _current_root(self, artifact_id, artifact_type):
        return f"{_CURRENT}/{artifact_type}/{artifact_id}"

    def _verify_snapshot(self, artifact_id, version_id, version):
        root = self._snapshot_root(artifact_id, version_id)
        for item in version["request"]["files"]:
            relative = f'{root}/{item["relative_path"]}'
            try:
                info = self._checked(relative).lstat()
                if info.st_size != version["sizes"][item["relative_path"]] or hash_project_file(self.root, relative) != item["sha256"]:
                    _fail("snapshot_changed", f'Confirmed snapshot differs from its accepted hash: {item["relative_path"]}')
            except (OSError, PathSafetyError) as exc:
                raise ConfirmedArtifactError("confirmed.snapshot_unavailable", f'Confirmed snapshot is unavailable: {item["relative_path"]}') from exc

    def status(self, *, verify=False):
        if type(verify) is not bool:
            _fail("invalid_schema", "verify must be boolean.")
        catalog = self._catalog()
        projection_errors = []
        try:
            projection = self._projection()
            pending = projection["catalog_revision"] != catalog["revision"] if catalog["revision"] else False
        except ConfirmedArtifactError as exc:
            pending = True
            projection_errors.append({"code": exc.code, "message": str(exc)})
        artifacts = []
        for artifact_id, artifact in sorted(catalog["artifacts"].items()):
            version_id = artifact["current_version"]
            artifacts.append({"artifact_id": artifact_id, "artifact_type": artifact["artifact_type"],
                              "version_id": version_id, "state": "confirmed" if version_id else "withdrawn",
                              "version_count": len(artifact["versions"])})
        conflicts = []
        if verify and catalog["revision"]:
            desired = self._desired(catalog)
            if not projection_errors:
                conflicts = self._projection_conflicts(desired, projection)
                pending = pending or bool(conflicts) or self._projection_outdated(desired)
        return {"catalog_revision": catalog["revision"], "current_count": sum(item["version_id"] is not None for item in artifacts),
                "withdrawn_count": sum(item["version_id"] is None for item in artifacts),
                "index_path": _INDEX, "artifacts": artifacts, "verification": ("current_verified" if not pending else "checked_with_pending_projection") if verify else "not_checked",
                "projection_pending": pending, "projection_conflicts": conflicts, "projection_errors": projection_errors}

    def list(self, *, verify=False):
        return self.status(verify=verify)

    def resolve(self, artifact_id, expected_revision=None):
        _identifier(artifact_id)
        catalog = self._catalog()
        self._cas(catalog, expected_revision, optional=True)
        artifact = catalog["artifacts"].get(artifact_id)
        if artifact is None:
            _fail("not_found", "No artifact has been accepted with this identifier.")
        version_id = artifact["current_version"]
        if version_id is None:
            _fail("withdrawn", "This artifact was withdrawn; no previous version is selected.")
        version = artifact["versions"][version_id]
        self._verify_snapshot(artifact_id, version_id, version)
        request = version["request"]
        snapshot_root = self._snapshot_root(artifact_id, version_id)
        current_root = self._current_root(artifact_id, artifact["artifact_type"])
        try:
            pending = self._projection()["catalog_revision"] != catalog["revision"]
        except ConfirmedArtifactError:
            pending = True
        return {"catalog_revision": catalog["revision"], "artifact_id": artifact_id,
                "artifact_type": artifact["artifact_type"], "version_id": version_id,
                "entrypoint": request["entrypoint"], "snapshot_path": f'{snapshot_root}/{request["entrypoint"]}',
                "current_path": f'{current_root}/{request["entrypoint"]}', "snapshot_root": snapshot_root,
                "files": [{**item, "size": version["sizes"][item["relative_path"]],
                           "snapshot_path": f'{snapshot_root}/{item["relative_path"]}',
                           "current_path": f'{current_root}/{item["relative_path"]}'} for item in request["files"]],
                "confirmation": request["confirmation"], "provenance": _clone(version["provenance"]),
                "verification": "snapshot_verified", "projection_pending": pending}

    def _cas(self, catalog, expected, optional=False):
        if optional and expected is None:
            return
        _revision(expected)
        if expected != catalog["revision"]:
            _fail("revision_conflict", "Catalog changed; refresh its revision before confirming.")

    @contextmanager
    def _parent_handle(self, relative):
        """Pin POSIX parent components; Windows also rechecks fs containment."""
        path = self._checked(relative)
        parent_info = path.parent.lstat()
        identity = (parent_info.st_dev, parent_info.st_ino)
        descriptor = None
        try:
            if os.name != "nt":
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(self.root, flags)
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) != self._root_identity:
                    _fail("unsafe_path", "Root changed while opening its directory handle.")
                for part in _relative_parts(relative)[:-1]:
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) != identity:
                    _fail("unsafe_path", "Parent changed while opening its directory handle.")
            yield descriptor
            info = self._checked(relative).parent.lstat()
            if (info.st_dev, info.st_ino) != identity:
                _fail("unsafe_path", "Parent changed during the file operation.")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _copy_verified(self, source, destination, expected_sha256):
        source_path = self._checked(source)
        target = self._checked(destination)
        self._mkdir(str(Path(destination).parent).replace(os.sep, "/"))
        before = source_path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
            _fail("unsafe_source", "Source must be a bounded, singly linked regular file.")
        handles = ExitStack()
        source_fd = None
        target_fd = None
        try:
            source_parent = handles.enter_context(self._parent_handle(source))
            target_parent = handles.enter_context(self._parent_handle(destination))
            source_fd = os.open(source_path.name if source_parent is not None else source_path,
                                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
                                dir_fd=source_parent)
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                _fail("source_changed", "Source changed while opening.")
            self._checked(source)
            self._checked(destination)
            target_fd = os.open(target.name if target_parent is not None else target,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600,
                                dir_fd=target_parent)
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    _fail("file_too_large", "Source exceeds the single-file limit.")
                digest.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(target_fd, remaining)
                    if written <= 0:
                        _fail("copy_failed", "Snapshot write made no progress.")
                    remaining = remaining[written:]
            os.fsync(target_fd)
            after = os.fstat(source_fd)
            named = self._checked(source).lstat()
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or named.st_nlink != 1:
                _fail("source_changed", "Source changed during the confirmation copy.")
            if digest.hexdigest() != expected_sha256:
                _fail("source_hash_mismatch", "Source bytes do not match the requested SHA-256.")
            # Release both handles before re-reading by path: Windows refuses the
            # read-only shared open while this process still holds the write handle
            # on the freshly written snapshot (ERROR_SHARING_VIOLATION).
            os.close(target_fd)
            target_fd = None
            os.close(source_fd)
            source_fd = None
            if hash_project_file(self.root, source) != expected_sha256 or hash_project_file(self.root, destination) != expected_sha256:
                _fail("source_changed", "Source or snapshot changed before confirmation.")
            _fsync_directory(target.parent)
            return total
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if source_fd is not None:
                os.close(source_fd)
            handles.close()

    def accept(self, request, provenance_factory):
        request = normalize_request(request)
        if not callable(provenance_factory):
            _fail("invalid_provenance", "A provenance validation callback is required.")
        digest = _digest(request)
        with self._lock():
            catalog = self._catalog()
            existing = self._idempotent(catalog, request["operation_id"], digest)
            if existing:
                return self._operation_result(catalog, existing, idempotent=True)
            self._cas(catalog, request["expected_catalog_revision"])
            artifact_id = request["artifact_id"]
            old = catalog["artifacts"].get(artifact_id)
            if old and old["artifact_type"] != request["artifact_type"]:
                _fail("artifact_type_changed", "An artifact identifier retains its original type across versions.")
            provenance = provenance_factory(_clone(request, MAX_REQUEST_BYTES))
            if type(provenance) is not dict:
                _fail("invalid_provenance", "Provenance callback must return a JSON object.")
            provenance = _clone(provenance, MAX_PROVENANCE_BYTES)
            version_id = f'v{catalog["revision"] + 1:08d}-{secrets.token_hex(8)}'
            snapshot = self._snapshot_root(artifact_id, version_id)
            self._mkdir(snapshot)
            sizes = {}
            for item in request["files"]:
                sizes[item["relative_path"]] = self._copy_verified(item["source_path"], f'{snapshot}/{item["relative_path"]}', item["sha256"])
                if sum(sizes.values()) > MAX_BUNDLE_BYTES:
                    _fail("bundle_too_large", "Bundle exceeds the total byte limit.")
            version = {"request": request, "provenance": provenance,
                       "accepted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "sizes": sizes}
            self._verify_snapshot(artifact_id, version_id, version)
            artifact = old or {"artifact_type": request["artifact_type"], "current_version": None, "versions": {}, "withdrawal": None}
            artifact["versions"][version_id] = version
            artifact["current_version"] = version_id
            artifact["withdrawal"] = None
            catalog["artifacts"][artifact_id] = artifact
            operation = self._new_operation(catalog, request["operation_id"], digest, "accept", artifact_id, version_id)
            return self._commit(catalog, request["operation_id"], operation)

    def withdraw(self, artifact_id, expected_revision, operation_id, reason):
        _identifier(artifact_id)
        _identifier(operation_id)
        _revision(expected_revision)
        _text(reason)
        request = {"artifact_id": artifact_id, "expected_revision": expected_revision,
                   "operation_id": operation_id, "reason": reason, "action": "withdraw"}
        digest = _digest(request)
        with self._lock():
            catalog = self._catalog()
            existing = self._idempotent(catalog, operation_id, digest)
            if existing:
                return self._operation_result(catalog, existing, idempotent=True)
            self._cas(catalog, expected_revision)
            artifact = catalog["artifacts"].get(artifact_id)
            if not artifact or artifact["current_version"] is None:
                _fail("not_found", "There is no current confirmation to withdraw.")
            artifact["current_version"] = None
            artifact["withdrawal"] = {"operation_id": operation_id, "reason": reason}
            operation = self._new_operation(catalog, operation_id, digest, "withdraw", artifact_id, None)
            return self._commit(catalog, operation_id, operation)

    def _idempotent(self, catalog, operation_id, digest):
        operation = catalog["operations"].get(operation_id)
        if operation and operation["request_sha256"] != digest:
            _fail("idempotency_conflict", "This operation ID was already used for different content.")
        return operation

    def _new_operation(self, catalog, operation_id, digest, action, artifact_id, version_id):
        if len(catalog["operations"]) >= MAX_OPERATIONS:
            _fail("catalog_full", "Catalog operation limit reached; preserve this catalog before continuing.")
        catalog["revision"] += 1
        operation = {"action": action, "request_sha256": digest, "artifact_id": artifact_id,
                     "version_id": version_id, "catalog_revision": catalog["revision"]}
        catalog["operations"][operation_id] = operation
        _bytes(catalog)
        return operation

    def _write_catalog(self, catalog):
        self._checked(_CATALOG)
        atomic_write_json(self.catalog_path, catalog)

    def _commit(self, catalog, operation_id, operation):
        commit_warning = None
        try:
            self._write_catalog(catalog)
        except Exception as exc:
            # An fsync error can be raised after replace. Inspect the commit point
            # before reporting whether the confirmation became authoritative.
            try:
                durable = self._catalog()
            except Exception as inspection_error:
                raise ConfirmedArtifactError("confirmed.commit_uncertain", "Catalog write outcome cannot be established; inspect before retrying.") from inspection_error
            if durable["operations"].get(operation_id) != operation:
                raise
            catalog = durable
            commit_warning = str(exc)[:1000]
        result = self._operation_result(catalog, operation, idempotent=False, repair=True)
        if commit_warning:
            result["commit_warning"] = commit_warning
        return result

    def _operation_result(self, catalog, operation, *, idempotent, repair=False):
        errors = []
        conflicts = []
        projection_backups = []
        if repair:
            try:
                projection_backups = self._project(catalog, backup_conflicts=False)
            except Exception as exc:
                errors.append({"code": getattr(exc, "code", "confirmed.projection_failed"), "message": str(exc)[:1000]})
                conflicts = getattr(exc, "conflicts", [])
                projection_backups = list(getattr(exc, "projection_backups", []))
        try:
            pending = self._projection()["catalog_revision"] != catalog["revision"] or bool(errors)
        except Exception:
            pending = True
        return {"committed": True, "idempotent": idempotent, "catalog_revision": catalog["revision"],
                "operation_revision": operation["catalog_revision"], "artifact_id": operation["artifact_id"],
                "version_id": operation["version_id"], "action": operation["action"],
                "projection_pending": pending, "projection_conflicts": conflicts, "projection_errors": errors,
                "projection_backups": projection_backups}

    def _desired(self, catalog):
        desired = {}
        lines = ["# 已确认产物", "", f'目录版本：{catalog["revision"]}', "",
                 "此目录只展示当前已验收版本；工作文件保留在原处，历史版本保存在项目内部版本库。", ""]
        for artifact_id, artifact in sorted(catalog["artifacts"].items()):
            version_id = artifact["current_version"]
            if version_id is None:
                lines.append(f"- `{artifact_id}`：已撤回，当前无确认版本。")
                continue
            version = artifact["versions"][version_id]
            self._verify_snapshot(artifact_id, version_id, version)
            request = version["request"]
            snapshot = self._snapshot_root(artifact_id, version_id)
            current = self._current_root(artifact_id, artifact["artifact_type"])
            for item in request["files"]:
                desired[f'{current}/{item["relative_path"]}'] = {
                    "sha256": item["sha256"], "source": f'{snapshot}/{item["relative_path"]}', "bytes": None,
                }
            from urllib.parse import quote
            link = quote(f'{current.removeprefix("artifacts/")}/{request["entrypoint"]}', safe="/")
            lines.append(f'- [{artifact_id}]({link}) · `{artifact["artifact_type"]}` · `{version_id}`')
        if not catalog["artifacts"]:
            lines.append("尚无已确认产物。")
        index = ("\n".join(lines) + "\n").encode("utf-8")
        desired[_INDEX] = {"sha256": hashlib.sha256(index).hexdigest(), "source": None, "bytes": index}
        return desired

    def _current_files(self):
        files = []
        root = self._checked(_CURRENT)
        if root.exists():
            if not root.is_dir():
                _fail("projection_conflict", "artifacts/current is not a directory.")
            pending = [root]
            count = 0
            while pending:
                directory = pending.pop()
                for path in directory.iterdir():
                    count += 1
                    if count > 10000:
                        _fail("projection_conflict", "Projection directory exceeds the bounded inspection size.")
                    relative = path.relative_to(self.root).as_posix()
                    checked = self._checked(relative)
                    info = checked.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(checked)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        files.append(relative)
                    else:
                        _fail("projection_conflict", f"Preserve the unexpected projection entry: {relative}")
        index = self._checked(_INDEX)
        if index.exists():
            info = index.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("projection_conflict", "The artifact index is not a plain file.")
            files.append(_INDEX)
        return files

    def _projection_conflicts(self, desired, projection):
        conflicts = set()
        for path in self._current_files():
            known = projection["files"].get(path)
            info = self._checked(path).lstat()
            # A crash can leave some newly published bytes before the marker.
            # Exact desired bytes can be adopted without replacing user content.
            allowed = {known, desired.get(path, {}).get("sha256")} - {None}
            if info.st_size > MAX_FILE_BYTES or hash_project_file(self.root, path) not in allowed:
                conflicts.add(path)
        # A file can become a directory (or vice versa) between bundle versions.
        # Inspect each existing prefix instead of resolving through a blocking
        # file. Even owned old bytes require an explicit preserving backup here.
        for path in desired:
            parts = _relative_parts(path)
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                checked = self._checked(prefix)
                if not checked.exists():
                    break
                info = checked.lstat()
                expected_kind = stat.S_ISREG if index == len(parts) else stat.S_ISDIR
                if not expected_kind(info.st_mode):
                    conflicts.add(prefix)
                    break
        return sorted(conflicts)

    def _projection_outdated(self, desired):
        for path, item in desired.items():
            checked = self._checked(path)
            if not checked.exists() or checked.stat().st_size > MAX_FILE_BYTES or hash_project_file(self.root, path) != item["sha256"]:
                return True
        return False

    def _projection_backup_root(self):
        """Create a private, never-overwritten home for retired view inodes."""
        relative = f"{_BASE}/projection-backups/replacement-{secrets.token_hex(12)}"
        self._mkdir(relative)
        return relative

    def _retire_projection_file(self, path, expected_digest):
        """Move an owned view to a unique backup before removing/replacing it.

        The final hash check is deliberately immediately before the rename. If
        another process edits the file in the race window, rename captures that
        newer inode instead of deleting it.
        """
        target = self._checked(path)
        if not target.exists():
            _fail("projection_conflict", "Projection disappeared while preparing replacement; retry explicitly.")
        if target.lstat().st_size > MAX_FILE_BYTES or hash_project_file(self.root, path) != expected_digest:
            _fail("projection_conflict", "Projection changed while preparing replacement; its content was preserved.")
        backup_root = self._projection_backup_root()
        backup = f"{backup_root}/{Path(path).name}"
        with self._parent_handle(path) as source_fd, self._parent_handle(backup) as backup_fd:
            source_name = self._checked(path).name if source_fd is not None else self._checked(path)
            backup_name = self._checked(backup).name if backup_fd is not None else self._checked(backup)
            try:
                os.rename(source_name, backup_name, src_dir_fd=source_fd, dst_dir_fd=backup_fd)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
                error = ConfirmedArtifactError("confirmed.projection_conflict", "Projection changed while preserving it; retry explicitly.")
                error.projection_backups = [backup]
                raise error from exc
            _fsync_directory(self._checked(path).parent)
            _fsync_directory(self._checked(backup).parent)
        self._projection_backups.append(backup)
        return backup

    def _publish_projection_file(self, temporary, target, expected_sha256):
        """Publish a staged file without ever clobbering a newly-created target."""
        with self._parent_handle(target) as target_fd, self._parent_handle(temporary) as staging_fd:
            source_name = self._checked(temporary).name if staging_fd is not None else self._checked(temporary)
            target_name = self._checked(target).name if target_fd is not None else self._checked(target)
            try:
                os.link(source_name, target_name, src_dir_fd=staging_fd, dst_dir_fd=target_fd, follow_symlinks=False)
            except FileExistsError as exc:
                error = ConfirmedArtifactError("confirmed.projection_conflict", "A new file appeared while publishing the projection; it was preserved.")
                error.projection_backups = list(self._projection_backups)
                raise error from exc
            except (NotImplementedError, TypeError, OSError):
                # Some filesystems do not expose directory-handle hard links.
                # The copy path still creates the destination with O_EXCL.
                try:
                    self._copy_verified(temporary, target, expected_sha256)
                except FileExistsError as exc:
                    error = ConfirmedArtifactError("confirmed.projection_conflict", "A new file appeared while publishing the projection; it was preserved.")
                    error.projection_backups = list(self._projection_backups)
                    raise error from exc
            _fsync_directory(self._checked(target).parent)
            _fsync_directory(self._checked(temporary).parent)
        # Only the staging inode is disposable; retired projection backups are
        # intentionally never unlinked.
        os.unlink(self._checked(temporary))

    def _write_projection_file(self, path, item, expected_digest=None):
        target = self._checked(path)
        parent_relative = Path(path).parent.as_posix()
        self._mkdir(parent_relative)
        staging = f"{_BASE}/projection-staging"
        self._mkdir(staging)
        temporary = f'{staging}/{secrets.token_hex(12)}.tmp'
        if item["source"]:
            self._copy_verified(item["source"], temporary, item["sha256"])
        else:
            temp = self._checked(temporary)
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
            try:
                raw = item["bytes"]
                offset = 0
                while offset < len(raw):
                    written = os.write(descriptor, raw[offset:])
                    if written <= 0:
                        _fail("copy_failed", "Projection write made no progress.")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        target = self._checked(path)
        if target.exists():
            if expected_digest is None or target.lstat().st_size > MAX_FILE_BYTES or hash_project_file(self.root, path) != expected_digest:
                _fail("projection_conflict", "Projection changed while preparing replacement; its content was preserved.")
            self._retire_projection_file(path, expected_digest)
        elif expected_digest is not None:
            _fail("projection_conflict", "Projection disappeared while preparing replacement; retry explicitly.")
        try:
            self._publish_projection_file(temporary, path, item["sha256"])
        except Exception as exc:
            # Keep the path list attached so callers can locate every retired
            # inode even when publication is interrupted.
            if not hasattr(exc, "projection_backups"):
                exc.projection_backups = list(self._projection_backups)
            raise
        finally:
            # A staged copy is disposable; retained projection backups are not.
            staged = self._checked(temporary)
            if staged.exists():
                staged.unlink()

    def _project(self, catalog, *, backup_conflicts):
        self._projection_backups = []
        desired = self._desired(catalog)
        marker_invalid = False
        try:
            projection = self._projection()
        except ConfirmedArtifactError:
            if not backup_conflicts:
                raise
            marker_invalid = True
            projection = {"catalog_revision": -1, "files": {}}
        try:
            conflicts = self._projection_conflicts(desired, projection)
        except ConfirmedArtifactError:
            if not backup_conflicts:
                raise
            conflicts = [_CURRENT]
        backups = []
        if conflicts or marker_invalid:
            if not backup_conflicts:
                error = ConfirmedArtifactError("confirmed.projection_conflict", "Current files contain unknown or edited content. Repair with backup_conflicts=True to preserve it before rebuilding.")
                error.conflicts = conflicts
                raise error
            backup_root = f'{_BASE}/projection-backups/{catalog["revision"]}-{secrets.token_hex(8)}'
            self._mkdir(backup_root)
            for source, name in ((_CURRENT, "current"), (_INDEX, "INDEX.md"),
                                 (_PROJECTION, "projection.json"), (_PROJECTION + ".bak", "projection.json.bak")):
                origin = self._checked(source)
                if origin.exists():
                    destination = f"{backup_root}/{name}"
                    os.replace(origin, self._checked(destination))
                    _fsync_directory(origin.parent)
                    _fsync_directory(self._checked(backup_root))
                    backups.append(destination)
            self._projection_backups.extend(backups)
            projection = {"catalog_revision": -1, "files": {}}
        # Check every desired immutable source and every existing view before
        # replacing anything. catalog.json was already the authority at this point.
        for path, item in desired.items():
            exists = self._checked(path).exists()
            current_digest = hash_project_file(self.root, path) if exists else None
            if current_digest != item["sha256"]:
                if exists and current_digest != projection["files"].get(path):
                    _fail("projection_conflict", "Projection changed after inspection; preserve it and repair explicitly.")
                self._write_projection_file(path, item, current_digest)
        for path, digest in projection["files"].items():
            if path in desired:
                continue
            checked = self._checked(path)
            if checked.exists():
                if hash_project_file(self.root, path) != digest:
                    _fail("projection_conflict", "An old view changed before removal; preserve it and repair explicitly.")
                self._retire_projection_file(path, digest)
        marker = {"catalog_revision": catalog["revision"], "files": {path: item["sha256"] for path, item in desired.items()}}
        atomic_write_json(self._checked(_PROJECTION), marker)
        return list(self._projection_backups)

    def repair(self, *, expected_revision, backup_conflicts=False):
        if type(backup_conflicts) is not bool:
            _fail("invalid_schema", "backup_conflicts must be explicitly boolean.")
        with self._lock():
            catalog = self._catalog()
            self._cas(catalog, expected_revision)
            backups = self._project(catalog, backup_conflicts=backup_conflicts)
            return {"catalog_revision": catalog["revision"], "projection_pending": False,
                    "backups": backups, "committed": False, "verification": "current_verified"}


def confirmed_artifact_summary(project_root):
    """Read bounded metadata only; never create directories, repair or hash bundles."""
    status = ConfirmedArtifactStore(project_root).status()
    return {key: status[key] for key in (
        "catalog_revision", "current_count", "withdrawn_count", "index_path",
        "verification", "projection_pending",
    )}
