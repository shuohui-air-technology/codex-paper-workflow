"""Restart stopped runs without treating historical artifacts as live inputs."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts.workflow_engine.store import StoreError, WorkflowStore
from scripts.workflow_manager import WorkflowService


class WorkflowRestartTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.project / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        skills = self.project / "skills"
        skill = skills / "restart-task"
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            "---\nname: restart-task\ndescription: Restart test task\n---\nPerform the task.\n",
            encoding="utf-8",
        )
        self.service = WorkflowService(self.project, skill_roots=(skills,))
        document = json.loads((Path(__file__).parent / "fixtures/workflow_valid_linear.json").read_text())
        document["external_inputs"] = []
        document["nodes"] = document["nodes"][:1]
        document["nodes"][0].update(inputs=[], skill_ref="restart-task")
        document["edges"] = []
        document["ui"]["positions"].pop("design")
        self.document = document
        self.activation = self.activate()

    def activate(self):
        validation = self.service.validate_document(self.document)
        self.assertEqual(validation["status"], "pass", validation["errors"])
        return self.service.activate(
            self.document, acknowledged_warning_codes=validation["required_warning_codes"]
        )

    def complete(self):
        invocation = self.service.claim("directions")
        self.project.joinpath("idea.md").write_text("Original result.", encoding="utf-8")
        self.service.submit_result({
            "schema_version": "node-result-v2",
            "run_id": invocation["run_id"],
            "node_id": "directions",
            "attempt": invocation["attempt"],
            "idempotency_token": invocation["idempotency_token"],
            "status": "succeeded",
            "outcome": "succeeded",
            "summary": "Recorded the original research idea.",
            "artifacts": [{"id": "research_idea_brief", "path": "idea.md"}],
            "consumed_sources": [],
            "uncertainties": [],
        })

    def receipt_path(self):
        return (
            self.service.store.paths.receipts
            / self.activation["run_id"]
            / "directions-attempt-1.json"
        )

    def test_restart_archives_stopped_evidence_after_output_is_edited(self):
        self.complete()
        self.service.deactivate()
        paths = self.service.store.paths
        events = paths.events.read_bytes()
        snapshot = paths.state.read_bytes()
        receipt = self.receipt_path().read_bytes()
        self.project.joinpath("idea.md").write_text("New research input.", encoding="utf-8")

        restarted = self.activate()
        self.assertNotEqual(restarted["run_id"], self.activation["run_id"])
        archives = list(paths.archived_runs.iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].joinpath(paths.events.name).read_bytes(), events)
        self.assertEqual(archives[0].joinpath(paths.state.name).read_bytes(), snapshot)
        self.assertEqual(self.receipt_path().read_bytes(), receipt)
        self.assertEqual(self.service.ready()["ready"], [{"node_id": "directions", "node_type": "task"}])

    def test_stopped_running_attempt_is_historical_during_recovery(self):
        self.service.claim("directions")
        self.service.deactivate()
        paths = self.service.store.paths
        events = paths.events.read_bytes()
        snapshot = paths.state.read_bytes()

        recovered = self.service.store.recover()
        self.assertEqual((recovered.status, recovered.code), ("clean", "recovery.clean"))
        self.assertEqual(paths.events.read_bytes(), events)
        self.assertEqual(paths.state.read_bytes(), snapshot)
        self.assertEqual(recovered.state.nodes["directions"].status.value, "running")
        self.assertEqual(self.service.summary()["mode"], "official")

    def test_recovery_repairs_stopped_projections_without_new_runtime_events(self):
        self.complete()
        self.service.deactivate()
        paths = self.service.store.paths
        events = paths.events.read_bytes()
        snapshot = paths.state.read_bytes()
        receipt = self.receipt_path().read_bytes()
        self.project.joinpath("idea.md").write_text("Changed after stopping.", encoding="utf-8")
        # These are disposable fixture projections, rebuilt from the committed log.
        paths.state.unlink()
        paths.summary.unlink()
        self.receipt_path().unlink()

        recovered = self.service.store.recover()
        self.assertEqual(recovered.status, "recovered")
        self.assertEqual(paths.events.read_bytes(), events)
        self.assertEqual(paths.state.read_bytes(), snapshot)
        self.assertEqual(self.receipt_path().read_bytes(), receipt)
        self.assertIn("- Status: `stopped`", paths.summary.read_text())

    def test_active_artifact_drift_still_requires_recovery(self):
        self.complete()
        self.project.joinpath("idea.md").write_text("Changed during an active run.", encoding="utf-8")
        with self.assertRaises(StoreError) as caught:
            self.service.ready()
        self.assertEqual(caught.exception.code, "recovery.required")
        recovered = self.service.store.recover()
        self.assertEqual((recovered.status, recovered.code), ("recovered", "recovery.artifact_drift"))
        self.assertEqual(recovered.state.nodes["directions"].status.value, "stale")
        self.assertEqual(self.service.store.read_run_events()[-1].event_type, "artifacts_marked_stale")

    def test_stopped_receipt_tampering_still_blocks_recovery_and_restart(self):
        self.complete()
        self.service.deactivate()
        receipt = json.loads(self.receipt_path().read_text())
        receipt["summary"] = "Altered historical receipt."
        self.receipt_path().write_text(json.dumps(receipt), encoding="utf-8")
        paths = self.service.store.paths
        events = paths.events.read_bytes()
        recovered = self.service.store.recover()
        self.assertEqual((recovered.status, recovered.code), ("blocked", "receipt.conflict"))
        with self.assertRaises(StoreError) as caught:
            self.activate()
        self.assertEqual(caught.exception.code, "receipt.conflict")
        self.assertEqual(paths.events.read_bytes(), events)

    def test_stopped_event_tampering_still_blocks_recovery_and_restart(self):
        self.complete()
        self.service.deactivate()
        paths = self.service.store.paths
        events = [json.loads(line) for line in paths.events.read_text().splitlines()]
        events[-1]["event_hash"] = "0" * 64
        paths.events.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        tampered = paths.events.read_bytes()
        recovered = self.service.store.recover()
        self.assertEqual((recovered.status, recovered.code), ("blocked", "events.hash_mismatch"))
        with self.assertRaises(StoreError) as caught:
            self.activate()
        self.assertEqual(caught.exception.code, "events.hash_mismatch")
        self.assertEqual(paths.events.read_bytes(), tampered)

    def test_stopped_snapshot_tampering_still_blocks_recovery_and_restart(self):
        self.complete()
        self.service.deactivate()
        paths = self.service.store.paths
        snapshot = json.loads(paths.state.read_text())
        snapshot["last_applied_event_hash"] = "0" * 64
        paths.state.write_text(json.dumps(snapshot), encoding="utf-8")
        recovered = self.service.store.recover()
        self.assertEqual(recovered.status, "blocked")
        with self.assertRaises(StoreError):
            self.activate()

    def test_official_guard_holds_a_default_mode_lease_without_creating_workflow_state(self):
        project = self.project / "default-project"
        project.mkdir()
        store = WorkflowStore(project)
        with store.locked_official() as selection:
            self.assertEqual(selection.mode, "official")
            store._assert_active_lease()
            self.assertTrue(store.paths.lock.exists())
            self.assertFalse(store.paths.selection.exists())
            self.assertFalse(store.paths.audit_events.exists())
            self.assertFalse(store.paths.workflow.exists())
            self.assertFalse(store.paths.run_dir.exists())
        self.assertIsNone(store._active_lease_token)

    def test_official_guard_rejects_custom_mode_before_entering_the_update(self):
        paths = self.service.store.paths
        before = {path: path.read_bytes() for path in (paths.selection, paths.workflow, paths.events)}
        with self.assertRaises(StoreError) as caught:
            with self.service.store.locked_official():
                self.fail("custom mode entered an official-only update")
        self.assertEqual(caught.exception.code, "selection.custom_active")
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_official_guard_rejects_corrupt_selection(self):
        self.service.deactivate()
        self.service.store.paths.selection.write_text("{}", encoding="utf-8")
        with self.assertRaises(StoreError) as caught:
            with self.service.store.locked_official():
                self.fail("corrupt selection entered an official-only update")
        self.assertEqual(caught.exception.code, "selection.invalid")
        self.assertEqual(self.service.store.paths.selection.read_text(), "{}")

    def test_official_guard_reads_journal_authority_without_repairing_an_old_projection(self):
        paths = self.service.store.paths
        old_projection = paths.selection.read_bytes()
        self.service.deactivate()
        paths.selection.write_bytes(old_projection)
        before = {path: path.read_bytes() for path in (
            paths.selection, paths.audit_events, paths.workflow, paths.events, paths.state,
        )}
        with self.service.store.locked_official() as selection:
            self.assertEqual(selection.mode, "official")
            self.service.store._assert_active_lease()
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
