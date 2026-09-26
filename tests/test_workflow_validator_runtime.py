"""Validator attempt authority before executable adapters are introduced."""

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

from scripts.workflow_engine import store as store_codec
from scripts.workflow_engine.receipts import build_claim_evidence, build_stage_receipt
from scripts.workflow_engine.scheduler import (
    ArtifactRuntime, EdgeStatus, NodeStatus, refresh_ready,
    claim_transition, result_transition, validator_claim_transition, validator_result_transition,
    validator_retry_transition,
)
from scripts.workflow_engine.schema import WorkflowError
from scripts.workflow_engine.store import StoreError, WorkflowEvent, WorkflowStore
from tests import test_workflow_scheduler as scheduler_fixtures
from tests import test_workflow_store as store_fixtures


START = "2026-09-23T00:00:00Z"
END = "2026-09-23T00:00:01Z"


def plan_with_gate():
    return scheduler_fixtures.WorkflowSchedulerTests().compile(
        [scheduler_fixtures.validator("check", entry=True), scheduler_fixtures.task("accepted")],
        [scheduler_fixtures.edge("gate", "check", "accepted", trigger="pass")],
        external_inputs=("section",),
    )


def append_rehashed(store, state, event_type="artifact_registered", extra=None):
    prior = store.read_run_events()[-1]
    forged = WorkflowEvent.create(
        event_seq=prior.event_seq + 1, run_id=prior.run_id,
        semantic_sha256=prior.semantic_sha256, event_type=event_type,
        payload={"state": store_codec._state_data(state), "run_status": "active", **(extra or {})},
        previous_event_hash=prior.event_hash,
    )
    with store.paths.events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged.to_payload(), sort_keys=True, separators=(",", ":")) + "\n")
    return forged


class ValidatorRuntimeAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.joinpath("section.md").write_text("verified section", encoding="utf-8")
        self.plan = plan_with_gate()
        self.store = WorkflowStore(self.root)
        self.store.start_run(self.plan, "run-validator-gate")

    def register(self):
        with self.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            artifact = ArtifactRuntime("section", "section.md", hashlib.sha256(b"verified section").hexdigest(),
                                       "verified", "external", 0)
            ready = refresh_ready(self.plan, replace(state, artifacts=MappingProxyType({"section": artifact})))
            transaction.commit_transition("artifact_registered", ready)
        return ready

    def claim(self, *, token="validator-token"):
        with self.store.locked_run() as transaction:
            _, ready = transaction.load_active_run()
            running = validator_claim_transition(self.plan, ready, "check", token)
            claim = build_claim_evidence(self.plan, running, "check", transaction.input_witnesses(), START)
            event = transaction.commit_transition("validator_claimed", running, {"claim_evidence": claim})
        return running, claim, event

    def complete(self, outcome="pass", *, status="succeeded"):
        with self.store.locked_run() as transaction:
            _, running = transaction.load_active_run()
            completed = validator_result_transition(self.plan, running, {
                "node_id": "check", "attempt": running.nodes["check"].attempt,
                "status": status, "outcome": outcome if status == "succeeded" else "",
                "outputs": {}, "artifacts": (),
            })
            claim_event = transaction.events("validator_claimed")[-1]
            receipt = build_stage_receipt(self.plan, completed, "check", claim_event.payload["claim_evidence"],
                summary="Validator completed.", uncertainties=[], completed_at=END,
                error=None if status == "succeeded" else {"code": "validator.crashed", "message": "Execution failed."})
            event = transaction.commit_receipted_transition(
                "validator_result_recorded", completed, receipt,
                result_sha256=hashlib.sha256(b"validator-internal-result").hexdigest(),
                claim_event_seq=claim_event.event_seq,
            )
        return completed, receipt, event

    def test_claim_freezes_form_identity_and_input_then_pass_opens_gate(self):
        self.register()
        running, claim, _ = self.claim()
        self.assertEqual(running.nodes["check"].status, NodeStatus.RUNNING)
        self.assertEqual(claim["validator_config"]["input_roles"], {"file": "section"})
        self.assertEqual(claim["input_artifacts"], [{"id": "section", "source_id": "section",
            "path": "section.md", "sha256": hashlib.sha256(b"verified section").hexdigest()}])
        completed, receipt, _ = self.complete()
        self.assertEqual(receipt["output_artifacts"], [])
        self.assertEqual(completed.edges["gate"].status, EdgeStatus.SATISFIED)
        self.assertEqual(completed.nodes["accepted"].status, NodeStatus.READY)
        self.assertEqual(self.store.recover().status, "clean")
        self.assertNotIn("validator-token", self.store.paths.events.read_text())
        self.assertNotIn("validator-token", json.dumps(receipt))
        self.assertEqual(json.loads((self.store.paths.receipts / "run-validator-gate" /
            "check-attempt-1.json").read_text()), receipt)

    def test_domain_negative_and_execution_failure_have_distinct_receipts(self):
        for outcome in ("fail", "blocked", "pass"):
            with self.subTest(outcome=outcome):
                # One project per outcome keeps the committed attempt unique.
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    root.joinpath("section.md").write_text("verified section")
                    case = ValidatorRuntimeAuthorityTests()
                    case.root, case.plan, case.store = root, plan_with_gate(), WorkflowStore(root)
                    case.store.start_run(case.plan, "run-validator-gate")
                    case.register(); case.claim()
                    completed, receipt, _ = case.complete(outcome)
                    self.assertEqual((completed.nodes["check"].status, receipt["status"], receipt["outcome"]),
                                     (NodeStatus.SUCCEEDED, "succeeded", outcome))
                    self.assertEqual(case.store.recover().status, "clean")
        self.register(); self.claim()
        completed, receipt, _ = self.complete(status="failed")
        self.assertEqual((completed.nodes["check"].status, receipt["status"], receipt["outcome"]),
                         (NodeStatus.FAILED, "failed", ""))
        self.assertEqual(completed.edges["gate"].status, EdgeStatus.FAILED)

    def test_mismatched_receipt_and_duplicate_completion_reject_before_write(self):
        self.register(); self.claim()
        with self.store.locked_run() as transaction:
            _, running = transaction.load_active_run()
            completed = validator_result_transition(self.plan, running, {"node_id": "check", "attempt": 1,
                "status": "succeeded", "outcome": "pass", "outputs": {}, "artifacts": ()})
            claim_event = transaction.events("validator_claimed")[-1]
            receipt = build_stage_receipt(self.plan, completed, "check", claim_event.payload["claim_evidence"],
                summary="Validator completed.", uncertainties=[], completed_at=END)
            baseline = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
            for changed in ({**receipt, "claim_token_sha256": "0" * 64},
                            {**receipt, "input_artifacts": []},
                            {**receipt, "resolved_identity": {**receipt["resolved_identity"], "sha256": "1" * 64}},
                            {**receipt, "output_artifacts": [{"id": "invented", "path": "section.md",
                                "sha256": hashlib.sha256(b"verified section").hexdigest()}]},
                            {**receipt, "unexpected": True},
                            {key: value for key, value in receipt.items() if key != "started_at"}):
                with self.subTest(changed=changed):
                    with self.assertRaises(StoreError):
                        transaction.commit_receipted_transition("validator_result_recorded", completed, changed,
                            result_sha256="0" * 64, claim_event_seq=claim_event.event_seq)
                    self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), baseline)
            with self.assertRaises(StoreError):
                transaction.commit_receipted_transition("node_result_recorded", completed, receipt,
                    result_sha256="0" * 64, claim_event_seq=claim_event.event_seq)
            self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), baseline)
        self.complete()
        with self.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            baseline = self.store.paths.events.read_bytes()
            with self.assertRaises(StoreError):
                transaction.commit_receipted_transition("validator_result_recorded", state,
                    transaction.events("validator_result_recorded")[-1].payload["receipt"],
                    result_sha256="0" * 64, claim_event_seq=3)
            self.assertEqual(self.store.paths.events.read_bytes(), baseline)

    def test_generic_forgery_and_rehashed_full_chain_or_suffix_replay_reject(self):
        self.register(); running, _, _ = self.claim()
        completed = validator_result_transition(self.plan, running, {"node_id": "check", "attempt": 1,
            "status": "succeeded", "outcome": "pass", "outputs": {}, "artifacts": ()})
        for event_type in ("artifact_registered", "node_result_recorded", "validator_result_recorded"):
            with self.store.locked_run() as transaction:
                transaction.load_active_run()
                baseline = self.store.paths.events.read_bytes()
                with self.assertRaises(StoreError):
                    transaction.commit_transition(event_type, completed)
                self.assertEqual(self.store.paths.events.read_bytes(), baseline)
        for boundary in ("suffix", "full"):
            with self.subTest(boundary=boundary):
                event = append_rehashed(self.store, completed)
                if boundary == "full":
                    snapshot = json.loads(self.store.paths.state.read_text())
                    snapshot["state"] = store_codec._state_data(completed)
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    self.store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
                baseline = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                result = WorkflowStore(self.root).recover()
                self.assertEqual(result.status, "blocked")
                self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), baseline)
                # Restore only this test's deliberately forged tail for the second case.
                if boundary == "suffix":
                    self.store.paths.events.write_bytes(baseline[0].rsplit(b"\n", 2)[0] + b"\n")

    def test_interrupted_validator_running_is_recovered_blocked(self):
        self.register(); self.claim()
        recovered = WorkflowStore(self.root).recover()
        self.assertEqual((recovered.status, recovered.code), ("blocked", "recovery.running_work_uncertain"))
        self.assertEqual(recovered.state.nodes["check"].status, NodeStatus.BLOCKED)
        with self.store.locked_run() as transaction:
            _, interrupted = transaction.load_active_run()
            retry = validator_retry_transition(self.plan, interrupted, "check")
            transaction.commit_transition("validator_retried", retry, {"node_id": "check"})
        running, _, _ = self.claim(token="after-interruption")
        self.assertEqual(running.nodes["check"].attempt, 2)

    def test_validator_retry_has_dedicated_event_authority(self):
        self.register(); self.claim(); failed, _, _ = self.complete(status="failed")
        failed_snapshot = self.store.paths.state.read_bytes()
        retry = validator_retry_transition(self.plan, failed, "check")
        with self.store.locked_run() as transaction:
            transaction.load_active_run()
            baseline = self.store.paths.events.read_bytes()
            with self.assertRaises(StoreError):
                transaction.commit_transition("artifact_registered", retry)
            self.assertEqual(self.store.paths.events.read_bytes(), baseline)
            transaction.commit_transition("validator_retried", retry, {"node_id": "check"})
        with self.store.locked_run() as transaction:
            _, current = transaction.load_active_run()
            self.assertEqual((current.nodes["check"].status, current.nodes["check"].attempt,
                              current.nodes["check"].claim_token_hash), (NodeStatus.READY, 1, ""))
        running, claim, _ = self.claim(token="second-validator-token")
        self.assertEqual((running.nodes["check"].attempt, claim["attempt"]), (2, 2))
        self.complete()
        self.assertEqual(WorkflowStore(self.root).recover().status, "clean")
        self.store.paths.state.write_bytes(failed_snapshot)
        replayed = WorkflowStore(self.root).recover()
        self.assertEqual((replayed.status, replayed.code), ("recovered", "recovery.replayed"))
        self.assertEqual((replayed.state.nodes["check"].status, replayed.state.nodes["check"].attempt),
                         (NodeStatus.SUCCEEDED, 2))

    def test_rehashed_generic_retry_forgery_blocks_full_chain_and_suffix(self):
        for boundary in ("suffix", "full"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.root.joinpath("section.md").write_text("verified section")
                self.plan = plan_with_gate()
                self.store = WorkflowStore(self.root)
                self.store.start_run(self.plan, "run-validator-gate")
                self.register(); self.claim(); failed, _, _ = self.complete(status="failed")
                retry = validator_retry_transition(self.plan, failed, "check")
                event = append_rehashed(self.store, retry, "artifact_registered")
                if boundary == "full":
                    snapshot = json.loads(self.store.paths.state.read_text())
                    snapshot["state"] = store_codec._state_data(retry)
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    self.store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
                evidence = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                blocked = WorkflowStore(self.root).recover()
                self.assertEqual(blocked.status, "blocked")
                self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), evidence)

    def test_claim_config_and_event_name_forgery_reject_before_write_and_on_replay(self):
        for boundary in ("suffix", "full"):
            for forgery in ("task_event", "changed_form"):
                with self.subTest(boundary=boundary, forgery=forgery), TemporaryDirectory() as temporary:
                    self.root = Path(temporary)
                    self.root.joinpath("section.md").write_text("verified section")
                    self.plan = plan_with_gate()
                    self.store = WorkflowStore(self.root)
                    self.store.start_run(self.plan, "run-validator-gate")
                    ready = self.register()
                    running = validator_claim_transition(self.plan, ready, "check", "token")
                    claim = build_claim_evidence(self.plan, running, "check", {}, START)
                    event_type = "node_claimed" if forgery == "task_event" else "validator_claimed"
                    if forgery == "changed_form":
                        claim["validator_config"]["options"]["phase"] = "final"
                    baseline = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                    with self.store.locked_run() as transaction:
                        transaction.load_active_run()
                        with self.assertRaises(StoreError):
                            transaction.commit_transition(event_type, running, {"claim_evidence": claim})
                    self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), baseline)
                    event = append_rehashed(self.store, running, event_type, {"claim_evidence": claim})
                    if boundary == "full":
                        snapshot = json.loads(self.store.paths.state.read_text())
                        snapshot["state"] = store_codec._state_data(running)
                        snapshot["last_applied_event_seq"] = event.event_seq
                        snapshot["last_applied_event_hash"] = event.event_hash
                        self.store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
                    evidence = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                    result = WorkflowStore(self.root).recover()
                    self.assertEqual(result.status, "blocked")
                    self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), evidence)

    def test_result_receipt_forgery_rejects_rehashed_full_chain_and_suffix(self):
        for boundary in ("suffix", "full"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.root.joinpath("section.md").write_text("verified section")
                self.plan = plan_with_gate()
                self.store = WorkflowStore(self.root)
                self.store.start_run(self.plan, "run-validator-gate")
                self.register(); running, claim, claim_event = self.claim()
                complete = validator_result_transition(self.plan, running, {"node_id": "check", "attempt": 1,
                    "status": "succeeded", "outcome": "pass", "outputs": {}, "artifacts": ()})
                receipt = build_stage_receipt(self.plan, complete, "check", claim,
                    summary="Validator completed.", uncertainties=[], completed_at=END)
                forged = {**receipt, "input_artifacts": []}
                event = append_rehashed(self.store, complete, "validator_result_recorded", {
                    "receipt": forged, "result_sha256": "0" * 64, "claim_event_seq": claim_event.event_seq,
                })
                if boundary == "full":
                    snapshot = json.loads(self.store.paths.state.read_text())
                    snapshot["state"] = store_codec._state_data(complete)
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    self.store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
                evidence = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                result = WorkflowStore(self.root).recover()
                self.assertEqual(result.status, "blocked")
                self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), evidence)

    def test_committed_validator_result_replays_missing_snapshot_and_receipt(self):
        self.register(); self.claim()
        claim_snapshot = self.store.paths.state.read_bytes()
        completed, receipt, _ = self.complete()
        receipt_path = self.store.paths.receipts / "run-validator-gate" / "check-attempt-1.json"
        receipt_path.unlink()
        self.store.paths.state.write_bytes(claim_snapshot)
        recovered = WorkflowStore(self.root).recover()
        self.assertEqual((recovered.status, recovered.code), ("recovered", "recovery.replayed"))
        self.assertEqual(recovered.state.nodes["check"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(recovered.state.edges["gate"].status, EdgeStatus.SATISFIED)
        self.assertEqual(json.loads(receipt_path.read_text()), receipt)
        self.assertEqual(WorkflowStore(self.root).recover().status, "clean")

    def test_missing_validator_receipt_projection_requires_recovery(self):
        self.register(); self.claim(); _, receipt, _ = self.complete()
        receipt_path = self.store.paths.receipts / "run-validator-gate" / "check-attempt-1.json"
        receipt_path.unlink()
        with self.store.locked_run() as transaction:
            with self.assertRaises(StoreError) as pending:
                transaction.load_active_run()
            self.assertEqual(pending.exception.code, "recovery.required")
        recovered = WorkflowStore(self.root).recover()
        self.assertEqual((recovered.status, recovered.code), ("recovered", "recovery.replayed"))
        self.assertEqual(json.loads(receipt_path.read_text()), receipt)

    def test_validator_input_drift_stales_completed_attempt(self):
        self.register(); self.claim(); self.complete()
        self.root.joinpath("section.md").write_text("changed bytes")
        recovered = WorkflowStore(self.root).recover()
        self.assertEqual((recovered.status, recovered.code), ("recovered", "recovery.artifact_drift"))
        self.assertEqual(recovered.state.nodes["check"].status, NodeStatus.STALE)

    def test_duplicate_completion_event_and_conflicting_projection_block_recovery(self):
        for boundary in ("suffix", "full"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.root.joinpath("section.md").write_text("verified section")
                self.plan = plan_with_gate()
                self.store = WorkflowStore(self.root)
                self.store.start_run(self.plan, "run-validator-gate")
                self.register(); self.claim(); completed, receipt, original = self.complete()
                duplicate = append_rehashed(self.store, completed, "validator_result_recorded", {
                    "receipt": receipt, "result_sha256": original.payload["result_sha256"],
                    "claim_event_seq": original.payload["claim_event_seq"],
                })
                if boundary == "full":
                    snapshot = json.loads(self.store.paths.state.read_text())
                    snapshot["last_applied_event_seq"] = duplicate.event_seq
                    snapshot["last_applied_event_hash"] = duplicate.event_hash
                    self.store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
                evidence = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                blocked = WorkflowStore(self.root).recover()
                self.assertEqual(blocked.status, "blocked")
                self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), evidence)

        with TemporaryDirectory() as temporary:
            self.root = Path(temporary)
            self.root.joinpath("section.md").write_text("verified section")
            self.plan = plan_with_gate()
            self.store = WorkflowStore(self.root)
            self.store.start_run(self.plan, "run-validator-gate")
            self.register(); self.claim(); self.complete()
            receipt_path = self.store.paths.receipts / "run-validator-gate" / "check-attempt-1.json"
            receipt_path.write_text("{}\n")
            baseline = receipt_path.read_bytes()
            blocked = WorkflowStore(self.root).recover()
            self.assertEqual((blocked.status, blocked.code), ("blocked", "receipt.conflict"))
            self.assertEqual(receipt_path.read_bytes(), baseline)

    def test_historical_output_map_is_frozen_for_validator_input(self):
        producer = scheduler_fixtures.task("source", entry=True, outputs=("original",))
        check = scheduler_fixtures.validator("check")
        check["inputs"] = ["section"]
        plan = scheduler_fixtures.WorkflowSchedulerTests().compile(
            [producer, check],
            [scheduler_fixtures.edge("source-check", "source", "check",
                                     output_map={"original": "section"})],
        )
        root = self.root / "mapped"
        root.mkdir()
        store = WorkflowStore(root)
        store.start_run(plan, "run-mapped-input")
        root.joinpath("source.md").write_text("verified section")
        with store.locked_run() as transaction:
            _, ready = transaction.load_active_run()
            running = claim_transition(plan, ready, "source", "source-token")
            store_fixtures.commit_claim(transaction, running)
            produced = result_transition(plan, running, {"node_id": "source", "attempt": 1,
                "status": "succeeded", "outcome": "succeeded", "outputs": {"original": "source.md"},
                "artifacts": (ArtifactRuntime("original", "source.md",
                    hashlib.sha256(b"verified section").hexdigest(), "verified", "source", 1),)})
            store_fixtures.commit_result(transaction, produced)
            self.assertEqual(produced.nodes["check"].status, NodeStatus.READY)
            validator_running = validator_claim_transition(plan, produced, "check", "validator-token")
            claim = build_claim_evidence(plan, validator_running, "check", transaction.input_witnesses(), START)
            self.assertEqual(claim["input_artifacts"], [{"id": "section", "source_id": "original",
                "path": "source.md", "sha256": hashlib.sha256(b"verified section").hexdigest()}])
            transaction.commit_transition("validator_claimed", validator_running, {"claim_evidence": claim})
        self.assertEqual(WorkflowStore(root).recover().code, "recovery.running_work_uncertain")

    def test_skip_branch_execution_failure_receipt_can_be_retried_and_replayed(self):
        check = scheduler_fixtures.validator("check", entry=True)
        check["failure_policy"] = "skip_branch"
        self.plan = scheduler_fixtures.WorkflowSchedulerTests().compile(
            [check, scheduler_fixtures.task("accepted")],
            [scheduler_fixtures.edge("gate", "check", "accepted", trigger="pass")],
            external_inputs=("section",),
        )
        project = self.root / "skip-case"
        project.mkdir()
        self.root = project
        self.store = WorkflowStore(project)
        project.joinpath("section.md").write_text("verified section")
        self.store.start_run(self.plan, "run-validator-skip")
        self.register(); self.claim()
        failed, receipt, _ = self.complete(status="failed")
        self.assertEqual((failed.nodes["check"].status, receipt["status"], receipt["outcome"]),
                         (NodeStatus.FAILED, "failed", ""))
        self.assertEqual(failed.edges["gate"].status, EdgeStatus.INACTIVE)
        self.assertEqual(failed.nodes["accepted"].status, NodeStatus.SKIPPED)
        with self.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            retried = validator_retry_transition(self.plan, state, "check")
            transaction.commit_transition("validator_retried", retried, {"node_id": "check"})
        self.assertEqual(retried.edges["gate"].status, EdgeStatus.WAITING)
        self.assertEqual(retried.nodes["accepted"].status, NodeStatus.PENDING)
        retry_snapshot = self.store.paths.state.read_bytes()
        running, _, _ = self.claim(token="second-token")
        self.assertEqual(running.nodes["check"].attempt, 2)
        passed, receipt2, _ = self.complete()
        self.assertEqual(passed.nodes["accepted"].status, NodeStatus.READY)
        self.assertEqual(receipt2["status"], "succeeded")
        self.assertEqual(WorkflowStore(self.root).recover().status, "clean")
        self.store.paths.state.write_bytes(retry_snapshot)
        recovered = WorkflowStore(self.store.project_root).recover()
        self.assertEqual((recovered.status, recovered.state.nodes["accepted"].status),
                         ("recovered", NodeStatus.READY))

    def test_validator_retry_preserves_unrelated_interrupted_task_attempt(self):
        check = scheduler_fixtures.validator("check", entry=True)
        check["failure_policy"] = "skip_branch"
        self.plan = scheduler_fixtures.WorkflowSchedulerTests().compile(
            [check, scheduler_fixtures.task("accepted"),
             scheduler_fixtures.task("independent", entry=True)],
            [scheduler_fixtures.edge("gate", "check", "accepted", trigger="pass")],
            external_inputs=("section",),
        )
        project = self.root / "parallel-interruption"
        project.mkdir()
        self.root = project
        self.store = WorkflowStore(project)
        project.joinpath("section.md").write_text("verified section")
        self.store.start_run(self.plan, "run-parallel-interruption")
        self.register(); self.claim(); self.complete(status="failed")
        with self.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            running = claim_transition(self.plan, state, "independent", "independent-token")
            store_fixtures.commit_claim(transaction, running)
        recovered = WorkflowStore(project).recover()
        self.assertEqual((recovered.state.nodes["independent"].status,
                          recovered.state.nodes["independent"].attempt),
                         (NodeStatus.BLOCKED, 1))
        blocked_snapshot = self.store.paths.state.read_bytes()
        with self.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            retried = validator_retry_transition(self.plan, state, "check")
            self.assertEqual((retried.nodes["independent"].status,
                              retried.nodes["independent"].attempt),
                             (NodeStatus.BLOCKED, 1))
            transaction.commit_transition("validator_retried", retried, {"node_id": "check"})
        self.assertEqual(WorkflowStore(project).recover().status, "clean")
        self.store.paths.state.write_bytes(blocked_snapshot)
        replayed = WorkflowStore(project).recover()
        self.assertEqual((replayed.status, replayed.state.nodes["check"].status,
                          replayed.state.nodes["independent"].status),
                         ("recovered", NodeStatus.READY, NodeStatus.BLOCKED))

    def test_unclaimed_dependency_blocked_validator_retry_has_zero_writes(self):
        self.plan = scheduler_fixtures.WorkflowSchedulerTests().compile(
            [scheduler_fixtures.task("source", entry=True), scheduler_fixtures.validator("check")],
            [scheduler_fixtures.edge("source-check", "source", "check")],
            external_inputs=("section",),
        )
        project = self.root / "unclaimed-case"
        project.mkdir()
        self.root = project
        self.store = WorkflowStore(project)
        project.joinpath("section.md").write_text("verified section")
        self.store.start_run(self.plan, "run-unclaimed")
        self.register()
        with self.store.locked_run() as transaction:
            _, ready = transaction.load_active_run()
            running = claim_transition(self.plan, ready, "source", "source-token")
            store_fixtures.commit_claim(transaction, running)
            failed = result_transition(self.plan, running, {"node_id": "source", "attempt": 1,
                "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
            store_fixtures.commit_result(transaction, failed)
        self.assertEqual((failed.nodes["check"].status, failed.nodes["check"].attempt),
                         (NodeStatus.BLOCKED, 0))
        baseline = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
        with self.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            with self.assertRaises(WorkflowError):
                candidate = validator_retry_transition(self.plan, state, "check")
                transaction.commit_transition("validator_retried", candidate, {"node_id": "check"})
            with self.assertRaises(StoreError):
                transaction.commit_transition("validator_retried", state, {"node_id": "check"})
        self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), baseline)

    def test_rehashed_unclaimed_validator_retry_blocks_full_chain_and_suffix(self):
        for boundary in ("suffix", "full"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.root.joinpath("section.md").write_text("verified section")
                self.plan = scheduler_fixtures.WorkflowSchedulerTests().compile(
                    [scheduler_fixtures.task("source", entry=True), scheduler_fixtures.validator("check")],
                    [scheduler_fixtures.edge("source-check", "source", "check")],
                    external_inputs=("section",),
                )
                self.store = WorkflowStore(self.root)
                self.store.start_run(self.plan, "run-unclaimed")
                self.register()
                with self.store.locked_run() as transaction:
                    _, ready = transaction.load_active_run()
                    running = claim_transition(self.plan, ready, "source", "source-token")
                    store_fixtures.commit_claim(transaction, running)
                    failed = result_transition(self.plan, running, {"node_id": "source", "attempt": 1,
                        "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
                    store_fixtures.commit_result(transaction, failed)
                self.assertEqual(failed.nodes["check"].status, NodeStatus.BLOCKED)
                event = append_rehashed(self.store, failed, "validator_retried", {"node_id": "check"})
                if boundary == "full":
                    snapshot = json.loads(self.store.paths.state.read_text())
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    self.store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
                evidence = (self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes())
                result = WorkflowStore(self.root).recover()
                self.assertEqual(result.status, "blocked")
                self.assertEqual((self.store.paths.events.read_bytes(), self.store.paths.state.read_bytes()), evidence)


if __name__ == "__main__":
    unittest.main()
