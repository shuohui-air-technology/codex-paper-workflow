"""Manager-owned invocation and completion protocol for explicit custom runs."""

from __future__ import annotations

import argparse
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

from scripts.workflow_engine.catalog import ROOT_CATALOG_FAILURE_CODES, discover_skills, load_install_receipts, load_validator_registry, resolve_skill_roots
from scripts.workflow_engine.compiler import compile_workflow
from scripts.workflow_engine.fs import (
    PathSafetyError,
    hash_project_file,
    read_project_json_object,
    resolve_project_path,
)
from scripts.workflow_engine.receipts import (
    ReceiptError,
    build_claim_evidence, build_invocation, build_stage_receipt,
    canonical_bytes, canonical_result_sha256, parse_result, resolved_identity,
)
from scripts.workflow_engine.reporting import summarize_run_progress
from scripts.workflow_engine.scheduler import (
    ArtifactRuntime, NodeStatus, approval_decision_name, approval_fingerprint, approval_state,
    claim_transition, ready_node_ids, refresh_ready,
    result_transition, retry_transition, stabilize_control_nodes, validator_claim_transition,
    validator_result_transition, record_condition_fact_transition,
    validator_retry_transition,
)
from scripts.workflow_engine.schema import (
    WorkflowDocument, WorkflowError, behavior_payload, document_data, document_sha256, normalize_workflow_document, parse_workflow,
)
from scripts.workflow_engine.store import StoreError, WorkflowStore
from scripts.workflow_engine.validators import (
    ValidatorError, ValidatorResult, build_validator_argv,
    run_validator as run_registered_validator, validate_validator_identity,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WorkflowManagerError(WorkflowError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _CLIError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise _CLIError("cli.invalid_arguments", message)


def _read_project_json(project_root, relative_path):
    """Read one bounded, plain project file containing a strict JSON object."""
    try:
        return read_project_json_object(project_root, relative_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, PathSafetyError) as exc:
        code = getattr(exc, "code", "cli.invalid_json")
        raise _CLIError(code, str(exc)) from exc


def _add_cli_common(parser, *, skills=False):
    parser.add_argument("--project", required=True, help="project directory")
    if skills:
        parser.add_argument("--skills-root", action="append", default=[], help="additional Skill catalog root")
    parser.add_argument("--json", action="store_true", help="emit the JSON protocol response")


def _build_cli_parser():
    parser = _JSONArgumentParser(prog="workflow_manager.py")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_JSONArgumentParser)

    validate = commands.add_parser("validate")
    _add_cli_common(validate, skills=True)
    validate.add_argument("--workflow", required=True)

    activate = commands.add_parser("activate")
    _add_cli_common(activate, skills=True)
    activate.add_argument("--workflow", required=True)
    activate.add_argument("--ack-warning-code", action="append", default=[])

    for command in ("ready", "summary", "deactivate"):
        item = commands.add_parser(command)
        _add_cli_common(item, skills=(command != "deactivate"))

    recover = commands.add_parser("recover")
    _add_cli_common(recover)
    recover.add_argument(
        "--confirm-interrupted", action="store_true", required=True,
        help="confirm that no previous task or validator process from this run is still executing",
    )

    for command in ("claim", "run-validator", "retry", "rerun-stale"):
        item = commands.add_parser(command)
        _add_cli_common(item, skills=True)
        item.add_argument("--node", required=True)

    submit = commands.add_parser("submit-result")
    _add_cli_common(submit, skills=True)
    submit.add_argument("--result", required=True)

    for command, argument in (
        ("register-artifact", "--artifact"),
        ("record-decision", "--decision"),
        ("record-fact", "--fact"),
        ("record-approval", "--approval"),
    ):
        item = commands.add_parser(command)
        _add_cli_common(item, skills=True)
        item.add_argument(argument, required=True)
    return parser


def _cli_error_exit_code(code):
    blocked_runtime_codes = {
        "activation.validation_blocked",
        "artifact.verification_failed",
        "events.invalid_json",
        "receipt.claim_missing",
        "receipt.conflict",
        "receipt.inspection_failed",
        "receipt.input_stale",
        "receipt.input_unverified",
        "receipt.invalid_projection",
        "receipt.orphan",
        "receipt.projection_too_large",
        "receipt.stale_attempt",
        "recovery.artifact_drift",
        "recovery.incomplete_run",
        "recovery.no_run",
        "recovery.required",
        "recovery.running_work_uncertain",
        "recovery.target_superseded",
        "run.already_active",
        "run.not_active",
        "run.not_found",
        "run.recovery_required",
        "runtime.artifact_registration_running",
        "runtime.decision_update_running",
        "runtime.fact_update_running",
        "runtime.identity_changed",
        "runtime.node_not_ready",
        "runtime.parallelism_exceeded",
        "runtime.recovery_required",
        "runtime.selection_mismatch",
        "runtime.stale_join_requires_new_run",
        "runtime.stale_rerun_requires_recovery",
        "runtime.stale_rerun_running",
        "selection.invalid",
        "selection.journal_conflict",
        "selection.journal_invalid",
        "validator.identity_changed",
        "validator.input_changed",
        "validator.unavailable",
    }
    if code in blocked_runtime_codes:
        return 2
    return 1


def _run_cli(args):
    project = Path(args.project)
    skills = tuple(Path(item) for item in getattr(args, "skills_root", ()))
    service = WorkflowService(project, skill_roots=skills)
    if args.command == "validate":
        return service.validate_document(_read_project_json(project, args.workflow))
    if args.command == "activate":
        document = _read_project_json(project, args.workflow)
        validation = service.validate_document(document)
        if validation["status"] == "blocked":
            return validation
        return service.activate(document, acknowledged_warning_codes=args.ack_warning_code)
    if args.command == "ready":
        return service.ready()
    if args.command == "summary":
        return service.summary()
    if args.command == "deactivate":
        return service.deactivate()
    if args.command == "recover":
        return service.recover(confirmed_interrupted=args.confirm_interrupted)
    if args.command == "claim":
        return service.claim(args.node)
    if args.command == "run-validator":
        return service.run_validator(args.node)
    if args.command == "retry":
        return service.retry(args.node)
    if args.command == "rerun-stale":
        return service.rerun_stale(args.node)
    if args.command == "submit-result":
        return service.submit_result(_read_project_json(project, args.result))
    if args.command == "register-artifact":
        payload = _read_project_json(project, args.artifact)
        if set(payload) != {"artifact_id", "path", "provenance_summary"}:
            raise _CLIError("cli.invalid_json", "artifact input must contain exactly artifact_id, path, and provenance_summary")
        return service.register_artifact(
            payload["artifact_id"], payload["path"], payload["provenance_summary"]
        )
    if args.command == "record-decision":
        payload = _read_project_json(project, args.decision)
        if set(payload) != {"name", "value", "provenance_summary"}:
            raise _CLIError("cli.invalid_json", "decision input must contain exactly name, value, and provenance_summary")
        return service.record_decision(payload["name"], payload["value"], payload["provenance_summary"])
    if args.command == "record-approval":
        payload = _read_project_json(project, args.approval)
        if set(payload) != {"node_id", "action", "provenance_summary"}:
            raise _CLIError("cli.invalid_json", "approval input must contain node_id, action, and provenance_summary")
        return service.record_approval(payload["node_id"], payload["action"], payload["provenance_summary"])
    if args.command == "record-fact":
        payload = _read_project_json(project, args.fact)
        if set(payload) not in ({"name", "value"}, {"name", "value", "provenance_summary"}):
            raise _CLIError("cli.invalid_json", "fact input must contain name and value, with optional provenance_summary")
        return service.record_fact(
            payload["name"], payload["value"],
            payload.get("provenance_summary", "User-recorded project fact."),
        )
    raise _CLIError("cli.unknown_command", "unknown workflow-manager command")


def main(argv=None):
    """Run the strict JSON command-line protocol."""
    try:
        args = _build_cli_parser().parse_args(argv)
        result = _run_cli(args)
        status = result.get("status") if isinstance(result, dict) else None
        output = result if isinstance(result, dict) else {"status": "pass", "result": result}
        code = 2 if status == "blocked" else 0
    except _CLIError as exc:
        output = {"status": "error", "error": {"code": exc.code, "message": str(exc)[:4000]}}
        code = _cli_error_exit_code(exc.code)
    except (WorkflowError, StoreError, ReceiptError, ValidatorError, PathSafetyError) as exc:
        error_code = getattr(exc, "code", "cli.runtime_error")
        output = {"status": "error", "error": {"code": error_code, "message": str(exc)[:4000]}}
        code = _cli_error_exit_code(error_code)
    except Exception:
        output = {"status": "error", "error": {
            "code": "cli.internal_error", "message": "Unexpected workflow-manager failure.",
        }}
        code = 1
    sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return code


class WorkflowService:
    def __init__(self, project_root: Path, *, skill_roots: tuple[Path, ...] = ()):
        self.store = WorkflowStore(project_root)
        self.project_root = self.store.project_root
        local_skills = self.project_root / ".agents" / "skills"
        roots = (local_skills,) + skill_roots if local_skills.is_dir() and not local_skills.is_symlink() else skill_roots
        self.skill_roots = resolve_skill_roots(roots, None, dict(os.environ))

    def _compile(self, document):
        parsed = (
            normalize_workflow_document(document)
            if isinstance(document, WorkflowDocument)
            else parse_workflow(document)
        )
        catalog = discover_skills(self.skill_roots, load_install_receipts(self.skill_roots))
        validators = load_validator_registry(REPOSITORY_ROOT / "references/workflows/validator-registry.v1.json", REPOSITORY_ROOT)
        projection = json.loads((REPOSITORY_ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text(encoding="utf-8"))
        return parsed, compile_workflow(parsed, catalog, validators, projection)

    def validate_document(self, document):
        parsed, compiled = self._compile(document)
        return {"status": "blocked" if compiled.errors else "pass",
                "errors": [asdict(item) for item in compiled.errors],
                "warnings": [asdict(item) for item in compiled.warnings],
                "document_sha256": document_sha256(parsed),
                "semantic_sha256": None if compiled.plan is None else compiled.plan.semantic_sha256,
                "required_warning_codes": sorted({item.code for item in compiled.warnings
                                                  if item.severity == "warning"})}

    def load_draft(self):
        return document_data(self.store.load_draft())

    def save_draft(self, document, *, expected_document_revision):
        parsed = (
            normalize_workflow_document(document)
            if isinstance(document, WorkflowDocument)
            else parse_workflow(document)
        )
        return document_data(self.store.save_draft(parsed, expected_document_revision=expected_document_revision))

    def activate(
        self,
        document,
        *,
        acknowledged_warning_codes=(),
        expected_document_revision=None,
    ):
        parsed, compiled = self._compile(document)
        if compiled.errors or compiled.plan is None:
            raise WorkflowManagerError("activation.validation_blocked", "workflow has blocking validation errors")
        required = sorted({item.code for item in compiled.warnings
                           if item.severity == "warning"})
        if sorted(acknowledged_warning_codes) != required:
            raise WorkflowManagerError("activation.acknowledgement_mismatch", "acknowledge the exact current warning-code set")
        initial_plan = compiled.plan
        try:
            previous = self.store.load_draft()
        except Exception as exc:
            if getattr(exc, "code", "") != "store.draft_missing":
                raise
            previous = None
        if expected_document_revision is None:
            current_revision = 0 if previous is None else previous.document_revision
            saved = self.store.save_draft(
                parsed, expected_document_revision=current_revision
            )
        else:
            if type(expected_document_revision) is not int or expected_document_revision < 0:
                raise StoreError("store.invalid_revision", "draft document revision is invalid")
            if previous is None or previous.document_revision != expected_document_revision:
                raise StoreError("store.revision_conflict", "draft document revision is stale")
            if document_sha256(parsed) != document_sha256(previous):
                raise StoreError(
                    "activation.draft_mismatch",
                    "activation must use the exact saved workflow draft",
                )
            saved = previous
        _, compiled = self._compile(saved)
        if compiled.errors or compiled.plan is None:
            raise WorkflowManagerError("activation.validation_blocked", "workflow has blocking validation errors")
        current_required = sorted({item.code for item in compiled.warnings
                                   if item.severity == "warning"})
        if sorted(acknowledged_warning_codes) != current_required:
            raise WorkflowManagerError("activation.acknowledgement_mismatch", "acknowledge the exact current warning-code set")
        # save_draft may legitimately advance semantic_revision, changing the
        # revision-bound hashes. Compare the actual behavior and resolved plan
        # instead of the whole CompiledPlan object.
        if (
            behavior_payload(parsed) != behavior_payload(saved)
            or any(
                getattr(initial_plan, field) != getattr(compiled.plan, field)
                for field in (
                    "workflow_id", "external_inputs", "nodes", "edges",
                    "incoming", "outgoing", "topological_order", "max_parallelism",
                )
            )
        ):
            raise WorkflowManagerError("activation.validation_changed", "workflow or installed Skill identity changed during activation")
        selection, state = self.store.activate_and_start_run(
            compiled.plan,
            high_risk_warning_codes=current_required,
            acknowledged_warning_codes=acknowledged_warning_codes,
            run_id="run-" + secrets.token_hex(16),
        )
        return {"status": "pass", "selection": selection.to_payload(), "run_id": state.run_id}

    def _load(self, transaction, *, repair_selection=True):
        plan, state = transaction.load_active_run()
        selection = self.store._selection_unlocked(repair_projection=repair_selection)
        if selection.mode != "custom" or selection.semantic_sha256 != plan.semantic_sha256:
            raise WorkflowManagerError("runtime.selection_mismatch", "run is not the selected custom workflow")
        return plan, state

    def _identity(self, node):
        if node.type == "task":
            # The activated plan owns the selected root. A later CLI process
            # need not repeat Studio's --skills-root argument.
            if node.skill is None or not node.skill.root.is_absolute():
                raise WorkflowManagerError(
                    "runtime.identity_changed",
                    "activated task has no absolute Skill root",
                )
            pinned_roots = (node.skill.root,)
            catalog = discover_skills(pinned_roots, load_install_receipts(pinned_roots))
            current = catalog.skills.get(node.skill.catalog_id)
            if (node.skill.catalog_id in catalog.failed_skill_ids
                    or any(issue.code in ROOT_CATALOG_FAILURE_CODES
                           for issue in catalog.errors)
                    or current is None or current != node.skill):
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
                current = hash_project_file(self.project_root, artifact["path"])
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
            invocation = build_invocation(
                claim, token, self.project_root,
                skill_root=plan.nodes[node_id].skill.root if plan.schema_version == "compiled-plan-v2" else None,
            )
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
            required_result_schema = "node-result-v2" if plan.schema_version == "compiled-plan-v2" else "node-result-v1"
            if result["schema_version"] != required_result_schema:
                raise WorkflowManagerError("receipt.result_version", f"active run requires {required_result_schema}")
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
            if required_result_schema == "node-result-v2":
                declared = {
                    item["id"]: (item["path"], item["sha256"])
                    for item in claim["input_artifacts"]
                }
                for source in result["consumed_sources"]:
                    if declared.get(source["id"]) != (source["path"], source["sha256"]):
                        raise WorkflowManagerError(
                            "receipt.undeclared_source",
                            "consumed project file was not a claimed input; declare it and rerun the stage",
                        )
            artifacts = []
            for artifact in result["artifacts"]:
                try:
                    output_digest = hash_project_file(self.project_root, artifact["path"])
                except (PathSafetyError, OSError, ValueError) as exc:
                    raise WorkflowManagerError(
                        "receipt.output_unsafe",
                        "result output is not a safely readable regular file",
                    ) from exc
                artifacts.append(ArtifactRuntime(artifact["id"], artifact["path"], output_digest, "verified", node_id, runtime.attempt))
            updated = result_transition(plan, state, {"node_id": node_id, "attempt": runtime.attempt,
                "status": result["status"], "outcome": result["outcome"],
                "outputs": {item["id"]: item["path"] for item in result["artifacts"]}, "artifacts": artifacts})
            receipt = build_stage_receipt(plan, updated, node_id, claim, summary=result["summary"],
                uncertainties=result["uncertainties"], completed_at=_now(), error=result.get("error"),
                consumed_sources=result.get("consumed_sources"))
            transaction.commit_receipted_transition("node_result_recorded", updated, receipt,
                result_sha256=digest, claim_event_seq=claim_event.event_seq)
            self._stabilize(transaction, plan, updated)
            return receipt

    def rerun_stale(self, node_id):
        """Explicitly requeue a stale closure; execution remains a separate claim."""
        if not isinstance(node_id, str):
            raise WorkflowManagerError("runtime.invalid_stale_rerun", "stale rerun requires one node ID")
        with self.store.locked_run() as transaction:
            plan, _state = self._load(transaction)
            if node_id not in plan.nodes:
                raise WorkflowManagerError("runtime.invalid_stale_rerun", "stale rerun target is unknown")
            event = transaction.request_stale_rerun(node_id)
        return {
            "status": "pass",
            "root_node_id": event.payload["root_node_id"],
            "affected_node_ids": list(event.payload["affected_node_ids"]),
            "attempts_before": dict(event.payload["attempts_before"]),
            "attempts_after": dict(event.payload["attempts_after"]),
        }

    def register_artifact(self, artifact_id, path, provenance_summary):
        """Hash and register one declared project input with recorded provenance."""
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(path, str)
            or not path
            or not isinstance(provenance_summary, str)
            or not provenance_summary
            or provenance_summary != provenance_summary.strip()
            or len(provenance_summary) > 4000
            or any(ord(char) < 32 for char in provenance_summary)
        ):
            raise WorkflowManagerError(
                "runtime.invalid_artifact_registration",
                "artifact ID, relative path, and provenance summary are required",
            )
        recovered_before_registration = False
        for attempt in range(2):
            try:
                with self.store.locked_run() as transaction:
                    plan, _state = self._load(transaction)
                    if artifact_id not in plan.external_inputs:
                        raise WorkflowManagerError(
                            "runtime.invalid_artifact_registration",
                            "artifact ID is not a declared external input",
                        )
                    try:
                        digest = hash_project_file(self.project_root, path)
                    except (PathSafetyError, OSError, ValueError) as exc:
                        raise WorkflowManagerError(
                            "runtime.artifact_unsafe",
                            "artifact path is not a safely readable project file",
                        ) from exc
                    artifact = ArtifactRuntime(
                        artifact_id, path, digest, "verified", "external", 0
                    )
                    event = transaction.register_external_artifacts(
                        {artifact_id: artifact},
                        provenance={artifact_id: provenance_summary},
                    )
                    if event is not None:
                        _, updated = transaction.load_active_run()
                        self._stabilize(transaction, plan, updated)
                    return {
                        "status": "pass",
                        "registration_status": "unchanged" if event is None else "registered",
                        "artifact_id": artifact_id,
                        "path": path,
                        "sha256": digest,
                        "event_seq": None if event is None else event.event_seq,
                        "recovered_before_registration": recovered_before_registration,
                    }
            except StoreError as exc:
                if exc.code != "recovery.required" or attempt != 0:
                    raise
                recovery = self.store.recover()
                if recovery.status not in {"clean", "recovered"}:
                    raise WorkflowManagerError(
                        "runtime.recovery_required",
                        "existing artifact changes need recovery before registration",
                    ) from exc
                recovered_before_registration = True
        raise WorkflowManagerError(
            "runtime.recovery_required", "artifact registration could not load an active run"
        )

    def _record_condition_value(self, event_type, name, value, provenance_summary):
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or len(name) > 4000
            or any(ord(char) < 32 for char in name)
            or not isinstance(provenance_summary, str)
            or not provenance_summary
            or provenance_summary != provenance_summary.strip()
            or len(provenance_summary) > 4000
            or any(ord(char) < 32 for char in provenance_summary)
        ):
            raise WorkflowManagerError(
                "runtime.invalid_fact_update",
                "a normalized name and bounded provenance summary are required",
            )
        if event_type == "fact_recorded" and type(value) is not bool:
            raise WorkflowManagerError(
                "runtime.invalid_project_fact", "project fact value must be a JSON boolean"
            )

        decision = event_type == "decision_recorded"
        recovered_before_update = False
        for attempt in range(2):
            try:
                with self.store.locked_run() as transaction:
                    plan, state = self._load(transaction)
                    try:
                        updated = record_condition_fact_transition(
                            plan, state, name, value, decision=decision
                        )
                    except WorkflowError as exc:
                        raise WorkflowManagerError(exc.code, str(exc)) from exc
                    if updated is state:
                        return {
                            "status": "pass",
                            "registration_status": "unchanged",
                            "name": name,
                            "value": value,
                            "event_seq": None,
                            "affected_node_ids": [],
                            "recovered_before_update": recovered_before_update,
                        }
                    event = transaction.commit_transition(
                        event_type,
                        updated,
                        {"name": name, "value": value, "provenance_summary": provenance_summary},
                    )
                    before_stabilization = state
                    _, committed = transaction.load_active_run()
                    stabilized = self._stabilize(transaction, plan, committed)
                    changed_nodes = sorted(
                        node_id for node_id in plan.nodes
                        if before_stabilization.nodes[node_id] != stabilized.nodes[node_id]
                    )
                    return {
                        "status": "pass",
                        "registration_status": "recorded",
                        "name": name,
                        "value": value,
                        "event_seq": event.event_seq,
                        "affected_node_ids": changed_nodes,
                        "recovered_before_update": recovered_before_update,
                    }
            except StoreError as exc:
                if exc.code != "recovery.required" or attempt != 0:
                    raise
                recovery = self.store.recover()
                if recovery.status not in {"clean", "recovered"}:
                    raise WorkflowManagerError(
                        "runtime.recovery_required",
                        "existing evidence changes need recovery before recording a fact or decision",
                    ) from exc
                recovered_before_update = True
        raise WorkflowManagerError(
            "runtime.recovery_required", "fact/decision update could not load an active run"
        )

    def record_fact(self, name, value, provenance_summary="User-recorded project fact."):
        """Record a registered project boolean and invalidate its condition lineage."""
        return self._record_condition_value(
            "fact_recorded", name, value, provenance_summary
        )

    def record_decision(self, name, value, provenance_summary):
        """Record one bounded decision value and invalidate dependent conditions."""
        if isinstance(name, str) and name.startswith("__approval__."):
            raise WorkflowManagerError("runtime.reserved_decision", "use record-approval for an approval gate")
        return self._record_condition_value(
            "decision_recorded", name, value, provenance_summary
        )

    def record_approval(self, node_id, action, provenance_summary):
        if action not in {"approve", "revise"} or not isinstance(node_id, str):
            raise WorkflowManagerError("runtime.invalid_approval", "approval action must be approve or revise")
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction)
            gate = plan.nodes.get(node_id)
            if gate is None or gate.approval_source is None:
                raise WorkflowManagerError("runtime.invalid_approval", "node is not an approval gate")
            fingerprint = approval_fingerprint(plan, state, node_id)
            if fingerprint is None:
                raise WorkflowManagerError("runtime.invalid_approval", "approval source is not currently complete")
        value = ("approved:" if action == "approve" else "revise:") + fingerprint
        recorded = self._record_condition_value(
            "decision_recorded", approval_decision_name(node_id), value, provenance_summary
        )
        return {**recorded, "node_id": node_id, "action": action}

    def retry(self, node_id):
        """Retry one failed, interrupted, or execution-skipped node without advancing its attempt."""
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction)
            if not isinstance(node_id, str) or node_id not in plan.nodes:
                raise WorkflowManagerError("runtime.unknown_node", "unknown node")
            node = plan.nodes[node_id]
            if node.type == "task":
                event_type = "node_retried"
                updated = retry_transition(plan, state, node_id)
            elif node.type == "validator":
                event_type = "validator_retried"
                updated = validator_retry_transition(plan, state, node_id)
            else:
                raise WorkflowManagerError("runtime.node_type", "only task or validator nodes can be retried")
            transaction.commit_transition(event_type, updated, {"node_id": node_id})
            stabilized = self._stabilize(transaction, plan, updated)
            runtime = stabilized.nodes[node_id]
            return {
                "status": "pass",
                "node_id": node_id,
                "node_status": runtime.status.value,
                "attempt": runtime.attempt,
            }

    def summary(self):
        """Resolve mode without repairing its projection, then return a summary."""
        from scripts.confirmed_artifacts import ConfirmedArtifactError, confirmed_artifact_summary

        selection = self.store.read_selection(repair_projection=False)
        try:
            confirmed = confirmed_artifact_summary(self.project_root)
        except ConfirmedArtifactError as exc:
            raise WorkflowManagerError(exc.code, str(exc)) from exc
        if selection.mode == "official":
            return {
                "status": "pass",
                "mode": "official",
                "run_status": "inactive",
                "nodes": [],
                "artifacts": [],
                "decisions": {},
                "project_booleans": {},
                "confirmed_artifacts": confirmed,
            }
        with self.store.locked_run() as transaction:
            plan, state = self._load(transaction, repair_selection=False)
            return {
                "status": "pass",
                "mode": "custom",
                "workflow_id": plan.workflow_id,
                "semantic_revision": plan.semantic_revision,
                "semantic_sha256": plan.semantic_sha256,
                "run_id": state.run_id,
                "run_status": "active",
                "progress": summarize_run_progress(state),
                "confirmed_artifacts": confirmed,
                "nodes": [
                    {
                        "node_id": node_id,
                        "node_type": plan.nodes[node_id].type,
                        "status": state.nodes[node_id].status.value,
                        "attempt": state.nodes[node_id].attempt,
                        "outcome": state.nodes[node_id].outcome,
                        **({"approval_source": plan.nodes[node_id].approval_source,
                            "approval_state": approval_state(plan, state, node_id)}
                           if plan.nodes[node_id].approval_source is not None else {}),
                    }
                    for node_id in sorted(plan.nodes)
                ],
                "artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "path": artifact.path,
                        "sha256": artifact.sha256,
                        "state": artifact.state,
                        "producer_node_id": artifact.producer_node_id,
                        "producer_attempt": artifact.producer_attempt,
                    }
                    for artifact_id, artifact in sorted(state.artifacts.items())
                ],
                "decisions": dict(state.decisions),
                "project_booleans": dict(state.project_booleans),
            }

    def deactivate(self):
        """Stop the active custom run and return the authoritative selection."""
        selection = self.store.deactivate_custom()
        return {"status": "pass", "selection": selection.to_payload()}

    def recover(self, *, confirmed_interrupted=False):
        """Recover durable records only after the caller confirms work stopped.

        Uncertain active claims become blocked for explicit retry. Recovery
        does not execute stages or turn interrupted work into successful work.
        """
        if confirmed_interrupted is not True:
            raise WorkflowManagerError(
                "recovery.confirmation_required",
                "Confirm that no task or validator from this run is still executing before recovery.",
            )
        recovered = self.store.recover()
        return {
            "status": "blocked" if recovered.status == "blocked" else "pass",
            "recovery_status": recovered.status,
            "recovery_code": recovered.code,
            "run_id": recovered.state.run_id if recovered.state else None,
        }


if __name__ == "__main__":
    raise SystemExit(main())
