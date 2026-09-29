"""Keep the latest explicitly confirmed research artifacts separate from drafts.

The confirmation catalog records adoption, not stage execution. Existing run
receipts keep their original paths; confirmed snapshots provide stable inputs
for later research or explicit registration in a new custom run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.confirmed_artifacts import (
    ConfirmedArtifactError, ConfirmedArtifactStore, normalize_request,
)
from scripts import progress_manager as progress
from scripts.workflow_engine.fs import PathSafetyError, hash_project_file, read_project_json_object, resolve_project_path
from scripts.workflow_engine.schema import WorkflowError
from scripts.workflow_engine.receipts import canonical_bytes
from scripts.workflow_engine.store import StoreError
from scripts.workflow_manager import WorkflowService


def _fail(code: str, message: str):
    raise ConfirmedArtifactError(code, message)


class ArtifactService:
    """Validate the selected workflow's evidence before recording adoption."""

    def __init__(self, project_root: Path | str):
        self.workflow = WorkflowService(Path(project_root))
        self.project_root = self.workflow.project_root
        self.catalog = ConfirmedArtifactStore(self.project_root)

    def _evidence(self, request, *, required=False):
        evidence = request.get("evidence", [])
        if required and not evidence:
            _fail("confirmation.evidence_required", "Provide the actual stage verification evidence before confirming an official artifact.")
        for item in evidence:
            if hash_project_file(self.project_root, item["path"]) != item["sha256"]:
                _fail("confirmation.evidence_changed", "Verification evidence changed; review it again before confirmation.")
        return evidence

    def _official_provenance(self, request):
        path = resolve_project_path(self.project_root, ".research/progress.md")
        snapshot = progress.summarize_text(progress.read_text(path))
        if not snapshot["valid"]:
            _fail("confirmation.progress_invalid", "Validate or restore official progress before confirming artifacts.")
        if request.get("expected_progress_sha256") != snapshot["document_sha256"]:
            _fail("confirmation.progress_changed", "Use the document_sha256 from the current official progress summary.")
        if snapshot["validity_status"] == "blocked":
            _fail("confirmation.validity_blocked", "Resolve active blocking validity issues before confirming a new official artifact.")
        return {
            "mode": "official",
            "project_id": snapshot["project_id"],
            "stage": snapshot["current_stage"],
            "entry_mode": snapshot["mode"],
            "progress_document_sha256": snapshot["document_sha256"],
            "validity_status": snapshot["validity_status"],
            "evidence": self._evidence(request, required=True),
        }

    def _custom_provenance(self, transaction, request):
        if request.get("expected_progress_sha256"):
            _fail("confirmation.mode_mismatch", "Custom confirmation uses committed run evidence, not official progress.")
        plan, state = self.workflow._load(transaction, repair_selection=False)
        result_events = transaction.events("node_result_recorded")
        bindings = []
        for item in request["files"]:
            source_id = item.get("source_artifact_id")
            artifact = state.artifacts.get(source_id) if isinstance(source_id, str) else None
            if (artifact is None or artifact.state != "verified"
                    or artifact.path != item["source_path"] or artifact.sha256 != item["sha256"]):
                _fail("confirmation.artifact_mismatch", "Each file must identify its exact verified source_artifact_id, path and hash in the selected run.")
            producer = state.nodes.get(artifact.producer_node_id)
            if (producer is None or producer.status.value != "succeeded"
                    or producer.attempt != artifact.producer_attempt):
                _fail("confirmation.stage_unaccepted", "Only outputs from a currently successful, committed task attempt can be confirmed.")
            matches = [event for event in result_events
                       if event.payload["receipt"]["node_id"] == artifact.producer_node_id
                       and event.payload["receipt"]["attempt"] == artifact.producer_attempt]
            if len(matches) != 1:
                _fail("confirmation.receipt_missing", "The source output must have one committed stage receipt.")
            event = matches[0]
            receipt = event.payload["receipt"]
            output = {"id": source_id, "path": item["source_path"], "sha256": item["sha256"]}
            if receipt["status"] != "succeeded" or output not in receipt["output_artifacts"]:
                _fail("confirmation.receipt_mismatch", "The committed successful receipt does not bind this exact output.")
            receipt_path = (self.workflow.store.paths.receipts / state.run_id
                            / f"{artifact.producer_node_id}-attempt-{artifact.producer_attempt}.json")
            relative_receipt = receipt_path.relative_to(self.project_root).as_posix()
            receipt_sha256 = hash_project_file(self.project_root, relative_receipt)
            if receipt_sha256 != hashlib.sha256(canonical_bytes(receipt) + b"\n").hexdigest():
                _fail("confirmation.receipt_mismatch", "Stage receipt changed after run validation; review the committed receipt before confirming.")
            bindings.append({
                "source_artifact_id": source_id,
                "original_binding_path": artifact.path,
                "sha256": artifact.sha256,
                "producer_node_id": artifact.producer_node_id,
                "producer_attempt": artifact.producer_attempt,
                "receipt_path": relative_receipt,
                "receipt_sha256": receipt_sha256,
                "event_seq": event.event_seq,
                "event_sha256": event.event_hash,
            })
        return {
            "mode": "custom", "workflow_id": plan.workflow_id,
            "semantic_revision": plan.semantic_revision,
            "semantic_sha256": plan.semantic_sha256, "run_id": state.run_id,
            "bindings": bindings, "evidence": self._evidence(request),
        }

    def accept(self, request, *, confirmed=False):
        if confirmed is not True:
            _fail("confirmation.required", "Confirm adoption of the verified artifact before publishing a current version.")
        request = normalize_request(request)
        selection = self.workflow.store.read_selection(repair_projection=False)
        if selection.mode == "official":
            with self.workflow.store.locked_official():
                path = resolve_project_path(self.project_root, ".research/progress.md")
                resolve_project_path(self.project_root, ".research/progress.md.lock")
                with progress.progress_lock(path):
                    return self.catalog.accept(request, self._official_provenance)
        with self.workflow.store.locked_run() as transaction:
            return self.catalog.accept(request, lambda value: self._custom_provenance(transaction, value))


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _fail("cli.invalid_arguments", message)


def build_parser():
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("status", "resolve", "accept", "withdraw", "repair"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True)
        command.add_argument("--json", action="store_true", help="emit the JSON response")
        if name == "status":
            command.add_argument("--verify", action="store_true", help="also verify the current confirmed files")
        if name == "resolve":
            command.add_argument("--artifact-id", required=True)
            command.add_argument("--expect-revision", type=int)
        if name == "accept":
            command.add_argument("--request", required=True, help="project-relative confirmation request JSON")
        if name in {"accept", "withdraw", "repair"}:
            command.add_argument("--confirm", action="store_true", required=True)
        if name in {"withdraw", "repair"}:
            command.add_argument("--expected-revision", type=int, required=True)
        if name == "withdraw":
            command.add_argument("--artifact-id", required=True)
            command.add_argument("--operation-id", required=True)
            command.add_argument("--reason", required=True)
        if name == "repair":
            command.add_argument("--backup-conflicts", action="store_true", help="preserve conflicting display files in a backup before rebuilding")
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        service = ArtifactService(args.project)
        if args.command == "status":
            result = service.catalog.status(verify=args.verify)
        elif args.command == "resolve":
            result = service.catalog.resolve(args.artifact_id, expected_revision=args.expect_revision)
        elif args.command == "accept":
            request = read_project_json_object(service.project_root, args.request)
            result = service.accept(request, confirmed=args.confirm)
        elif args.command == "withdraw":
            result = service.catalog.withdraw(args.artifact_id, args.expected_revision, args.operation_id, args.reason)
        else:
            result = service.catalog.repair(expected_revision=args.expected_revision, backup_conflicts=args.backup_conflicts)
        output = {"status": "pass", **result}
        # Confirmation may be committed even when its display needs repair.
        if result.get("projection_pending"):
            output["status"] = "attention"
        code = 0
    except (ConfirmedArtifactError, StoreError, WorkflowError, PathSafetyError, progress.ProgressError, ValueError, OSError) as exc:
        output = {"status": "blocked", "error": {
            "code": getattr(exc, "code", "confirmation.invalid_input"), "message": str(exc)[:4000],
        }}
        code = 2
    except Exception:
        output = {"status": "error", "error": {
            "code": "cli.internal_error", "message": "Unexpected artifact-manager failure.",
        }}
        code = 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
