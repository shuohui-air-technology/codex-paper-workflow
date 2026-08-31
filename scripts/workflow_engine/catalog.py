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

from .schema import WorkflowIssue


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
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


def _sha256(path: Path, *, prefixed: bool) -> str:
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    return "sha256:" + value if prefixed else value


def tree_sha256(path: Path) -> str:
    """Return the installer-compatible digest for a literal Skill tree."""
    root = path.resolve()
    if _is_link(path) or not root.is_dir():
        raise CatalogError("catalog.invalid_tree", f"Skill tree is not a regular directory: {path}")
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if _is_link(candidate):
            raise CatalogError("catalog.symlink_in_tree", f"Skill tree contains a symlink or reparse point: {candidate}")
        if candidate.is_file():
            files.append(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + candidate.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def _frontmatter_name(skill_file: Path) -> str:
    try:
        lines = skill_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError("catalog.invalid_frontmatter", f"Skill frontmatter is unreadable: {skill_file}") from exc
    if not lines or lines[0] != "---":
        raise CatalogError("catalog.invalid_frontmatter", f"Skill must start with YAML frontmatter: {skill_file}")
    try:
        close = lines.index("---", 1)
    except ValueError as exc:
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

    for supplied_root in roots:
        root = supplied_root.expanduser().resolve()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            direct_link = _is_link(child)
            try:
                candidate = child.resolve()
            except OSError:
                errors.append(_issue("error", "catalog.invalid_skill", f"Skill path cannot be resolved: {child}"))
                continue
            if direct_link and not _contains(root, candidate):
                errors.append(_issue("error", "catalog.symlink_escape", f"Skill directory symlink escapes root: {child}"))
                continue
            if not candidate.is_dir():
                continue
            skill_file = candidate / "SKILL.md"
            if _is_link(skill_file):
                errors.append(_issue("error", "catalog.symlink_in_tree", f"Skill tree contains a symlink or reparse point: {skill_file}"))
                continue
            if not skill_file.is_file():
                continue
            try:
                catalog_id = _frontmatter_name(skill_file)
                if catalog_id != child.name:
                    raise CatalogError("catalog.name_mismatch", f"Skill directory and frontmatter name differ: {child}")
                skill_hash = _sha256(skill_file, prefixed=False)
                tree_hash = tree_sha256(candidate)
            except CatalogError as exc:
                errors.append(_issue("error", exc.code, str(exc)))
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
                errors.append(_issue("error", "catalog.ambiguous_skill", f"Skill ID has different contents across roots: {catalog_id}"))
    return CatalogResult(skills=skills, errors=tuple(errors), warnings=tuple(warnings))


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
