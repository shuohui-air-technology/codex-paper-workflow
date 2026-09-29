"""Resolve installed Skill identities and pinned validator implementations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from scripts.install_workflow import is_generated_python_cache_file

from .schema import WorkflowIssue


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_SKILLS_PER_ROOT = 512
_MAX_SKILL_TREE_ENTRIES = 20_000
_MAX_SKILL_TREE_FILES = 10_000
_MAX_SKILL_FILE_BYTES = 32 * 1024 * 1024
_MAX_SKILL_TREE_BYTES = 512 * 1024 * 1024
_MAX_SKILL_FRONTMATTER_BYTES = 256 * 1024
_MAX_INSTALL_RECEIPT_BYTES = 1024 * 1024
_INSTALL_RECEIPT_NAME = ".paper-workflow-install.json"
ROOT_CATALOG_FAILURE_CODES = frozenset({"catalog.invalid_root", "catalog.too_many_skills"})
_ADAPTERS = frozenset(
    {
        "experiment_contract_v1",
        "figure_contract_v1",
        "final_edit_receipt_v1",
        "humanizer_preflight_v1",
        "paper_section_v1",
    }
)


class CatalogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SkillIdentity:
    catalog_id: str
    root: Path
    relative_path: str
    skill_sha256: str
    tree_sha256: str
    locked: bool


@dataclass(frozen=True)
class ValidatorIdentity:
    validator_id: str
    script: Path
    sha256: str
    adapter: str
    input_schema: str
    control_tags: tuple[str, ...]
    outcomes: tuple[str, ...]


@dataclass(frozen=True)
class CatalogResult:
    skills: dict[str, SkillIdentity]
    errors: tuple[WorkflowIssue, ...]
    warnings: tuple[WorkflowIssue, ...]
    failed_skill_ids: frozenset[str] = frozenset()


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


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path, *, prefixed: bool, max_bytes: int | None = None) -> str:
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise CatalogError("catalog.file_too_large", f"Skill file exceeds the per-file size limit: {path}")
    digest = hashlib.sha256()
    total_read = 0
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            total_read += len(chunk)
            if max_bytes is not None and total_read > max_bytes:
                raise CatalogError("catalog.file_too_large", f"Skill file grew beyond the per-file size limit: {path}")
            digest.update(chunk)
    value = digest.hexdigest()
    return "sha256:" + value if prefixed else value


def tree_sha256(path: Path) -> str:
    """Return the installer-compatible digest for a literal Skill tree."""
    tree_hash, _ = _tree_hashes(path)
    return tree_hash


def _tree_hashes(path: Path, *, selected_file: Path | None = None) -> tuple[str, str | None]:
    """Hash a Skill tree and optionally one file after a complete metadata preflight."""
    try:
        root = path.resolve()
    except OSError as exc:
        raise CatalogError("catalog.invalid_tree", f"Skill tree cannot be resolved: {path}") from exc
    if _is_link(path) or not root.is_dir():
        raise CatalogError("catalog.invalid_tree", f"Skill tree is not a regular directory: {path}")
    selected = selected_file
    if selected is not None:
        try:
            selected = selected.resolve()
        except OSError as exc:
            raise CatalogError("catalog.invalid_tree", f"Skill file cannot be resolved: {selected_file}") from exc
        if not _contains(root, selected):
            raise CatalogError("catalog.invalid_tree", f"Selected Skill file is outside its tree: {selected_file}")
    try:
        return _tree_digest_contents(root, path, selected_file=selected)
    except OSError as exc:
        raise CatalogError("catalog.unreadable_tree", f"Skill tree cannot be read safely: {path}") from exc


def _tree_digest_contents(
    root: Path, path: Path, *, selected_file: Path | None = None
) -> tuple[str, str | None]:
    files: list[tuple[Path, int]] = []
    entries = 0
    total_bytes = 0
    for candidate in root.rglob("*"):
        entries += 1
        if entries > _MAX_SKILL_TREE_ENTRIES:
            raise CatalogError("catalog.tree_too_many_entries", f"Skill tree exceeds the entry limit: {path}")
        if _is_link(candidate):
            raise CatalogError("catalog.symlink_in_tree", f"Skill tree contains a symlink or reparse point: {candidate}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise CatalogError("catalog.invalid_tree", f"Skill tree contains a non-regular filesystem entry: {candidate}")
        if is_generated_python_cache_file(candidate.relative_to(root)):
            continue
        size = candidate.stat().st_size
        if size > _MAX_SKILL_FILE_BYTES:
            raise CatalogError("catalog.file_too_large", f"Skill file exceeds the per-file size limit: {candidate}")
        total_bytes += size
        if total_bytes > _MAX_SKILL_TREE_BYTES:
            raise CatalogError("catalog.tree_too_large", f"Skill tree exceeds the total size limit: {path}")
        files.append((candidate, size))
        if len(files) > _MAX_SKILL_TREE_FILES:
            raise CatalogError("catalog.tree_too_many_files", f"Skill tree exceeds the file-count limit: {path}")
    digest = hashlib.sha256()
    selected_digest = hashlib.sha256() if selected_file is not None else None
    selected_found = selected_file is None
    total_read = 0
    for candidate, expected_size in sorted(files, key=lambda item: item[0]):
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        is_selected = selected_file == candidate
        if is_selected:
            selected_found = True
        file_read = 0
        with candidate.open("rb") as source:
            while chunk := source.read(_HASH_CHUNK_BYTES):
                file_read += len(chunk)
                total_read += len(chunk)
                if file_read > _MAX_SKILL_FILE_BYTES or total_read > _MAX_SKILL_TREE_BYTES:
                    raise CatalogError("catalog.tree_too_large", f"Skill tree changed while being scanned: {path}")
                digest.update(chunk)
                if is_selected and selected_digest is not None:
                    selected_digest.update(chunk)
        if file_read != expected_size:
            raise CatalogError("catalog.tree_changed", f"Skill file changed while being scanned: {candidate}")
        digest.update(b"\0")
    if not selected_found:
        raise CatalogError("catalog.invalid_tree", f"Selected Skill file is not a regular tree file: {selected_file}")
    selected_hash = selected_digest.hexdigest() if selected_digest is not None else None
    return "sha256:" + digest.hexdigest(), selected_hash


def _frontmatter_name(skill_file: Path) -> str:
    try:
        with skill_file.open("rb") as source:
            raw = source.read(_MAX_SKILL_FRONTMATTER_BYTES + 1)
        lines = raw[:_MAX_SKILL_FRONTMATTER_BYTES].decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError("catalog.invalid_frontmatter", f"Skill frontmatter is unreadable: {skill_file}") from exc
    if not lines or lines[0] != "---":
        raise CatalogError("catalog.invalid_frontmatter", f"Skill must start with YAML frontmatter: {skill_file}")
    try:
        close = lines.index("---", 1)
    except ValueError as exc:
        if len(raw) > _MAX_SKILL_FRONTMATTER_BYTES:
            raise CatalogError("catalog.frontmatter_too_large", f"Skill frontmatter exceeds the size limit: {skill_file}") from exc
        raise CatalogError("catalog.invalid_frontmatter", f"Skill frontmatter is not closed: {skill_file}") from exc
    names = [line[5:].strip() for line in lines[1:close] if line.startswith("name:")]
    if len(names) != 1 or not _SKILL_NAME_RE.fullmatch(names[0]):
        raise CatalogError("catalog.invalid_frontmatter", f"Skill frontmatter has no valid name: {skill_file}")
    return names[0]


def _receipt_tree_hash(receipt: object, catalog_id: str) -> str | None:
    if not isinstance(receipt, Mapping):
        return None
    skills = receipt.get("skills")
    if not isinstance(skills, Mapping):
        return None
    record = skills.get(catalog_id)
    if not isinstance(record, Mapping):
        return None
    value = record.get("tree_hash")
    return value if isinstance(value, str) else None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate installation receipt key: {key}")
        result[key] = value
    return result


def load_install_receipts(roots: tuple[Path, ...]) -> dict[Path, object]:
    """Read bounded installer receipts; malformed or unsafe files confer no lock.

    The receipt is local metadata, not a signature. A Skill is considered locked
    only when its actual tree digest also matches the receipt entry.
    """
    from scripts.install_workflow import _validate_receipt_shape

    receipts: dict[Path, object] = {}
    for supplied_root in roots:
        try:
            root = supplied_root.expanduser().resolve()
            path = root / _INSTALL_RECEIPT_NAME
            if _is_link(path):
                continue
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_INSTALL_RECEIPT_BYTES:
                    continue
                raw = source.read(_MAX_INSTALL_RECEIPT_BYTES + 1)
                after = os.fstat(source.fileno())
            current = path.lstat()
            if (
                len(raw) != info.st_size
                or len(raw) > _MAX_INSTALL_RECEIPT_BYTES
                or _is_link(path)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or (current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_nlink)
                != (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)
                != (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
            ):
                continue
            receipt = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            _validate_receipt_shape(receipt)
            receipts[root] = receipt
        except (OSError, UnicodeError, ValueError, TypeError):
            # Missing, malformed, redirected, or unreadable receipt: Skills in
            # this root remain usable but explicitly unlocked.
            continue
    return receipts


def _issue(severity: str, code: str, message: str) -> WorkflowIssue:
    return WorkflowIssue(severity=severity, code=code, message=message)


def discover_skills(
    roots: tuple[Path, ...], install_receipts: Mapping[Path, object]
) -> CatalogResult:
    """Discover direct-child Skills without executing any Skill-owned content."""
    receipts = {Path(root).expanduser().resolve(): receipt for root, receipt in install_receipts.items()}
    skills: dict[str, SkillIdentity] = {}
    errors: list[WorkflowIssue] = []
    warnings: list[WorkflowIssue] = []
    ambiguous: set[str] = set()
    failed_skill_ids: set[str] = set()

    for supplied_root in roots:
        root = supplied_root.expanduser().resolve()
        if not root.is_dir():
            continue
        children: list[Path] = []
        try:
            for child in root.iterdir():
                children.append(child)
                if len(children) > _MAX_SKILLS_PER_ROOT:
                    raise CatalogError(
                        "catalog.too_many_skills",
                        f"Skill root exceeds the catalog limit of {_MAX_SKILLS_PER_ROOT}: {root}",
                    )
        except CatalogError as exc:
            errors.append(_issue("error", exc.code, str(exc)))
            continue
        except OSError as exc:
            errors.append(_issue("error", "catalog.invalid_root", f"Skill root cannot be read: {root}: {exc}"))
            continue
        for child in sorted(children, key=lambda item: item.name):
            direct_link = _is_link(child)
            try:
                candidate = child.resolve()
            except OSError:
                errors.append(_issue("error", "catalog.invalid_skill", f"Skill path cannot be resolved: {child}"))
                failed_skill_ids.add(child.name)
                continue
            if direct_link and not _contains(root, candidate):
                errors.append(_issue("error", "catalog.symlink_escape", f"Skill directory symlink escapes root: {child}"))
                failed_skill_ids.add(child.name)
                continue
            if not candidate.is_dir():
                continue
            skill_file = candidate / "SKILL.md"
            if _is_link(skill_file):
                errors.append(_issue("error", "catalog.symlink_in_tree", f"Skill tree contains a symlink or reparse point: {skill_file}"))
                failed_skill_ids.add(child.name)
                continue
            if not skill_file.is_file():
                continue
            try:
                catalog_id = _frontmatter_name(skill_file)
                if catalog_id != child.name:
                    raise CatalogError("catalog.name_mismatch", f"Skill directory and frontmatter name differ: {child}")
                tree_hash, skill_hash = _tree_hashes(candidate, selected_file=skill_file)
                if skill_hash is None:
                    raise CatalogError("catalog.invalid_tree", f"Skill entrypoint is missing from its tree: {skill_file}")
            except CatalogError as exc:
                errors.append(_issue("error", exc.code, str(exc)))
                failed_skill_ids.add(child.name)
                continue

            locked = _receipt_tree_hash(receipts.get(root), catalog_id) == tree_hash
            identity = SkillIdentity(
                catalog_id=catalog_id,
                root=root,
                relative_path=child.name,
                skill_sha256=skill_hash,
                tree_sha256=tree_hash,
                locked=locked,
            )
            if not locked:
                warnings.append(_issue("warning", "catalog.unlocked_skill", f"Skill is not bound by an installation receipt: {child}"))
            if catalog_id in ambiguous:
                continue
            previous = skills.get(catalog_id)
            if previous is None:
                skills[catalog_id] = identity
            elif (previous.skill_sha256, previous.tree_sha256) != (identity.skill_sha256, identity.tree_sha256):
                del skills[catalog_id]
                ambiguous.add(catalog_id)
                failed_skill_ids.add(catalog_id)
                errors.append(_issue("error", "catalog.ambiguous_skill", f"Skill ID has different contents across roots: {catalog_id}"))
    return CatalogResult(
        skills=skills,
        errors=tuple(errors),
        warnings=tuple(warnings),
        failed_skill_ids=frozenset(failed_skill_ids),
    )


def resolve_skill_roots(
    explicit: tuple[Path, ...], install_target: Path | None, environ: dict[str, str]
) -> tuple[Path, ...]:
    ordered = list(explicit)
    if install_target is not None:
        ordered.append(install_target)
    codex_root = Path(environ["CODEX_HOME"]) if environ.get("CODEX_HOME") else Path.home() / ".codex"
    ordered.append(codex_root / "skills")
    unique: list[Path] = []
    for path in ordered:
        expanded = path.expanduser()
        if expanded not in unique:
            unique.append(expanded)
    return tuple(unique)


def _safe_script(repository_root: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise CatalogError("validator.invalid_script_path", "validator script must be a non-empty relative path")
    normalized = raw.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts or any(part in {"", "."} for part in posix.parts):
        raise CatalogError("validator.invalid_script_path", f"validator script is not a safe relative path: {raw}")
    path = repository_root
    for part in posix.parts:
        path = path / part
        if _is_link(path):
            raise CatalogError("validator.symlink", f"validator script must not use a symlink or reparse point: {raw}")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise CatalogError("validator.invalid_script_path", f"validator script cannot be resolved: {raw}") from exc
    if not _contains(repository_root, resolved) or not resolved.is_file():
        raise CatalogError("validator.invalid_script_path", f"validator script is outside the repository or missing: {raw}")
    return resolved


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise CatalogError("validator.invalid_registry", f"{label} must be a list of non-empty strings")
    return tuple(value)


def load_validator_registry(path: Path, repository_root: Path) -> dict[str, ValidatorIdentity]:
    """Load only the fixed, hash-bound validator adapters shipped by the repository."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("validator.invalid_registry", f"validator registry is unreadable JSON: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != "paper-workflow-validator-registry-v1":
        raise CatalogError("validator.invalid_registry", "unsupported validator registry schema")
    entries = data.get("validators")
    if not isinstance(entries, dict):
        raise CatalogError("validator.invalid_registry", "validator registry must contain a validators object")
    root = repository_root.expanduser().resolve()
    validators: dict[str, ValidatorIdentity] = {}
    fields = {"script", "sha256", "adapter", "input_schema", "control_tags", "outcomes"}
    for validator_id, entry in sorted(entries.items()):
        if not isinstance(validator_id, str) or not _NAME_RE.fullmatch(validator_id) or not isinstance(entry, dict) or set(entry) != fields:
            raise CatalogError("validator.invalid_registry", f"invalid validator registry entry: {validator_id!r}")
        digest = entry["sha256"]
        adapter = entry["adapter"]
        input_schema = entry["input_schema"]
        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
            raise CatalogError("validator.invalid_registry", f"validator has an invalid SHA-256: {validator_id}")
        if not isinstance(adapter, str) or adapter not in _ADAPTERS:
            raise CatalogError("validator.invalid_adapter", f"validator adapter is not registered: {validator_id}")
        if not isinstance(input_schema, str) or not input_schema:
            raise CatalogError("validator.invalid_registry", f"validator input_schema is invalid: {validator_id}")
        control_tags = _string_tuple(entry["control_tags"], f"validator control_tags: {validator_id}")
        outcomes = _string_tuple(entry["outcomes"], f"validator outcomes: {validator_id}")
        script = _safe_script(root, entry["script"])
        actual = _sha256(script, prefixed=False)
        if actual != digest:
            raise CatalogError("validator.hash_mismatch", f"validator script hash does not match registry: {validator_id}")
        validators[validator_id] = ValidatorIdentity(
            validator_id=validator_id,
            script=script,
            sha256=digest,
            adapter=adapter,
            input_schema=input_schema,
            control_tags=control_tags,
            outcomes=outcomes,
        )
    return validators
