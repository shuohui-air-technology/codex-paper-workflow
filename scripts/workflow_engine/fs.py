"""Crash-safe, project-contained filesystem primitives for custom workflows."""

from __future__ import annotations

import json
import errno
import os
import stat
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024


class PathSafetyError(RuntimeError):
    """Raised when a filesystem operation cannot prove project containment."""


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PathSafetyError(f"could not inspect path component: {path}") from exc
    if stat.S_ISLNK(value.st_mode):
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_parts(relative: str | os.PathLike[str]) -> tuple[str, ...]:
    raw = os.fspath(relative)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PathSafetyError("project path must be a non-empty text path")
    if "\\" in raw:
        windows = PureWindowsPath(raw)
        if windows.is_absolute() or windows.drive or windows.root:
            raise PathSafetyError("absolute project paths are forbidden")
        if any(part in {"", ".", ".."} for part in windows.parts):
            raise PathSafetyError("ambiguous or traversing project path is forbidden")
        raise PathSafetyError("backslash-separated project paths are forbidden")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise PathSafetyError("absolute project paths are forbidden")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise PathSafetyError("ambiguous or traversing project path is forbidden")
    return tuple(posix.parts)


def resolve_project_path(
    root: Path | str, relative: str | os.PathLike[str]
) -> Path:
    """Resolve one literal project-relative path without following child links.

    The component walk is performed twice.  Callers that mutate the returned
    path must still re-run this check immediately before the final open/replace;
    the store does that while holding its project lock.
    """

    supplied_root = Path(root).expanduser()
    if _is_link_or_reparse(supplied_root):
        raise PathSafetyError("project root must not be a symlink or reparse point")
    try:
        resolved_root = supplied_root.resolve(strict=True)
    except OSError as exc:
        raise PathSafetyError("project root must be an existing directory") from exc
    if not resolved_root.is_dir():
        raise PathSafetyError("project root must be an existing directory")
    parts = _relative_parts(relative)
    candidate = resolved_root.joinpath(*parts)

    for _ in range(2):
        current = resolved_root
        for part in parts:
            current = current / part
            if _is_link_or_reparse(current):
                raise PathSafetyError(f"project path contains a link or reparse point: {current}")
        try:
            resolved_candidate = candidate.resolve(strict=False)
        except OSError as exc:
            raise PathSafetyError(f"project path cannot be resolved safely: {candidate}") from exc
        if not _contained(resolved_root, resolved_candidate):
            raise PathSafetyError("project path escapes the selected project")
    return candidate


def ensure_project_directory(
    root: Path | str, relative: str | os.PathLike[str]
) -> Path:
    """Create a project-contained directory one checked component at a time."""

    parts = _relative_parts(relative)
    resolved_root = Path(root).expanduser().resolve(strict=True)
    current_relative: list[str] = []
    for part in parts:
        current_relative.append(part)
        current = resolve_project_path(resolved_root, "/".join(current_relative))
        created = False
        try:
            current.mkdir()
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise PathSafetyError(f"could not create project directory: {current}") from exc
        checked = resolve_project_path(resolved_root, "/".join(current_relative))
        if not checked.is_dir() or _is_link_or_reparse(checked):
            raise PathSafetyError(f"project path is not a plain directory: {checked}")
        if created:
            _fsync_directory(checked.parent)
    return resolve_project_path(resolved_root, "/".join(parts))


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            raise
    finally:
        os.close(descriptor)


def _plain_regular_file(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PathSafetyError(f"could not inspect material file: {path}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise PathSafetyError(f"material path is not a plain regular file: {path}")
    attributes = getattr(value, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise PathSafetyError(f"material path is a reparse point: {path}")
    return True


def _parent_identity(path: Path) -> tuple[int, int]:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise PathSafetyError(f"material parent is not a plain directory: {path}")
    value = path.stat(follow_symlinks=False)
    return value.st_dev, value.st_ino


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    value = path.stat(follow_symlinks=False)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _write_bytes_atomic(
    path: Path,
    data: bytes,
    *,
    expected_target_identity: tuple[int, int, int, int] | None = None,
    require_absent: bool = False,
) -> None:
    parent_identity = _parent_identity(path.parent)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _parent_identity(path.parent) != parent_identity:
            raise PathSafetyError("material parent changed during atomic write")
        target_exists = _plain_regular_file(path)
        if require_absent and target_exists:
            raise PathSafetyError("material target appeared during atomic write")
        if expected_target_identity is not None:
            if not target_exists or _file_identity(path) != expected_target_identity:
                raise PathSafetyError("material target changed during atomic write")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if temporary.exists() and not _is_link_or_reparse(temporary):
                temporary.unlink()
        except OSError:
            pass


def atomic_write_json(path: Path | str, value: object) -> None:
    """Durably replace canonical JSON and retain a validated backup generation."""

    target = Path(path)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (RecursionError, TypeError, ValueError) as exc:
        raise PathSafetyError("JSON material cannot be encoded canonically") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise PathSafetyError("refusing to write JSON beyond the bounded material size")
    _parent_identity(target.parent)
    target_identity: tuple[int, int, int, int] | None = None
    if _plain_regular_file(target):
        target_identity = _file_identity(target)
        try:
            old_bytes = target.read_bytes()
            if len(old_bytes) > MAX_JSON_BYTES:
                raise ValueError("persisted JSON exceeds the bounded input size")
            json.loads(
                old_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PathSafetyError(f"refusing to replace invalid JSON evidence: {target}") from exc
        backup = target.with_name(target.name + ".bak")
        backup_identity: tuple[int, int, int, int] | None = None
        if _plain_regular_file(backup):
            backup_identity = _file_identity(backup)
            try:
                backup_bytes = backup.read_bytes()
                if len(backup_bytes) > MAX_JSON_BYTES:
                    raise ValueError("persisted JSON backup exceeds the bounded input size")
                json.loads(
                    backup_bytes.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_pairs,
                    parse_constant=_reject_constant,
                )
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise PathSafetyError(f"refusing to replace invalid JSON backup: {backup}") from exc
        _write_bytes_atomic(
            backup,
            old_bytes,
            expected_target_identity=backup_identity,
            require_absent=backup_identity is None,
        )
        if _file_identity(target) != target_identity:
            raise PathSafetyError("material target changed while its backup was created")
    _write_bytes_atomic(
        target,
        raw,
        expected_target_identity=target_identity,
        require_absent=target_identity is None,
    )


def append_event(path: Path | str, event: object) -> None:
    """Append and fsync one canonical event line without following links."""

    target = Path(path)
    parent_identity = _parent_identity(target.parent)
    if hasattr(event, "to_payload"):
        payload = event.to_payload()  # type: ignore[attr-defined]
    elif isinstance(event, Mapping):
        payload = dict(event)
    else:
        raise PathSafetyError("event must be a mapping or expose to_payload()")
    try:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (RecursionError, TypeError, ValueError) as exc:
        raise PathSafetyError("event cannot be encoded canonically") from exc
    if len(data) > MAX_EVENT_BYTES:
        raise PathSafetyError("event exceeds the bounded canonical line size")
    target_existed = _plain_regular_file(target)
    target_identity = _file_identity(target) if target_existed else None
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise PathSafetyError(f"could not open event log safely: {target}") from exc
    try:
        opened = os.fstat(descriptor)
        named = target.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise PathSafetyError("event path changed while opening")
        if target_identity is not None and (named.st_dev, named.st_ino) != target_identity[:2]:
            raise PathSafetyError("event target changed before append")
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        if not target_existed:
            _fsync_directory(target.parent)
    finally:
        os.close(descriptor)
    final = target.stat(follow_symlinks=False)
    if (
        _parent_identity(target.parent) != parent_identity
        or _is_link_or_reparse(target)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise PathSafetyError("event path changed during durable append")
