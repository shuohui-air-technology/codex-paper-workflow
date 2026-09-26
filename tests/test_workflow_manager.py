import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest import mock

from scripts.workflow_engine.receipts import (
    ReceiptError,
    canonical_result_sha256,
    parse_result,
    sha256_file,
    validate_stage_receipt,
)


def task_result():
    return {
        "schema_version": "node-result-v1", "run_id": "run-1",
        "node_id": "produce", "attempt": 1, "idempotency_token": "secret-token",
        "status": "succeeded", "outcome": "succeeded", "summary": "Created draft.",
        "artifacts": [{"id": "draft", "path": "draft.md"}], "uncertainties": [],
    }


def task_receipt():
    return {
        "schema_version": "stage-receipt-v2", "workflow_id": "flow",
        "semantic_revision": 1, "semantic_sha256": "a" * 64, "run_id": "run-1",
        "node_id": "produce", "node_type": "task", "attempt": 1,
        "claim_token_sha256": hashlib.sha256(b"secret-token").hexdigest(),
        "resolved_identity": {
            "kind": "skill", "catalog_id": "test-skill", "relative_path": "test-skill",
            "skill_sha256": "b" * 64, "tree_sha256": "sha256:" + "c" * 64, "locked": False,
        },
        "input_artifacts": [{"id": "source", "source_id": "original", "path": "source.md", "sha256": "d" * 64}],
        "output_artifacts": [{"id": "draft", "path": "draft.md", "sha256": "e" * 64}],
        "status": "succeeded", "outcome": "succeeded",
        "started_at": "2026-09-23T00:00:00Z", "completed_at": "2026-09-23T00:00:01Z",
        "summary": "Created draft.", "uncertainties": [], "error": None,
    }


class ReceiptCodecTests(unittest.TestCase):
    def test_receipt_preserves_exact_hashed_evidence(self):
        receipt = task_receipt()
        actual = validate_stage_receipt(receipt)
        self.assertEqual(actual, receipt)
        self.assertNotIn("secret-token", json.dumps(actual))
        self.assertEqual(actual["input_artifacts"][0]["source_id"], "original")

    def test_validator_receipt_codec_only_accepts_declared_domain_outcomes(self):
        receipt = task_receipt()
        receipt.update(node_type="validator", outcome="pass", output_artifacts=[], resolved_identity={
            "kind": "validator", "validator_id": "figure-contract", "script": "scripts/figure_contract_validator.py",
            "sha256": "a" * 64, "adapter": "figure_contract_v1", "input_schema": "figure_contract_v1",
            "outcomes": ["pass", "fail", "blocked"],
        })
        self.assertEqual(validate_stage_receipt(receipt)["outcome"], "pass")
        with self.assertRaises(ReceiptError):
            validate_stage_receipt(dict(receipt, outcome="invented"))

    def test_receipt_rejects_missing_or_extra_keys_at_each_level(self):
        for section in (None, "resolved_identity", "input_artifacts", "output_artifacts"):
            for change in ("missing", "extra"):
                with self.subTest(section=section, change=change):
                    receipt = task_receipt()
                    target = receipt if section is None else receipt[section]
                    if isinstance(target, list):
                        target = target[0]
                    if change == "missing":
                        target.pop(next(iter(target)))
                    else:
                        target["client_trusted"] = True
                    with self.assertRaises(ReceiptError):
                        validate_stage_receipt(receipt)

    def test_receipt_rejects_bad_types_hashes_time_and_unbounded_text(self):
        changes = [
            ("attempt", True), ("semantic_revision", 0), ("claim_token_sha256", "A" * 64),
            ("summary", ""), ("summary", "x" * 4001), ("uncertainties", [False]),
            ("started_at", "2026-02-30T00:00:00Z"),
            ("completed_at", "2026-09-22T00:00:00Z"),
            ("completed_at", "2026-09-23T00:00:01+00:00"),
            ("error", {"code": "failed", "message": "broken", "extra": 1}),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=str(value)[:50]):
                receipt = task_receipt()
                receipt[key] = value
                with self.assertRaises(ReceiptError):
                    validate_stage_receipt(receipt)

    def test_receipt_rejects_unsafe_paths_duplicate_ids_and_unordered_artifacts(self):
        for path in ("../draft.md", "/draft.md", "a/../draft.md", "a\\draft.md"):
            receipt = task_receipt()
            receipt["output_artifacts"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ReceiptError):
                validate_stage_receipt(receipt)
        for artifact_id in ("draft", "alpha"):
            receipt = task_receipt()
            item = dict(receipt["output_artifacts"][0], id=artifact_id)
            receipt["output_artifacts"].append(item)
            with self.subTest(artifact_id=artifact_id), self.assertRaises(ReceiptError):
                validate_stage_receipt(receipt)

    def test_failed_receipt_requires_structured_error_and_no_scientific_outcome(self):
        receipt = task_receipt()
        receipt.update(status="failed", outcome="", output_artifacts=[], error={"code": "task.failed", "message": "Execution failed."})
        self.assertEqual(validate_stage_receipt(receipt)["status"], "failed")
        for update in ({"error": None}, {"outcome": "fail"}, {"output_artifacts": task_receipt()["output_artifacts"]}):
            with self.subTest(update=update), self.assertRaises(ReceiptError):
                validate_stage_receipt(dict(receipt, **update))

    def test_result_canonical_digest_is_order_independent_but_payload_sensitive(self):
        result = task_result()
        parsed = parse_result(json.dumps(result).encode())
        self.assertEqual(canonical_result_sha256(parsed), canonical_result_sha256(dict(reversed(list(result.items())))))
        changed = dict(result, summary="Different report.")
        self.assertNotEqual(canonical_result_sha256(result), canonical_result_sha256(changed))
        self.assertEqual(parsed["artifacts"], [{"id": "draft", "path": "draft.md"}])

    def test_result_rejects_duplicate_json_keys_and_client_hashes(self):
        raw = json.dumps(task_result())
        with self.assertRaises(ReceiptError):
            parse_result(raw[:-1] + ', "attempt": 2}')
        result = task_result()
        result["artifacts"][0]["sha256"] = "f" * 64
        with self.assertRaises(ReceiptError):
            parse_result(result)
        for update in ({"attempt": True}, {"status": "pass"}, {"summary": ""}, {"artifacts": []}, {"uncertainties": [None]}):
            # Zero outputs is valid at codec level; declarations are checked by manager.
            if update == {"artifacts": []}:
                self.assertEqual(parse_result(dict(task_result(), **update))["artifacts"], [])
                continue
            with self.subTest(update=update), self.assertRaises(ReceiptError):
                parse_result(dict(task_result(), **update))

    def test_untyped_status_and_nested_oversized_envelopes_fail_as_protocol_errors(self):
        for value in ([], {}, True, None):
            with self.subTest(status=value), self.assertRaises(ReceiptError):
                validate_stage_receipt(dict(task_receipt(), status=value))
        with self.assertRaises(ReceiptError):
            parse_result(" " * (1024 * 1024 + 1))

    def test_file_hash_uses_actual_bytes(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "result"
            path.write_bytes(b"abc")
            self.assertEqual(sha256_file(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class TaskProtocolTests(unittest.TestCase):
    def setUp(self):
        from scripts.workflow_manager import WorkflowService
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.project / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        root = self.project / "skills"
        (root / "test-workflow-task").mkdir(parents=True)
        (root / "test-workflow-task" / "SKILL.md").write_text("---\nname: test-workflow-task\ndescription: Test skill\n---\nPerform the task.\n")
        self.service = WorkflowService(self.project, skill_roots=(root,))
        document = json.loads((Path(__file__).parent / "fixtures/workflow_valid_linear.json").read_text())
        document["external_inputs"] = []
        document["nodes"] = document["nodes"][:1]
        document["nodes"][0]["inputs"] = []
        document["nodes"][0]["skill_ref"] = "test-workflow-task"
        document["edges"] = []
        document["ui"]["positions"].pop("design")
        self.document = document
        validation = self.service.validate_document(document)
        self.assertEqual(validation["status"], "pass", validation["errors"])
        self.service.activate(document, acknowledged_warning_codes=validation["required_warning_codes"])

    def result(self, invocation):
        (self.project / "idea.md").write_text("evidence-backed idea", encoding="utf-8")
        return dict(task_result(), run_id=invocation["run_id"], node_id="directions", attempt=invocation["attempt"], idempotency_token=invocation["idempotency_token"], artifacts=[{"id": "research_idea_brief", "path": "idea.md"}])

    def test_claim_submit_and_duplicate_bind_hashes_without_persisting_token(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        receipt = self.service.submit_result(result)
        self.assertEqual(receipt["output_artifacts"][0]["sha256"], sha256_file(self.project / "idea.md"))
        self.assertEqual(receipt["claim_token_sha256"], hashlib.sha256(invocation["idempotency_token"].encode()).hexdigest())
        before = self.service.store.paths.events.read_bytes()
        self.assertEqual(self.service.submit_result(result), receipt)
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)
        self.assertNotIn(invocation["idempotency_token"].encode(), before)
        path = self.service.store.paths.receipts / invocation["run_id"] / "directions-attempt-1.json"
        self.assertEqual(json.loads(path.read_bytes()), receipt)

    def test_changed_duplicate_and_token_reject_without_writes(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        self.service.submit_result(result)
        before = self.service.store.paths.events.read_bytes()
        for update, code in (({"summary": "Changed"}, "receipt.idempotency_conflict"), ({"idempotency_token": "different"}, "receipt.stale_attempt")):
            with self.subTest(update=update), self.assertRaises(Exception) as caught:
                self.service.submit_result(dict(result, **update))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_generic_claim_event_cannot_bypass_evidence(self):
        from scripts.workflow_engine.scheduler import claim_transition
        from scripts.workflow_engine.store import StoreError
        with self.service.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
            running = claim_transition(plan, state, "directions", "token")
            before = self.service.store.paths.events.read_bytes()
            for event_type in ("node_claimed", "fact_recorded"):
                with self.subTest(event_type=event_type), self.assertRaises(StoreError):
                    transaction.commit_transition(event_type, running)
                self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_claim_evidence_rejects_noncanonical_timestamp_before_write(self):
        from scripts.workflow_engine.receipts import build_claim_evidence
        from scripts.workflow_engine.scheduler import claim_transition
        from scripts.workflow_engine.store import StoreError
        with self.service.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
            running = claim_transition(plan, state, "directions", "token")
            claim = build_claim_evidence(plan, running, "directions", transaction.input_witnesses(), "2026-09-23T00:00:00Z")
            claim["started_at"] = "2026-09-23 00:00:00Z"
            before = self.service.store.paths.events.read_bytes()
            with self.assertRaises(StoreError):
                transaction.commit_transition("node_claimed", running, {"claim_evidence": claim})
            self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_generic_terminal_event_cannot_bypass_evidence(self):
        from scripts.workflow_engine.scheduler import result_transition
        from scripts.workflow_engine.store import StoreError
        self.service.claim("directions")
        with self.service.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
            failed = result_transition(plan, state, {"node_id": "directions", "attempt": 1, "status": "failed", "outcome": "", "outputs": {}, "artifacts": []})
            before = self.service.store.paths.events.read_bytes()
            for event_type in ("node_result", "artifact_registered", "node_result_recorded"):
                with self.subTest(event_type=event_type), self.assertRaises(StoreError):
                    transaction.commit_transition(event_type, failed)
                self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_generic_event_cannot_skip_running_claim_by_failing_a_ready_task(self):
        from scripts.workflow_engine.scheduler import NodeStatus
        from scripts.workflow_engine.store import StoreError
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            forged = replace(state, nodes={"directions": replace(state.nodes["directions"], status=NodeStatus.FAILED)})
            before = self.service.store.paths.events.read_bytes()
            with self.assertRaises(StoreError):
                transaction.commit_transition("fact_recorded", forged)
            self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_generic_event_cannot_invent_skipped_task(self):
        from scripts.workflow_engine.scheduler import NodeStatus
        from scripts.workflow_engine.store import StoreError
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            forged = replace(state, nodes={"directions": replace(state.nodes["directions"], status=NodeStatus.SKIPPED)})
            before = self.service.store.paths.events.read_bytes()
            for event_type in ("fact_recorded", "unknown_event"):
                with self.subTest(event_type=event_type), self.assertRaises(StoreError):
                    transaction.commit_transition(event_type, forged)
                self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_invocation_codec_rejects_changed_token_identity_or_unexpected_fields(self):
        from scripts.workflow_engine.receipts import validate_invocation
        invocation = self.service.claim("directions")
        self.assertEqual(validate_invocation(invocation), invocation)
        for update in ({"idempotency_token": "different"}, {"command": "execute"}, {"allowed_project_root": "relative"}):
            with self.subTest(update=update), self.assertRaises(ReceiptError):
                validate_invocation(dict(invocation, **update))
        changed = copy.deepcopy(invocation)
        changed["resolved_identity"]["command"] = "execute"
        with self.assertRaises(ReceiptError):
            validate_invocation(changed)

    def test_receipt_projection_missing_recovers_and_conflicting_file_blocks(self):
        invocation = self.service.claim("directions")
        receipt = self.service.submit_result(self.result(invocation))
        path = self.service.store.paths.receipts / invocation["run_id"] / "directions-attempt-1.json"
        path.unlink()
        with self.assertRaises(Exception) as caught:
            self.service.ready()
        self.assertEqual(caught.exception.code, "recovery.required")
        self.assertEqual(self.service.store.recover().status, "recovered")
        self.assertEqual(json.loads(path.read_bytes()), receipt)
        path.write_text("{}\n")
        before = self.service.store.paths.events.read_bytes()
        self.assertEqual(self.service.store.recover().status, "blocked")
        self.assertEqual(path.read_text(), "{}\n")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_completion_append_before_snapshot_is_recovered_with_same_receipt(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        original = self.service.store._atomic_json
        def fail_snapshot(path, value):
            if path == self.service.store.paths.state:
                raise OSError("injected snapshot failure")
            return original(path, value)
        with mock.patch.object(self.service.store, "_atomic_json", side_effect=fail_snapshot):
            with self.assertRaises(OSError):
                self.service.submit_result(result)
        recovered = self.service.store.recover()
        self.assertEqual(recovered.status, "recovered")
        self.assertEqual(recovered.state.nodes["directions"].status.value, "succeeded")
        receipt = self.service.submit_result(result)
        self.assertEqual(receipt["output_artifacts"][0]["sha256"], sha256_file(self.project / "idea.md"))

    def test_generic_events_cannot_edit_frozen_running_or_successful_evidence(self):
        from scripts.workflow_engine.store import StoreError
        invocation = self.service.claim("directions")
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            changed = replace(state, nodes={"directions": replace(state.nodes["directions"], selected_inputs={"invented": "other.md"})})
            before = self.service.store.paths.events.read_bytes()
            with self.assertRaises(StoreError):
                transaction.commit_transition("decision_recorded", changed)
            self.assertEqual(self.service.store.paths.events.read_bytes(), before)
        self.service.submit_result(self.result(invocation))
        (self.project / "other.md").write_text("different output")
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            artifact = state.artifacts["research_idea_brief"]
            changed = replace(state,
                nodes={"directions": replace(state.nodes["directions"], outputs={"research_idea_brief": "other.md"})},
                artifacts={"research_idea_brief": replace(artifact, path="other.md", sha256=sha256_file(self.project / "other.md"))})
            before = self.service.store.paths.events.read_bytes()
            with self.assertRaises(StoreError):
                transaction.commit_transition("artifact_registered", changed)
            self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_rehashed_forged_claim_completion_and_duplicate_fail_before_recovery_writes(self):
        from scripts.workflow_engine.store import WorkflowEvent, WorkflowStore
        invocation = self.service.claim("directions")
        self.service.submit_result(self.result(invocation))
        store = self.service.store
        original_log = store.paths.events.read_bytes()
        original_state = store.paths.state.read_bytes()
        events = [json.loads(line) for line in original_log.splitlines()]
        for boundary in ("full_chain", "suffix"):
            for forgery in ("claim_missing", "claim_mislabeled", "claim_identity", "claim_inputs", "completion_mislabeled", "receipt_token", "receipt_output", "receipt_identity", "claim_sequence", "duplicate_completion"):
                with self.subTest(boundary=boundary, forgery=forgery):
                    changed = copy.deepcopy(events)
                    if forgery == "claim_missing":
                        changed[1]["payload"].pop("claim_evidence")
                    elif forgery == "claim_mislabeled":
                        changed[1]["event_type"] = "fact_recorded"
                    elif forgery == "claim_identity":
                        changed[1]["payload"]["claim_evidence"]["resolved_identity"]["skill_sha256"] = "f" * 64
                    elif forgery == "claim_inputs":
                        changed[1]["payload"]["claim_evidence"]["input_artifacts"] = task_receipt()["input_artifacts"]
                    elif forgery == "completion_mislabeled":
                        changed[2]["event_type"] = "artifact_registered"
                    elif forgery == "receipt_token":
                        changed[2]["payload"]["receipt"]["claim_token_sha256"] = "f" * 64
                    elif forgery == "receipt_output":
                        changed[2]["payload"]["receipt"]["output_artifacts"][0]["sha256"] = "f" * 64
                    elif forgery == "receipt_identity":
                        changed[2]["payload"]["receipt"]["resolved_identity"]["skill_sha256"] = "f" * 64
                    elif forgery == "claim_sequence":
                        changed[2]["payload"]["claim_event_seq"] = 1
                    else:
                        changed.append(copy.deepcopy(changed[-1]))
                    previous = "0" * 64
                    for index, event in enumerate(changed, 1):
                        event["event_seq"] = index
                        event["previous_event_hash"] = previous
                        normalized = WorkflowEvent.create(**{key: value for key, value in event.items() if key != "event_hash"})
                        changed[index - 1] = normalized.to_payload()
                        previous = normalized.event_hash
                    store.paths.events.write_text("".join(json.dumps(event) + "\n" for event in changed))
                    snapshot = json.loads(original_state)
                    target = changed[-1] if boundary == "full_chain" else changed[0]
                    snapshot.update(state=target["payload"]["state"], last_applied_event_seq=target["event_seq"], last_applied_event_hash=target["event_hash"])
                    store.paths.state.write_text(json.dumps(snapshot))
                    if boundary == "suffix":
                        with store.paths.events.open("ab") as handle:
                            handle.write(b'{"partial":')
                    before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                    recovered = WorkflowStore(self.project).recover()
                    self.assertEqual(recovered.status, "blocked", recovered.code)
                    self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                    self.assertFalse(store.paths.recovery.exists())
                    store.paths.events.write_bytes(original_log)
                    store.paths.state.write_bytes(original_state)

    def test_failed_result_is_receipted_and_idempotent(self):
        invocation = self.service.claim("directions")
        result = dict(self.result(invocation), status="failed", outcome="", artifacts=[], error={"code": "task.failed", "message": "Task could not finish."})
        receipt = self.service.submit_result(result)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["output_artifacts"], [])
        self.assertEqual(self.service.submit_result(result), receipt)

    def test_result_cannot_echo_plaintext_claim_token_into_durable_report(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        result["summary"] = "Echoed token: " + invocation["idempotency_token"]
        before = self.service.store.paths.events.read_bytes()
        with self.assertRaises(Exception) as caught:
            self.service.submit_result(result)
        self.assertEqual(caught.exception.code, "receipt.token_disclosure")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_changed_skill_and_symlink_outputs_are_rejected_without_writes(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        before = self.service.store.paths.events.read_bytes()
        (self.project / "linked.md").symlink_to(self.project / "idea.md")
        with self.assertRaises(Exception):
            self.service.submit_result(dict(result, artifacts=[{"id": "research_idea_brief", "path": "linked.md"}]))
        skill = self.project / "skills/test-workflow-task/SKILL.md"
        skill.write_text(skill.read_text() + "Changed instruction.")
        with self.assertRaises(Exception) as caught:
            self.service.submit_result(result)
        self.assertEqual(caught.exception.code, "runtime.identity_changed")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_changed_output_requires_recovery_and_old_receipt_never_reactivates_it(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        self.service.submit_result(result)
        (self.project / "idea.md").write_text("Changed bytes.")
        with self.assertRaises(Exception) as caught:
            self.service.submit_result(result)
        self.assertEqual(caught.exception.code, "recovery.required")
        self.assertEqual(self.service.store.recover().code, "recovery.artifact_drift")
        with self.assertRaises(Exception) as caught:
            self.service.submit_result(result)
        self.assertEqual(caught.exception.code, "receipt.stale_attempt")

    def test_snapshot_without_receipt_projection_is_recovered(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        with mock.patch.object(self.service.store, "_write_receipts", side_effect=OSError("injected receipt failure")):
            with self.assertRaises(OSError):
                self.service.submit_result(result)
        self.assertEqual(self.service.store.recover().status, "recovered")
        self.assertEqual(self.service.submit_result(result)["status"], "succeeded")

    def test_unsafe_receipt_parent_is_rejected_before_completion_append(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        before = self.service.store.paths.events.read_bytes()
        self.service.store.paths.receipts.write_text("occupied by a file")
        with self.assertRaises(Exception):
            self.service.submit_result(result)
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_task_without_outputs_still_requires_and_records_summary_receipt(self):
        self.service.store.deactivate_custom()
        document = copy.deepcopy(self.document)
        document["nodes"][0]["outputs"] = []
        document["nodes"][0]["write_scopes"] = []
        validation = self.service.validate_document(document)
        self.service.activate(document, acknowledged_warning_codes=validation["required_warning_codes"])
        invocation = self.service.claim("directions")
        result = dict(self.result(invocation), artifacts=[])
        receipt = self.service.submit_result(result)
        self.assertEqual(receipt["output_artifacts"], [])
        self.assertEqual(receipt["summary"], "Created draft.")

    def test_running_resets_and_malformed_recovery_fail_commit_and_rehashed_replay(self):
        from scripts.workflow_engine import store as store_module
        from scripts.workflow_engine.scheduler import NodeStatus
        from scripts.workflow_engine.store import StoreError, WorkflowEvent, WorkflowStore
        self.service.claim("directions")
        store = self.service.store
        original_log, original_snapshot = store.paths.events.read_bytes(), store.paths.state.read_bytes()
        with store.locked_run() as transaction:
            _, state = transaction.load_active_run()
        for status, event_type, payload in (
            (NodeStatus.READY, "fact_recorded", {}),
            (NodeStatus.PENDING, "unknown_event", {}),
            (NodeStatus.BLOCKED, "recovery_running_blocked", {"node_ids": ["directions"]}),
            (NodeStatus.STALE, "artifacts_marked_stale", {"artifact_ids": [], "witness_producer_ids": []}),
        ):
            forged = replace(state, nodes={"directions": replace(state.nodes["directions"], status=status, claim_token_hash="")})
            with self.subTest(status=status, boundary="commit"), store.locked_run() as transaction:
                transaction.load_active_run()
                with self.assertRaises(StoreError):
                    transaction.commit_transition(event_type, forged, payload)
            self.assertEqual(store.paths.events.read_bytes(), original_log)
            for boundary in ("full_chain", "suffix"):
                with self.subTest(status=status, boundary=boundary):
                    previous = json.loads(original_log.splitlines()[-1])
                    event = WorkflowEvent.create(event_seq=previous["event_seq"] + 1,
                        run_id=state.run_id, semantic_sha256=state.semantic_sha256,
                        event_type=event_type, previous_event_hash=previous["event_hash"],
                        payload={**payload, "state": store_module._state_data(forged), "run_status": "active"})
                    store.paths.events.write_bytes(original_log + store_module._canonical_bytes(event.to_payload(), newline=True))
                    if boundary == "full_chain":
                        snapshot = json.loads(original_snapshot)
                        snapshot.update(state=store_module._state_data(forged), last_applied_event_seq=event.event_seq, last_applied_event_hash=event.event_hash)
                        store.paths.state.write_text(json.dumps(snapshot))
                    else:
                        with store.paths.events.open("ab") as handle:
                            handle.write(b'{"partial":')
                    before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                    recovered = WorkflowStore(self.project).recover()
                    self.assertEqual(recovered.status, "blocked", recovered.code)
                    self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                    self.assertFalse(store.paths.recovery.exists())
                    store.paths.events.write_bytes(original_log)
                    store.paths.state.write_bytes(original_snapshot)

    def test_current_receipt_drift_is_checked_after_legal_serial_same_id_replacement(self):
        from scripts.workflow_engine.store import WorkflowStore
        self.service.store.deactivate_custom()
        document = copy.deepcopy(self.document)
        other = dict(document["nodes"][0], id="other", entry=False)
        document["nodes"].append(other)
        document["ui"]["positions"]["other"] = {"x": 400, "y": 80}
        document["edges"] = [{"id": "serial", "source": "directions", "target": "other", "trigger": "succeeded", "output_map": {}}]
        validation = self.service.validate_document(document)
        self.assertEqual(validation["status"], "pass", validation)
        self.service.activate(document, acknowledged_warning_codes=validation["required_warning_codes"])
        first = self.service.claim("directions")
        original_result = self.result(first)
        original_receipt = self.service.submit_result(original_result)
        second = self.service.claim("other")
        (self.project / "other.md").write_text("new output")
        later_result = dict(task_result(), run_id=second["run_id"], node_id="other", attempt=second["attempt"],
            idempotency_token=second["idempotency_token"], artifacts=[{"id": "research_idea_brief", "path": "other.md"}])
        self.service.submit_result(later_result)
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            self.assertEqual(state.artifacts["research_idea_brief"].path, "other.md")
        before = self.service.store.paths.events.read_bytes()
        self.assertEqual(self.service.submit_result(original_result), original_receipt)
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)
        self.assertEqual(self.service.store.recover().status, "clean")
        receipt_path = self.service.store.paths.receipts / first["run_id"] / "directions-attempt-1.json"
        old_receipt_bytes = receipt_path.read_bytes()
        (self.project / "idea.md").write_text("actual old-output byte drift")
        with self.assertRaises(Exception) as caught:
            self.service.submit_result(original_result)
        self.assertEqual(caught.exception.code, "recovery.required")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)
        recovered = WorkflowStore(self.project).recover()
        self.assertEqual((recovered.status, recovered.code), ("recovered", "recovery.artifact_drift"))
        self.assertEqual(recovered.state.nodes["directions"].status.value, "stale")
        self.assertEqual(recovered.state.nodes["other"].status.value, "stale")
        self.assertEqual(receipt_path.read_bytes(), old_receipt_bytes)
        self.assertEqual(WorkflowStore(self.project).recover().status, "clean")

    def test_recovered_blocked_attempt_retains_its_claim_evidence(self):
        from scripts.workflow_engine.store import StoreError
        self.service.claim("directions")
        recovered = self.service.store.recover()
        self.assertEqual(recovered.state.nodes["directions"].status.value, "blocked")
        before = self.service.store.paths.events.read_bytes(), self.service.store.paths.state.read_bytes()
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            forged = replace(state, nodes={"directions": replace(state.nodes["directions"], claim_token_hash="")})
            with self.assertRaises(StoreError) as caught:
                transaction.commit_transition("fact_recorded", forged)
            self.assertEqual(caught.exception.code, "events.invalid_evidence")
        self.assertEqual((self.service.store.paths.events.read_bytes(), self.service.store.paths.state.read_bytes()), before)

    def test_stale_attempt_quarantines_live_claim_but_preserves_history(self):
        from scripts.workflow_engine.store import StoreError
        invocation = self.service.claim("directions")
        self.service.submit_result(self.result(invocation))
        (self.project / "idea.md").write_text("drift")
        recovered = self.service.store.recover()
        self.assertEqual(recovered.state.nodes["directions"].status.value, "stale")
        self.assertEqual(recovered.state.nodes["directions"].claim_token_hash, "")
        self.assertEqual(recovered.state.nodes["directions"].outcome, "")
        historical = self.service.store.read_run_events()
        claim = next(event for event in historical if event.event_type == "node_claimed")
        completion = next(event for event in historical if event.event_type == "node_result_recorded")
        self.assertEqual(claim.payload["claim_evidence"]["attempt"], 1)
        self.assertEqual(completion.payload["receipt"]["attempt"], 1)
        before = self.service.store.paths.events.read_bytes(), self.service.store.paths.state.read_bytes()
        with self.service.store.locked_run() as transaction:
            _, state = transaction.load_active_run()
            forged = replace(state, nodes={"directions": replace(state.nodes["directions"], claim_token_hash="f" * 64)})
            with self.assertRaises(StoreError) as caught:
                transaction.commit_transition("fact_recorded", forged)
            self.assertEqual(caught.exception.code, "events.invalid_evidence")
        self.assertEqual((self.service.store.paths.events.read_bytes(), self.service.store.paths.state.read_bytes()), before)

    def test_directory_and_oversized_receipt_projections_block_without_rewrites(self):
        invocation = self.service.claim("directions")
        self.service.submit_result(self.result(invocation))
        store = self.service.store
        path = store.paths.receipts / invocation["run_id"] / "directions-attempt-1.json"
        original = path.read_bytes()
        for kind in ("directory", "oversize"):
            path.unlink()
            if kind == "directory":
                path.mkdir()
            else:
                path.write_bytes(b"x" * (1024 * 1024 + 2))
            before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
            with self.subTest(kind=kind):
                result = store.recover()
                self.assertEqual(result.status, "blocked")
                self.assertIn(result.code, {"receipt.invalid_projection", "receipt.projection_too_large"})
                with self.assertRaises(Exception) as caught:
                    self.service.ready()
                self.assertEqual(caught.exception.code, result.code)
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
            if kind == "directory":
                path.rmdir()
            else:
                path.unlink()
            path.write_bytes(original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not supported on this platform")
    def test_fifo_receipt_projection_never_blocks_recovery_on_open(self):
        invocation = self.service.claim("directions")
        self.service.submit_result(self.result(invocation))
        store = self.service.store
        path = store.paths.receipts / invocation["run_id"] / "directions-attempt-1.json"
        path.unlink()
        os.mkfifo(path)
        before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
        probe = subprocess.run([sys.executable, "-B", "-c",
            "import json,sys; from scripts.workflow_engine.store import WorkflowStore; r=WorkflowStore(sys.argv[1]).recover(); print(json.dumps({'status':r.status,'code':r.code}))",
            str(self.project)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=2)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(json.loads(probe.stdout), {"status": "blocked", "code": "receipt.invalid_projection"})
        self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_duplicate_rechecks_receipt_after_other_locked_preflight_work(self):
        invocation = self.service.claim("directions")
        result = self.result(invocation)
        self.service.submit_result(result)
        before = self.service.store.paths.events.read_bytes()
        original_identity = self.service._identity
        def mutate_after_load(node):
            identity = original_identity(node)
            (self.project / "idea.md").write_text("changed after load but before duplicate response")
            return identity
        with mock.patch.object(self.service, "_identity", side_effect=mutate_after_load):
            with self.assertRaises(Exception) as caught:
                self.service.submit_result(result)
        self.assertEqual(caught.exception.code, "recovery.required")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_projection_read_and_listing_errors_are_structured_and_zero_write(self):
        invocation = self.service.claim("directions")
        self.service.submit_result(self.result(invocation))
        store = self.service.store
        directory = store.paths.receipts / invocation["run_id"]
        path = directory / "directions-attempt-1.json"
        before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
        original_open, original_iterdir = os.open, Path.iterdir
        def denied_read(target, *args, **kwargs):
            if Path(target) == path:
                raise PermissionError("injected receipt read failure")
            return original_open(target, *args, **kwargs)
        def denied_listing(target):
            if target == directory:
                raise PermissionError("injected receipt list failure")
            return original_iterdir(target)
        for target, replacement in (("os.open", denied_read), ("pathlib.Path.iterdir", denied_listing)):
            with self.subTest(target=target), mock.patch(target, side_effect=replacement, autospec=True):
                recovered = store.recover()
                self.assertEqual((recovered.status, recovered.code), ("blocked", "receipt.inspection_failed"))
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_rerun_stale_requeues_without_claim_and_invalid_target_is_zero_write(self):
        before = self.service.store.paths.events.read_bytes()
        with self.assertRaises(Exception) as invalid:
            self.service.rerun_stale("directions")
        self.assertEqual(invalid.exception.code, "runtime.invalid_stale_rerun")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

        invocation = self.service.claim("directions")
        self.service.submit_result(self.result(invocation))
        (self.project / "idea.md").write_text("changed", encoding="utf-8")
        self.service.store.recover()
        before_claims = sum(
            event.event_type == "node_claimed"
            for event in self.service.store.read_run_events()
        )
        response = self.service.rerun_stale("directions")
        self.assertEqual(response["affected_node_ids"], ["directions"])
        self.assertEqual(response["root_node_id"], "directions")
        self.assertEqual(
            sum(event.event_type == "node_claimed" for event in self.service.store.read_run_events()),
            before_claims,
        )
        with self.service.store.locked_run() as transaction:
            _plan, state = transaction.load_active_run()
        self.assertEqual(state.nodes["directions"].status.value, "ready")
        self.assertEqual(state.nodes["directions"].attempt, 1)
        self.assertEqual(state.artifacts["research_idea_brief"].state, "stale")

    def test_task_retry_summary_and_deactivate_lifecycle(self):
        invocation = self.service.claim("directions")
        failed = dict(
            task_result(), run_id=invocation["run_id"], node_id="directions",
            attempt=invocation["attempt"], idempotency_token=invocation["idempotency_token"],
            status="failed", outcome="", artifacts=[],
            error={"code": "test.failure", "message": "Fixture task failure."},
        )
        receipt = self.service.submit_result(failed)
        self.assertEqual(receipt["status"], "failed")
        retry = self.service.retry("directions")
        self.assertEqual(retry["attempt"], 1)
        self.assertEqual(retry["node_status"], "ready")
        self.assertEqual(self.service.store.read_run_events()[-1].event_type, "node_retried")

        before = self.service.store.paths.events.read_bytes(), self.service.store.paths.state.read_bytes()
        first, second = self.service.summary(), self.service.summary()
        self.assertEqual(first, second)
        self.assertEqual(first["run_status"], "active")
        self.assertEqual(first["nodes"][0]["node_id"], "directions")
        self.assertEqual((self.service.store.paths.events.read_bytes(), self.service.store.paths.state.read_bytes()), before)

        deactivated = self.service.deactivate()
        self.assertEqual(deactivated["selection"]["mode"], "official")
        snapshot = json.loads(self.service.store.paths.state.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["run_status"], "stopped")

    def test_summary_does_not_repair_a_lagging_selection_projection(self):
        from scripts.workflow_engine.store import Selection

        store = self.service.store
        store.paths.selection.write_text(
            json.dumps(Selection("official").to_payload(), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        before = (
            store.paths.selection.read_bytes(),
            store.paths.events.read_bytes(),
            store.paths.state.read_bytes(),
        )
        summary = self.service.summary()
        self.assertEqual(summary["run_status"], "active")
        self.assertEqual(
            (
                store.paths.selection.read_bytes(),
                store.paths.events.read_bytes(),
                store.paths.state.read_bytes(),
            ),
            before,
        )


class ValidatorManagerTests(unittest.TestCase):
    PAPER = (
        "# A paper\n## Introduction\nBackground.\n## Methods\nProcedure.\n"
        "## Results\nFindings.\n## Discussion\nThe mechanism explains scope and limitations.\n"
        "## Conclusion\nConclusion.\n"
    )
    PAPER_OPTIONS = {
        "phase": "body", "paper_type": "empirical", "language": "en",
        "method_profile": "method-first", "validity_status": "clear",
        "discussion_integrated": False,
    }

    def setUp(self):
        from scripts.workflow_manager import WorkflowService
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.project / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.skill_root = self.project / "skills"
        skill = self.skill_root / "test-workflow-task"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: test-workflow-task\ndescription: Test task\n---\nRun the test task.\n", encoding="utf-8")
        self.service = WorkflowService(self.project, skill_roots=(self.skill_root,))
        (self.project / "paper.md").write_text(self.PAPER, encoding="utf-8")

    def document(self, *, validity_status="clear", with_routes=False):
        value = json.loads((Path(__file__).parent / "fixtures/workflow_valid_linear.json").read_text())
        value.update(workflow_id="validator-manager-flow", external_inputs=["paper"])
        check = {
            "id": "check", "type": "validator", "display_name": "Check paper",
            "entry": True, "enabled": True, "skill_ref": None,
            "validator_ref": "paper-section",
            "validator_config": {"input_roles": {"file": "paper"}, "options": {
                **self.PAPER_OPTIONS, "validity_status": validity_status,
            }},
            "origin_projection_node_id": None, "inputs": ["paper"], "outputs": [],
            "outcomes": ["pass", "fail", "blocked"], "write_scopes": [],
            "failure_policy": "block", "condition_cases": [], "join_mode": "all_active",
        }
        nodes = [check]
        edges = []
        positions = {"check": {"x": 120, "y": 80}}
        if with_routes:
            for suffix, trigger in (("pass", "pass"), ("blocked", "blocked")):
                node_id = "on_" + suffix
                nodes.append({
                    "id": node_id, "type": "task", "display_name": "On " + suffix,
                    "entry": False, "enabled": True, "skill_ref": "test-workflow-task",
                    "validator_ref": None, "validator_config": None,
                    "origin_projection_node_id": None, "inputs": [], "outputs": [],
                    "outcomes": ["succeeded"], "write_scopes": [], "failure_policy": "block",
                    "condition_cases": [], "join_mode": "all_active",
                })
                positions[node_id] = {"x": 420, "y": 100 if suffix == "pass" else 260}
                edges.append({"id": "check_" + suffix, "source": "check", "target": node_id,
                              "trigger": trigger, "output_map": {}})
        value["nodes"] = nodes
        value["edges"] = edges
        value["ui"] = {"positions": positions}
        return value

    def activate(self, *, validity_status="clear", with_routes=False):
        document = self.document(validity_status=validity_status, with_routes=with_routes)
        validation = self.service.validate_document(document)
        self.assertEqual(validation["status"], "pass", validation["errors"])
        self.service.activate(document, acknowledged_warning_codes=validation["required_warning_codes"])
        self.register_paper()

    def register_paper(self):
        from scripts.workflow_engine.scheduler import ArtifactRuntime, refresh_ready
        with self.service.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
            data = (self.project / "paper.md").read_bytes()
            artifact = ArtifactRuntime("paper", "paper.md", hashlib.sha256(data).hexdigest(),
                                       "verified", "external", 0)
            artifacts = dict(state.artifacts)
            artifacts["paper"] = artifact
            updated = refresh_ready(plan, replace(state, artifacts=MappingProxyType(artifacts)))
            transaction.commit_transition("artifact_registered", updated, {
                "artifact_id": "paper", "path": "paper.md", "sha256": artifact.sha256,
            })

    def test_real_validator_pass_receipt_and_downstream_route(self):
        self.activate(with_routes=True)
        receipt = self.service.run_validator("check")
        self.assertEqual((receipt["status"], receipt["outcome"], receipt["node_type"]),
                         ("succeeded", "pass", "validator"))
        self.assertEqual(receipt["output_artifacts"], [])
        self.assertEqual(receipt["input_artifacts"][0]["path"], "paper.md")
        self.assertEqual(receipt["input_artifacts"][0]["sha256"], hashlib.sha256((self.project / "paper.md").read_bytes()).hexdigest())
        with self.service.store.locked_run() as transaction:
            _plan, state = transaction.load_active_run()
            self.assertEqual(state.nodes["on_pass"].status.value, "ready")
            self.assertEqual(state.nodes["on_blocked"].status.value, "skipped")
        events = self.service.store.read_run_events()
        self.assertEqual([event.event_type for event in events].count("validator_claimed"), 1)
        self.assertEqual([event.event_type for event in events].count("validator_result_recorded"), 1)
        claim = next(event.payload["claim_evidence"] for event in events if event.event_type == "validator_claimed")
        self.assertEqual(claim["validator_config"]["input_roles"], {"file": "paper"})
        self.assertEqual(claim["validator_config"]["options"]["language"], "en")
        self.assertEqual(claim["input_artifacts"][0]["sha256"], receipt["input_artifacts"][0]["sha256"])
        self.assertEqual(self.service.store.recover().status, "clean")

    def test_failed_validator_retry_requeues_same_attempt_without_running_it(self):
        from scripts.workflow_engine.validators import ValidatorError
        self.activate()
        with mock.patch("scripts.workflow_manager.run_registered_validator",
                        side_effect=ValidatorError("validator.fixture_failure", "fixture failure")):
            receipt = self.service.run_validator("check")
        self.assertEqual(receipt["status"], "failed")
        retry = self.service.retry("check")
        self.assertEqual((retry["node_status"], retry["attempt"]), ("ready", 1))
        self.assertEqual(self.service.store.read_run_events()[-1].event_type, "validator_retried")

    def test_domain_fail_and_blocked_are_successful_validator_completions(self):
        (self.project / "paper.md").write_text("# A paper\n", encoding="utf-8")
        self.activate()
        receipt = self.service.run_validator("check")
        self.assertEqual((receipt["status"], receipt["outcome"]), ("succeeded", "fail"))
        self.assertEqual(self.service.store.recover().status, "clean")

        with TemporaryDirectory() as second_temp:
            project = Path(second_temp)
            paper = project / "paper.md"
            paper.write_text(self.PAPER, encoding="utf-8")
            skill_root = project / "skills"
            skill = skill_root / "test-workflow-task"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: test-workflow-task\ndescription: Test task\n---\nRun the test task.\n", encoding="utf-8")
            from scripts.workflow_manager import WorkflowService
            service = WorkflowService(project, skill_roots=(skill_root,))
            document = self.document(validity_status="blocked")
            validation = service.validate_document(document)
            self.assertEqual(validation["status"], "pass", validation["errors"])
            service.activate(document, acknowledged_warning_codes=validation["required_warning_codes"])
            with service.store.locked_run() as transaction:
                from scripts.workflow_engine.scheduler import ArtifactRuntime, refresh_ready
                plan, state = transaction.load_active_run()
                digest = hashlib.sha256(paper.read_bytes()).hexdigest()
                artifacts = {"paper": ArtifactRuntime("paper", "paper.md", digest, "verified", "external", 0)}
                transaction.commit_transition("artifact_registered", refresh_ready(plan, replace(state, artifacts=MappingProxyType(artifacts))))
            blocked = service.run_validator("check")
            self.assertEqual((blocked["status"], blocked["outcome"]), ("succeeded", "blocked"))
            self.assertEqual(service.store.recover().status, "clean")

    def test_process_failure_has_empty_outcome_and_structured_receipt(self):
        from scripts.workflow_engine.validators import ValidatorResult
        self.activate()
        with mock.patch("scripts.workflow_manager.run_registered_validator",
                        return_value=ValidatorResult(None, "Validator execution failed.",
                                                     "validator.timeout", "Validator exceeded its time limit.")):
            receipt = self.service.run_validator("check")
        self.assertEqual((receipt["status"], receipt["outcome"]), ("failed", ""))
        self.assertEqual(receipt["error"], {"code": "validator.timeout", "message": "Validator exceeded its time limit."})
        self.assertEqual(receipt["output_artifacts"], [])
        self.assertEqual(self.service.store.recover().status, "clean")

    def test_store_lock_is_available_while_validator_runs(self):
        from scripts.workflow_engine.validators import run_validator as real_runner
        self.activate()
        observed = []

        def checking_runner(node, inputs, project_root, repository_root):
            acquired = threading.Event()

            def inspect_ready():
                self.service.ready()
                acquired.set()

            thread = threading.Thread(target=inspect_ready)
            thread.start()
            thread.join(timeout=1.0)
            observed.append(acquired.is_set())
            return real_runner(node, inputs, project_root, repository_root)

        with mock.patch("scripts.workflow_manager.run_registered_validator", side_effect=checking_runner):
            self.service.run_validator("check")
        self.assertEqual(observed, [True])

    def test_changed_input_is_recovered_without_validator_result(self):
        from scripts.workflow_engine.validators import ValidatorResult
        self.activate()

        def change_then_pass(node, inputs, project_root, repository_root):
            (self.project / "paper.md").write_text("changed while validator ran", encoding="utf-8")
            return ValidatorResult("pass", "Validator passed.")

        with mock.patch("scripts.workflow_manager.run_registered_validator", side_effect=change_then_pass):
            with self.assertRaises(Exception) as caught:
                self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "runtime.recovery_required")
        event_types = [event.event_type for event in self.service.store.read_run_events()]
        self.assertNotIn("validator_result_recorded", event_types)
        self.assertIn(self.service.store.recover().status, {"clean", "recovered"})

    def test_transient_observed_input_drift_recovers_even_after_bytes_return(self):
        from scripts.workflow_engine.validators import ValidatorError
        self.activate()
        original = (self.project / "paper.md").read_bytes()

        def transient_change(node, inputs, project_root, repository_root):
            (self.project / "paper.md").write_bytes(b"temporary replacement")
            (self.project / "paper.md").write_bytes(original)
            raise ValidatorError("validator.input_changed", "Validator input changed during prelaunch checks.")

        with mock.patch("scripts.workflow_manager.run_registered_validator", side_effect=transient_change):
            with self.assertRaises(Exception) as caught:
                self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "runtime.recovery_required")
        events = self.service.store.read_run_events()
        self.assertNotIn("validator_result_recorded", [event.event_type for event in events])
        with self.service.store.locked_run() as transaction:
            _plan, state = transaction.load_active_run()
            self.assertEqual(state.nodes["check"].status.value, "blocked")

    def test_drift_recovery_does_not_touch_a_run_deactivated_before_recovery(self):
        from scripts.workflow_engine.validators import ValidatorError
        self.activate()
        original_recover = self.service.store.recover
        events_after_deactivate = []

        def deactivate_before_recover(*, expected_claim=None):
            self.service.store.deactivate_custom()
            events_after_deactivate.append(self.service.store.paths.events.read_bytes())
            return original_recover(expected_claim=expected_claim)

        with mock.patch("scripts.workflow_manager.run_registered_validator",
                        side_effect=ValidatorError("validator.input_changed", "Validator input changed.")):
            with mock.patch.object(self.service.store, "recover", side_effect=deactivate_before_recover):
                with self.assertRaises(Exception) as caught:
                    self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "receipt.stale_attempt")
        self.assertEqual(self.service.store.paths.events.read_bytes(), events_after_deactivate[0])

    def test_superseded_attempt_result_is_rejected_without_another_write(self):
        from scripts.workflow_engine.scheduler import validator_retry_transition
        from scripts.workflow_engine.validators import ValidatorResult
        self.activate()
        event_bytes_after_retry = []

        def recover_retry_then_pass(node, inputs, project_root, repository_root):
            self.assertEqual(self.service.store.recover().status, "blocked")
            with self.service.store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                retried = validator_retry_transition(plan, state, "check")
                transaction.commit_transition("validator_retried", retried, {"node_id": "check"})
            event_bytes_after_retry.append(self.service.store.paths.events.read_bytes())
            return ValidatorResult("pass", "Validator passed.")

        with mock.patch("scripts.workflow_manager.run_registered_validator", side_effect=recover_retry_then_pass):
            with self.assertRaises(Exception) as caught:
                self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "receipt.stale_attempt")
        self.assertEqual(self.service.store.paths.events.read_bytes(), event_bytes_after_retry[0])
        with self.service.store.locked_run() as transaction:
            _plan, state = transaction.load_active_run()
            self.assertEqual((state.nodes["check"].attempt, state.nodes["check"].status.value), (1, "ready"))

    def test_humanizer_legacy_call_has_no_process_or_event_write(self):
        from scripts.workflow_engine.catalog import load_validator_registry
        self.activate()
        with self.service.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
        root = Path(__file__).resolve().parents[1]
        registry = load_validator_registry(root / "references/workflows/validator-registry.v1.json", root)
        nodes = dict(plan.nodes)
        nodes["check"] = replace(nodes["check"], validator=registry["humanizer-preflight"])
        legacy_plan = replace(plan, nodes=MappingProxyType(nodes))
        before = self.service.store.paths.events.read_bytes()
        with mock.patch.object(self.service, "_load", return_value=(legacy_plan, state)):
            with mock.patch("scripts.workflow_manager.run_registered_validator") as runner:
                with self.assertRaises(Exception) as caught:
                    self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "validator.unavailable")
        runner.assert_not_called()
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_invalid_compiled_form_is_rejected_before_claim_event(self):
        self.activate()
        with self.service.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
        nodes = dict(plan.nodes)
        node = nodes["check"]
        invalid_config = {"input_roles": {"file": "paper"}, "options": {"unapproved": True}}
        nodes["check"] = replace(node, validator_config=invalid_config)
        forged_plan = replace(plan, nodes=MappingProxyType(nodes))
        before = self.service.store.paths.events.read_bytes()
        def load_then_forge(transaction):
            transaction.load_active_run()
            return forged_plan, state

        with mock.patch.object(self.service, "_load", side_effect=load_then_forge):
            with self.assertRaises(Exception) as caught:
                self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "validator.invalid_form")
        self.assertEqual(self.service.store.paths.events.read_bytes(), before)

    def test_identity_drift_is_recovered_without_domain_result(self):
        from scripts.workflow_engine.validators import ValidatorError, ValidatorResult, validate_validator_identity
        self.activate()
        calls = []

        def identity_once_then_drift(node, repository_root):
            calls.append(1)
            if len(calls) == 1:
                return validate_validator_identity(node, repository_root)
            raise ValidatorError("validator.identity_changed", "Validator identity changed.")

        with mock.patch("scripts.workflow_manager.validate_validator_identity", side_effect=identity_once_then_drift):
            with mock.patch("scripts.workflow_manager.run_registered_validator",
                            return_value=ValidatorResult("pass", "Validator passed.")):
                with self.assertRaises(Exception) as caught:
                    self.service.run_validator("check")
        self.assertEqual(caught.exception.code, "runtime.recovery_required")
        self.assertNotIn("validator_result_recorded", [event.event_type for event in self.service.store.read_run_events()])
        self.assertIn(self.service.store.recover().status, {"clean", "recovered"})


class HistoricalInputEvidenceTests(unittest.TestCase):
    def test_aliased_input_freezes_historical_winner_not_overwritten_registry(self):
        from scripts.workflow_engine.receipts import build_claim_evidence
        from scripts.workflow_engine.scheduler import claim_transition
        from scripts.workflow_engine.store import WorkflowStore
        from tests.test_workflow_store import compiled_winner_plan, complete_compiled_winner, commit_claim
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            store = WorkflowStore(project)
            plan = compiled_winner_plan()
            store.start_run(plan, "run-historical-claim")
            complete_compiled_winner(store, plan, project, downstream=False)
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                self.assertEqual(state.artifacts["draft"].path, "a.txt")
                running = claim_transition(plan, state, "consumer", "consumer-token")
                claim = build_claim_evidence(plan, running, "consumer", transaction.input_witnesses(), "2026-09-23T00:00:00Z")
                self.assertEqual(claim["input_artifacts"], [{"id": "joined", "source_id": "draft", "path": "b.txt", "sha256": hashlib.sha256(b"winner").hexdigest()}])
                commit_claim(transaction, running)
