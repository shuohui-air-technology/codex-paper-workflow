"""Crash-safe, project-contained filesystem primitives for custom workflows."""

from __future__ import annotations

import json
import errno
import hashlib
import ntpath
import os
import stat
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024


class PathSafetyError(RuntimeError):
    """Raised when a filesystem operation cannot prove project containment."""


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def reject_nonfinite_constant(value: str) -> object:
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
    if raw != "/".join(posix.parts):
        raise PathSafetyError("project path must use canonical relative spelling")
    return tuple(posix.parts)


def read_project_json_object(root: Path | str, relative: str | os.PathLike[str]) -> dict[str, object]:
    """Read one bounded, stable project JSON object for command interfaces."""
    path = resolve_project_path(root, relative)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_size > MAX_JSON_BYTES:
        raise PathSafetyError("JSON input must be a bounded, singly linked regular file")
    before_hash = hash_project_file(root, relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise PathSafetyError("JSON input is not a plain regular file")
        if opened.st_size > MAX_JSON_BYTES:
            raise PathSafetyError("JSON input exceeds the size limit")
        chunks = bytearray()
        while len(chunks) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_JSON_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or len(chunks) > MAX_JSON_BYTES
        ):
            raise PathSafetyError("JSON input changed while being read")
    finally:
        os.close(descriptor)
    raw = bytes(chunks)
    after_hash = hash_project_file(root, relative)
    if before_hash != after_hash or hashlib.sha256(raw).hexdigest() != after_hash:
        raise PathSafetyError("JSON input changed while being read")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs, parse_constant=reject_nonfinite_constant)
    except RecursionError as exc:
        raise ValueError("JSON input nesting is too deep") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def hash_regular_file(path: Path) -> str:
    """Hash a stable, singly linked regular file without following a leaf link."""
    try:
        before = path.lstat()
        attributes = getattr(before, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or attributes & reparse:
            raise PathSafetyError("artifact is not a plain regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise PathSafetyError("artifact changed while opening")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            named = path.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if (
                (after.st_dev, after.st_ino) != identity
                or (named.st_dev, named.st_ino) != identity
                or after.st_nlink != 1
                or named.st_nlink != 1
                or not stat.S_ISREG(named.st_mode)
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise PathSafetyError("artifact changed while hashing")
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    except PathSafetyError:
        raise
    except (OSError, NotImplementedError) as exc:
        raise PathSafetyError("artifact cannot be hashed safely") from exc


def _windows_path_key(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _windows_path_is_within(root: str, candidate: str) -> bool:
    try:
        return ntpath.commonpath((root, candidate)) == root and candidate != root
    except ValueError:
        return False


def _validate_windows_project_parts(parts: tuple[str, ...]) -> None:
    reserved = {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
        *(f"COM{number}" for number in "¹²³"),
        *(f"LPT{number}" for number in "¹²³"),
    }
    for part in parts:
        stem = part.split(".", 1)[0].rstrip(" .").upper()
        if (
            not part
            or part.endswith((".", " "))
            or stem in reserved
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        ):
            raise PathSafetyError("project path contains a Windows-reserved name")


def _windows_extended_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _hash_project_file_windows(
    root: Path,
    parts: tuple[str, ...],
) -> str:
    """Hash a Windows project file using pinned handles and final-path checks."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    read_file = kernel32.ReadFile
    read_file.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    read_file.restype = wintypes.BOOL

    invalid_handle = ctypes.c_void_p(-1).value
    file_read_attributes = 0x0080
    generic_read = 0x80000000
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    flag_sequential_scan = 0x08000000
    attr_directory = 0x00000010
    attr_device = 0x00000040
    attr_reparse_point = 0x00000400
    file_type_disk = 1
    _validate_windows_project_parts(parts)
    requested_root = Path(root).expanduser().absolute()
    root_path: Path
    root_key: str
    candidate: Path
    candidate_key: str
    handles: list[object] = []

    def win_error(action: str) -> PathSafetyError:
        code = ctypes.get_last_error()
        return PathSafetyError(f"{action} failed safely (Windows error {code})")

    def open_path(path: Path, access: int, share: int, flags: int):
        handle = create_file(
            _windows_extended_path(path), access, share, None,
            open_existing, flags, None,
        )
        value = handle if isinstance(handle, int) else getattr(handle, "value", None)
        if value is None or value == invalid_handle:
            raise win_error("project file open")
        handles.append(handle)
        return handle

    def information(handle) -> tuple[int, ...]:
        value = ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(value)):
            raise win_error("project file inspection")
        filetime = lambda item: (item.high << 32) | item.low
        return (
            value.attributes,
            value.link_count,
            (value.size_high << 32) | value.size_low,
            value.volume_serial,
            (value.file_index_high << 32) | value.file_index_low,
            filetime(value.creation_time),
            filetime(value.last_write_time),
        )

    def final_path(handle) -> str:
        needed = get_final_path(handle, None, 0, 0)
        if not needed:
            raise win_error("project file final-path lookup")
        buffer = ctypes.create_unicode_buffer(needed + 1)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise win_error("project file final-path lookup")
        return buffer.value

    def path_from_final_path(value: str) -> Path:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def require_expected_path(actual: str, expected: str) -> None:
        actual_key = _windows_path_key(actual)
        if not _windows_path_is_within(root_key, actual_key) or actual_key != expected:
            raise PathSafetyError("project file handle resolved outside its declared path")

    try:
        root_handle = open_path(
            requested_root,
            file_read_attributes,
            share_read | share_write,
            flag_backup_semantics | flag_open_reparse_point,
        )
        root_info = information(root_handle)
        if not root_info[0] & attr_directory or root_info[0] & attr_reparse_point:
            raise PathSafetyError("project root is not a plain directory")
        root_final_path = final_path(root_handle)
        root_path = path_from_final_path(root_final_path)
        root_key = _windows_path_key(root_final_path)
        if not root_path.is_absolute() or _windows_path_key(root_path) != root_key:
            raise PathSafetyError("project root handle has an invalid final path")
        candidate = root_path.joinpath(*parts)
        candidate_key = _windows_path_key(candidate)
        validated_candidate = resolve_project_path(root_path, "/".join(parts))
        if _windows_path_key(validated_candidate) != candidate_key:
            raise PathSafetyError("project path changed while resolving the artifact")

        parent = root_path
        for part in parts[:-1]:
            parent = parent / part
            expected_parent = _windows_path_key(parent)
            parent_handle = open_path(
                parent,
                file_read_attributes,
                share_read | share_write,
                flag_backup_semantics | flag_open_reparse_point,
            )
            parent_info = information(parent_handle)
            if (
                not parent_info[0] & attr_directory
                or parent_info[0] & attr_reparse_point
            ):
                raise PathSafetyError("project path contains a reparse point")
            require_expected_path(final_path(parent_handle), expected_parent)

        file_handle = open_path(
            candidate,
            generic_read,
            share_read,
            flag_open_reparse_point | flag_sequential_scan,
        )
        before = information(file_handle)
        if (
            before[0] & (attr_directory | attr_device | attr_reparse_point)
            or before[1] != 1
            or get_file_type(file_handle) != file_type_disk
        ):
            raise PathSafetyError("artifact is not a plain regular file")
        require_expected_path(final_path(file_handle), candidate_key)

        digest = hashlib.sha256()
        buffer = ctypes.create_string_buffer(1024 * 1024)
        while True:
            byte_count = wintypes.DWORD()
            if not read_file(
                file_handle, buffer, len(buffer), ctypes.byref(byte_count), None
            ):
                raise win_error("project file read")
            if not byte_count.value:
                break
            digest.update(buffer.raw[:byte_count.value])

        after = information(file_handle)
        if (
            after != before
            or _windows_path_key(final_path(file_handle)) != candidate_key
            or _windows_path_key(final_path(root_handle)) != root_key
        ):
            raise PathSafetyError("artifact changed while hashing")
        return digest.hexdigest()
    except PathSafetyError:
        raise
    except (OSError, NotImplementedError, TypeError) as exc:
        raise PathSafetyError("artifact cannot be hashed safely") from exc
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def hash_project_file(root: Path | str, relative: str | os.PathLike[str]) -> str:
    """Hash a project file through no-follow directory descriptors.

    The directory handles bind every component to the selected project root,
    even if another process replaces a named parent while hashing.
    """
    parts = _relative_parts(relative)
    if os.name == "nt":
        return _hash_project_file_windows(Path(root), parts)

    supplied_root = Path(root).expanduser()
    try:
        root_before = supplied_root.lstat()
    except OSError as exc:
        raise PathSafetyError("project root must be an existing directory") from exc
    attributes = getattr(root_before, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or stat.S_ISLNK(root_before.st_mode)
        or attributes & reparse
    ):
        raise PathSafetyError("project root must be a plain directory")

    resolved = resolve_project_path(supplied_root, relative)
    try:
        root_after = supplied_root.lstat()
    except OSError as exc:
        raise PathSafetyError("project root changed while resolving the artifact") from exc
    root_identity = (root_before.st_dev, root_before.st_ino)
    if (
        (root_after.st_dev, root_after.st_ino) != root_identity
        or not stat.S_ISDIR(root_after.st_mode)
        or getattr(root_after, "st_file_attributes", 0) & reparse
    ):
        raise PathSafetyError("project root changed while resolving the artifact")

    resolved_root = resolved
    for _part in parts:
        resolved_root = resolved_root.parent
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise PathSafetyError("platform cannot hash project artifacts with safe handles")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    directory = -1
    file_descriptor = -1
    try:
        directory = os.open(resolved_root, directory_flags)
        opened_root = os.fstat(directory)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino) != root_identity
        ):
            raise PathSafetyError("project root is not a directory")
        for part in parts[:-1]:
            next_directory = os.open(part, directory_flags, dir_fd=directory)
            if not stat.S_ISDIR(os.fstat(next_directory).st_mode):
                os.close(next_directory)
                raise PathSafetyError("artifact parent is not a directory")
            os.close(directory)
            directory = next_directory
        before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PathSafetyError("artifact is not a plain regular file")
        file_descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PathSafetyError("artifact changed while opening")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(file_descriptor)
        named = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_nlink != 1
            or named.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise PathSafetyError("artifact changed while hashing")
        # Confirm the path still names the same file through the root as well.
        if resolved != resolve_project_path(supplied_root, relative):
            raise PathSafetyError("artifact path changed while hashing")
        final_named = resolved.lstat()
        if (final_named.st_dev, final_named.st_ino) != (opened.st_dev, opened.st_ino):
            raise PathSafetyError("artifact path changed while hashing")
        return digest.hexdigest()
    except PathSafetyError:
        raise
    except (OSError, NotImplementedError, TypeError) as exc:
        raise PathSafetyError("artifact cannot be hashed safely") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if directory >= 0:
            os.close(directory)


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
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
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
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=reject_nonfinite_constant,
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
                    object_pairs_hook=reject_duplicate_pairs,
                    parse_constant=reject_nonfinite_constant,
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
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
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
