"""Track working materials, review changes, and resolve adopted versions.

The registry describes candidate bundles, never which candidate is current.
Adoption and immutable reading use the existing confirmed-artifact catalog.
Discovery is advisory: file dates and names never select an adopted version.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import progress_manager as progress
from scripts.artifact_manager import ArtifactService
from scripts.confirmed_artifacts import (
    ConfirmedArtifactError, ConfirmedArtifactStore, MAX_FILES, MAX_FILE_BYTES,
    _bytes, _check_collisions, _hash, _identifier, _keys, _path, _revision,
    _text, normalize_request,
)
from scripts.workflow_engine.fs import (
    PathSafetyError, _file_identity, _write_bytes_atomic, atomic_write_json,
    ensure_project_directory, hash_project_file, read_project_json_object,
    resolve_project_path,
)
from scripts.workflow_engine.schema import WorkflowError
from scripts.workflow_engine.store import StoreError

BASE = ".research/materials"
REGISTRY = f"{BASE}/registry.json"
SCAN = f"{BASE}/last-scan.json"
INDEX = f"{BASE}/INDEX.md"
REGISTRY_SCHEMA = "material-registry-v1"
ROLE_SCHEMA = "material-role-v1"
REVIEW_SCHEMA = "material-review-v1"
SCAN_SCHEMA = "material-scan-v1"
MAX_ROLES = 128
MAX_CANDIDATES = 16
MAX_SCAN_FILES = 10000
MAX_SCAN_BYTES = 2 * 1024 * 1024 * 1024
SKIP_DIRS = {".git", ".agents", ".codex", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_PREFIXES = (BASE, ".research/confirmed-artifacts", ".research/custom-workflow", ".research/agent_runs", ".research/stage_receipts", "artifacts/current", "artifacts/INDEX.md")


class MaterialError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = f"materials.{code}"


def fail(code, message):
    raise MaterialError(code, message)


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def working_path(value):
    _path(value)
    folded = value.casefold()
    if any(folded == prefix or folded.startswith(prefix + "/") for prefix in SKIP_PREFIXES) or ".git" in folded.split("/"):
        fail("generated_source", "Register original working materials, not generated state, snapshots or display copies.")
    return value


def bundle_signature(entrypoint, files):
    """Paths may move; relative layout and bytes determine bundle equality."""
    return digest({"entrypoint": entrypoint, "files": sorted(
        [{"relative_path": item["relative_path"], "sha256": item["sha256"]} for item in files],
        key=lambda item: item["relative_path"],
    )})


def validate_candidate(value, *, persisted=False):
    _keys(value, {"entrypoint", "files"}, {"registered_revision"})
    if persisted:
        _revision(value.get("registered_revision"))
    if type(value["files"]) is not list or not 1 <= len(value["files"]) <= MAX_FILES:
        fail("invalid_bundle", "Declare a non-empty, bounded file bundle.")
    for item in value["files"]:
        _keys(item, {"source_path", "relative_path"}, {"source_artifact_id", "registered_sha256"})
        working_path(item["source_path"])
        _path(item["relative_path"])
        if "source_artifact_id" in item:
            _identifier(item["source_artifact_id"])
        if persisted:
            _hash(item.get("registered_sha256"))
    _check_collisions([item["source_path"] for item in value["files"]])
    _check_collisions([item["relative_path"] for item in value["files"]])
    if value["entrypoint"] not in {item["relative_path"] for item in value["files"]}:
        fail("invalid_entrypoint", "Entrypoint must name one declared bundle file.")


class MaterialService:
    def __init__(self, project_root):
        self.catalog = ConfirmedArtifactStore(project_root)
        self.root = self.catalog.root
        self.artifacts = ArtifactService(self.root)

    def _read(self, relative):
        path = self.catalog._checked(relative)
        return read_project_json_object(self.root, relative) if path.exists() else None

    def _write(self, relative, value):
        _path(relative)
        ensure_project_directory(self.root, str(Path(relative).parent).replace("\\", "/"))
        atomic_write_json(self.catalog._checked(relative), value)

    @contextmanager
    def _locked_registry(self):
        ensure_project_directory(self.root, BASE)
        path = self.catalog._checked(REGISTRY)
        self.catalog._checked(REGISTRY + ".lock")
        with progress.progress_lock(path):
            yield

    def _registry(self):
        value = self._read(REGISTRY)
        if value is None:
            return {"schema_version": REGISTRY_SCHEMA, "revision": 0, "roles": {}}
        _keys(value, {"schema_version", "revision", "roles"})
        if value["schema_version"] != REGISTRY_SCHEMA:
            fail("invalid_registry", "Unsupported material registry schema.")
        _revision(value["revision"])
        if type(value["roles"]) is not dict or len(value["roles"]) > MAX_ROLES:
            fail("invalid_registry", "Material roles must be a bounded object.")
        for role_id, role in value["roles"].items():
            _identifier(role_id)
            _keys(role, {"kind", "artifact_type", "candidates"})
            self._kind(role)
            if type(role["candidates"]) is not dict or not 1 <= len(role["candidates"]) <= MAX_CANDIDATES:
                fail("invalid_registry", "Each role requires a bounded set of candidates.")
            for candidate_id, candidate in role["candidates"].items():
                _identifier(candidate_id)
                validate_candidate(candidate, persisted=True)
                if candidate["registered_revision"] > value["revision"]:
                    fail("invalid_registry", "Candidate revision exceeds the registry revision.")
        return value

    @staticmethod
    def _kind(role):
        if role["kind"] not in {"input", "output"}:
            fail("invalid_kind", "Choose input for supplied materials or output for checked stage results.")
        _identifier(role["artifact_type"])
        if role["kind"] == "input" and role["artifact_type"] != "materials":
            fail("invalid_kind", "Supplied input materials use the materials type.")

    def register(self, request):
        _keys(request, {"schema_version", "expected_registry_revision", "role_id", "candidate_id", "kind", "artifact_type", "entrypoint", "files"})
        if request["schema_version"] != ROLE_SCHEMA:
            fail("invalid_schema", "Unsupported material role request.")
        _revision(request["expected_registry_revision"])
        for key in ("role_id", "candidate_id"):
            _identifier(request[key])
        self._kind(request)
        candidate = {"entrypoint": request["entrypoint"], "files": request["files"]}
        validate_candidate(candidate)
        with self._locked_registry():
            registry = self._registry()
            if registry["revision"] != request["expected_registry_revision"]:
                fail("registry_changed", "Refresh the registry before changing a role.")
            catalog = self.catalog.metadata()
            prior = catalog["artifacts"].get(request["role_id"])
            if prior and (prior["artifact_type"] != request["artifact_type"] or
                          request["kind"] == "input" and any(version["provenance"].get("mode") != "material-input" for version in prior["versions"].values())):
                fail("role_conflict", "An existing checked output cannot be reclassified as supplied input.")
            role = registry["roles"].get(request["role_id"])
            if role and (role["kind"] != request["kind"] or role["artifact_type"] != request["artifact_type"]):
                fail("role_conflict", "A stable role retains its kind and type.")
            if role is None:
                if len(registry["roles"]) >= MAX_ROLES:
                    fail("registry_full", "The registry role limit was reached.")
                role = {"kind": request["kind"], "artifact_type": request["artifact_type"], "candidates": {}}
            if request["candidate_id"] not in role["candidates"] and len(role["candidates"]) >= MAX_CANDIDATES:
                fail("registry_full", "The role's candidate limit was reached.")
            files = []
            for item in candidate["files"]:
                observed = self._observe(item["source_path"])
                if observed.get("error"):
                    fail("unavailable_source", f'Cannot register {item["source_path"]}: {observed["error"]}')
                files.append({**{key: val for key, val in item.items() if key != "registered_sha256"}, "registered_sha256": observed["sha256"]})
            existing = role["candidates"].get(request["candidate_id"])
            if existing and existing["entrypoint"] == candidate["entrypoint"] and existing["files"] == files:
                return {"registry_revision": registry["revision"], "unchanged": True, "confirmed": False}
            registry["revision"] += 1
            role["candidates"][request["candidate_id"]] = {"entrypoint": candidate["entrypoint"], "files": files, "registered_revision": registry["revision"]}
            registry["roles"][request["role_id"]] = role
            self._write(REGISTRY, registry)
            return {"registry_revision": registry["revision"], "unchanged": False, "confirmed": False}

    def _observe(self, relative):
        try:
            path = resolve_project_path(self.root, _path(relative))
            info = path.lstat()
            if info.st_size > MAX_FILE_BYTES:
                return {"error": "too_large"}
            sha = hash_project_file(self.root, relative)
            if path.lstat().st_size != info.st_size:
                return {"error": "changed_during_scan"}
            return {"sha256": sha, "size": info.st_size}
        except FileNotFoundError:
            return {"error": "missing"}
        except (OSError, PathSafetyError, ConfirmedArtifactError):
            return {"error": "unsafe_or_unreadable"}

    @staticmethod
    def _scopes(scopes):
        scopes = sorted(set(scopes or ["."]))
        if not 1 <= len(scopes) <= 32:
            fail("invalid_scope", "Provide at most 32 discovery scopes.")
        for scope in scopes:
            if scope != ".":
                working_path(scope)
        return scopes

    def _previous_scan(self):
        previous = self._read(SCAN)
        if previous is None:
            return {"schema_version": SCAN_SCHEMA, "scopes": [], "files": {}, "complete": True}
        _keys(previous, {"schema_version", "scopes", "files", "complete"})
        if previous["schema_version"] != SCAN_SCHEMA or type(previous["complete"]) is not bool or type(previous["scopes"]) is not list or type(previous["files"]) is not dict or len(previous["files"]) > MAX_SCAN_FILES:
            fail("invalid_scan", "Invalid previous material inventory.")
        self._scopes(previous["scopes"])
        for path, item in previous["files"].items():
            working_path(path)
            _keys(item, {"sha256", "size"})
            _hash(item["sha256"])
            if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_FILE_BYTES:
                fail("invalid_scan", "Invalid observed file size.")
        return previous

    def _inventory(self, scopes):
        files, errors, visited = {}, [], set()
        pending = list(reversed(scopes))
        byte_count = 0
        while pending:
            relative = pending.pop()
            if relative in visited:
                continue
            visited.add(relative)
            if len(visited) > MAX_SCAN_FILES * 4:
                errors.append({"path": relative, "error": "scan_limit"})
                break
            try:
                path = self.root if relative == "." else resolve_project_path(self.root, relative)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as entries:
                        children = sorted(entries, key=lambda entry: entry.name)
                    for entry in reversed(children):
                        child = entry.name if relative == "." else f"{relative}/{entry.name}"
                        folded = child.casefold()
                        if entry.name.casefold() in SKIP_DIRS or entry.name == ".DS_Store" or folded.startswith(".research/progress.md") or any(folded == prefix or folded.startswith(prefix + "/") for prefix in SKIP_PREFIXES):
                            continue
                        pending.append(child)
                    continue
                if len(files) >= MAX_SCAN_FILES or byte_count + info.st_size > MAX_SCAN_BYTES:
                    errors.append({"path": relative, "error": "scan_limit"})
                    break
                observed = self._observe(relative)
                if observed.get("error"):
                    errors.append({"path": relative, **observed})
                else:
                    files[relative] = observed
                    byte_count += observed["size"]
            except (OSError, PathSafetyError, ConfirmedArtifactError) as exc:
                errors.append({"path": relative, "error": str(exc)[:300]})
        return files, errors

    def _configs(self, registry, catalog):
        roles = json.loads(_bytes(registry["roles"]))
        for role_id, artifact in catalog["artifacts"].items():
            version_id = artifact["current_version"]
            # Withdrawn roles still retain their source description for review.
            version = artifact["versions"][version_id] if version_id else max(artifact["versions"].values(), key=lambda item: item["request"]["expected_catalog_revision"])
            request, provenance = version["request"], version["provenance"]
            role = roles.setdefault(role_id, {"kind": "input" if provenance.get("mode") == "material-input" else "output", "artifact_type": artifact["artifact_type"], "candidates": {}})
            candidate_id = provenance.get("material_candidate_id", "working")
            candidate = role["candidates"].get(candidate_id)
            if candidate is None or candidate.get("registered_revision", 0) <= provenance.get("material_registry_revision", 0):
                role["candidates"][candidate_id] = {"entrypoint": request["entrypoint"], "files": [
                    {**{key: val for key, val in item.items() if key != "sha256"}, "registered_sha256": item["sha256"]} for item in request["files"]
                ]}
        return roles

    def _review(self, scopes, *, operation_id=None):
        workflow = self.artifacts.workflow.summary()
        binding = {"mode": workflow["mode"]}
        if workflow["mode"] == "official":
            path = resolve_project_path(self.root, ".research/progress.md")
            binding["progress_sha256"] = None
            if path.exists():
                summary = progress.summarize_text(progress.read_text(path))
                if not summary["valid"]:
                    fail("progress_invalid", "Validate or restore official progress before reviewing materials.")
                binding["progress_sha256"] = summary["document_sha256"]
        else:
            binding.update(run_id=workflow["run_id"], semantic_sha256=workflow["semantic_sha256"])
        registry, catalog, previous = self._registry(), self.catalog.metadata(), self._previous_scan()
        files, errors = self._inventory(scopes)
        by_hash = {}
        for path, item in files.items():
            by_hash.setdefault(item["sha256"], []).append(path)
        comparable = previous["scopes"] == scopes and previous["complete"] and not errors
        old = previous["files"] if comparable else {}
        removed = sorted(set(old) - set(files))
        added = sorted(set(files) - set(old))
        moves = []
        for path in removed:
            destinations = [new for new in added if files[new]["sha256"] == old[path]["sha256"]]
            sources = [missing for missing in removed if old[missing]["sha256"] == old[path]["sha256"]]
            if len(destinations) == len(sources) == 1:
                moves.append({"from": path, "to": destinations[0]})
        reports, known = [], set()
        for role_id, role in sorted(self._configs(registry, catalog).items()):
            artifact = catalog["artifacts"].get(role_id)
            current_id = artifact["current_version"] if artifact else None
            current = artifact["versions"][current_id]["request"] if current_id else None
            history = {} if not artifact else {bundle_signature(item["request"]["entrypoint"], item["request"]["files"]): version for version, item in artifact["versions"].items()}
            candidates = []
            for candidate_id, candidate in sorted(role["candidates"].items()):
                observed_files, unavailable, relocated = [], [], []
                for declared in candidate["files"]:
                    source = declared["source_path"]
                    known.add(source)
                    observed = files.get(source) or self._observe(source)
                    possible_locations = []
                    if observed.get("error") == "missing":
                        expected = previous["files"].get(source, {}).get("sha256", declared["registered_sha256"])
                        matches = by_hash.get(expected, [])
                        possible_locations = matches
                        if len(matches) == 1:
                            relocated.append({"relative_path": declared["relative_path"], "from": source, "to": matches[0]})
                            source, observed = matches[0], files[matches[0]]
                            known.add(source)
                    if observed.get("error"):
                        unavailable.append({"path": source, "error": observed["error"], "possible_locations": possible_locations})
                    else:
                        observed_files.append({"source_path": source, "relative_path": declared["relative_path"], "sha256": observed["sha256"],
                                               **({"source_artifact_id": declared["source_artifact_id"]} if "source_artifact_id" in declared else {})})
                signature = None if unavailable else bundle_signature(candidate["entrypoint"], observed_files)
                if signature is None:
                    state = "unavailable"
                elif current and signature == bundle_signature(current["entrypoint"], current["files"]):
                    state = "unchanged"
                elif signature in history:
                    state = "historical"
                else:
                    state = "changed" if current else "new"
                prior = {} if current is None else {item["relative_path"]: item["sha256"] for item in current["files"]}
                after = {item["relative_path"]: item["sha256"] for item in observed_files}
                previews = self._text_previews(role_id, current_id, current, observed_files) if current else []
                candidates.append({"candidate_id": candidate_id, "entrypoint": candidate["entrypoint"], "files": observed_files,
                                   "bundle_sha256": signature, "state": state, "historical_version": history.get(signature) if state == "historical" else None,
                                   "changes": {"added": sorted(set(after) - set(prior)), "removed": sorted(set(prior) - set(after)),
                                               "modified": sorted(key for key in set(prior) & set(after) if prior[key] != after[key]), "relocated": relocated,
                                               "text_previews": previews},
                                   "unavailable": unavailable})
            choices = {candidate["bundle_sha256"] for candidate in candidates if candidate["state"] in {"new", "changed"}}
            reports.append({"role_id": role_id, "kind": role["kind"], "artifact_type": role["artifact_type"], "current_version": current_id,
                            "candidates": candidates, "needs_confirmation": bool(choices), "needs_selection": len(choices) > 1})
        review = {"schema_version": REVIEW_SCHEMA, "operation_id": operation_id or f"review-{uuid.uuid4().hex}",
                  "registry_revision": registry["revision"], "catalog_revision": catalog["revision"], "scopes": scopes,
                  "workflow_binding": binding,
                  "roles": reports, "discovery": {"complete": not errors, "file_count": len(files), "baseline_reset": not comparable,
                      "added": added, "modified": sorted(path for path in set(old) & set(files) if old[path]["sha256"] != files[path]["sha256"]),
                      "missing": removed, "relocated": moves, "duplicates": [paths for paths in by_hash.values() if len(paths) > 1],
                      "unregistered": sorted(set(files) - known), "errors": errors}}
        _bytes(review)
        return review, {"schema_version": SCAN_SCHEMA, "scopes": scopes, "files": files, "complete": not errors}

    def _text_previews(self, role_id, version_id, current, files):
        previews = []
        prior = {item["relative_path"]: item for item in current["files"]}
        remaining = 20000
        for item in files:
            old = prior.get(item["relative_path"])
            if not old or old["sha256"] == item["sha256"] or Path(item["relative_path"]).suffix.lower() not in {".md", ".txt", ".tex", ".csv", ".json", ".yml", ".yaml", ".py"}:
                continue
            old_path = f'{self.catalog._snapshot_root(role_id, version_id)}/{item["relative_path"]}'
            try:
                text = []
                for relative, sha in ((old_path, old["sha256"]), (item["source_path"], item["sha256"])):
                    path = self.catalog._checked(relative)
                    if path.lstat().st_size > 128 * 1024 or hash_project_file(self.root, relative) != sha:
                        raise ValueError("Preview unavailable")
                    text.append(progress.read_text(path))
                    if hash_project_file(self.root, relative) != sha:
                        raise ValueError("Preview source changed")
                lines = list(difflib.unified_diff(text[0].splitlines(), text[1].splitlines(), fromfile="current", tofile="candidate", lineterm="", n=2))
                preview = "\n".join(lines[:80])
                truncated = len(lines) > 80 or len(preview) > remaining
                preview = preview[:remaining]
                previews.append({"relative_path": item["relative_path"], "diff": preview, "truncated": truncated})
                remaining -= len(preview)
                if remaining <= 0:
                    break
            except (OSError, ValueError, PathSafetyError, ConfirmedArtifactError, progress.ProgressError):
                # Binary/large/unavailable sources keep the hash and file diff.
                continue
        return previews

    def review(self, scopes=None, *, save_scan=False, output=None):
        # Preserve the user's saved discovery boundary on later sessions;
        # registered candidates are still checked even outside this boundary.
        scopes = self._scopes(scopes if scopes is not None else self._previous_scan()["scopes"])
        if output:
            _path(output)
            if not output.startswith(BASE + "/reviews/") or not output.endswith(".json"):
                fail("invalid_review_path", f"Save reviews under {BASE}/reviews/ as JSON.")
        if save_scan or output:
            with self._locked_registry():
                review, scan = self._review(scopes)
                if save_scan:
                    self._persist_relocations(review)
                    self._write(SCAN, scan)
                    self._write_index(review)
                if output:
                    self._write(output, review)
                return review
        return self._review(scopes)[0]

    def _persist_relocations(self, review):
        """Keep candidate identity after a uniquely verified move and later edits.

        Caller holds the registry lock. Only working associations change here;
        adopted version pointers and snapshot bytes remain untouched.
        """
        registry = self._registry()
        configs = self._configs(registry, self.catalog.metadata())
        updated = []
        for role in review["roles"]:
            for candidate in role["candidates"]:
                if candidate["unavailable"] or not candidate["changes"]["relocated"]:
                    continue
                definition = configs[role["role_id"]]
                if role["role_id"] not in registry["roles"] and len(registry["roles"]) >= MAX_ROLES:
                    fail("registry_full", "The registry role limit was reached; register this relocation explicitly.")
                observed = registry["roles"].setdefault(role["role_id"], {
                    "kind": definition["kind"], "artifact_type": definition["artifact_type"], "candidates": {}})
                if candidate["candidate_id"] not in observed["candidates"] and len(observed["candidates"]) >= MAX_CANDIDATES:
                    fail("registry_full", "The candidate limit was reached; register this relocation explicitly.")
                working = json.loads(_bytes(definition["candidates"][candidate["candidate_id"]]))
                paths = {item["relative_path"]: item for item in candidate["files"]}
                for declared in working["files"]:
                    current = paths[declared["relative_path"]]
                    working_path(current["source_path"])
                    if hash_project_file(self.root, current["source_path"]) != current["sha256"]:
                        fail("scan_changed", "A relocated candidate changed during the saved scan; scan again.")
                    declared["source_path"] = current["source_path"]
                    declared["registered_sha256"] = current["sha256"]
                working["registered_revision"] = registry["revision"] + 1
                validate_candidate(working, persisted=True)
                observed["candidates"][candidate["candidate_id"]] = working
                updated.append(role["role_id"])
        if updated:
            registry["revision"] += 1
            self._write(REGISTRY, registry)
            review["registry_revision"] = registry["revision"]

    def _write_index(self, review):
        lines = ["# 项目材料与当前版本", "", f'材料登记版本：{review["registry_revision"]}；确认目录版本：{review["catalog_revision"]}', "",
                 "这是生成时的阅读视图；恢复会话使用 material_manager.py resume 重新检查。", "",
                 "当前版本由确认目录决定；候选稿、文件日期和文件名不改变当前采用状态。", ""]
        labels = {"unchanged": "内容未变，沿用确认", "changed": "有修改，尚未采用", "new": "新候选，尚未采用", "historical": "历史版本，重新采用需确认", "unavailable": "材料缺失或不可读"}
        for role in review["roles"]:
            lines.extend([f'## {role["role_id"]}', "", f'当前确认版本：{role["current_version"] or "无"}', ""])
            for candidate in role["candidates"]:
                lines.append(f'- `{candidate["candidate_id"]}`：{labels[candidate["state"]]}。')
                for item in candidate["files"]:
                    lines.append(f'  - `{item["source_path"]}` → `{item["relative_path"]}`；SHA-256 `{item["sha256"][:12]}`。')
                for item in candidate["changes"]["relocated"]:
                    lines.append(f'  - 内容相同的路径迁移：`{item["from"]}` → `{item["to"]}`。')
        discovery = review["discovery"]
        lines.extend(["", "## 材料扫描", "", f'扫描文件：{discovery["file_count"]}；未登记：{len(discovery["unregistered"])}；完整扫描：{discovery["complete"]}。', "",
                      "未登记文件仅用于提示；没有自动选择或确认它们。last-scan.json 保存扫描基线；完整差异使用 review 命令。", ""])
        target = self.catalog._checked(INDEX)
        _write_bytes_atomic(target, "\n".join(lines).encode("utf-8"), expected_target_identity=_file_identity(target) if target.exists() else None, require_absent=not target.exists())

    def accept(self, review, selections, decision=None, *, confirmed=False, evidence=None):
        _keys(review, {"schema_version", "operation_id", "registry_revision", "catalog_revision", "scopes", "roles", "discovery", "workflow_binding"})
        if review["schema_version"] != REVIEW_SCHEMA:
            fail("invalid_review", "Unsupported review schema.")
        _identifier(review["operation_id"])
        _revision(review["registry_revision"])
        _revision(review["catalog_revision"])
        if type(selections) is not dict or not selections or len(selections) > MAX_FILES:
            fail("invalid_selection", "Select one candidate per adopted role.")
        if type(review["roles"]) is not list or len(review["roles"]) > MAX_ROLES:
            fail("invalid_review", "Invalid review role collection.")
        reviewed_roles = {role["role_id"]: role for role in review["roles"]}
        if len(reviewed_roles) != len(review["roles"]):
            fail("invalid_review", "Review role IDs must be unique.")
        requests, selected, unchanged = [], {}, []
        for role_id, candidate_id in sorted(selections.items()):
            _identifier(role_id)
            _identifier(candidate_id)
            role = reviewed_roles.get(role_id)
            if role is None:
                fail("invalid_selection", "Select a role that occurs in the review.")
            candidates = [item for item in role["candidates"] if item["candidate_id"] == candidate_id]
            if len(candidates) != 1 or candidates[0]["state"] == "unavailable":
                fail("invalid_selection", "Select an available, unambiguous reviewed candidate.")
            candidate = candidates[0]
            selected[role_id] = (role, candidate)
            if candidate["state"] == "unchanged":
                unchanged.append(role_id)
                continue
            if confirmed is not True:
                fail("confirmation_required", "Record the user's adoption decision for the selected changes.")
            _text(decision)
            request = {"schema_version": "confirmed-artifact-request-v1", "operation_id": "material-" + digest({"review": review["operation_id"], "role": role_id, "candidate": candidate_id})[:48],
                       "artifact_id": role_id, "artifact_type": role["artifact_type"], "expected_catalog_revision": review["catalog_revision"] + len(requests),
                       "entrypoint": candidate["entrypoint"], "files": candidate["files"], "confirmation": decision}
            # An input adoption establishes which supplied material to use; it
            # never confers a successful stage or scientific validation.
            self._kind(role)
            if role["kind"] == "output":
                if evidence is not None:
                    request["evidence"] = evidence
                binding = review["workflow_binding"]
                if binding.get("mode") == "official" and binding.get("progress_sha256"):
                    request["expected_progress_sha256"] = binding["progress_sha256"]
            requests.append(normalize_request(request))
        with self._locked_registry():
            catalog = self.catalog.metadata()
            # Original complete commits remain inspectable even after source
            # deletion, later confirmations, or a failed display refresh.
            recorded = [catalog["operations"].get(item["operation_id"]) for item in requests]
            retry = bool(requests) and all(recorded)
            if not retry:
                fresh = self._review(self._scopes(review["scopes"]), operation_id=review["operation_id"])[0]
                if fresh["registry_revision"] != review["registry_revision"] or fresh["catalog_revision"] != review["catalog_revision"]:
                    fail("review_stale", "The material registry or current selection changed; refresh the review.")
                latest = {role["role_id"]: role for role in fresh["roles"]}
                for role_id, (role, candidate) in selected.items():
                    if role["kind"] == "output" and fresh["workflow_binding"] != review["workflow_binding"]:
                        fail("review_stale", "The workflow/progress generation changed; refresh output verification.")
                    current_role = latest.get(role_id)
                    current_candidates = [] if current_role is None else [item for item in current_role["candidates"] if item["candidate_id"] == candidate["candidate_id"]]
                    compared = ("candidate_id", "entrypoint", "files", "bundle_sha256", "state")
                    if current_role is None or len(current_candidates) != 1 or any(current_role[key] != role[key] for key in ("kind", "artifact_type", "current_version")) or any(current_candidates[0][key] != candidate[key] for key in compared):
                        fail("review_stale", "Selected bytes, bundle layout or candidate identity changed since review.")
                    if candidate["state"] == "unchanged":
                        self.catalog.resolve(role_id, expected_revision=catalog["revision"])
            if not requests:
                return {"committed": False, "catalog_revision": catalog["revision"], "unchanged_roles": unchanged, "results": []}
            if retry:
                result = self.catalog.accept_batch(requests, lambda request: {})
                return {**result, "unchanged_roles": unchanged}
            with self.artifacts.confirmation_context() as output_provenance:
                def provenance(request):
                    role, candidate = selected[request["artifact_id"]]
                    if role["kind"] == "output":
                        result = output_provenance(request)
                    else:
                        prior = catalog["artifacts"].get(request["artifact_id"])
                        if prior and any(item["provenance"].get("mode") != "material-input" for item in prior["versions"].values()):
                            fail("role_conflict", "Checked stage outputs cannot use input adoption.")
                        result = {"mode": "material-input", "meaning": "user-selected supplied material; not a verified stage output"}
                    return {**result, "material_candidate_id": candidate["candidate_id"], "material_registry_revision": review["registry_revision"]}

                result = self.catalog.accept_batch(requests, provenance)
            # A human-facing view is derived, never a second selection authority.
            # Failure here cannot undo or obscure the committed catalog batch.
            try:
                refreshed = self._review(self._scopes(review["scopes"]))[0]
                self._write_index(refreshed)
                result["material_index_pending"] = False
            except (OSError, ValueError, PathSafetyError, ConfirmedArtifactError, progress.ProgressError, WorkflowError, StoreError, MaterialError) as exc:
                result["material_index_pending"] = True
                result["material_index_error"] = str(exc)[:500]
            return {**result, "unchanged_roles": unchanged}

    def resume(self, roles=None, scopes=None):
        workflow = self.artifacts.workflow.summary()
        official = None
        if workflow["mode"] == "official":
            path = resolve_project_path(self.root, ".research/progress.md")
            if path.exists():
                official = progress.summarize_text(progress.read_text(path))
                if not official["valid"]:
                    fail("progress_invalid", "Validate or restore progress before resuming.")
        review = self.review(scopes)
        targets = list(dict.fromkeys(roles)) if roles else [role["role_id"] for role in review["roles"] if role["current_version"]]
        bindings, issues = [], []
        for role_id in targets:
            try:
                bindings.append(self.catalog.resolve(role_id, expected_revision=review["catalog_revision"]))
            except ConfirmedArtifactError as exc:
                issues.append({"role_id": role_id, "code": exc.code, "message": str(exc)})
        # Reading the current snapshot remains valid while a working candidate
        # is edited. Unrelated candidate changes do not block this task.
        run_differences = []
        current_versions = {item["role_id"]: item["current_version"] for item in review["roles"]}
        for artifact in workflow.get("artifacts", []):
            parts = artifact.get("path", "").split("/")
            if len(parts) >= 6 and parts[:3] == [".research", "confirmed-artifacts", "versions"]:
                role_id, frozen_version = parts[3:5]
                if role_id in current_versions and current_versions[role_id] != frozen_version:
                    run_differences.append({"artifact_id": artifact["artifact_id"], "role_id": role_id, "run_version": frozen_version,
                                            "current_version": current_versions[role_id], "action": "retain_frozen_run_input"})
        compact_roles = []
        for role in review["roles"]:
            if roles and role["role_id"] not in roles:
                continue
            candidates = []
            for candidate in role["candidates"]:
                changes = candidate["changes"]
                candidates.append({key: candidate[key] for key in ("candidate_id", "entrypoint", "state", "bundle_sha256", "historical_version")})
                candidates[-1].update(file_count=len(candidate["files"]), unavailable=candidate["unavailable"], changes={
                    key: {"count": len(changes[key]), "items": changes[key][:12]} for key in ("added", "removed", "modified", "relocated")})
                candidates[-1]["changes"]["text_previews"] = [{**preview, "diff": preview["diff"][:4000],
                    "truncated": preview["truncated"] or len(preview["diff"]) > 4000} for preview in changes["text_previews"][:2]]
            compact_roles.append({**role, "candidates": candidates})
        discovery = review["discovery"]
        compact_discovery = {key: discovery[key] for key in ("complete", "file_count", "baseline_reset")}
        compact_discovery.update({key: {"count": len(discovery[key]), "items": discovery[key][:20]} for key in (
            "added", "modified", "missing", "relocated", "duplicates", "unregistered", "errors")})
        return {"mode": workflow["mode"], "workflow": workflow, "official_progress": official,
                "registry_revision": review["registry_revision"], "catalog_revision": review["catalog_revision"],
                "ready_to_read": not issues, "bindings": bindings, "issues": issues, "roles": compact_roles,
                "run_input_differences": run_differences, "other_pending_role_count": sum(role["needs_confirmation"] for role in review["roles"] if roles and role["role_id"] not in roles),
                "discovery": compact_discovery}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("cli_arguments", message)


def main(argv=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("register", "scan", "review", "accept", "resume", "resolve"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True)
        command.add_argument("--json", action="store_true")
        if name == "register":
            command.add_argument("--request", required=True)
        if name in {"scan", "review", "resume"}:
            command.add_argument("--scope", action="append")
        if name in {"scan", "review"}:
            command.add_argument("--output")
        if name in {"resume", "resolve"}:
            command.add_argument("--role", action="append", required=name == "resolve")
        if name == "accept":
            command.add_argument("--review", required=True)
            command.add_argument("--select", action="append", required=True, help="role=candidate; one candidate per adopted role")
            command.add_argument("--decision")
            command.add_argument("--evidence", help="project-relative JSON containing an evidence list")
            command.add_argument("--confirm", action="store_true")
    try:
        args = parser.parse_args(argv)
        service = MaterialService(args.project)
        if args.command == "register":
            result = service.register(read_project_json_object(service.root, args.request))
        elif args.command in {"review", "scan"}:
            result = service.review(args.scope, save_scan=args.command == "scan", output=args.output)
        elif args.command == "resume":
            result = service.resume(args.role, args.scope)
        elif args.command == "resolve":
            result = {"bindings": [service.catalog.resolve(role) for role in args.role]}
        else:
            selections = {}
            for item in args.select:
                role, separator, candidate = item.partition("=")
                if not separator or role in selections:
                    fail("invalid_selection", "Use role=candidate and select each role once.")
                selections[role] = candidate
            evidence = None
            if args.evidence:
                supplied = read_project_json_object(service.root, args.evidence)
                _keys(supplied, {"evidence"})
                evidence = supplied["evidence"]
            result = service.accept(read_project_json_object(service.root, args.review), selections, args.decision, confirmed=args.confirm, evidence=evidence)
        incomplete = result.get("discovery", {}).get("complete") is False
        output = {"status": "attention" if result.get("projection_pending") or result.get("material_index_pending") or result.get("ready_to_read") is False or incomplete else "pass", **result}
        code = 0 if output["status"] == "pass" or result.get("committed") else 2
    except (MaterialError, ConfirmedArtifactError, PathSafetyError, WorkflowError, StoreError, progress.ProgressError, OSError, ValueError, TypeError, KeyError) as exc:
        output = {"status": "blocked", "error": {"code": getattr(exc, "code", "materials.invalid_input"), "message": str(exc)[:2000]}}
        code = 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
