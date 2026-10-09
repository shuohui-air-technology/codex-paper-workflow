"""Material identity, efficient adoption, and authoritative reading across sessions."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scripts import progress_manager as progress
from scripts import material_manager as mm
from scripts.artifact_manager import ArtifactService
from scripts.confirmed_artifacts import ConfirmedArtifactError
from scripts.workflow_manager import WorkflowService, WorkflowManagerError

ROOT = Path(__file__).resolve().parents[1]


class MaterialManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "paper"
        self.project.mkdir()
        self.service = mm.MaterialService(self.project)

    def write(self, path, text="input v1\n"):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    def register(self, role="source-notes", candidate="working", paths=None, kind="input", artifact_type=None):
        paths = paths or {"notes.md": f"inputs/{role}.md"}
        for source in paths.values():
            if not (self.project / source).exists():
                self.write(source)
        return self.service.register({
            "schema_version": mm.ROLE_SCHEMA, "expected_registry_revision": self.service._registry()["revision"],
            "role_id": role, "candidate_id": candidate, "kind": kind,
            "artifact_type": artifact_type or ("materials" if kind == "input" else "manuscripts"),
            "entrypoint": next(iter(paths)), "files": [{"relative_path": relative, "source_path": source} for relative, source in paths.items()],
        })

    def adopt(self, role="source-notes", candidate="working", review=None, **kwargs):
        return self.service.accept(review or self.service.review(), {role: candidate}, "采用本次所选材料版本。", confirmed=True, **kwargs)

    def candidate(self, review, role="source-notes", candidate="working"):
        group = next(item for item in review["roles"] if item["role_id"] == role)
        return next(item for item in group["candidates"] if item["candidate_id"] == candidate)

    def assertCode(self, code, callback, *args, **kwargs):
        with self.assertRaises((mm.MaterialError, ConfirmedArtifactError)) as result:
            callback(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_readonly_empty_project_and_no_metadata_created(self):
        resumed = self.service.resume()
        self.assertTrue(resumed["ready_to_read"])
        self.assertEqual(resumed["roles"], [])
        self.assertEqual(list(self.project.iterdir()), [])

    def test_register_is_observation_not_adoption_and_is_idempotent(self):
        self.assertFalse(self.register()["confirmed"])
        again = self.register()
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["registry_revision"], 1)
        self.assertFalse(self.service.catalog.catalog_path.exists())
        self.assertEqual(self.candidate(self.service.review())["state"], "new")

    def test_saved_discovery_boundary_is_reused_and_can_be_overridden(self):
        self.register()
        self.write("elsewhere/large-results.txt", "unrelated")
        self.service.review(["inputs"], save_scan=True)
        resumed = mm.MaterialService(self.project).review()
        self.assertEqual(resumed["scopes"], ["inputs"])
        self.assertNotIn("elsewhere/large-results.txt", resumed["discovery"]["unregistered"])
        expanded = self.service.review(["."])
        self.assertIn("elsewhere/large-results.txt", expanded["discovery"]["unregistered"])

    def test_accept_refreshes_view_and_display_failure_does_not_repeat_adoption(self):
        self.register()
        review = self.service.review()
        with mock.patch.object(self.service, "_write_index", side_effect=OSError("display failed")):
            first = self.adopt(review=review)
        self.assertTrue(first["committed"])
        self.assertTrue(first["material_index_pending"])
        revision = self.service.catalog.metadata()["revision"]
        retry = self.adopt(review=review)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(self.service.catalog.metadata()["revision"], revision)
        self.service.review(save_scan=True)
        index = (self.project / mm.INDEX).read_text()
        self.assertIn("内容未变，沿用确认", index)
        self.assertIn(self.service.catalog.resolve("source-notes")["version_id"], index)

    def test_invalid_review_output_does_not_save_scan(self):
        self.register()
        self.assertCode("materials.invalid_review_path", self.service.review, save_scan=True, output="README.json")
        self.assertFalse((self.project / mm.SCAN).exists())

    def test_saved_unique_move_keeps_identity_after_later_edits(self):
        self.write("work/a.bib", "@article{citation, title={Example}}\n")
        self.register(paths={"main.md": "work/a.md", "references.bib": "work/a.bib"})
        self.adopt()
        catalog_before = self.service.catalog.catalog_path.read_bytes()
        self.service.review(save_scan=True)
        (self.project / "work/a.md").rename(self.project / "work/b.md")
        (self.project / "work/a.bib").rename(self.project / "work/b.bib")
        moved = self.service.review(save_scan=True)
        self.assertEqual(self.candidate(moved)["state"], "unchanged")
        self.service.accept(moved, {"source-notes": "working"})
        self.assertEqual(self.service.catalog.catalog_path.read_bytes(), catalog_before)
        self.write("work/b.md", "revised after moving\n")
        inspected = mm.MaterialService(self.project).review()
        changed = self.candidate(inspected)
        self.assertEqual(changed["state"], "changed")
        self.assertEqual(changed["changes"]["modified"], ["main.md"])
        self.assertNotIn("work/b.md", inspected["discovery"]["unregistered"])
        self.adopt(review=inspected)
        self.assertEqual((self.project / self.service.catalog.resolve("source-notes")["snapshot_path"]).read_text(), "revised after moving\n")

    def test_artifacts_working_files_are_not_reserved_display_sources(self):
        self.register(paths={"draft.md": "artifacts/draft.md"})
        self.assertEqual(self.candidate(self.service.review())["state"], "new")

    def test_registry_tracks_move_before_first_adoption_and_readonly_move_stays_readonly(self):
        self.register()
        registry_before = (self.project / mm.REGISTRY).read_bytes()
        (self.project / "inputs/source-notes.md").rename(self.project / "inputs/renamed.md")
        self.assertEqual(self.candidate(self.service.review())["state"], "new")
        self.assertEqual((self.project / mm.REGISTRY).read_bytes(), registry_before)
        moved = self.service.review(save_scan=True)
        self.assertEqual(moved["registry_revision"], 2)
        self.write("inputs/renamed.md", "edited before first adoption\n")
        changed = self.candidate(self.service.review())
        self.assertEqual(changed["files"][0]["source_path"], "inputs/renamed.md")
        self.assertEqual(changed["state"], "new")
        self.adopt()

    def test_confirmation_requires_decision_and_first_adoption_has_snapshot(self):
        self.register()
        review = self.service.review()
        self.assertCode("materials.confirmation_required", self.service.accept, review, {"source-notes": "working"})
        result = self.adopt(review=review)
        self.assertTrue(result["committed"])
        bound = self.service.catalog.resolve("source-notes")
        self.assertEqual(bound["provenance"]["mode"], "material-input")
        self.assertEqual((self.project / bound["snapshot_path"]).read_text(), "input v1\n")

    def test_unchanged_needs_no_approval_and_creates_no_version(self):
        self.register()
        self.adopt()
        review = self.service.review()
        self.assertFalse(review["roles"][0]["needs_confirmation"])
        catalog_before = self.service.catalog.catalog_path.read_bytes()
        result = self.service.accept(review, {"source-notes": "working"})
        self.assertEqual(result["unchanged_roles"], ["source-notes"])
        self.assertFalse(result["committed"])
        self.assertEqual(self.service.catalog.catalog_path.read_bytes(), catalog_before)

    def test_changed_candidate_has_diff_while_resume_uses_adopted_snapshot(self):
        self.register()
        self.adopt()
        self.write("inputs/source-notes.md", "input v2\n")
        candidate = self.candidate(self.service.review())
        self.assertEqual(candidate["state"], "changed")
        self.assertEqual(candidate["changes"]["modified"], ["notes.md"])
        self.assertIn("+input v2", candidate["changes"]["text_previews"][0]["diff"])
        resumed = mm.MaterialService(self.project).resume(["source-notes"])
        self.assertTrue(resumed["ready_to_read"])
        self.assertEqual((self.project / resumed["bindings"][0]["snapshot_path"]).read_text(), "input v1\n")
        self.adopt()
        self.assertEqual((self.project / self.service.catalog.resolve("source-notes")["snapshot_path"]).read_text(), "input v2\n")

    def test_two_candidates_never_select_by_filename_or_modification_date(self):
        self.register(candidate="original")
        self.adopt(candidate="original")
        self.write("inputs/latest-final.md", "other content\n")
        self.register(candidate="alternative", paths={"notes.md": "inputs/latest-final.md"})
        resumed = self.service.resume(["source-notes"])
        self.assertEqual((self.project / resumed["bindings"][0]["snapshot_path"]).read_text(), "input v1\n")
        self.write("inputs/third.md", "third content\n")
        self.register(candidate="third", paths={"notes.md": "inputs/third.md"})
        role = self.service.review()["roles"][0]
        self.assertTrue(role["needs_selection"])

    def test_exact_duplicate_does_not_require_reconfirmation(self):
        self.register()
        self.adopt()
        self.write("inputs/duplicate.md")
        self.register(candidate="copy", paths={"notes.md": "inputs/duplicate.md"})
        review = self.service.review()
        self.assertEqual(self.candidate(review, candidate="copy")["state"], "unchanged")
        self.assertFalse(review["roles"][0]["needs_confirmation"])
        self.assertTrue(review["discovery"]["duplicates"])
        self.assertFalse(self.service.accept(review, {"source-notes": "copy"})["committed"])

    def test_unique_move_is_recognized_and_snapshot_remains_readable(self):
        self.register()
        self.adopt()
        self.service.review(save_scan=True)
        (self.project / "inputs/source-notes.md").rename(self.project / "inputs/renamed.md")
        review = self.service.review()
        candidate = self.candidate(review)
        self.assertEqual(candidate["state"], "unchanged")
        self.assertEqual(candidate["files"][0]["source_path"], "inputs/renamed.md")
        self.assertEqual(review["discovery"]["relocated"], [{"from": "inputs/source-notes.md", "to": "inputs/renamed.md"}])
        self.assertTrue(self.service.resume(["source-notes"])["ready_to_read"])

    def test_ambiguous_move_is_reported_not_guessed(self):
        self.register()
        self.write("inputs/a.md")
        self.write("inputs/b.md")
        (self.project / "inputs/source-notes.md").unlink()
        candidate = self.candidate(self.service.review())
        self.assertEqual(candidate["state"], "unavailable")
        self.assertEqual(candidate["unavailable"][0]["possible_locations"], ["inputs/a.md", "inputs/b.md"])
        self.assertCode("materials.invalid_selection", self.adopt)

    def test_changes_after_review_reject_even_if_size_and_mtime_unchanged(self):
        self.register()
        review = self.service.review()
        source = self.project / "inputs/source-notes.md"
        before = source.stat()
        self.write("inputs/source-notes.md", "input v9\n")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertCode("materials.review_stale", self.adopt, review=review)
        self.assertFalse(self.service.catalog.catalog_path.exists())

    def test_registry_catalog_and_tampered_review_are_rejected(self):
        self.register()
        review = self.service.review()
        self.register("another")
        self.assertCode("materials.review_stale", self.adopt, review=review)
        fresh = self.service.review()
        altered = copy.deepcopy(fresh)
        self.candidate(altered)["files"][0]["sha256"] = "a" * 64
        self.assertCode("materials.review_stale", self.adopt, review=altered)
        self.adopt(review=fresh)
        self.assertCode("materials.review_stale", self.service.accept, fresh, {"another": "working"}, "采用", confirmed=True)

    def test_batch_adopts_two_roles_and_retries_original_commit(self):
        self.register()
        self.register("second")
        review = self.service.review()
        selected = {"source-notes": "working", "second": "working"}
        result = self.service.accept(review, selected, "采用这两组材料", confirmed=True)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["catalog_revision"], 2)
        (self.project / "inputs/source-notes.md").unlink()
        retried = mm.MaterialService(self.project).accept(review, selected, "采用这两组材料", confirmed=True)
        self.assertTrue(retried["idempotent"])
        self.assertEqual(retried["catalog_revision"], 2)
        self.assertCode("confirmed.idempotency_conflict", self.service.accept, review, selected, "不同决定", confirmed=True)

    def test_failed_batch_keeps_all_roles_unadopted(self):
        self.register()
        self.register("second")
        review = self.service.review()
        real_copy = self.service.catalog._copy_verified
        count = 0

        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("interrupted after first snapshot preparation")
            return real_copy(*args)

        with mock.patch.object(self.service.catalog, "_copy_verified", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.service.accept(review, {"source-notes": "working", "second": "working"}, "采用", confirmed=True)
        self.assertEqual(self.service.catalog.metadata()["revision"], 0)
        self.assertFalse(self.service.catalog.catalog_path.exists())

    def test_only_changed_roles_receive_new_versions_in_a_batch(self):
        self.register()
        self.register("second")
        selected = {"source-notes": "working", "second": "working"}
        self.service.accept(self.service.review(), selected, "采用", confirmed=True)
        self.write("inputs/second.md", "changed\n")
        result = self.service.accept(self.service.review(), selected, "采用第二组更新", confirmed=True)
        self.assertEqual(result["unchanged_roles"], ["source-notes"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["catalog_revision"], 3)

    def test_history_is_selectable_but_does_not_prompt_for_approval_on_resume(self):
        self.register(candidate="v1")
        self.adopt(candidate="v1")
        self.write("inputs/v2.md", "input v2\n")
        self.register(candidate="v2", paths={"notes.md": "inputs/v2.md"})
        self.adopt(candidate="v2")
        review = self.service.review()
        self.assertEqual(self.candidate(review, candidate="v1")["state"], "historical")
        self.assertFalse(review["roles"][0]["needs_confirmation"])
        self.adopt(candidate="v1", review=review)
        self.assertEqual((self.project / self.service.catalog.resolve("source-notes")["snapshot_path"]).read_text(), "input v1\n")

    def official_output(self):
        self.write(".research/progress.md", progress.template("paper"))
        evidence = self.write("checks.json", '{"status":"pass"}\n')
        self.register("main-paper", kind="output")
        return [{"path": "checks.json", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]

    def test_official_output_uses_existing_checks_and_implicit_existing_role(self):
        evidence = self.official_output()
        self.assertCode("confirmation.evidence_required", self.adopt, "main-paper")
        result = self.adopt("main-paper", evidence=evidence)
        self.assertTrue(result["committed"])
        bound = self.service.catalog.resolve("main-paper")
        self.assertEqual(bound["provenance"]["mode"], "official")
        (self.project / mm.REGISTRY).unlink()
        implicit = mm.MaterialService(self.project).review()
        self.assertEqual(self.candidate(implicit, "main-paper")["state"], "unchanged")

    def test_official_progress_evidence_drift_and_role_downgrade_are_rejected(self):
        evidence = self.official_output()
        review = self.service.review()
        self.write(".research/progress.md", progress.template("changed-project"))
        self.assertCode("materials.review_stale", self.adopt, "main-paper", review=review, evidence=evidence)
        fresh = self.service.review()
        self.write("checks.json", '{"status":"fail"}\n')
        self.assertCode("confirmation.evidence_changed", self.adopt, "main-paper", review=fresh, evidence=evidence)
        self.write("checks.json", '{"status":"pass","rerun":true}\n')
        evidence[0]["sha256"] = hashlib.sha256((self.project / "checks.json").read_bytes()).hexdigest()
        self.adopt("main-paper", evidence=evidence)
        self.assertCode("materials.role_conflict", self.register, "main-paper", kind="input")

    def test_unrelated_changes_do_not_block_role_and_missing_snapshot_does(self):
        self.register()
        self.adopt()
        self.register("other")
        self.write("inputs/other.md", "unconfirmed other changes\n")
        self.assertTrue(self.service.resume(["source-notes"])["ready_to_read"])
        missing = self.service.resume(["unknown-role"])
        self.assertFalse(missing["ready_to_read"])
        binding = self.service.catalog.resolve("source-notes")
        self.write(binding["snapshot_path"], "tampered snapshot\n")
        self.assertFalse(self.service.resume(["source-notes"])["ready_to_read"])

    def test_scan_tracks_add_modify_missing_without_modifying_work_or_progress(self):
        self.register()
        progress_file = self.write(".research/progress.md", progress.template("paper"))
        before = progress_file.read_bytes()
        self.service.review(save_scan=True)
        self.write("inputs/source-notes.md", "modified\n")
        self.write("inputs/new.pdf", "new fixture\n")
        self.write("node_modules/ignored.txt")
        review = self.service.review(save_scan=True)
        self.assertEqual(review["discovery"]["modified"], ["inputs/source-notes.md"])
        self.assertEqual(review["discovery"]["added"], ["inputs/new.pdf"])
        self.assertEqual(progress_file.read_bytes(), before)
        self.assertTrue((self.project / mm.INDEX).is_file())
        (self.project / "inputs/new.pdf").unlink()
        self.assertEqual(self.service.review()["discovery"]["missing"], ["inputs/new.pdf"])

    def test_incomplete_scan_does_not_claim_missing_or_complete(self):
        self.register()
        self.service.review(save_scan=True)
        with mock.patch.object(mm, "MAX_SCAN_BYTES", 1):
            review = self.service.review()
        self.assertFalse(review["discovery"]["complete"])
        self.assertEqual(review["discovery"]["missing"], [])
        self.assertTrue(review["discovery"]["errors"])

    def test_scope_change_does_not_report_out_of_scope_files_as_deleted(self):
        self.register()
        self.write("work/draft.md")
        self.service.review(save_scan=True)
        review = self.service.review(["inputs"])
        self.assertTrue(review["discovery"]["baseline_reset"])
        self.assertEqual(review["discovery"]["missing"], [])

    def test_unsafe_paths_and_generated_sources_are_rejected(self):
        for path in ("../outside.md", "artifacts/current/source.md", "ARTIFACTS/CURRENT/source.md", ".research/confirmed-artifacts/versions/x.md", ".RESEARCH/MATERIALS/fake.md"):
            with self.subTest(path=path):
                request = {"schema_version": mm.ROLE_SCHEMA, "expected_registry_revision": 0, "role_id": "unsafe", "candidate_id": "working", "kind": "input", "artifact_type": "materials", "entrypoint": "file.md", "files": [{"source_path": path, "relative_path": "file.md"}]}
                with self.assertRaises((mm.MaterialError, ConfirmedArtifactError)):
                    self.service.register(request)
        with self.assertRaises(mm.MaterialError):
            self.service.review(output="inputs/user-data.json")
        if hasattr(os, "symlink"):
            target = self.write("inputs/real.md")
            try:
                (self.project / "inputs/link.md").symlink_to(target)
            except OSError:
                return
            self.assertFalse(self.service.review()["discovery"]["complete"])

    def test_two_concurrent_approvals_of_same_review_record_one_version(self):
        self.register()
        review = self.service.review()

        def adopt():
            return mm.MaterialService(self.project).accept(review, {"source-notes": "working"}, "采用", confirmed=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: adopt(), range(2)))
        self.assertEqual(self.service.catalog.metadata()["revision"], 1)
        self.assertEqual(sum(result["idempotent"] for result in results), 1)

    def test_cli_saved_review_restart_accept_and_resolve(self):
        self.register()
        output = f"{mm.BASE}/reviews/adoption.json"
        script = str(ROOT / "scripts/material_manager.py")

        def cli(*args):
            result = subprocess.run([sys.executable, "-B", script, *args, "--project", str(self.project), "--json"], capture_output=True, text=True)
            return result.returncode, json.loads(result.stdout)

        code, reviewed = cli("scan", "--output", output)
        self.assertEqual(code, 0)
        self.assertEqual(reviewed["schema_version"], mm.REVIEW_SCHEMA)
        code, accepted = cli("accept", "--review", output, "--select", "source-notes=working", "--decision", "采用这份材料", "--confirm")
        self.assertEqual(code, 0, accepted)
        code, resumed = cli("resume", "--role", "source-notes")
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["roles"][0]["candidates"][0]["state"], "unchanged")
        code, resolved = cli("resolve", "--role", "source-notes")
        self.assertEqual(code, 0)
        self.assertEqual(resolved["bindings"][0]["verification"], "snapshot_verified")

    def activate_custom(self):
        self.write(".agents/skills/material-test/SKILL.md", "---\nname: material-test\ndescription: Use supplied notes to create a fixture draft.\n---\nRead the declared input and write a draft.\n")
        doc = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())
        node = doc["nodes"][0]
        node.update(skill_ref="material-test", inputs=["user_request"], outputs=["draft"], write_scopes=["draft"])
        doc.update(nodes=[node], edges=[], ui={"positions": {node["id"]: {"x": 0, "y": 0}}})
        workflow = WorkflowService(self.project)
        valid = workflow.validate_document(doc)
        self.assertEqual(valid["status"], "pass", valid)
        workflow.activate(doc, acknowledged_warning_codes=valid["required_warning_codes"])
        return workflow

    def test_custom_pipeline_reads_confirmed_snapshot_and_reports_new_adoption(self):
        self.register()
        self.adopt()
        original = self.service.catalog.resolve("source-notes")
        workflow = self.activate_custom()
        progress_path = self.write(".research/progress.md", "outdated invalid official progress\n")
        registered = workflow.register_confirmed_input("user_request", "source-notes", 1)
        self.assertEqual(registered["path"], original["snapshot_path"])
        self.write("inputs/source-notes.md", "new supplied material\n")
        self.adopt()
        resumed = self.service.resume(["source-notes"])
        self.assertEqual(resumed["mode"], "custom")
        self.assertIsNone(resumed["official_progress"])
        self.assertEqual(resumed["run_input_differences"][0]["run_version"], original["version_id"])
        invocation = workflow.claim("directions")
        self.assertEqual(invocation["input_artifacts"][0]["sha256"], registered["sha256"])
        self.assertEqual(invocation["input_artifacts"][0]["path"], original["snapshot_path"])
        self.write("work/draft.md", "A fixture stage result based on the declared input.\n")
        receipt = workflow.submit_result({
            "schema_version": invocation["result_schema"], "run_id": invocation["run_id"], "node_id": invocation["node_id"],
            "attempt": invocation["attempt"], "idempotency_token": invocation["idempotency_token"],
            "status": "succeeded", "outcome": "succeeded", "summary": "Created a fixture draft from the bound snapshot.",
            "artifacts": [{"id": "draft", "path": "work/draft.md"}], "uncertainties": [],
            "consumed_sources": [{key: item[key] for key in ("id", "path", "sha256")} for item in invocation["input_artifacts"]],
        })
        self.assertEqual(receipt["status"], "succeeded")
        self.register("main-paper", kind="output", paths={"paper.md": "work/draft.md"})
        registry = self.service._registry()
        candidate = registry["roles"]["main-paper"]["candidates"]["working"]
        candidate["files"][0]["source_artifact_id"] = "draft"
        self.service._write(mm.REGISTRY, registry)
        before = (workflow.store.paths.state.read_bytes(), workflow.store.paths.events.read_bytes(), progress_path.read_bytes())
        self.adopt("main-paper")
        self.assertEqual(self.service.catalog.resolve("main-paper")["provenance"]["mode"], "custom")
        self.assertEqual(before, (workflow.store.paths.state.read_bytes(), workflow.store.paths.events.read_bytes(), progress_path.read_bytes()))

    def test_confirmed_input_cli_and_snapshot_drift_before_registration(self):
        self.register()
        self.adopt()
        workflow = self.activate_custom()
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/workflow_manager.py"), "register-confirmed-input",
            "--project", str(self.project), "--artifact-id", "user_request", "--role", "source-notes", "--expect-catalog-revision", "1", "--json"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["confirmed_role"], "source-notes")
        resolved = self.service.catalog.resolve("source-notes")
        real_register = workflow.register_artifact

        def mutate(*args, **kwargs):
            self.write(resolved["snapshot_path"], "changed between resolution and registration\n")
            return real_register(*args, **kwargs)

        with mock.patch.object(workflow, "register_artifact", side_effect=mutate):
            with self.assertRaises(WorkflowManagerError) as raised:
                workflow.register_confirmed_input("user_request", "source-notes", 1)
        self.assertEqual(raised.exception.code, "runtime.confirmed_input_changed")

    def test_custom_output_without_committed_source_cannot_be_adopted(self):
        self.activate_custom()
        self.register("unverified", kind="output")
        self.assertCode("confirmation.artifact_mismatch", self.adopt, "unverified")
        self.assertFalse(self.service.catalog.catalog_path.exists())


if __name__ == "__main__":
    unittest.main()
