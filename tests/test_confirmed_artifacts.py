"""Confirmation authority, immutable bundles and recoverable latest projections."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scripts import confirmed_artifacts as ca


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ConfirmedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = ca.ConfirmedArtifactStore(self.root)

    def write(self, name, raw=b"a checked draft\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def request(self, *, revision=0, operation="accept-first", artifact="main-paper", files=None):
        if files is None:
            files = {"paper.md": b"a checked draft\n"}
        items = []
        for name, raw in files.items():
            self.write("work/" + name, raw)
            items.append({"source_path": "work/" + name, "relative_path": name, "sha256": digest(raw)})
        return {"schema_version": ca.REQUEST_SCHEMA, "operation_id": operation,
                "artifact_id": artifact, "artifact_type": "manuscripts",
                "expected_catalog_revision": revision, "entrypoint": next(iter(files)),
                "files": items, "confirmation": "已检查并确认采用这组文件。"}

    def accept(self, request=None):
        return self.store.accept(request or self.request(), lambda request: {"mode": "official", "stage": "intake"})

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(ca.ConfirmedArtifactError) as result:
            function(*args, **kwargs)
        self.assertEqual(result.exception.code, "confirmed." + code)
        return result.exception

    def test_empty_reads_are_zero_write_and_do_not_verify_files(self):
        with mock.patch.object(ca, "hash_project_file", side_effect=AssertionError("must not hash bundles")):
            self.assertEqual(self.store.status()["catalog_revision"], 0)
            self.assertEqual(self.store.list()["verification"], "not_checked")
            summary = ca.confirmed_artifact_summary(self.root)
            self.assertEqual(summary["current_count"], 0)
            self.assertNotIn("artifacts", summary)
            self.assertCode("not_found", self.store.resolve, "missing")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_multifile_bundle_and_callback_metadata(self):
        files = {"paper.tex": b"\\input{sections/results.tex}\n", "sections/results.tex": b"Verified results", "figures/plot.svg": b"<svg/>"}
        request = self.request(files=files)
        request["files"][0]["source_artifact_id"] = "draft"
        request["expected_progress_sha256"] = "a" * 64
        request["evidence"] = [{"path": "checks.json", "sha256": "b" * 64}]
        seen = []

        def callback(normalized):
            seen.append(copy.deepcopy(normalized))
            normalized["files"].clear()
            return {"mode": "custom", "run_id": "run-one"}

        result = self.store.accept(request, callback)
        self.assertTrue(result["committed"])
        self.assertFalse(result["projection_pending"], result)
        self.assertEqual(seen, [request])
        resolved = self.store.resolve("main-paper", expected_revision=1)
        self.assertEqual(resolved["entrypoint"], "paper.tex")
        self.assertEqual(resolved["provenance"]["run_id"], "run-one")
        self.assertEqual(len(resolved["files"]), 3)
        for item in resolved["files"]:
            raw = files[item["relative_path"]]
            self.assertEqual((self.root / item["snapshot_path"]).read_bytes(), raw)
            self.assertEqual((self.root / item["current_path"]).read_bytes(), raw)
            self.assertEqual((self.root / item["source_path"]).read_bytes(), raw)
        self.assertIn("current/manuscripts/main-paper/paper.tex", self.store.index_path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.status(verify=True)["verification"], "current_verified")

    def test_readonly_status_only_checks_metadata_even_when_content_changed(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        (self.root / resolved["snapshot_path"]).write_bytes(b"tampered")
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        with mock.patch.object(ca, "hash_project_file", side_effect=AssertionError("do not hash bundles")):
            self.assertEqual(self.store.status()["verification"], "not_checked")
            self.assertEqual(ca.confirmed_artifact_summary(self.root)["catalog_revision"], 1)
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))
        self.assertCode("snapshot_changed", self.store.resolve, "main-paper")

    def test_source_edits_leave_snapshot_stable_new_accept_keeps_old_version(self):
        first_request = self.request()
        self.accept(first_request)
        first = self.store.resolve("main-paper")
        self.write("work/paper.md", b"a working draft, not confirmed")
        self.assertEqual(self.store.resolve("main-paper")["version_id"], first["version_id"])
        self.assertEqual((self.root / first["snapshot_path"]).read_bytes(), b"a checked draft\n")
        second_request = self.request(revision=1, operation="accept-second", files={"paper.md": b"version two"})
        second = self.accept(second_request)
        self.assertFalse(second["projection_pending"], second)
        current = self.store.resolve("main-paper")
        self.assertNotEqual(current["version_id"], first["version_id"])
        self.assertEqual((self.root / current["current_path"]).read_bytes(), b"version two")
        self.assertEqual((self.root / first["snapshot_path"]).read_bytes(), b"a checked draft\n")
        (self.root / current["snapshot_path"]).unlink()
        self.assertCode("snapshot_unavailable", self.store.resolve, "main-paper")

    def test_idempotency_precedes_cas_and_never_reruns_callback_or_changes_current(self):
        request = self.request()
        callback = mock.Mock(return_value={"mode": "official"})
        first = self.store.accept(request, callback)
        self.write("work/paper.md", b"changed after confirmation")
        second = self.store.accept(request, callback)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["version_id"], first["version_id"])
        callback.assert_called_once()
        different = copy.deepcopy(request)
        different["confirmation"] = "different approval"
        self.assertCode("idempotency_conflict", self.store.accept, different, callback)
        stale = copy.deepcopy(request)
        stale["operation_id"] = "new-stale-request"
        self.assertCode("revision_conflict", self.store.accept, stale, callback)
        self.assertCode("revision_conflict", self.store.resolve, "main-paper", expected_revision=0)

    def test_parallel_cas_allows_exactly_one_commit(self):
        request = self.request()
        other = copy.deepcopy(request)
        other["operation_id"] = "other-confirmation"

        def attempt(value):
            try:
                return self.accept(value)["committed"]
            except ca.ConfirmedArtifactError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, [request, other]))
        self.assertCountEqual(outcomes, [True, "confirmed.revision_conflict"])
        self.assertEqual(self.store.status()["catalog_revision"], 1)

    def test_callback_and_bad_hash_failures_never_commit_or_publish(self):
        request = self.request()
        callback = mock.Mock(side_effect=ValueError("stage not accepted"))
        with self.assertRaisesRegex(ValueError, "stage not accepted"):
            self.store.accept(request, callback)
        self.assertFalse(self.store.catalog_path.exists())
        self.assertFalse((self.root / "artifacts").exists())
        request["files"][0]["sha256"] = "0" * 64
        self.assertCode("source_hash_mismatch", self.accept, request)
        self.assertFalse(self.store.catalog_path.exists())
        self.assertFalse((self.root / "artifacts").exists())
        self.assertEqual(self.store.status()["current_count"], 0)

    def test_catalog_error_before_commit_leaves_latest_unchanged(self):
        self.accept()
        catalog = self.store.catalog_path.read_bytes()
        resolved = self.store.resolve("main-paper")
        current = (self.root / resolved["current_path"]).read_bytes()
        request = self.request(revision=1, operation="accept-second", files={"paper.md": b"second"})
        with mock.patch.object(self.store, "_write_catalog", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.accept(request)
        self.assertEqual(self.store.catalog_path.read_bytes(), catalog)
        self.assertEqual((self.root / resolved["current_path"]).read_bytes(), current)

    def test_catalog_error_after_atomic_replace_reports_committed(self):
        original = self.store._write_catalog

        def write_then_fail(value):
            original(value)
            raise OSError("directory fsync failed")

        with mock.patch.object(self.store, "_write_catalog", side_effect=write_then_fail):
            result = self.accept()
        self.assertTrue(result["committed"])
        self.assertIn("commit_warning", result)
        self.assertEqual(self.store.resolve("main-paper")["catalog_revision"], 1)

    def test_partial_projection_can_be_repaired_without_new_acceptance(self):
        request = self.request(files={"paper.md": b"text", "figures/plot.svg": b"<svg/>"})
        original = self.store._write_projection_file
        calls = []

        def interrupted(path, item, expected_digest=None):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("simulated interruption")
            return original(path, item, expected_digest)

        with mock.patch.object(self.store, "_write_projection_file", side_effect=interrupted):
            result = self.accept(request)
        self.assertTrue(result["committed"])
        self.assertTrue(result["projection_pending"])
        self.assertEqual(self.store.resolve("main-paper")["verification"], "snapshot_verified")
        repair = self.store.repair(expected_revision=1)
        self.assertFalse(repair["committed"])
        self.assertEqual(repair["backups"], [])
        self.assertFalse(self.store.status(verify=True)["projection_pending"])

    def test_marker_failure_after_complete_projection_can_be_repaired(self):
        original = ca.atomic_write_json

        def fail_marker(path, value):
            if Path(path).name == "projection.json":
                raise OSError("marker interrupted")
            return original(path, value)

        with mock.patch.object(ca, "atomic_write_json", side_effect=fail_marker):
            result = self.accept()
        self.assertTrue(result["committed"])
        self.assertTrue(result["projection_pending"])
        self.store.repair(expected_revision=1)
        self.assertFalse(self.store.status(verify=True)["projection_pending"])

    def test_unknown_and_modified_files_require_explicit_preserving_backup(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        self.write(resolved["current_path"], b"user-edited current")
        self.write("artifacts/current/personal-note.txt", b"keep this note")
        request = self.request(revision=1, operation="accept-second", files={"paper.md": b"new accepted version"})
        result = self.accept(request)
        self.assertTrue(result["committed"])
        self.assertTrue(result["projection_pending"])
        self.assertIn(resolved["current_path"], result["projection_conflicts"])
        self.assertEqual((self.root / resolved["current_path"]).read_bytes(), b"user-edited current")
        self.assertCode("projection_conflict", self.store.repair, expected_revision=2)
        repaired = self.store.repair(expected_revision=2, backup_conflicts=True)
        saved = next(path for path in repaired["backups"] if path.endswith("/current"))
        self.assertEqual((self.root / saved / "personal-note.txt").read_bytes(), b"keep this note")
        self.assertEqual((self.root / saved / "manuscripts/main-paper/paper.md").read_bytes(), b"user-edited current")
        self.assertEqual((self.root / resolved["current_path"]).read_bytes(), b"new accepted version")
        self.assertFalse(self.store.status(verify=True)["projection_pending"])

    def assert_structure_repair(self, old_relative, new_relative, *, user_files=False):
        self.accept(self.request(files={old_relative: b"old accepted bytes"}))
        first = self.store.resolve("main-paper")
        before_index = self.store.index_path.read_bytes()
        self.write("work/new-version.txt", b"new accepted bytes")
        request = {"schema_version": ca.REQUEST_SCHEMA, "operation_id": "new-shape",
                   "artifact_id": "main-paper", "artifact_type": "manuscripts",
                   "expected_catalog_revision": 1, "entrypoint": new_relative,
                   "files": [{"source_path": "work/new-version.txt", "relative_path": new_relative,
                              "sha256": digest(b"new accepted bytes")}],
                   "confirmation": "Accept the new bundle layout."}
        unrelated = self.write("artifacts/my-own-work.txt", b"outside current")
        result = self.accept(request)
        self.assertTrue(result["committed"])
        self.assertTrue(result["projection_pending"])
        self.assertEqual((self.root / first["current_path"]).read_bytes(), b"old accepted bytes")
        self.assertEqual(self.store.index_path.read_bytes(), before_index)
        self.assertCode("projection_conflict", self.store.repair, expected_revision=2)
        if user_files:
            self.write("artifacts/current/my-own-note.txt", b"created after confirmation")
        repaired = self.store.repair(expected_revision=2, backup_conflicts=True)
        saved = next(path for path in repaired["backups"] if path.endswith("/current"))
        self.assertEqual((self.root / saved / "manuscripts/main-paper" / old_relative).read_bytes(), b"old accepted bytes")
        if user_files:
            self.assertEqual((self.root / saved / "my-own-note.txt").read_bytes(), b"created after confirmation")
        latest = self.store.resolve("main-paper")
        self.assertEqual((self.root / latest["current_path"]).read_bytes(), b"new accepted bytes")
        self.assertEqual((self.root / first["snapshot_path"]).read_bytes(), b"old accepted bytes")
        self.assertEqual((self.root / "work" / old_relative).read_bytes(), b"old accepted bytes")
        self.assertEqual(unrelated.read_bytes(), b"outside current")
        self.assertFalse(self.store.status(verify=True)["projection_pending"])

    def test_directory_to_file_projection_requires_and_allows_preserving_backup(self):
        self.assert_structure_repair("sections/x.md", "sections")

    def test_file_to_directory_projection_requires_and_allows_preserving_backup(self):
        self.assert_structure_repair("sections", "sections/x.md")

    def test_structure_repair_also_preserves_new_user_files_and_unrelated_artifacts(self):
        self.assert_structure_repair("sections/x.md", "sections", user_files=True)

    def test_user_change_during_projection_preparation_is_not_overwritten(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        original = self.store._copy_verified

        def edit_during_copy(source, destination, expected):
            result = original(source, destination, expected)
            if "/projection-staging/" in destination:
                self.write(resolved["current_path"], b"user concurrently editing")
            return result

        request = self.request(revision=1, operation="accept-second", files={"paper.md": b"second"})
        with mock.patch.object(self.store, "_copy_verified", side_effect=edit_during_copy):
            result = self.accept(request)
        self.assertTrue(result["committed"])
        self.assertTrue(result["projection_pending"])
        self.assertEqual((self.root / resolved["current_path"]).read_bytes(), b"user concurrently editing")
        self.assertFalse(any(path.name.endswith(".tmp") for path in (self.root / "artifacts/current").rglob("*")))

    def test_save_after_final_hash_is_preserved_in_retired_projection(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        current = self.root / resolved["current_path"]
        real_replace, real_rename = os.replace, os.rename
        injected = False

        def mutate_then_call(function, source, destination, **kwargs):
            nonlocal injected
            if not injected and (Path(source).name == "paper.md" or Path(destination).name == "paper.md"):
                injected = True
                current.write_bytes(b"last-moment editor save")
            return function(source, destination, **kwargs)

        request = self.request(revision=1, operation="second", files={"paper.md": b"new accepted"})
        with mock.patch.object(ca.os, "replace", side_effect=lambda *args, **kwargs: mutate_then_call(real_replace, *args, **kwargs)), \
                mock.patch.object(ca.os, "rename", side_effect=lambda *args, **kwargs: mutate_then_call(real_rename, *args, **kwargs)):
            result = self.accept(request)
        self.assertTrue(injected)
        self.assertTrue(result["committed"])
        self.assertFalse(result["projection_pending"], result)
        backups = [self.root / path for path in result.get("projection_backups", [])]
        self.assertTrue(any(path.is_file() and path.read_bytes() == b"last-moment editor save" for path in backups))
        self.assertEqual(current.read_bytes(), b"new accepted")

    def test_projection_publish_has_no_clobber_fallback_when_hard_links_unavailable(self):
        self.accept()
        request = self.request(revision=1, operation="second", files={"paper.md": b"new accepted"})
        with mock.patch.object(ca.os, "link", side_effect=NotImplementedError):
            result = self.accept(request)
        self.assertTrue(result["committed"])
        self.assertFalse(result["projection_pending"], result)
        self.assertEqual((self.root / self.store.resolve("main-paper")["current_path"]).read_bytes(), b"new accepted")

    def test_withdrawal_save_after_final_hash_is_preserved_not_unlinked(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        current = self.root / resolved["current_path"]
        real_unlink, real_rename = Path.unlink, os.rename
        injected = False

        def racing_unlink(path, *args, **kwargs):
            nonlocal injected
            if path == current and not injected:
                injected = True
                current.write_bytes(b"last-moment withdrawal save")
            return real_unlink(path, *args, **kwargs)

        def racing_rename(source, destination, **kwargs):
            nonlocal injected
            if Path(source).name == "paper.md" and not injected:
                injected = True
                current.write_bytes(b"last-moment withdrawal save")
            return real_rename(source, destination, **kwargs)

        with mock.patch.object(Path, "unlink", new=racing_unlink), mock.patch.object(ca.os, "rename", side_effect=racing_rename):
            result = self.store.withdraw("main-paper", 1, "withdraw-first", "Needs revision.")
        self.assertTrue(injected)
        self.assertTrue(result["committed"])
        backups = [self.root / path for path in result.get("projection_backups", [])]
        self.assertTrue(any(path.is_file() and path.read_bytes() == b"last-moment withdrawal save" for path in backups))
        self.assertFalse(current.exists())

    def test_withdrawal_removes_latest_without_falling_back_and_retains_history(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        result = self.store.withdraw("main-paper", 1, "withdraw-first", "The output needs revision.")
        self.assertTrue(result["committed"])
        self.assertFalse(result["projection_pending"])
        self.assertFalse((self.root / resolved["current_path"]).exists())
        self.assertTrue((self.root / resolved["snapshot_path"]).is_file())
        self.assertCode("withdrawn", self.store.resolve, "main-paper")
        self.assertEqual(ca.confirmed_artifact_summary(self.root)["withdrawn_count"], 1)
        repeated = self.store.withdraw("main-paper", 1, "withdraw-first", "The output needs revision.")
        self.assertTrue(repeated["idempotent"])
        self.assertCode("idempotency_conflict", self.store.withdraw, "main-paper", 1, "withdraw-first", "different reason")

    def test_reaccept_withdrawn_role_is_explicit_and_preserves_scope(self):
        self.accept()
        self.store.withdraw("main-paper", 1, "withdraw-first", "Needs revision.")
        request = self.request(revision=2, operation="accept-other-run", files={"paper.md": b"new run output"})
        self.store.accept(request, lambda request: {"mode": "custom", "run_id": "new-run"})
        resolved = self.store.resolve("main-paper")
        self.assertEqual(resolved["provenance"]["run_id"], "new-run")
        self.assertEqual(self.store.status()["artifacts"][0]["version_count"], 2)
        changed_type = self.request(revision=3, operation="wrong-type")
        changed_type["artifact_type"] = "figures"
        self.assertCode("artifact_type_changed", self.accept, changed_type)

    def test_mutable_projection_cannot_be_reused_as_working_source(self):
        request = self.request()
        for name in ("artifacts/INDEX.md", "artifacts/current/manuscripts/paper.md",
                     "ARTIFACTS/INDEX.MD", "ARTIFACTS/CURRENT/manuscripts/paper.md"):
            with self.subTest(path=name):
                request["files"][0]["source_path"] = name
                self.assertCode("mutable_projection_source", ca.normalize_request, request)

    def test_invalid_request_paths_and_unknown_fields(self):
        request = self.request()
        for path in ("../outside", "/absolute", "C:/outside", "a\\b", "./a", "a//b", "a/../b", "a/CON.txt", "NUL", "part.", "name ", "a:b", "e\u0301.txt", "a\x00b"):
            with self.subTest(path=path):
                changed = copy.deepcopy(request)
                changed["files"][0]["relative_path"] = path
                changed["entrypoint"] = path
                self.assertCode("unsafe_path", ca.normalize_request, changed)
        for change in ({"extra": True}, {"expected_catalog_revision": True}, {"entrypoint": "undeclared"}, {"confirmation": ""}):
            with self.subTest(change=change):
                changed = {**request, **change}
                with self.assertRaises(ca.ConfirmedArtifactError):
                    ca.normalize_request(changed)
        request["files"][0]["unknown"] = 1
        self.assertCode("invalid_schema", ca.normalize_request, request)

    def test_case_duplicates_file_parent_collisions_and_duplicate_sources(self):
        for names in (("paper.md", "PAPER.md"), ("paper.md", "paper.md/section")):
            with self.subTest(names=names):
                request = self.request()
                request["files"].append({"source_path": "work/other", "relative_path": names[1], "sha256": "a" * 64})
                self.assertCode("duplicate_path", ca.normalize_request, request)
        request = self.request()
        request["files"].append({**request["files"][0], "relative_path": "other.md"})
        self.assertCode("duplicate_path", ca.normalize_request, request)

    def test_projection_source_rejection_is_case_insensitive(self):
        request = self.request()
        request["files"][0]["source_path"] = "ARTIFACTS/CURRENT/manuscripts/main-paper/paper.md"
        self.assertCode("mutable_projection_source", ca.normalize_request, request)

    def test_invalid_evidence_and_finite_json_only(self):
        request = self.request()
        for evidence in ([{"path": "../escape", "sha256": "a" * 64}], [{"path": "check.json", "sha256": "A" * 64}], [{"path": "check.json", "sha256": "a" * 64, "extra": 1}], "check.json"):
            with self.subTest(evidence=evidence), self.assertRaises(ca.ConfirmedArtifactError):
                ca.normalize_request({**request, "evidence": evidence})
        self.assertCode("invalid_json", self.store.accept, request, lambda request: {"confidence": float("nan")})

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_sources_reject_leaf_and_parent_links_hardlinks_and_fifo(self):
        request = self.request()
        source = self.root / "work/paper.md"
        real = self.write("actual.md", b"a checked draft\n")
        source.unlink()
        source.symlink_to(real)
        self.assertCode("unsafe_path", self.accept, request)
        source.unlink()
        os.link(real, source)
        self.assertCode("unsafe_source", self.accept, request)
        source.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(source)
            self.assertCode("unsafe_source", self.accept, request)
            source.unlink()
        (self.root / "work").rmdir()
        target = self.root / "elsewhere"
        target.mkdir()
        (target / "paper.md").write_bytes(b"a checked draft\n")
        (self.root / "work").symlink_to(target, target_is_directory=True)
        self.assertCode("unsafe_path", self.accept, request)
        self.assertFalse(self.store.catalog_path.exists())

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_current_nested_link_is_preserved_by_explicit_backup(self):
        self.accept()
        external = self.write("outside-note.txt", b"not a view")
        link = self.root / "artifacts/current/personal-link"
        link.symlink_to(external)
        result = self.accept(self.request(revision=1, operation="next"))
        self.assertTrue(result["projection_pending"])
        self.assertTrue(link.is_symlink())
        repaired = self.store.repair(expected_revision=2, backup_conflicts=True)
        saved = next(path for path in repaired["backups"] if path.endswith("/current"))
        self.assertTrue((self.root / saved / "personal-link").is_symlink())
        self.assertEqual(external.read_bytes(), b"not a view")

    def test_copy_rejects_source_changed_during_stream(self):
        request = self.request(files={"paper.md": b"a" * 2048})
        original = os.read
        source = self.root / "work/paper.md"
        changed = False

        def racing_read(descriptor, size):
            nonlocal changed
            chunk = original(descriptor, size)
            if not changed and chunk == b"a" * 2048:
                changed = True
                source.write_bytes(b"b" * 2048)
            return chunk

        with mock.patch.object(ca.os, "read", side_effect=racing_read):
            self.assertCode("source_changed", self.accept, request)
        self.assertFalse(self.store.catalog_path.exists())

    def test_bounded_file_and_bundle_limits(self):
        request = self.request(files={"a": b"1234", "b": b"5678"})
        with mock.patch.object(ca, "MAX_FILES", 1):
            self.assertCode("invalid_files", ca.normalize_request, request)
        with mock.patch.object(ca, "MAX_FILE_BYTES", 3):
            self.assertCode("unsafe_source", self.accept, request)
        with mock.patch.object(ca, "MAX_BUNDLE_BYTES", 7):
            self.assertCode("bundle_too_large", self.accept, request)
        self.assertFalse(self.store.catalog_path.exists())

    def test_catalog_tampering_cannot_select_old_version_or_missing_operation(self):
        self.accept()
        first = self.store.resolve("main-paper")
        self.accept(self.request(revision=1, operation="second", files={"paper.md": b"version two"}))
        catalog = json.loads(self.store.catalog_path.read_text(encoding="utf-8"))
        catalog["artifacts"]["main-paper"]["current_version"] = first["version_id"]
        self.store.catalog_path.write_text(json.dumps(catalog))
        self.assertCode("invalid_catalog", self.store.status)
        self.assertCode("invalid_catalog", self.store.resolve, "main-paper")

    def test_corrupt_projection_marker_is_backup_repairable_not_silent(self):
        self.accept()
        marker = self.root / ".research/confirmed-artifacts/projection.json"
        marker.write_bytes(b"{broken")
        status = self.store.status()
        self.assertTrue(status["projection_pending"])
        self.assertTrue(status["projection_errors"])
        with self.assertRaises(ca.ConfirmedArtifactError):
            self.store.repair(expected_revision=1)
        repaired = self.store.repair(expected_revision=1, backup_conflicts=True)
        saved = next(path for path in repaired["backups"] if path.endswith("/projection.json"))
        self.assertEqual((self.root / saved).read_bytes(), b"{broken")
        self.assertFalse(self.store.status(verify=True)["projection_pending"])

    def test_verify_checks_snapshot_even_when_projection_marker_is_corrupt(self):
        self.accept()
        resolved = self.store.resolve("main-paper")
        self.write(resolved["snapshot_path"], b"tampered snapshot")
        self.write(".research/confirmed-artifacts/projection.json", b"{broken")
        self.assertTrue(self.store.status()["projection_pending"])
        self.assertCode("snapshot_changed", self.store.status, verify=True)

    def test_json_duplicate_fields_and_nonobjects_fail_closed(self):
        self.accept()
        for raw in (b'{"revision":1,"revision":2}', b"[]", b'{"value":NaN}'):
            with self.subTest(raw=raw):
                self.store.catalog_path.write_bytes(raw)
                self.assertCode("invalid_json", self.store.status)


if __name__ == "__main__":
    unittest.main()
