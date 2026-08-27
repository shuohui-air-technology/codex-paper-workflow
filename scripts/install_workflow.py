#!/usr/bin/env python3
"""Install the paper workflow and pinned downstream skills safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "dependencies.lock.json"
INSTALL_RECEIPT = ".paper-workflow-install.json"
TRANSACTION_RECEIPT = ".paper-workflow-install.transaction.json"
PROFILES = {"core", "standard", "full"}
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_ARCHIVE_MEMBERS = 20_000
MAX_MEMBER_UNCOMPRESSED = 64 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED = 512 * 1024 * 1024
MAX_ARCHIVE_COMPRESSED = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class InstallError(ValueError):
    pass


def default_target() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured) / "skills") if configured else (Path.home() / ".codex" / "skills")


def _is_link(path: Path) -> bool:
    """Detect symlinks and Windows reparse-point links before resolving them."""
    if path.is_symlink() or os.path.islink(path):
        return True
    if os.name == "nt" and path.exists():
        try:
            attrs = path.stat(follow_symlinks=False).st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def _assert_no_link_components(path: Path) -> None:
    """Reject symlink/reparse-point parents before resolving or creating a path."""
    current = path
    while True:
        if _is_link(current):
            raise InstallError(f"path component must not be a symlink or reparse point: {current}")
        if current.parent == current:
            break
        current = current.parent


def _assert_safe_control_paths(target: Path) -> None:
    """Protect installer-owned control paths from junction/symlink redirection."""
    _assert_no_link_components(target)
    for relative in (INSTALL_RECEIPT, TRANSACTION_RECEIPT, ".paper-workflow-backups"):
        candidate = target / relative
        if _is_link(candidate):
            raise InstallError(f"installer control path must not be a symlink or reparse point: {candidate}")


def _validate_skill_name(name: Any) -> str:
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name) or name.casefold() in WINDOWS_RESERVED_NAMES:
        raise InstallError(f"invalid skill name: {name!r}")
    return name


def _validate_manifest_path(raw: Any, field: str) -> str:
    """Validate a manifest path before any repository or staging I/O."""
    if not isinstance(raw, str) or not raw.strip():
        raise InstallError(f"{field} must be a non-empty relative path")
    normalised = raw.replace("\\", "/")
    if normalised in {"", ".", "./"}:
        return "."
    if not validate_archive_member(normalised):
        raise InstallError(f"{field} must be a safe relative path")
    return normalised.rstrip("/") or "."


def _safe_relative_source(root: Path, raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise InstallError(f"{field} must be a non-empty relative path")
    normalised = raw.replace("\\", "/")
    if normalised in {"", ".", "./"}:
        root = root.expanduser()
        _assert_no_link_components(root)
        return root.resolve()
    if not validate_archive_member(normalised):
        raise InstallError(f"{field} must be a safe relative path")
    root = root.expanduser()
    _assert_no_link_components(root)
    path = root
    for component in PurePosixPath(normalised).parts:
        path = path / component
        if _is_link(path):
            raise InstallError(f"{field} must not use a symlink or reparse point: {raw}")
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise InstallError(f"{field} escapes the repository root") from exc
    return resolved


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"dependency manifest is not readable JSON: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), dict) or not isinstance(value.get("skills"), dict):
        raise InstallError("dependency manifest must contain profiles and skills objects")
    if value.get("schema_version") != "workflow-dependencies-v1":
        raise InstallError("unsupported dependency manifest schema_version")
    skills = value["skills"]
    for name, entry in skills.items():
        _validate_skill_name(name)
        if not isinstance(entry, dict):
            raise InstallError(f"manifest skill entry is not an object: {name}")
        source_type = entry.get("source")
        if source_type not in {"repository", "bundled", "github"}:
            raise InstallError(f"unsupported dependency source for {name}: {source_type}")
        required_fields = {"source", "path", "license"}
        if source_type == "github":
            required_fields |= {"repository", "commit"}
        if set(entry) != required_fields:
            raise InstallError(f"manifest entry has unexpected or missing fields: {name}")
        _validate_manifest_path(entry.get("path"), f"skills.{name}.path")
        if source_type == "github":
            repository = entry.get("repository")
            commit = entry.get("commit")
            if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise InstallError(f"invalid GitHub repository for {name}: {repository}")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise InstallError(f"dependency is not pinned to a full commit SHA: {name}")
        if not isinstance(entry.get("license"), str) or not entry["license"].strip():
            raise InstallError(f"manifest license status is missing for {name}")
    for profile, names in value["profiles"].items():
        if profile not in PROFILES:
            raise InstallError(f"unknown profile in manifest: {profile}")
        if not isinstance(names, list) or not names:
            raise InstallError(f"profile is empty or malformed: {profile}")
        seen: set[str] = set()
        for name in names:
            _validate_skill_name(name)
            if name in seen:
                raise InstallError(f"profile contains duplicate skill: {profile}/{name}")
            seen.add(name)
            if name not in skills:
                raise InstallError(f"profile references missing skill: {profile}/{name}")
    profile_sets = {profile: set(names) for profile, names in value["profiles"].items()}
    if "core" in profile_sets and "standard" in profile_sets and not profile_sets["core"].issubset(profile_sets["standard"]):
        raise InstallError("standard profile must include every core skill")
    if "standard" in profile_sets and "full" in profile_sets and not profile_sets["standard"].issubset(profile_sets["full"]):
        raise InstallError("full profile must include every standard skill")
    return value


def resolve_profile(manifest: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    if profile not in PROFILES:
        raise InstallError(f"unknown profile: {profile}")
    names = manifest["profiles"].get(profile)
    if not isinstance(names, list) or not names:
        raise InstallError(f"profile is empty or malformed: {profile}")
    entries: list[dict[str, Any]] = []
    for name in names:
        _validate_skill_name(name)
        entry = manifest["skills"].get(name)
        if not isinstance(entry, dict):
            raise InstallError(f"profile references missing skill: {name}")
        source_type = entry.get("source")
        required_fields = {"source", "path", "license"}
        if source_type == "github":
            required_fields |= {"repository", "commit"}
        if source_type not in {"repository", "bundled", "github"} or set(entry) != required_fields:
            raise InstallError(f"manifest entry has unexpected or missing fields: {name}")
        _validate_manifest_path(entry.get("path"), f"skills.{name}.path")
        if not isinstance(entry.get("license"), str) or not entry["license"].strip():
            raise InstallError(f"manifest license status is missing for {name}")
        if source_type == "github":
            if not isinstance(entry.get("repository"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repository"]):
                raise InstallError(f"invalid GitHub repository for {name}: {entry.get('repository')}")
            if not isinstance(entry.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", entry["commit"]):
                raise InstallError(f"dependency is not pinned to a full commit SHA: {name}")
        item = dict(entry)
        item["name"] = name
        entries.append(item)
    return entries


def validate_archive_member(name: str) -> bool:
    if not isinstance(name, str):
        return False
    normalised = name.replace("\\", "/")
    path = PurePosixPath(normalised)
    windows_path = PureWindowsPath(normalised)
    for component in path.parts:
        trimmed = component.rstrip(" .")
        canonical = unicodedata.normalize("NFKC", trimmed).casefold()
        if component != trimmed or not trimmed or canonical.split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
            return False
    return (
        "\x00" not in normalised
        and not path.is_absolute()
        and not windows_path.is_absolute()
        and not windows_path.drive
        and ".." not in path.parts
        and all(part not in {"", "."} for part in path.parts)
        and all(":" not in part for part in path.parts)
    )


def _normalise_link_target(link_name: str, target: str) -> str:
    """Resolve an in-archive relative link without touching the host filesystem."""
    if not isinstance(target, str) or any(ord(character) < 32 or ord(character) == 127 for character in target):
        raise InstallError(f"symlink target contains unsupported control characters: {link_name}")
    target = target.replace("\\", "/")
    if not target or target.startswith("/") or PureWindowsPath(target).drive:
        raise InstallError(f"unsafe absolute symlink target: {link_name} -> {target}")
    parts = list(PurePosixPath(link_name).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise InstallError(f"symlink target escapes archive: {link_name} -> {target}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise InstallError(f"symlink target resolves to archive root: {link_name} -> {target}")
    resolved = "/".join(parts)
    if not validate_archive_member(resolved):
        raise InstallError(f"unsafe symlink target: {link_name} -> {target}")
    return resolved


def _windows_member_key(name: str) -> str:
    """Return a collision key for names that Windows treats as equivalent."""
    parts = []
    for part in PurePosixPath(name.replace("\\", "/")).parts:
        parts.append(unicodedata.normalize("NFKC", part.rstrip(" .")).casefold())
    return "/".join(parts)


def _materialize_archive_links(extract_root: Path, links: dict[str, str]) -> None:
    """Materialize validated relative archive links as ordinary files/directories.

    GitHub archives from some otherwise safe repositories encode directory aliases as
    symlinks. We never preserve those links on disk; only links whose targets stay
    inside the same archive are copied after extraction. This keeps installation
    portable and prevents link traversal.
    """
    resolving: set[str] = set()

    def resolve(name: str) -> str:
        if name not in links:
            return name
        if name in resolving:
            raise InstallError(f"symlink cycle in archive: {name}")
        resolving.add(name)
        resolved = resolve(links[name])
        resolving.remove(name)
        return resolved

    # Materialize deeper aliases first so a copied directory cannot contain an
    # unresolved alias that _copy_tree would later reject.
    for link_name in sorted(links, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        target_name = resolve(link_name)
        link_path = extract_root.joinpath(*PurePosixPath(link_name).parts)
        target_path = extract_root.joinpath(*PurePosixPath(target_name).parts)
        if not target_path.exists():
            raise InstallError(f"symlink target is missing from archive: {link_name} -> {target_name}")
        if link_path.exists() or _is_link(link_path):
            raise InstallError(f"symlink destination collides with archive member: {link_name}")
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.is_dir():
            _copy_tree(target_path, link_path)
        elif target_path.is_file():
            shutil.copy2(target_path, link_path)
        else:
            raise InstallError(f"unsupported symlink target type: {link_name} -> {target_name}")


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if _is_link(path):
            raise InstallError(f"symlink is not allowed in installed tree: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def _skill_name(root: Path) -> str:
    if _is_link(root):
        raise InstallError(f"skill root must not be a symlink or reparse point: {root}")
    skill_file = root / "SKILL.md"
    if not skill_file.is_file():
        raise InstallError(f"installed tree has no SKILL.md: {root}")
    text = skill_file.read_text(encoding="utf-8")
    match = re.search(r"^name:\s*([a-z0-9-]+)\s*$", text, re.MULTILINE)
    if not match:
        raise InstallError(f"SKILL.md has no valid name: {skill_file}")
    return match.group(1)


def _copy_tree(source: Path, destination: Path) -> None:
    if _is_link(source) or not source.is_dir():
        raise InstallError(f"skill source directory does not exist: {source}")
    if _is_link(destination):
        raise InstallError(f"skill destination must not be a symlink or reparse point: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in {".git", ".gitignore"}:
            continue
        target = destination / item.name
        if _is_link(item):
            raise InstallError(f"symlink is not allowed in skill source: {item}")
        if item.is_dir():
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def _download_github(entry: dict[str, Any], staging: Path) -> Path:
    repository = entry.get("repository")
    commit = entry.get("commit")
    raw_source_path = entry.get("path", ".")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise InstallError(f"invalid GitHub repository: {repository}")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise InstallError(f"dependency is not pinned to a full commit SHA: {repository}")
    if not isinstance(raw_source_path, str) or not raw_source_path.strip():
        raise InstallError(f"invalid pinned skill path: {repository}/{raw_source_path}")
    source_path = raw_source_path.replace("\\", "/")
    if source_path in {"", ".", "./"}:
        source_path = "."
    elif not validate_archive_member(source_path):
        raise InstallError(f"unsafe pinned skill path: {repository}/{raw_source_path}")
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / f"{repository.replace('/', '-')}-{commit}.zip"
    url = f"https://github.com/{repository}/archive/{commit}.zip"
    extract_root = staging / f"extract-{repository.replace('/', '-')}-{commit}"
    if _is_link(archive) or _is_link(extract_root):
        raise InstallError(f"download cache path must not be a symlink or reparse point: {repository}")
    if not extract_root.exists():
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                content_length = getattr(getattr(response, "headers", None), "get", lambda *_: None)("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_ARCHIVE_COMPRESSED:
                            raise InstallError(f"downloaded archive exceeds compressed size limit: {repository}")
                    except (TypeError, ValueError):
                        pass
                downloaded = 0
                temporary_archive = archive.with_name(archive.name + f".tmp-{os.getpid()}-{time.time_ns()}")
                try:
                    with temporary_archive.open("wb") as handle:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            downloaded += len(chunk)
                            if downloaded > MAX_ARCHIVE_COMPRESSED:
                                raise InstallError(f"downloaded archive exceeds compressed size limit: {repository}")
                            handle.write(chunk)
                    os.replace(temporary_archive, archive)
                finally:
                    if temporary_archive.exists():
                        temporary_archive.unlink()
        except InstallError:
            if archive.exists() and not _is_link(archive):
                archive.unlink()
            raise
        except Exception as exc:  # pragma: no cover - network dependent
            if archive.exists() and not _is_link(archive):
                archive.unlink()
            raise InstallError(f"failed to download {repository}@{commit}: {exc}") from exc
        extract_root.mkdir()
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise InstallError(f"archive has too many members: {len(members)}")
                links: dict[str, str] = {}
                member_types: dict[str, str] = {}
                total_size = 0
                for info in members:
                    if not validate_archive_member(info.filename):
                        raise InstallError(f"unsafe archive member: {info.filename}")
                    member_name = info.filename.replace("\\", "/").rstrip("/")
                    if not member_name:
                        raise InstallError(f"archive member has no canonical name: {info.filename!r}")
                    key = _windows_member_key(member_name)
                    if key in member_types:
                        raise InstallError(f"duplicate archive member: {info.filename}")
                    mode = (info.external_attr >> 16) & 0o170000
                    member_type = "dir" if info.is_dir() else "file"
                    if mode == 0o120000:
                        member_type = "link"
                    for ancestor in PurePosixPath(member_name).parents:
                        ancestor_key = _windows_member_key("/".join(ancestor.parts))
                        if ancestor_key and member_types.get(ancestor_key) in {"file", "link"}:
                            raise InstallError(f"archive member is beneath a file: {info.filename}")
                    if member_type in {"file", "link"} and any(existing.startswith(key + "/") for existing in member_types):
                        raise InstallError(f"archive file/directory collision: {info.filename}")
                    member_types[key] = member_type
                    if info.file_size > MAX_MEMBER_UNCOMPRESSED:
                        raise InstallError(f"archive member is too large: {info.filename}")
                    total_size += info.file_size
                    if total_size > MAX_ARCHIVE_UNCOMPRESSED:
                        raise InstallError("archive expands beyond the configured size limit")
                    if info.file_size and not info.compress_size:
                        raise InstallError(f"archive member has invalid compression metadata: {info.filename}")
                    if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                        raise InstallError(f"archive compression ratio is too high: {info.filename}")
                    if mode == 0o120000:
                        try:
                            target = bundle.read(info).decode("utf-8")
                        except (UnicodeError, OSError) as exc:
                            raise InstallError(f"symlink target is not valid UTF-8: {info.filename}") from exc
                        links[member_name] = _normalise_link_target(member_name, target)
                        continue
                    destination = extract_root.joinpath(*PurePosixPath(member_name).parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, destination.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                _materialize_archive_links(extract_root, links)
        except zipfile.BadZipFile as exc:
            raise InstallError(f"downloaded archive is not a valid ZIP: {repository}") from exc
    roots = list(extract_root.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise InstallError(f"unexpected archive layout: {repository}")
    source = roots[0] if source_path == "." else roots[0].joinpath(*PurePosixPath(source_path).parts)
    if not source.is_dir():
        raise InstallError(f"pinned skill path does not exist: {repository}/{source_path}")
    return source


def _materialize(entry: dict[str, Any], repository_root: Path, staging: Path) -> Path:
    source_type = entry.get("source")
    name = _validate_skill_name(entry.get("name"))
    if source_type in {"repository", "bundled"}:
        source = _safe_relative_source(repository_root, entry.get("path"), f"{name}.path")
        output = staging / name
        if source_type == "repository":
            output.mkdir(parents=True, exist_ok=True)
            for item_name in ("SKILL.md", "LICENSE", "agents", "assets", "references", "scripts"):
                item = source / item_name
                if item.exists():
                    if _is_link(item):
                        raise InstallError(f"repository source contains a symlink or reparse point: {item}")
                    if item.is_dir():
                        _copy_tree(item, output / item_name)
                    else:
                        shutil.copy2(item, output / item_name)
        else:
            _copy_tree(source, output)
        return output
    if source_type == "github":
        source = _download_github(entry, staging / "downloads")
        output = staging / name
        _copy_tree(source, output)
        # Preserve the upstream license when it lives above the selected skill
        # subdirectory. A repository without a declared license is left as-is
        # and is already surfaced by the lock manifest.
        archive_root = source
        for _ in PurePosixPath(str(entry.get("path", ".")).replace("\\", "/")).parts:
            archive_root = archive_root.parent
        for license_name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
            license_path = archive_root / license_name
            if license_path.is_file() and not _is_link(license_path):
                shutil.copy2(license_path, output / f"UPSTREAM-{license_name}")
                break
        return output
    raise InstallError(f"unsupported dependency source: {source_type}")


def _read_receipt(target: Path) -> dict[str, Any] | None:
    path = target / INSTALL_RECEIPT
    if _is_link(path):
        raise InstallError(f"installation receipt must not be a symlink: {path}")
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"installation receipt is unreadable: {path}") from exc
    return value if isinstance(value, dict) else None


def _validate_receipt_shape(receipt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise InstallError("installation receipt must be a JSON object")
    if receipt.get("schema_version") != "paper-workflow-install-v1":
        raise InstallError("unsupported or incomplete installation receipt")
    if set(receipt) != {"schema_version", "profile", "skills"}:
        raise InstallError("installation receipt has unexpected or missing fields")
    profile = receipt.get("profile")
    if profile not in PROFILES:
        raise InstallError("installation receipt has an invalid profile")
    skills = receipt.get("skills")
    if not isinstance(skills, dict) or not skills:
        raise InstallError("installation receipt has no managed skills")
    for name, data in skills.items():
        _validate_skill_name(name)
        if not isinstance(data, dict) or set(data) != {"tree_hash", "source"} or not HASH_RE.fullmatch(str(data.get("tree_hash", ""))):
            raise InstallError(f"installation receipt has an invalid tree hash: {name}")
        source = data.get("source")
        if not isinstance(source, dict):
            raise InstallError(f"installation receipt has invalid source metadata: {name}")
        source_type = source.get("source")
        required_source = {"name", "source", "path", "license"}
        if source_type == "github":
            required_source |= {"repository", "commit"}
        if source_type not in {"repository", "bundled", "github"} or set(source) != required_source:
            raise InstallError(f"installation receipt has invalid source metadata: {name}")
        if source.get("name") != name:
            raise InstallError(f"installation receipt source name mismatch: {name}")
        _validate_manifest_path(source.get("path"), f"receipt.skills.{name}.source.path")
        if not isinstance(source.get("license"), str) or not source["license"].strip():
            raise InstallError(f"installation receipt has invalid license metadata: {name}")
        if source_type == "github":
            repository = source.get("repository")
            commit = source.get("commit")
            if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise InstallError(f"installation receipt has invalid repository metadata: {name}")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise InstallError(f"installation receipt has invalid commit metadata: {name}")
    return skills


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_transaction(path: Path, state: dict[str, Any]) -> None:
    """Write a small write-ahead marker atomically and durably when possible."""
    if _is_link(path):
        raise InstallError(f"installation transaction marker must not be a symlink: {path}")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not _is_link(temporary):
            temporary.unlink()


def _recover_transaction(target: Path) -> None:
    """Recover a rename transaction left by an interrupted installer run."""
    _assert_safe_control_paths(target)
    marker = target / TRANSACTION_RECEIPT
    if _is_link(marker):
        raise InstallError(f"installation transaction marker must not be a symlink: {marker}")
    if not marker.is_file():
        return
    completed = False
    try:
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallError(f"installation transaction marker is unreadable: {marker}") from exc
        if not isinstance(state, dict):
            raise InstallError("installation transaction marker is malformed")
        expected_state_fields = {"schema_version", "backup_root", "backups", "created", "backup_intent", "create_intent", "old_receipt", "committed"}
        if set(state) != expected_state_fields:
            raise InstallError("installation transaction marker has unexpected or missing fields")
        if not isinstance(state.get("committed"), bool):
            raise InstallError("installation transaction marker has an invalid committed flag")
        if state.get("schema_version") != "paper-workflow-install-transaction-v1":
            raise InstallError("installation transaction marker has an unsupported schema")
        if state.get("committed") is True:
            completed = True
            return
        backup_raw = state.get("backup_root")
        if not isinstance(backup_raw, str) or not backup_raw.replace("\\", "/").startswith(".paper-workflow-backups/"):
            raise InstallError("installation transaction backup root is outside the control directory")
        backup_root = _safe_relative_source(target, backup_raw, "transaction.backup_root")
        if _is_link(backup_root):
            raise InstallError("transaction backup root must not be a symlink or reparse point")
        names = state.get("backups", [])
        created = state.get("created", [])
        backup_intent = state.get("backup_intent", "")
        create_intent = state.get("create_intent", "")
        if not isinstance(names, list) or not isinstance(created, list) or not isinstance(backup_intent, str) or not isinstance(create_intent, str):
            raise InstallError("installation transaction marker has malformed names")
        if len(names) != len(set(names)) or len(created) != len(set(created)):
            raise InstallError("installation transaction marker contains duplicate names")
        if backup_intent and create_intent:
            raise InstallError("installation transaction marker has conflicting intents")
        for name in names + created + ([backup_intent] if backup_intent else []) + ([create_intent] if create_intent else []):
            _validate_skill_name(name)
            backup_path = backup_root / name
            destination = target / name
            if _is_link(backup_path) or _is_link(destination):
                raise InstallError(f"transaction path must not be a symlink or reparse point: {name}")
        if backup_intent and backup_intent not in names:
            backup_candidate = backup_root / backup_intent
            destination = target / backup_intent
            if backup_candidate.exists() and destination.exists():
                raise InstallError(f"backup intent is ambiguous because both paths exist: {backup_intent}")
            if not backup_candidate.exists() and not destination.exists():
                raise InstallError(f"incomplete backup transaction cannot be recovered: {backup_intent}")
            if backup_candidate.exists():
                names.append(backup_intent)
        for name in created:
            destination = target / name
            if _is_link(destination):
                raise InstallError(f"cannot recover unsafe transaction destination: {destination}")
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
        if create_intent:
            destination = target / create_intent
            if destination.exists() and not _is_link(destination):
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
        for name in names:
            backup = backup_root / name
            destination = target / name
            backup_exists = backup.exists()
            destination_exists = destination.exists()
            if backup_exists and destination_exists:
                if name not in created and name != create_intent:
                    raise InstallError(f"backup and destination both exist during recovery: {name}")
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
                destination_exists = False
            if backup_exists and not destination_exists:
                backup.rename(destination)
            elif not backup_exists and not destination_exists:
                raise InstallError(f"backup is missing during transaction recovery: {name}")
        old_receipt = state.get("old_receipt")
        receipt_path = target / INSTALL_RECEIPT
        if isinstance(old_receipt, str) and old_receipt:
            old_receipt_normalised = old_receipt.replace("\\", "/")
            if not old_receipt_normalised.startswith(backup_raw.rstrip("/") + "/") or not old_receipt_normalised.endswith("/INSTALL_RECEIPT.old"):
                raise InstallError("transaction.old_receipt is outside the backup transaction")
            old_path = _safe_relative_source(target, old_receipt, "transaction.old_receipt")
            if not old_path.is_file() or _is_link(old_path):
                raise InstallError("transaction old receipt backup is missing or unsafe")
            if _is_link(receipt_path):
                raise InstallError("installation receipt is a symlink during recovery")
            os.replace(old_path, receipt_path)
        elif receipt_path.exists():
            if _is_link(receipt_path):
                raise InstallError("installation receipt is a symlink during recovery")
            receipt_path.unlink()
        completed = True
    finally:
        if completed and marker.exists() and not _is_link(marker):
            marker.unlink()


def _prepare_target(target: Path, staged: dict[str, Path], update: bool, prune: bool) -> tuple[list[str], list[str]]:
    receipt_value = _read_receipt(target)
    managed = _validate_receipt_shape(receipt_value) if receipt_value is not None else {}
    adopted: list[str] = []
    for name, source in staged.items():
        destination = target / name
        if not destination.exists():
            continue
        if _is_link(destination):
            raise InstallError(f"symlink destination is not allowed: {destination}")
        if not destination.is_dir():
            raise InstallError(f"existing destination is not a directory: {destination}")
        current_hash = _tree_hash(destination)
        desired_hash = _tree_hash(source)
        if current_hash == desired_hash:
            adopted.append(name)
            continue
        if not update or name not in managed:
            raise InstallError(f"existing unmanaged or changed skill blocks installation: {destination}; use --update after adopting it")
    orphaned = sorted(set(managed) - set(staged))
    if orphaned and not prune:
        raise InstallError("managed skills outside the selected profile remain; rerun with --prune to remove them: " + ", ".join(orphaned))
    return adopted, orphaned


def install(manifest: dict[str, Any], profile: str, target: Path, repository_root: Path, *, dry_run: bool = False, update: bool = False, prune: bool = False) -> dict[str, Any]:
    entries = resolve_profile(manifest, profile)
    target = target.expanduser()
    if _is_link(target):
        raise InstallError(f"symlink target directory is not allowed: {target}")
    _assert_no_link_components(target.parent)
    target = target.resolve()
    if target.exists() and not target.is_dir():
        raise InstallError(f"target is not a directory: {target}")
    _assert_safe_control_paths(target)
    if target.exists():
        _recover_transaction(target)
    parent = target.parent
    if parent.exists() and not parent.is_dir():
        raise InstallError(f"target parent is not a directory: {parent}")
    if not parent.exists() and not dry_run:
        parent.mkdir(parents=True, exist_ok=True)
    staging_parent = str(parent) if parent.exists() else None
    with tempfile.TemporaryDirectory(prefix="paper-workflow-install-", dir=staging_parent) as temp_name:
        staging = Path(temp_name)
        staged: dict[str, Path] = {}
        for entry in entries:
            materialized = _materialize(entry, repository_root, staging)
            actual_name = _skill_name(materialized)
            if actual_name != entry["name"]:
                raise InstallError(f"skill name mismatch: expected {entry['name']}, found {actual_name}")
            staged[entry["name"]] = materialized
        adopted, orphaned = _prepare_target(target, staged, update, prune)
        summary = {"status": "dry-run" if dry_run else "pass", "profile": profile, "target": str(target), "skills": sorted(staged)}
        summary["licenses"] = {entry["name"]: entry.get("license", "") for entry in entries}
        summary["license_warnings"] = [
            entry["name"] for entry in entries
            if str(entry.get("license", "")).lower().startswith("not declared")
            or "cc by-nc" in str(entry.get("license", "")).lower()
        ]
        if orphaned:
            summary["pruned"] = orphaned
        if dry_run:
            return summary
        existing_receipt = _read_receipt(target) if target.exists() else None
        existing_managed = _validate_receipt_shape(existing_receipt) if existing_receipt is not None else None
        expected_sources = {entry["name"]: entry for entry in entries}
        if (
            existing_receipt is not None
            and existing_receipt.get("profile") == profile
            and not orphaned
            and set(adopted) == set(staged)
            and set(existing_managed or {}) == set(expected_sources)
            and all(_canonical_json(existing_managed[name]["source"]) == _canonical_json(expected_sources[name]) for name in expected_sources)
        ):
            summary["backups"] = {}
            return summary
        target.mkdir(parents=True, exist_ok=True)
        _assert_safe_control_paths(target)
        backups: dict[str, str] = {}
        receipt = _read_receipt(target)
        managed = _validate_receipt_shape(receipt) if receipt is not None else {}
        backup_parent = target / ".paper-workflow-backups"
        if _is_link(backup_parent):
            raise InstallError(f"backup directory must not be a symlink or reparse point: {backup_parent}")
        backup_parent.mkdir(parents=True, exist_ok=True)
        _assert_no_link_components(backup_parent)
        backup_root = backup_parent / str(time.time_ns())
        if _is_link(backup_root) or backup_root.exists():
            raise InstallError(f"backup transaction directory is not available: {backup_root}")
        touched: set[str] = set()
        receipt_path = target / INSTALL_RECEIPT
        old_receipt = receipt_path.read_bytes() if receipt_path.is_file() else None
        transaction_path = target / TRANSACTION_RECEIPT
        old_receipt_backup: Path | None = None
        transaction_state = {
            "schema_version": "paper-workflow-install-transaction-v1",
            "backup_root": backup_root.relative_to(target).as_posix(),
            "backups": [],
            "created": [],
            "backup_intent": "",
            "create_intent": "",
            "old_receipt": "",
            "committed": False,
        }
        try:
            if old_receipt is not None:
                old_receipt_backup = backup_root / "INSTALL_RECEIPT.old"
                old_receipt_backup.parent.mkdir(parents=True, exist_ok=True)
                old_receipt_backup.write_bytes(old_receipt)
                transaction_state["old_receipt"] = old_receipt_backup.relative_to(target).as_posix()
            _write_transaction(transaction_path, transaction_state)
            for name, source in staged.items():
                destination = target / name
                if _is_link(destination):
                    raise InstallError(f"symlink destination is not allowed: {destination}")
                if name in adopted and destination.is_dir():
                    # A byte-identical tree is already installed. Keep it in
                    # place so a repeated install is content- and backup-idempotent.
                    continue
                if destination.exists() and (name in managed or _tree_hash(destination) == _tree_hash(source)):
                    backup = backup_root / name
                    if _is_link(backup_root):
                        raise InstallError(f"unsafe backup path: {backup}")
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    _assert_no_link_components(backup_root)
                    transaction_state["backup_intent"] = name
                    _write_transaction(transaction_path, transaction_state)
                    destination.rename(backup)
                    backups[name] = str(backup)
                    transaction_state["backups"].append(name)
                    transaction_state["backup_intent"] = ""
                    _write_transaction(transaction_path, transaction_state)
                touched.add(name)
                transaction_state["create_intent"] = name
                _write_transaction(transaction_path, transaction_state)
                source.rename(destination)
                transaction_state["created"].append(name)
                transaction_state["create_intent"] = ""
                _write_transaction(transaction_path, transaction_state)
            for name in orphaned:
                destination = target / name
                if not destination.exists() or _is_link(destination):
                    raise InstallError(f"managed skill to prune is missing or unsafe: {name}")
                backup = backup_root / name
                if _is_link(backup_root):
                    raise InstallError(f"unsafe backup path: {backup}")
                backup.parent.mkdir(parents=True, exist_ok=True)
                _assert_no_link_components(backup_root)
                transaction_state["backup_intent"] = name
                _write_transaction(transaction_path, transaction_state)
                destination.rename(backup)
                backups[name] = str(backup)
                transaction_state["backups"].append(name)
                transaction_state["backup_intent"] = ""
                _write_transaction(transaction_path, transaction_state)
                touched.add(name)
            new_receipt = {
                "schema_version": "paper-workflow-install-v1",
                "profile": profile,
                "skills": {name: {"tree_hash": _tree_hash(target / name), "source": next(e for e in entries if e["name"] == name)} for name in staged},
            }
            receipt_tmp = target / f".{INSTALL_RECEIPT}.tmp-{os.getpid()}-{time.time_ns()}"
            try:
                receipt_tmp.write_text(json.dumps(new_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                _validate_receipt_shape(new_receipt)
                os.replace(receipt_tmp, receipt_path)
            finally:
                if receipt_tmp.exists():
                    receipt_tmp.unlink()
            transaction_state["committed"] = True
            _write_transaction(transaction_path, transaction_state)
            # The committed marker is deliberately written before cleanup. If
            # cleanup is interrupted, the next invocation can safely remove the
            # marker without rolling back an already committed installation.
            if transaction_path.exists():
                if _is_link(transaction_path):
                    raise InstallError("installation transaction marker became a symlink after commit")
                transaction_path.unlink()
        except BaseException:
            if transaction_state.get("committed"):
                # The installation is committed; never turn a cleanup failure
                # into a destructive rollback. Leave the marker for recovery.
                raise
            rollback_error: BaseException | None = None
            created_or_inflight = set(transaction_state["created"])
            if transaction_state.get("create_intent"):
                created_or_inflight.add(transaction_state["create_intent"])
            for name in touched:
                destination = target / name
                if name in created_or_inflight and destination.exists():
                    try:
                        if _is_link(destination):
                            raise InstallError(f"cannot roll back a symlink destination: {destination}")
                        if destination.is_dir():
                            shutil.rmtree(destination)
                        else:
                            destination.unlink()
                    except BaseException as exc:
                        rollback_error = rollback_error or exc
            backup_names = set(backups)
            if transaction_state.get("backup_intent"):
                backup_names.add(transaction_state["backup_intent"])
            for name in backup_names:
                backup = backup_root / name
                destination = target / name
                try:
                    if _is_link(backup):
                        raise InstallError(f"cannot roll back from a symlink backup: {backup}")
                    if backup.exists():
                        if destination.exists():
                            if _is_link(destination):
                                raise InstallError(f"cannot roll back over a symlink destination: {destination}")
                            if name in created_or_inflight:
                                if destination.is_dir():
                                    shutil.rmtree(destination)
                                else:
                                    destination.unlink()
                            else:
                                raise InstallError(f"backup and destination both exist during rollback: {name}")
                        backup.rename(destination)
                    elif not destination.exists():
                        raise InstallError(f"backup is missing during rollback: {name}")
                except BaseException as exc:
                    rollback_error = rollback_error or exc
            try:
                if old_receipt is None:
                    if receipt_path.exists():
                        if _is_link(receipt_path):
                            raise InstallError(f"cannot remove a symlink receipt: {receipt_path}")
                        receipt_path.unlink()
                else:
                    if _is_link(receipt_path):
                        raise InstallError(f"cannot restore over a symlink receipt: {receipt_path}")
                    receipt_path.write_bytes(old_receipt)
            except BaseException as exc:
                rollback_error = rollback_error or exc
            if rollback_error is None:
                try:
                    if transaction_path.exists():
                        if _is_link(transaction_path):
                            raise InstallError(f"cannot remove a symlink transaction marker: {transaction_path}")
                        transaction_path.unlink()
                except BaseException as exc:
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise InstallError("installation failed and rollback was incomplete; transaction evidence was retained") from rollback_error
            raise
        summary["backups"] = backups
        return summary


def verify(target: Path, profile: str | None = None, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    target = target.expanduser()
    if _is_link(target):
        raise InstallError(f"symlink target directory is not allowed: {target}")
    _assert_no_link_components(target.parent)
    target = target.resolve()
    _assert_safe_control_paths(target)
    if target.exists():
        _recover_transaction(target)
    receipt = _read_receipt(target)
    if not receipt:
        raise InstallError(f"no valid installation receipt found: {target / INSTALL_RECEIPT}")
    managed = _validate_receipt_shape(receipt)
    errors = []
    if profile is not None:
        if manifest is None:
            raise InstallError("manifest is required when verifying a requested profile")
        expected_entries = resolve_profile(manifest, profile)
        expected = {entry["name"]: entry for entry in expected_entries}
        if receipt.get("profile") != profile:
            errors.append(f"installation receipt profile mismatch: expected {profile}, found {receipt.get('profile')}")
        if set(managed) != set(expected):
            errors.append("installation receipt skill set does not match the requested profile")
        for name, entry in expected.items():
            actual = managed.get(name, {}).get("source") if isinstance(managed.get(name), dict) else None
            if actual is not None and _canonical_json(actual) != _canonical_json(entry):
                errors.append(f"installation receipt source metadata mismatch: {name}")
    for name, data in managed.items():
        path = target / name
        if _is_link(path) or not path.is_dir():
            errors.append(f"missing installed skill: {name}")
        elif data.get("tree_hash") != _tree_hash(path):
            errors.append(f"installed skill hash mismatch: {name}")
        else:
            try:
                if _skill_name(path) != name:
                    errors.append(f"installed skill name mismatch: {name}")
            except InstallError as exc:
                errors.append(str(exc))
    return {"status": "pass" if not errors else "blocked", "target": str(target), "profile": receipt.get("profile"), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="standard")
    parser.add_argument("--target", type=Path, default=default_target())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--prune", action="store_true", help="remove managed skills outside the selected profile after backing them up")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            manifest = load_manifest(args.manifest)
            result = verify(args.target, args.profile, manifest)
        else:
            result = install(load_manifest(args.manifest), args.profile, args.target, args.manifest.resolve().parent, dry_run=args.dry_run, update=args.update, prune=args.prune)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"pass", "dry-run"} else 1
    except Exception as exc:
        print(json.dumps({"status": "blocked", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
