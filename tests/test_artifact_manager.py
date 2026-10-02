"""Confirmation must preserve workflow evidence while identifying one latest version."""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import progress_manager as progress
from scripts.artifact_manager import ArtifactService
from scripts.confirmed_artifacts import ConfirmedArtifactError
from scripts.workflow_manager import WorkflowService


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtifactManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "paper"
        self.project.mkdir()
        self.source = self.project / "work" / "paper.md"
        self.source.parent.mkdir()
        self.source.write_text("已确认的第一版\n", encoding="utf-8")
        self.evidence = self.project / "checks.json"
        self.evidence.write_text('{"status":"pass"}\n', encoding="utf-8")
        self.progress = self.project / ".research" / "progress.md"
        progress.atomic_write(self.progress, progress.template("confirmation-project"))
        self.service = ArtifactService(self.project)

    def request(self, *, operation="confirm-first", revision=0):
        return {
            "schema_version": "confirmed-artifact-request-v1",
            "operation_id": operation,
            "artifact_id": "main-manuscript",
            "artifact_type": "manuscripts",
            "expected_catalog_revision": revision,
            "entrypoint": "paper.md",
            "files": [{"source_path": "work/paper.md", "relative_path": "paper.md", "sha256": digest(self.source)}],
            "confirmation": "用户确认采纳这份已检查的稿件。",
            "expected_progress_sha256": progress.document_sha256(progress.read_text(self.progress)),
            "evidence": [{"path": "checks.json", "sha256": digest(self.evidence)}],
        }

    def activate_custom(self):
        skill_root = self.root / "skills"
        skill = skill_root / "test-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: test-skill\ndescription: Produce a fixture draft.\n---\nCreate a test draft.\n", encoding="utf-8")
        document = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text(encoding="utf-8"))
        node = document["nodes"][0]
        node.update(skill_ref="test-skill", inputs=[], outputs=["draft"], write_scopes=["draft"])
        document.update(nodes=[node], edges=[], external_inputs=[], ui={"positions": {node["id"]: {"x": 80, "y": 80}}})
        workflow = WorkflowService(self.project, skill_roots=(skill_root,))
        validated = workflow.validate_document(document)
        self.assertEqual(validated["status"], "pass", validated)
        workflow.activate(document, acknowledged_warning_codes=validated["required_warning_codes"])
        invocation = workflow.claim(node["id"])
        workflow.submit_result({
            "schema_version": "node-result-v2", "run_id": invocation["run_id"],
            "node_id": invocation["node_id"], "attempt": invocation["attempt"],
            "idempotency_token": invocation["idempotency_token"],
            "status": "succeeded", "outcome": "succeeded", "summary": "A checked draft was produced.",
            "artifacts": [{"id": "draft", "path": "work/paper.md"}], "uncertainties": [], "consumed_sources": [],
        })
        return workflow

    def test_confirmation_is_explicit_and_failure_leaves_no_catalog(self):
        with self.assertRaises(ConfirmedArtifactError):
            self.service.accept(self.request())
        self.assertFalse((self.project / ".research/confirmed-artifacts/catalog.json").exists())

    def test_official_confirmation_binds_the_read_progress_and_evidence(self):
        request = self.request()
        old_progress = self.progress.read_bytes()
        result = self.service.accept(request, confirmed=True)
        self.assertTrue(result["committed"])
        resolved = self.service.catalog.resolve("main-manuscript", expected_revision=1)
        self.assertEqual(resolved["provenance"]["mode"], "official")
        self.assertEqual(resolved["provenance"]["stage"], "intake")
        self.assertEqual((self.project / resolved["snapshot_path"]).read_bytes(), self.source.read_bytes())
        self.assertEqual(self.progress.read_bytes(), old_progress)
        self.assertTrue(self.source.is_file())

    def test_stale_progress_or_evidence_cannot_be_confirmed(self):
        for changed in ("progress", "evidence"):
            with self.subTest(changed=changed):
                request = self.request()
                if changed == "progress":
                    request["expected_progress_sha256"] = "0" * 64
                else:
                    request["evidence"][0]["sha256"] = "0" * 64
                with self.assertRaises(ConfirmedArtifactError):
                    self.service.accept(request, confirmed=True)
                self.assertFalse((self.project / ".research/confirmed-artifacts/catalog.json").exists())

    def test_official_blocking_rule_prevents_new_confirmation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            progress.cmd_record_error(types.SimpleNamespace(
                file=str(self.progress), stage="intake", error="Source data failed verification",
                cause="Mismatching records", impact="Claims require checking", severity="critical",
                blocking="true", rule="Recheck source data", check="Compare against the source",
                stages="intake", refs="checks.json",
            ))
        with self.assertRaises(ConfirmedArtifactError) as caught:
            self.service.accept(self.request(), confirmed=True)
        self.assertEqual(caught.exception.code, "confirmation.validity_blocked")
        self.assertFalse((self.project / ".research/confirmed-artifacts/catalog.json").exists())

    def test_new_working_draft_does_not_change_latest_confirmed_snapshot(self):
        request = self.request()
        self.service.accept(request, confirmed=True)
        first = self.service.catalog.resolve("main-manuscript")
        self.source.write_text("尚未确认的第二版\n", encoding="utf-8")
        unchanged = self.service.catalog.resolve("main-manuscript")
        self.assertEqual(first["version_id"], unchanged["version_id"])
        self.assertEqual((self.project / first["snapshot_path"]).read_text(encoding="utf-8"), "已确认的第一版\n")
        repeated = self.service.accept(request, confirmed=True)
        self.assertTrue(repeated["idempotent"])
        self.service.accept(self.request(operation="confirm-second", revision=1), confirmed=True)
        second = self.service.catalog.resolve("main-manuscript", expected_revision=2)
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertTrue((self.project / first["snapshot_path"]).is_file())
        self.assertEqual((self.project / second["snapshot_path"]).read_bytes(), self.source.read_bytes())

    def test_withdrawing_current_does_not_fall_back_to_history(self):
        self.service.accept(self.request(), confirmed=True)
        self.service.catalog.withdraw("main-manuscript", 1, "withdraw-first", "The user withdrew this approval.")
        with self.assertRaises(ConfirmedArtifactError):
            self.service.catalog.resolve("main-manuscript")

    def test_custom_confirmation_preserves_original_receipts_and_state(self):
        workflow = self.activate_custom()
        request = self.request()
        request.pop("expected_progress_sha256")
        request["files"][0]["source_artifact_id"] = "draft"
        before = workflow.store.paths.state.read_bytes(), workflow.store.paths.events.read_bytes(), self.progress.read_bytes()
        self.service.accept(request, confirmed=True)
        resolved = self.service.catalog.resolve("main-manuscript")
        self.assertEqual(resolved["provenance"]["mode"], "custom")
        self.assertEqual(resolved["provenance"]["bindings"][0]["original_binding_path"], "work/paper.md")
        self.assertEqual((workflow.store.paths.state.read_bytes(), workflow.store.paths.events.read_bytes(), self.progress.read_bytes()), before)
        self.source.write_text("New work after confirmation.\n", encoding="utf-8")
        self.assertEqual(self.service.catalog.resolve("main-manuscript")["version_id"], resolved["version_id"])

    def test_custom_confirmation_rejects_unregistered_sources(self):
        self.activate_custom()
        request = self.request()
        request.pop("expected_progress_sha256")
        request["files"][0]["source_artifact_id"] = "invented"
        with self.assertRaises(ConfirmedArtifactError):
            self.service.accept(request, confirmed=True)

    def test_custom_receipt_change_after_run_validation_cannot_be_recorded_as_provenance(self):
        self.activate_custom()
        request = self.request()
        request.pop("expected_progress_sha256")
        request["files"][0]["source_artifact_id"] = "draft"
        original = self.service.workflow._load

        def changed_receipt(*args, **kwargs):
            plan, state = original(*args, **kwargs)
            artifact = state.artifacts["draft"]
            receipt = (self.service.workflow.store.paths.receipts / state.run_id
                       / f"{artifact.producer_node_id}-attempt-{artifact.producer_attempt}.json")
            receipt.write_text('{"unrelated":"changed after validation"}\n', encoding="utf-8")
            return plan, state

        with mock.patch.object(self.service.workflow, "_load", side_effect=changed_receipt):
            with self.assertRaises(ConfirmedArtifactError) as caught:
                self.service.accept(request, confirmed=True)
        self.assertEqual(caught.exception.code, "confirmation.receipt_mismatch")
        self.assertFalse((self.project / ".research/confirmed-artifacts/catalog.json").exists())

    def test_cli_direct_and_module_status_are_read_only(self):
        empty = self.root / "empty-project"
        empty.mkdir()
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        for command in ([sys.executable, "-B", str(ROOT / "scripts/artifact_manager.py")],
                        [sys.executable, "-B", "-m", "scripts.artifact_manager"]):
            result = subprocess.run([*command, "status", "--project", str(empty), "--json"], cwd=self.root, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["catalog_revision"], 0)
            self.assertEqual(list(empty.iterdir()), [])

    def test_cli_accept_requires_confirmation_flag(self):
        request = self.project / "confirmation.json"
        request.write_text(json.dumps(self.request(), ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/artifact_manager.py"), "accept", "--project", str(self.project), "--request", "confirmation.json"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "cli.invalid_arguments")
        self.assertFalse((self.project / ".research/confirmed-artifacts/catalog.json").exists())

    def test_workflow_summary_reports_compact_confirmation_metadata(self):
        self.service.accept(self.request(), confirmed=True)
        summary = self.service.workflow.summary()["confirmed_artifacts"]
        self.assertEqual(summary["catalog_revision"], 1)
        self.assertEqual(summary["current_count"], 1)
        self.assertEqual(summary["verification"], "not_checked")
        self.assertNotIn("artifacts", summary)

    def test_corrupt_confirmation_catalog_cannot_masquerade_as_no_confirmations(self):
        from scripts.workflow_manager import WorkflowManagerError
        self.service.accept(self.request(), confirmed=True)
        catalog = self.project / ".research/confirmed-artifacts/catalog.json"
        catalog.write_text('{"schema_version":"broken"}', encoding="utf-8")
        before = catalog.read_bytes()
        with self.assertRaises(WorkflowManagerError):
            self.service.workflow.summary()
        self.assertEqual(catalog.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
