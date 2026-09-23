import copy
import hashlib
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
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
