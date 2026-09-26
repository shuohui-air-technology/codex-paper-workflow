"""Manager-owned invocation and completion protocol for explicit custom runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.workflow_engine.catalog import discover_skills, load_validator_registry, resolve_skill_roots
from scripts.workflow_engine.compiler import compile_workflow
from scripts.workflow_engine.fs import PathSafetyError, resolve_project_path
from scripts.workflow_engine.receipts import (
    build_claim_evidence, build_invocation, build_stage_receipt,
    canonical_bytes, canonical_result_sha256, parse_result, resolved_identity, sha256_file,
)
from scripts.workflow_engine.scheduler import (
    ArtifactRuntime, NodeStatus, claim_transition, ready_node_ids, refresh_ready,
    result_transition, stabilize_control_nodes, validator_claim_transition,
    validator_result_transition,
)
from scripts.workflow_engine.schema import (
    WorkflowDocument, WorkflowError, document_sha256, normalize_workflow_document, parse_workflow,
)
from scripts.workflow_engine.store import StoreError, WorkflowStore, _document_data
from scripts.workflow_engine.validators import (
    ValidatorError, ValidatorResult, build_validator_argv,
    run_validator as run_registered_validator, validate_validator_identity,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WorkflowManagerError(WorkflowError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkflowService:
    def __init__(self, project_root: Path, *, skill_roots: tuple[Path, ...] = ()):
        self.store = WorkflowStore(project_root)
        self.project_root = self.store.project_root
        self.skill_roots = resolve_skill_roots(skill_roots, None, dict(os.environ))

    def _compile(self, document):
        parsed = (
            normalize_workflow_document(document)
            if isinstance(document, WorkflowDocument)
            else parse_workflow(document)
        )
        catalog = discover_skills(self.skill_roots, {})
        validators = load_validator_registry(REPOSITORY_ROOT / "references/workflows/validator-registry.v1.json", REPOSITORY_ROOT)
        projection = json.loads((REPOSITORY_ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text())
        return parsed, compile_workflow(parsed, catalog, validators, projection)

    def validate_document(self, document):
        parsed, compiled = self._compile(document)
        return {"status": "blocked" if compiled.errors else "pass",
                "errors": [asdict(item) for item in compiled.errors],
                "warnings": [asdict(item) for item in compiled.warnings],
                "document_sha256": document_sha256(parsed),
                "semantic_sha256": None if compiled.plan is None else compiled.plan.semantic_sha256,
                "required_warning_codes": sorted({item.code for item in compiled.warnings})}

    def load_draft(self):
        return _document_data(self.store.load_draft())

    def save_draft(self, document, *, expected_document_revision):
        parsed = (
            normalize_workflow_document(document)
            if isinstance(document, WorkflowDocument)
            else parse_workflow(document)
        )
        return _document_data(self.store.save_draft(parsed, expected_document_revision=expected_document_revision))

    def activate(self, document, *, acknowledged_warning_codes=()):
        parsed, compiled = self._compile(document)
        if compiled.errors:
            raise WorkflowManagerError("activation.validation_blocked", "workflow has blocking validation errors")
        required = sorted({item.code for item in compiled.warnings})
        if sorted(acknowledged_warning_codes) != required:
            raise WorkflowManagerError("activation.acknowledgement_mismatch", "acknowledge the exact current warning-code set")
        try:
            previous = self.store.load_draft()
        except Exception as exc:
            if getattr(exc, "code", "") != "store.draft_missing":
                raise
            previous = None
        saved = self.store.save_draft(parsed, expected_document_revision=0 if previous is None else previous.document_revision)
        _, compiled = self._compile(saved)
        selection = self.store.activate_custom(compiled.plan, high_risk_warning_codes=required,
                                               acknowledged_warning_codes=acknowledged_warning_codes)
        state = self.store.start_run(compiled.plan, "run-" + secrets.token_hex(16))
        return {"status": "pass", "selection": selection.to_payload(), "run_id": state.run_id}

    def _load(self, transaction):
        plan, state = transaction.load_active_run()
        selection = self.store._selection_unlocked()
        if selection.mode != "custom" or selection.semantic_sha256 != plan.semantic_sha256:
            raise WorkflowManagerError("runtime.selection_mismatch", "run is not the selected custom workflow")
        return plan, state

    def _identity(self, node):
        if node.type == "task":
            catalog = discover_skills(self.skill_roots, {})
            current = catalog.skills.get(node.skill.catalog_id)
            if current is None or current != node.skill:
                raise WorkflowManagerError("runtime.identity_changed", "installed Skill identity differs from activated plan")
        elif node.type == "validator":
            try:
                validate_validator_identity(node, REPOSITORY_ROOT)
            except ValidatorError as exc:
                raise WorkflowManagerError(exc.code, str(exc)) from exc
        return resolved_identity(node)

    def _hash_inputs(self, evidence):
        for artifact in evidence:
            try:
                path = resolve_project_path(self.project_root, artifact["path"])
                current = sha256_file(path)
            except (PathSafetyError, OSError, ValueError) as exc:
                raise WorkflowManagerError("receipt.input_stale", "claimed input is no longer safely readable") from exc
            if current != artifact["sha256"]:
                raise WorkflowManagerError("receipt.input_stale", "claimed input bytes changed")

    @staticmethod
    def _validator_terminal(result):
        """Return manager-owned scheduler fields for one normalized adapter result."""
        if (isinstance(result, ValidatorResult)
                and type(result.outcome) is str and result.outcome in {"pass", "fail", "blocked"}
                and result.error_code is None and result.error_message is None
                and isinstance(result.summary, str) and result.summary.strip()
                and len(result.summary) <= 4000 and "\x00" not in result.summary):
            return "succeeded", result.outcome, result.summary[:4000], None
        if (isinstance(result, ValidatorResult) and result.outcome is None
                and isinstance(result.error_code, str) and result.error_code
                and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", result.error_code)
                and isinstance(result.error_message, str) and result.error_message.strip()
                and "\x00" not in result.error_message):
            return "failed", "", "Validator execution failed.", {
                "code": result.error_code[:128], "message": result.error_message[:4000],
            }
        return "failed", "", "Validator execution failed.", {
            "code": "validator.invalid_result", "message": "Validator returned an invalid internal result.",
        }

    def _recover_validator_drift(self, run_id, semantic_sha256, node_id, attempt, token_hash):
        """Let store recovery classify an in-flight attempt after evidence drift."""
        recovery = self.store.recover(expected_claim=(run_id, semantic_sha256, node_id, attempt, token_hash))
        if recovery.code == "recovery.target_superseded":
            raise WorkflowManagerError("receipt.stale_attempt", "validator result belongs to a superseded attempt")
        if recovery.status == "blocked":
            message = "Validator evidence changed while it was running; recovery blocked the run."
        else:
            message = "Validator evidence changed while it was running; recover the workflow before continuing."
        raise WorkflowManagerError("runtime.recovery_required", message) from None

    def run_validator(self, node_id):
        """Run one ready registered validator while keeping the store lock free for its subprocess."""
        token = secrets.token_urlsafe(32)
        frozen_claim = None
        frozen_run_id = None
        frozen_semantic = None
        frozen_attempt = None

        # Validate the complete adapter contract before making the claim durable.
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction)
            if not isinstance(node_id, str) or node_id not in plan.nodes:
                raise WorkflowManagerError("runtime.unknown_node", "unknown validator node")
            node = plan.nodes[node_id]
            if node.type != "validator":
                raise WorkflowManagerError("runtime.node_type", "only validator nodes can be run by this operation")
            if node.validator is None:
                raise WorkflowManagerError("runtime.identity_changed", "validator identity is missing")
            if node.validator.validator_id == "humanizer-preflight":
                raise WorkflowManagerError("validator.unavailable", "Humanizer preflight is disabled for custom workflows")
            self._identity(node)
            updated = validator_claim_transition(plan, state, node_id, token)
            claim = build_claim_evidence(plan, updated, node_id, transaction.input_witnesses(), _now())
            # This builds only trusted argv and validates role/form/path/hash bindings;
            # the runner repeats these checks immediately before launching.
            try:
                build_validator_argv(node, claim["input_artifacts"], self.project_root, REPOSITORY_ROOT)
            except ValidatorError as exc:
                raise WorkflowManagerError(exc.code, str(exc)) from exc
            self._hash_inputs(claim["input_artifacts"])
            transaction.commit_transition("validator_claimed", updated, {"claim_evidence": claim})
            frozen_claim = json.loads(canonical_bytes(claim))
            frozen_run_id = state.run_id
            frozen_semantic = plan.semantic_sha256
            frozen_attempt = updated.nodes[node_id].attempt

        adapter_result = None
        adapter_error = None
        try:
            adapter_result = run_registered_validator(
                node, frozen_claim["input_artifacts"], self.project_root, REPOSITORY_ROOT,
            )
        except ValidatorError as exc:
            # Pre-launch input/identity drift is resolved after reacquiring the lock.
            adapter_error = exc
        except Exception:
            adapter_error = ValidatorError("validator.internal_error", "Validator could not complete safely.")

        try:
            with self.store.locked_run() as transaction:
                plan, state = self._load(transaction)
                runtime = state.nodes.get(node_id)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if (state.run_id != frozen_run_id or plan.semantic_sha256 != frozen_semantic
                        or runtime is None or runtime.status is not NodeStatus.RUNNING
                        or runtime.attempt != frozen_attempt or runtime.claim_token_hash != token_hash):
                    raise WorkflowManagerError("receipt.stale_attempt", "validator result belongs to a superseded attempt")
                claims = [event for event in transaction.events("validator_claimed")
                          if event.payload.get("claim_evidence", {}).get("node_id") == node_id
                          and event.payload.get("claim_evidence", {}).get("attempt") == frozen_attempt]
                if len(claims) != 1:
                    raise WorkflowManagerError("receipt.claim_missing", "validator attempt lacks one frozen claim")
                claim_event = claims[0]
                claim = claim_event.payload["claim_evidence"]
                if (canonical_bytes(claim) != canonical_bytes(frozen_claim)
                        or claim["claim_token_sha256"] != token_hash):
                    raise WorkflowManagerError("receipt.stale_attempt", "frozen validator claim changed")
                self._identity(plan.nodes[node_id])
                self._hash_inputs(claim["input_artifacts"])
                if adapter_error is not None and adapter_error.code in {
                    "validator.input_changed", "validator.identity_changed", "validator.unsafe_path",
                }:
                    # Once the adapter observed evidence drift, a later
                    # recheck cannot prove what the process saw in between.
                    raise WorkflowManagerError(adapter_error.code, str(adapter_error))
                if adapter_error is not None:
                    # Identity and input checks above distinguish evidence drift
                    # from a bounded adapter/launch failure.
                    adapter_result = ValidatorResult(
                        None, "Validator execution failed.", adapter_error.code, str(adapter_error),
                    )
                status, outcome, summary, error = self._validator_terminal(adapter_result)
                updated = validator_result_transition(plan, state, {
                    "node_id": node_id, "attempt": frozen_attempt, "status": status,
                    "outcome": outcome, "outputs": {}, "artifacts": (),
                })
                receipt = build_stage_receipt(
                    plan, updated, node_id, claim, summary=summary, uncertainties=[],
                    completed_at=_now(), error=error,
                )
                digest_payload = {
                    "node_id": node_id, "attempt": frozen_attempt, "status": status,
                    "outcome": outcome, "summary": summary, "error": error,
                }
                result_digest = hashlib.sha256(canonical_bytes(digest_payload)).hexdigest()
                transaction.commit_receipted_transition(
                    "validator_result_recorded", updated, receipt,
                    result_sha256=result_digest, claim_event_seq=claim_event.event_seq,
                )
                self._stabilize(transaction, plan, updated)
                return receipt
        except StoreError as exc:
            if exc.code in {"recovery.required", "artifact.verification_failed", "path.unsafe"}:
                self._recover_validator_drift(frozen_run_id, frozen_semantic, node_id,
                                              frozen_attempt, hashlib.sha256(token.encode("utf-8")).hexdigest())
            raise
        except WorkflowManagerError as exc:
            if exc.code in {"receipt.input_stale", "validator.input_changed", "validator.unsafe_path", "validator.identity_changed"}:
                self._recover_validator_drift(frozen_run_id, frozen_semantic, node_id,
                                              frozen_attempt, hashlib.sha256(token.encode("utf-8")).hexdigest())
            raise

    def _stabilize(self, transaction, plan, state):
        refreshed = refresh_ready(plan, state)
        if refreshed != state:
            transaction.commit_transition("readiness_refreshed", refreshed)
        stabilized, controls = stabilize_control_nodes(plan, refreshed)
        if controls:
            transaction.commit_control_transitions(controls, stabilized)
        return stabilized

    def ready(self):
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction)
            state = self._stabilize(transaction, plan, state)
            return {"status": "pass", "ready": [{"node_id": node_id, "node_type": plan.nodes[node_id].type}
                                                   for node_id in ready_node_ids(plan, state)]}

    def claim(self, node_id):
        token = secrets.token_urlsafe(32)
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction)
            if node_id not in plan.nodes:
                raise WorkflowManagerError("runtime.unknown_node", "unknown node")
            self._identity(plan.nodes[node_id])
            updated = claim_transition(plan, state, node_id, token)
            claim = build_claim_evidence(plan, updated, node_id, transaction.input_witnesses(), _now())
            self._hash_inputs(claim["input_artifacts"])
            invocation = build_invocation(claim, token, self.project_root)
            transaction.commit_transition("node_claimed", updated, {"claim_evidence": claim})
        return invocation

    def submit_result(self, value):
        result = parse_result(value)
        durable_report = {key: item for key, item in result.items() if key != "idempotency_token"}
        if result["idempotency_token"] in canonical_bytes(durable_report).decode("utf-8"):
            raise WorkflowManagerError("receipt.token_disclosure", "result report must not echo its claim token")
        digest = canonical_result_sha256(result)
        token_hash = hashlib.sha256(result["idempotency_token"].encode()).hexdigest()
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction)
            node_id = result["node_id"]
            runtime = state.nodes.get(node_id)
            if runtime is None or result["run_id"] != state.run_id or runtime.attempt != result["attempt"] or runtime.claim_token_hash != token_hash or runtime.status.value == "stale":
                raise WorkflowManagerError("receipt.stale_attempt", "result does not match the current attempt")
            self._identity(plan.nodes[node_id])
            for event in transaction.events("node_result_recorded"):
                receipt = event.payload["receipt"]
                if receipt["node_id"] == node_id and receipt["attempt"] == result["attempt"]:
                    if event.payload["result_sha256"] != digest:
                        raise WorkflowManagerError("receipt.idempotency_conflict", "same attempt supplied a different result")
                    if self.store._current_receipt_drift_nodes(state, (event,)):
                        raise WorkflowManagerError("recovery.required", "current attempt receipt bytes changed")
                    return json.loads(canonical_bytes(receipt))
            claims = [event for event in transaction.events("node_claimed")
                      if event.payload["claim_evidence"]["node_id"] == node_id and event.payload["claim_evidence"]["attempt"] == runtime.attempt]
            if len(claims) != 1:
                raise WorkflowManagerError("receipt.claim_missing", "result lacks one frozen claim")
            claim_event = claims[0]
            claim = claim_event.payload["claim_evidence"]
            self._hash_inputs(claim["input_artifacts"])
            artifacts = []
            for artifact in result["artifacts"]:
                path = resolve_project_path(self.project_root, artifact["path"])
                artifacts.append(ArtifactRuntime(artifact["id"], artifact["path"], sha256_file(path), "verified", node_id, runtime.attempt))
            updated = result_transition(plan, state, {"node_id": node_id, "attempt": runtime.attempt,
                "status": result["status"], "outcome": result["outcome"],
                "outputs": {item["id"]: item["path"] for item in result["artifacts"]}, "artifacts": artifacts})
            receipt = build_stage_receipt(plan, updated, node_id, claim, summary=result["summary"],
                uncertainties=result["uncertainties"], completed_at=_now(), error=result.get("error"))
            transaction.commit_receipted_transition("node_result_recorded", updated, receipt,
                result_sha256=digest, claim_event_seq=claim_event.event_seq)
            self._stabilize(transaction, plan, updated)
            return receipt
