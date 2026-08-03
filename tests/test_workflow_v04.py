from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PM = ROOT / "scripts" / "progress_manager.py"
PREFLIGHT = ROOT / "scripts" / "humanizer_preflight.py"
SECTION_VALIDATOR = ROOT / "scripts" / "paper_section_validator.py"
EXPERIMENT_VALIDATOR = ROOT / "scripts" / "experiment_contract_validator.py"


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


CODEX_SKILLS = codex_home() / "skills"
GLOBAL = CODEX_SKILLS / "paper-workflow-orchestrator"
ROUTER = CODEX_SKILLS / "research-skill-router" / "SKILL.md"
AUTORESEARCH_ROOT = CODEX_SKILLS / "0-autoresearch-skill"


def run_script(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
    )


class WorkflowV04Tests(unittest.TestCase):
    def test_progress_init_blocker_and_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            result = run_script(PM, "init", "--project-dir", str(project), "--project-id", "test-paper")
            self.assertEqual(result.returncode, 0, result.stderr)
            progress = project / ".research" / "progress.md"
            checked = run_script(PM, "validate", "--file", str(progress))
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            summary = json.loads(run_script(PM, "summary", "--file", str(progress)).stdout)
            self.assertEqual(summary["validity_status"], "pending")

            recorded = run_script(
                PM,
                "record-error",
                "--file",
                str(progress),
                "--stage",
                "integrity",
                "--error",
                "measurement may change the core conclusion",
                "--cause",
                "unvalidated measurement",
                "--impact",
                "core conclusion not identifiable",
                "--severity",
                "critical",
                "--blocking",
                "true",
                "--rule",
                "obtain additional measurement evidence",
                "--check",
                "rerun the validity audit",
                "--refs",
                "results/[raw].json",
            )
            self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
            summary = json.loads(run_script(PM, "summary", "--file", str(progress)).stdout)
            self.assertEqual(summary["validity_status"], "blocked")
            self.assertEqual(run_script(PM, "validate", "--file", str(progress)).returncode, 0)

            legacy = project / "legacy.md"
            legacy.write_text(
                progress.read_text(encoding="utf-8")
                .replace("paper-workflow-orchestrator-v0.4", "paper-workflow-orchestrator-v0.3")
                .replace("- validity_status: pending\n", "")
                .replace("  - severity: critical\n", "")
                .replace("  - blocking: true\n", ""),
                encoding="utf-8",
            )
            refused = run_script(PM, "migrate", "--file", str(legacy), "--mode", "write_or_revise")
            self.assertNotEqual(refused.returncode, 0)
            migrated = run_script(
                PM,
                "migrate",
                "--file",
                str(legacy),
                "--mode",
                "write_or_revise",
                "--confirm",
            )
            self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
            self.assertEqual(run_script(PM, "validate", "--file", str(legacy)).returncode, 0)
            self.assertTrue(Path(str(legacy) + ".legacy-v0.3").is_file())

    def test_progress_blocked_without_active_rule_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            run_script(PM, "init", "--project-dir", str(project), "--project-id", "test-paper")
            progress = project / ".research" / "progress.md"
            text = progress.read_text(encoding="utf-8").replace("- validity_status: pending", "- validity_status: blocked")
            progress.write_text(text, encoding="utf-8")
            checked = run_script(PM, "validate", "--file", str(progress))
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("active blocking", checked.stdout)

    def test_progress_concurrent_append_is_serial_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            run_script(PM, "init", "--project-dir", str(project), "--project-id", "concurrent-paper")
            progress = project / ".research" / "progress.md"

            def append(index: int) -> int:
                result = run_script(
                    PM,
                    "record-error",
                    "--file", str(progress),
                    "--stage", "drafting",
                    "--error", f"bounded test error {index}",
                    "--cause", "concurrent test",
                    "--impact", "no scientific impact",
                    "--severity", "minor",
                    "--blocking", "false",
                    "--rule", "keep the append-only record",
                    "--check", "validate progress before the next stage",
                    "--refs", f"agent[{index}].json",
                )
                return result.returncode

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(append, range(8)))
            self.assertEqual(results, [0] * 8)
            checked = run_script(PM, "validate", "--file", str(progress))
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            text = progress.read_text(encoding="utf-8")
            self.assertEqual(text.count("- R"), 8)
            self.assertEqual(text.count("- EVT-"), 9)

    def test_humanizer_preflight_fails_closed_on_missing_contract_and_format_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_file = root / "paper.tex"
            input_file.write_text("\\section{Intro}\nText", encoding="utf-8")
            args = (
                "--input",
                str(input_file),
                "--candidate",
                str(root / "candidate.tex"),
                "--format",
                "markdown",
                "--adapter-contract",
                str(root / "contract.json"),
                "--immutable-copy",
                str(root / "copy.tex"),
                "--protected-manifest",
                str(root / "manifest.json"),
                "--claim-diff",
                str(root / "claims.json"),
                "--integrity-report",
                str(root / "integrity.md"),
            )
            result = run_script(PREFLIGHT, *args)
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertIn("conflicts with input suffix", payload["reason"])

    def test_humanizer_preflight_requires_bound_contract_and_accepts_complete_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_file = root / "paper.md"
            input_file.write_text("A bounded prose paragraph with a claim 42.", encoding="utf-8")
            candidate = root / "paper.candidate.md"
            candidate.write_text("A natural bounded prose paragraph with a claim 42.", encoding="utf-8")
            immutable = root / "paper.original.md"
            immutable.write_bytes(input_file.read_bytes())

            def digest(path: Path) -> str:
                return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

            input_hash = digest(input_file)
            candidate_hash = digest(candidate)
            text_digest = lambda value: "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
            old_excerpt = "bounded prose paragraph"
            new_excerpt = "natural bounded prose paragraph"
            old_hash = text_digest(old_excerpt)
            new_hash = text_digest(new_excerpt)
            claim_inventory = [{"claim_id": "C1", "old_hash": old_hash, "new_hash": new_hash}]
            manifest = root / "manifest.json"
            protected_span_hash = text_digest("42")
            manifest.write_text(
                json.dumps({"schema_version": 1, "format": "markdown", "input_sha256": input_hash, "offset_unit": "utf8_byte", "spans": [{
                    "id": "PROT-001", "kind": "number", "exact_text": "42", "canonical_hash": protected_span_hash,
                    "source_offset": [input_file.read_text(encoding="utf-8").index("42"), input_file.read_text(encoding="utf-8").index("42") + 2],
                    "candidate_exact_text": "42", "candidate_span_sha256": protected_span_hash,
                    "candidate_offset": [candidate.read_text(encoding="utf-8").index("42"), candidate.read_text(encoding="utf-8").index("42") + 2],
                }]}),
                encoding="utf-8",
            )
            diff = root / "claims.json"
            verifier_receipt = root / "claim-verifier.json"
            verifier_id = "fixture-independent-check"
            inventory_hash = text_digest(json.dumps(claim_inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            verifier_receipt.write_text(json.dumps({
                "schema_version": 1, "status": "pass", "verifier_id": verifier_id,
                "format": "markdown",
                "input_sha256": input_hash, "candidate_sha256": candidate_hash,
                "claim_inventory_sha256": inventory_hash,
                "grounded_excerpts": True,
                "verified_claims": [{
                    "claim_id": "C1", "semantic_checks": True,
                    "old_hash": old_hash, "new_hash": new_hash,
                    "evidence_refs": ["fixture-independent-claim"],
                    "protected_fields": ["entities", "numeric_units", "modality", "negation", "scope", "statistical_qualifiers", "citations", "equations_labels"],
                    "protected_field_ledger": {
                        "entities": {"status": "verified", "count": 0}, "numeric_units": {"status": "verified", "count": 1},
                        "modality": {"status": "verified", "count": 0}, "negation": {"status": "verified", "count": 0},
                        "scope": {"status": "verified", "count": 0}, "statistical_qualifiers": {"status": "verified", "count": 0},
                        "citations": {"status": "verified", "count": 0}, "equations_labels": {"status": "verified", "count": 0},
                    },
                }],
                "evidence_refs": ["fixture-independent-source"],
            }), encoding="utf-8")
            diff.write_text(
                json.dumps({
                    "schema_version": 1,
                    "format": "markdown",
                    "input_sha256": input_hash,
                    "original_sha256": input_hash,
                    "candidate_sha256": candidate_hash,
                    "status": "pass",
                    "grounding_mode": "text",
                    "claim_count": 1,
                    "claim_inventory_sha256": inventory_hash,
                    "verifier_receipt": {"status": "pass", "verifier_id": verifier_id, "input_sha256": input_hash, "candidate_sha256": candidate_hash, "grounded_excerpts": True},
                    "verifier_receipt_path": verifier_receipt.name,
                    "verifier_receipt_sha256": digest(verifier_receipt),
                    "claims": [{
                        "claim_id": "C1",
                        "evidence_refs": ["fixture-source"],
                        "old_excerpt": old_excerpt,
                        "new_excerpt": new_excerpt,
                        "old_hash": old_hash,
                        "new_hash": new_hash,
                        "verifier": "fixture-verifier",
                        "status": "pass",
                        "semantic_checks": True,
                        "protected_fields": ["entities", "numeric_units", "modality", "negation", "scope", "statistical_qualifiers", "citations", "equations_labels"],
                        "entities": [], "numeric_units": ["42"], "modality": [], "negation": [], "scope": [],
                        "statistical_qualifiers": [], "citations": [], "equations_labels": [],
                        "protected_field_ledger": {
                            "entities": {"status": "verified", "count": 0}, "numeric_units": {"status": "verified", "count": 1},
                            "modality": {"status": "verified", "count": 0}, "negation": {"status": "verified", "count": 0},
                            "scope": {"status": "verified", "count": 0}, "statistical_qualifiers": {"status": "verified", "count": 0},
                            "citations": {"status": "verified", "count": 0}, "equations_labels": {"status": "verified", "count": 0},
                        },
                    }],
                }),
                encoding="utf-8",
            )
            integrity = root / "integrity.md"
            integrity.write_text(f"schema_version: 1\nreceipt_id: fixture-integrity\nvalidation_status: pass\nvalidity_status: clear\ninput_sha256: {input_hash}\ncandidate_sha256: {candidate_hash}\nprotected_manifest_sha256: {digest(manifest)}\nclaim_diff_sha256: {digest(diff)}\n", encoding="utf-8")
            entrypoint = root / "adapter.py"
            humanizer = root / "humanizer" / "SKILL.md"
            humanizer.parent.mkdir()
            humanizer.write_text("---\nname: humanizer\nmetadata:\n  version: 2.9.1\n---\n", encoding="utf-8")
            humanizer_hash = digest(humanizer)
            humanizer_version = "2.9.1"
            contract_id = "fixture-contract"
            operations = {name: True for name in ("extract_protected", "reassemble", "verify_protected", "verify_claims", "rollback")}
            rollback = root / "rollback.md"
            rollback.write_text("immutable rollback target", encoding="utf-8")
            rollback_hash = digest(rollback)
            entrypoint.write_text(
                "import argparse, hashlib, json\n"
                "from pathlib import Path\n"
                "p=argparse.ArgumentParser(); p.add_argument('--self-test', action='store_true'); p.add_argument('--input'); p.add_argument('--candidate'); p.add_argument('--contract-id'); p.add_argument('--self-test-report'); a=p.parse_args()\n"
                "if a.self_test:\n"
                f" print(json.dumps({{'schema_version':2,'status':'pass','adapter_id':'fixture','format':'markdown','contract_id':'{contract_id}','entrypoint_sha256':'sha256:' + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'tested_input_sha256':'sha256:' + hashlib.sha256(Path(a.input).read_bytes()).hexdigest(),'tested_candidate_sha256':'sha256:' + hashlib.sha256(Path(a.candidate).read_bytes()).hexdigest(),'rollback_target_sha256':'{rollback_hash}','rollback_verified':True,'humanizer_skill_sha256':'{humanizer_hash}','humanizer_version':'{humanizer_version}','canonical_mutation_allowed':False,'offset_mapping_verified':True,'self_test_report_sha256':'sha256:' + hashlib.sha256(Path(a.self_test_report).read_bytes()).hexdigest(),**{operations!r}}}))\n",
                encoding="utf-8",
            )
            self_test = root / "self_test.json"
            entrypoint_hash = digest(entrypoint)
            self_test.write_text(
                json.dumps({"schema_version": 2, "status": "pass", "adapter_id": "fixture", "format": "markdown", "contract_id": contract_id, "entrypoint_sha256": entrypoint_hash, "tested_input_sha256": input_hash, "tested_candidate_sha256": candidate_hash, "humanizer_skill_sha256": humanizer_hash, "humanizer_version": humanizer_version, "rollback_target_sha256": rollback_hash, "rollback_verified": True, "canonical_mutation_allowed": False, "offset_mapping_verified": True, **operations}),
                encoding="utf-8",
            )
            kinds = ["math", "code", "table", "figure_caption", "citation", "bibliography", "url", "doi", "quote", "technical_term", "data", "number", "unit", "statistical_qualifier", "negative_finding", "modality", "negation", "scope", "frontmatter", "label"]
            contract = root / "contract.json"
            contract.write_text(
                json.dumps({
                    "schema_version": 2,
                    "format": "markdown",
                    "adapter_id": "fixture",
                    "contract_id": contract_id,
                    "adapter_version": "1",
                    "parser_version": "fixture-parser-1",
                    "entrypoint": str(entrypoint),
                    "entrypoint_sha256": entrypoint_hash,
                    "humanizer_skill_sha256": humanizer_hash,
                    "humanizer_version": humanizer_version,
                    "operations": operations,
                    "protected_kinds": kinds,
                    "network_scope": "none",
                    "bounded_chunks": True,
                    "immutable_original": True,
                    "rollback_target": str(rollback),
                    "rollback_target_sha256": rollback_hash,
                    "self_test_command": [sys.executable, str(entrypoint), "--self-test", "--input", "{input}", "--candidate", "{candidate}", "--contract-id", "{contract_id}", "--self-test-report", "{self_test_report}"],
                    "self_test_report": str(self_test),
                    "self_test_report_sha256": digest(self_test),
                    "immutable_copy": str(immutable),
                    "immutable_copy_sha256": digest(immutable),
                    "candidate": str(candidate),
                    "candidate_sha256": candidate_hash,
                    "protected_manifest": str(manifest),
                    "protected_manifest_sha256": digest(manifest),
                    "claim_diff": str(diff),
                    "claim_diff_sha256": digest(diff),
                    "integrity_report": str(integrity),
                    "integrity_report_sha256": digest(integrity),
                }),
                encoding="utf-8",
            )
            args = (
                "--input", str(input_file), "--candidate", str(candidate), "--adapter-contract", str(contract),
                "--immutable-copy", str(immutable), "--protected-manifest", str(manifest),
                "--claim-diff", str(diff), "--integrity-report", str(integrity),
                "--humanizer-skill", str(humanizer),
            )
            ready = run_script(PREFLIGHT, *args)
            self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
            self.assertEqual(json.loads(ready.stdout)["status"], "ready")
            original_contract = json.loads(contract.read_text(encoding="utf-8"))
            forged_network = dict(original_contract, network_scope="local-only")
            contract.write_text(json.dumps(forged_network), encoding="utf-8")
            network_blocked = run_script(PREFLIGHT, *args)
            self.assertNotEqual(network_blocked.returncode, 0)
            self.assertIn("network_scope", json.loads(network_blocked.stdout)["reason"])
            contract.write_text(json.dumps(original_contract), encoding="utf-8")
            rollback.write_text("tampered rollback target", encoding="utf-8")
            rollback_blocked = run_script(PREFLIGHT, *args)
            self.assertNotEqual(rollback_blocked.returncode, 0)
            self.assertIn("rollback_target hash", json.loads(rollback_blocked.stdout)["reason"])
            rollback.write_text("immutable rollback target", encoding="utf-8")
            forged = dict(original_contract)
            forged["self_test_command"] = [sys.executable, "-c", "print('{}')"]
            contract.write_text(json.dumps(forged), encoding="utf-8")
            command_blocked = run_script(PREFLIGHT, *args)
            self.assertNotEqual(command_blocked.returncode, 0)
            self.assertIn("must include --self-test", json.loads(command_blocked.stdout)["reason"])
            forged = dict(original_contract)
            forged["entrypoint_sha256"] = "sha256:" + ("0" * 64)
            contract.write_text(json.dumps(forged), encoding="utf-8")
            blocked = run_script(PREFLIGHT, *args)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual(json.loads(blocked.stdout)["status"], "blocked")

            malformed = run_script(PREFLIGHT)
            self.assertNotEqual(malformed.returncode, 0)
            self.assertEqual(json.loads(malformed.stdout)["status"], "blocked")

    def test_contract_and_router_mirror(self) -> None:
        local_files = [
            "SKILL.md",
            "references/paper-section-contract.md",
            "references/progress-schema.md",
            "references/stage-contracts.md",
            "references/humanizer-adapter.md",
            "scripts/progress_manager.py",
            "scripts/humanizer_preflight.py",
            "scripts/paper_section_validator.py",
            "scripts/experiment_contract_validator.py",
        ]
        for relative in local_files:
            local = ROOT / relative
            self.assertTrue(local.is_file(), relative)
        contract = (ROOT / "references/paper-section-contract.md").read_text(encoding="utf-8")
        self.assertIn("## Abstract", contract)
        self.assertIn("## 5 Conclusion", contract)
        self.assertIn("Discussion (optional heading)", contract)
        if not GLOBAL.is_dir() or not ROUTER.is_file() or not AUTORESEARCH_ROOT.is_dir():
            self.skipTest("Codex-installed integration skills are unavailable; local package checks passed")
        for relative in local_files:
            local = ROOT / relative
            installed = GLOBAL / relative
            self.assertTrue(installed.is_file(), relative)
            self.assertEqual(local.read_bytes(), installed.read_bytes(), relative)
        autoresearch = (AUTORESEARCH_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Codex safety boundary", autoresearch)
        self.assertIn("Do not create `/loop`, `cron`", autoresearch)
        router = ROUTER.read_text(encoding="utf-8")
        self.assertIn("Existing manuscript/draft audit", router)
        self.assertIn("approved_by: orchestrator", router)
        self.assertIn("never infer permission", router)

        forbidden = ("20-ml-paper-writing", "21-research-ideation", "pip install", "cron.add", "Telegram", "WhatsApp", "Slack", "weasyprint", "playwright", "wkhtmltopdf")
        for reference in (AUTORESEARCH_ROOT / "SKILL.md", *sorted((AUTORESEARCH_ROOT / "references").glob("*.md"))):
            reference_text = reference.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, reference_text, f"unsafe autoresearch reference token {token}: {reference}")

    def test_experiment_contract_validator_is_complete_and_fail_closed(self) -> None:
        required = {
            "objective": "test objective",
            "metric": "accuracy",
            "direction": "maximize",
            "baseline": "fixed baseline",
            "budget": 1,
            "max_runs": 2,
            "max_wall_time": 60,
            "stop_conditions": ["max_runs or max_wall_time reached"],
            "data_code_scope": ["data/", "src/"],
            "network_scope": "none",
            "write_and_commit_policy": "no_auto_commit",
            "report_destination": "local-only",
            "validation_status": "pass",
            "approved_by": "orchestrator",
            "user_confirmation": "recorded",
            "stage_receipt": ".research/stage_receipts/exp.yml",
            "validity_status": "clear",
        }
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            (project_root / "data").mkdir()
            (project_root / "src").mkdir()
            expiry = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
            scope_payload = {key: required[key] for key in ("objective", "metric", "direction", "baseline", "budget", "max_runs", "max_wall_time", "stop_conditions", "data_code_scope", "network_scope", "write_and_commit_policy", "report_destination", "validation_status", "approved_by", "user_confirmation", "validity_status")}
            scope_hash = "sha256:" + hashlib.sha256(json.dumps(scope_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            receipt = project_root / "stage_receipt.json"
            receipt.write_text(json.dumps({"status": "confirmed", "approved_by": "orchestrator", "user_confirmation": "recorded", "validity_status": "clear", "scope_hash": scope_hash, "expires_at": expiry}), encoding="utf-8")
            required["stage_receipt"] = "stage_receipt.json"
            required["stage_receipt_sha256"] = "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest()
            contract = Path(directory) / "experiment_contract.yml"
            contract.write_text(json.dumps(required), encoding="utf-8")
            passed = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
            self.assertEqual(json.loads(passed.stdout)["status"], "pass")
            yaml_contract = contract.with_name("experiment_contract_block.yml")
            yaml_contract.write_text(
                "\n".join([
                    "objective: test objective", "metric: accuracy", "direction: maximize",
                    "baseline: fixed baseline", "budget: 1", "max_runs: 2", "max_wall_time: 60",
                    "stop_conditions:", "  - max_runs or max_wall_time reached", "data_code_scope:", "  - data/", "  - src/",
                    "network_scope: none", "write_and_commit_policy: no_auto_commit",
                    "report_destination: local-only", "validation_status: pass",
                    "approved_by: orchestrator", "user_confirmation: recorded",
                    "stage_receipt: stage_receipt.json", "stage_receipt_sha256: " + required["stage_receipt_sha256"], "validity_status: clear",
                ]),
                encoding="utf-8",
            )
            yaml_passed = run_script(EXPERIMENT_VALIDATOR, "--contract", str(yaml_contract))
            self.assertEqual(yaml_passed.returncode, 0, yaml_passed.stdout + yaml_passed.stderr)
            for field in required:
                incomplete = dict(required)
                incomplete.pop(field)
                contract.write_text(json.dumps(incomplete), encoding="utf-8")
                blocked = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
                self.assertNotEqual(blocked.returncode, 0, field)
                self.assertEqual(json.loads(blocked.stdout)["status"], "blocked")
            invalid = dict(required, network_scope="internet")
            contract.write_text(json.dumps(invalid), encoding="utf-8")
            blocked = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("network_scope", json.loads(blocked.stdout)["errors"][0])
            mutated_budget = dict(required, budget=2)
            contract.write_text(json.dumps(mutated_budget), encoding="utf-8")
            budget_blocked = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
            self.assertNotEqual(budget_blocked.returncode, 0)
            self.assertIn("scope_hash", " ".join(json.loads(budget_blocked.stdout)["errors"]))
            adversarial = dict(required, direction="sideways")
            contract.write_text(json.dumps(adversarial), encoding="utf-8")
            self.assertNotEqual(run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract)).returncode, 0)
            adversarial = dict(required, budget=float("nan"))
            contract.write_text(json.dumps(adversarial), encoding="utf-8")
            self.assertNotEqual(run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract)).returncode, 0)
            adversarial = dict(required, data_code_scope=["*"])
            contract.write_text(json.dumps(adversarial), encoding="utf-8")
            self.assertNotEqual(run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract)).returncode, 0)
            adversarial = dict(required, data_code_scope=["."])
            contract.write_text(json.dumps(adversarial), encoding="utf-8")
            root_scope_blocked = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
            self.assertNotEqual(root_scope_blocked.returncode, 0)
            self.assertIn("entire project root", " ".join(json.loads(root_scope_blocked.stdout)["errors"]))
            not_directory = project_root / "not-a-directory"
            not_directory.write_text("file", encoding="utf-8")
            adversarial = dict(required, data_code_scope=["not-a-directory/"])
            contract.write_text(json.dumps(adversarial), encoding="utf-8")
            self.assertNotEqual(run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract)).returncode, 0)
            receipt.write_text(json.dumps({"status": "confirmed", "approved_by": "orchestrator", "user_confirmation": "not-recorded", "validity_status": "clear", "scope_hash": scope_hash, "expires_at": expiry}), encoding="utf-8")
            forged_receipt = dict(required, stage_receipt_sha256="sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest())
            contract.write_text(json.dumps(forged_receipt), encoding="utf-8")
            receipt_blocked = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
            self.assertNotEqual(receipt_blocked.returncode, 0)
            self.assertIn("user_confirmation", " ".join(json.loads(receipt_blocked.stdout)["errors"]))
            far_expiry = (datetime.now(timezone.utc) + timedelta(days=91)).replace(microsecond=0).isoformat()
            receipt.write_text(json.dumps({
                "status": "confirmed", "approved_by": "orchestrator", "user_confirmation": "recorded",
                "validity_status": "clear", "scope_hash": scope_hash, "expires_at": far_expiry,
            }), encoding="utf-8")
            far_contract = dict(required, stage_receipt_sha256="sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest())
            contract.write_text(json.dumps(far_contract), encoding="utf-8")
            far_result = run_script(EXPERIMENT_VALIDATOR, "--contract", str(contract))
            self.assertNotEqual(far_result.returncode, 0)
            self.assertIn("too far in the future", " ".join(json.loads(far_result.stdout)["errors"]))

            # A new project may not have provisioned its approved directory
            # scopes yet.  Directory-like relative scopes are reported as
            # pending (and can be created only after validation); missing file
            # scopes remain fail-closed.
            with tempfile.TemporaryDirectory() as fresh_directory:
                fresh_root = Path(fresh_directory)
                fresh_receipt = fresh_root / "stage_receipt.json"
                fresh_receipt.write_text(json.dumps({
                    "status": "confirmed", "approved_by": "orchestrator",
                    "user_confirmation": "recorded", "validity_status": "clear",
                    "scope_hash": scope_hash, "expires_at": expiry,
                }), encoding="utf-8")
                fresh_contract = fresh_root / "experiment_contract.json"
                fresh_contract.write_text(json.dumps({
                    **required,
                    "stage_receipt": "stage_receipt.json",
                    "stage_receipt_sha256": "sha256:" + hashlib.sha256(fresh_receipt.read_bytes()).hexdigest(),
                }), encoding="utf-8")
                fresh_result = run_script(EXPERIMENT_VALIDATOR, "--contract", str(fresh_contract))
                self.assertEqual(fresh_result.returncode, 0, fresh_result.stdout + fresh_result.stderr)
                self.assertEqual(json.loads(fresh_result.stdout)["scope_paths_pending"], ["data/", "src/"])
                missing_file = dict(required, data_code_scope=["data/input.csv"])
                fresh_contract.write_text(json.dumps(missing_file), encoding="utf-8")
                missing_result = run_script(EXPERIMENT_VALIDATOR, "--contract", str(fresh_contract))
                self.assertNotEqual(missing_result.returncode, 0)
                self.assertIn("does not exist", " ".join(json.loads(missing_result.stdout)["errors"]))

    def test_section_validator_enforces_abstract_late_and_conclusion(self) -> None:
        body = """# Title\n## 1 Introduction\ntext\n## 2 Materials and Methods\ntext\n## 3 Results\ntext with interpretation, application boundary, and limitations\n## 5 Conclusion\ntext\n## References\nrefs\n"""
        with tempfile.TemporaryDirectory() as directory:
            manuscript = Path(directory) / "paper.md"
            manuscript.write_text(body, encoding="utf-8")
            def semantic_receipt(path: Path, paper_type: str = "empirical", discussion_integrated: bool = False) -> Path:
                receipt = path.with_name(path.stem + ".semantic.json")
                receipt_sections = ["introduction", "methods", "discussion", "conclusion"]
                if paper_type == "empirical":
                    receipt_sections.insert(2, "results")
                manuscript_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                verifier_id = "fixture-semantic-verifier"
                verifier = path.with_name(path.stem + ".semantic-verifier.json")
                verifier.write_text(json.dumps({
                    "schema_version": 1, "status": "pass", "verifier_id": verifier_id,
                    "manuscript_sha256": manuscript_hash, "paper_type": paper_type, "language": "en",
                    "method_profile": "method-first", "discussion_integrated": discussion_integrated,
                    "checks": {"discussion_function": "pass", "conclusion_function": "pass", "abstract_consistency": "pass"},
                    "sections": receipt_sections,
                    "evidence_refs": ["fixture-independent-audit"],
                }), encoding="utf-8")
                receipt.write_text(json.dumps({
                    "schema_version": 1, "status": "pass", "paper_type": paper_type, "language": "en",
                    "method_profile": "method-first", "discussion_integrated": discussion_integrated,
                    "validity_status": "clear",
                    "manuscript_sha256": manuscript_hash, "verifier_id": verifier_id,
                    "verifier_receipt_path": verifier.name,
                    "verifier_receipt_sha256": "sha256:" + hashlib.sha256(verifier.read_bytes()).hexdigest(),
                    "discussion_function": "pass", "conclusion_function": "pass",
                    "abstract_consistency": "pass",
                    "evidence_refs": ["fixture-audit"],
                    "sections": receipt_sections,
                }), encoding="utf-8")
                return receipt

            body_result = run_script(SECTION_VALIDATOR, "--file", str(manuscript), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertEqual(body_result.returncode, 0, body_result.stdout + body_result.stderr)
            self.assertFalse(json.loads(body_result.stdout)["warnings"])

            early = manuscript.with_name("early.md")
            early.write_text("## Abstract\ntext\n" + body, encoding="utf-8")
            early_result = run_script(SECTION_VALIDATOR, "--file", str(early), "--phase", "body", "--paper-type", "empirical", "--language", "en")
            self.assertNotEqual(early_result.returncode, 0)
            self.assertIn("only after", early_result.stdout)

            final = manuscript.with_name("final.md")
            final.write_text("# Title\n## Abstract\ntext\n## Keywords\nterms\n" + body.replace("## 5 Conclusion", "## 4 Results and Discussion\ninterpretation, comparison, application boundary, and limitations\n## 5 Conclusion"), encoding="utf-8")
            final_receipt = semantic_receipt(final, discussion_integrated=True)
            final_result = run_script(SECTION_VALIDATOR, "--file", str(final), "--phase", "final", "--paper-type", "empirical", "--language", "en", "--discussion-integrated", "--semantic-receipt", str(final_receipt))
            self.assertEqual(final_result.returncode, 0, final_result.stdout + final_result.stderr)

            malformed_receipt = final.with_name("malformed.semantic.json")
            malformed_payload = json.loads(final_receipt.read_text(encoding="utf-8"))
            malformed_payload["sections"] = None
            malformed_receipt.write_text(json.dumps(malformed_payload), encoding="utf-8")
            malformed_result = run_script(SECTION_VALIDATOR, "--file", str(final), "--phase", "final", "--paper-type", "empirical", "--language", "en", "--discussion-integrated", "--semantic-receipt", str(malformed_receipt))
            self.assertNotEqual(malformed_result.returncode, 0)
            self.assertIn("sections must be a list", malformed_result.stdout)

            blocked_result = run_script(SECTION_VALIDATOR, "--file", str(final), "--phase", "final", "--paper-type", "empirical", "--language", "en", "--validity-status", "blocked", "--discussion-integrated", "--semantic-receipt", str(final_receipt))
            self.assertNotEqual(blocked_result.returncode, 0)

            missing_conclusion = manuscript.with_name("missing-conclusion.md")
            missing_conclusion.write_text(body.replace("## 5 Conclusion\ntext\n", ""), encoding="utf-8")
            missing_result = run_script(SECTION_VALIDATOR, "--file", str(missing_conclusion), "--phase", "body", "--paper-type", "empirical", "--language", "en")
            self.assertNotEqual(missing_result.returncode, 0)
            self.assertIn("missing required section: conclusion", missing_result.stdout)

            combined = manuscript.with_name("combined.md")
            combined.write_text("""# Title\n## Abstract\nclaim\n## Keywords\nterms\n## 1 Introduction\ntext\n## 2 Materials and Methods\nmethod\n## 3 Results and Discussion\ninterpretation, comparison, application boundary, and limitations\n## 5 Conclusion\nanswer\n## References\nrefs\n""", encoding="utf-8")
            combined_receipt = semantic_receipt(combined)
            combined_result = run_script(SECTION_VALIDATOR, "--file", str(combined), "--phase", "final", "--paper-type", "empirical", "--language", "en", "--semantic-receipt", str(combined_receipt))
            self.assertEqual(combined_result.returncode, 0, combined_result.stdout + combined_result.stderr)

            theoretical = manuscript.with_name("theoretical.md")
            theoretical.write_text("""# Title\n## Abstract\nclaim\n## Keywords\nterms\n## 1 Introduction\ntext\n## 2 Materials and Methods\nmethod\n## 4 Analysis\ninterpretation, application boundary, and limitations\n## 5 Conclusion\nanswer\n## References\nrefs\n""", encoding="utf-8")
            theoretical_receipt = semantic_receipt(theoretical, "theoretical", discussion_integrated=True)
            theoretical_result = run_script(SECTION_VALIDATOR, "--file", str(theoretical), "--phase", "final", "--paper-type", "theoretical", "--language", "en", "--discussion-integrated", "--semantic-receipt", str(theoretical_receipt))
            self.assertEqual(theoretical_result.returncode, 0, theoretical_result.stdout + theoretical_result.stderr)

            chinese = manuscript.with_name("chinese.md")
            chinese.write_text("""# 研究标题
## 摘要
摘要内容
## 关键词
时空模型；标准化
## 1 引言
研究背景
## 2 材料与方法
方法内容
## 3 结果
结果内容
## 4 讨论
解释机制，比较前人研究，说明适用范围和局限性
## 5 结论
结论内容
## 参考文献
文献
""", encoding="utf-8")
            chinese_receipt = chinese.with_name("chinese.semantic.json")
            chinese_verifier = chinese.with_name("chinese.semantic-verifier.json")
            chinese_hash = "sha256:" + hashlib.sha256(chinese.read_bytes()).hexdigest()
            chinese_verifier.write_text(json.dumps({
                "schema_version": 1, "status": "pass", "verifier_id": "fixture-cn-verifier",
                "manuscript_sha256": chinese_hash, "paper_type": "empirical", "language": "zh",
                "method_profile": "method-first", "discussion_integrated": False,
                "checks": {"discussion_function": "pass", "conclusion_function": "pass", "abstract_consistency": "pass"},
                "sections": ["introduction", "methods", "results", "discussion", "conclusion"],
                "evidence_refs": ["fixture-cn-independent-audit"],
            }), encoding="utf-8")
            chinese_receipt.write_text(json.dumps({
                "schema_version": 1, "status": "pass", "paper_type": "empirical", "language": "zh",
                "method_profile": "method-first", "discussion_integrated": False,
                "validity_status": "clear",
                "manuscript_sha256": chinese_hash, "verifier_id": "fixture-cn-verifier",
                "verifier_receipt_path": chinese_verifier.name,
                "verifier_receipt_sha256": "sha256:" + hashlib.sha256(chinese_verifier.read_bytes()).hexdigest(),
                "discussion_function": "pass", "conclusion_function": "pass",
                "abstract_consistency": "pass", "evidence_refs": ["fixture-cn"],
                "sections": ["introduction", "methods", "results", "discussion", "conclusion"],
            }), encoding="utf-8")
            chinese_result = run_script(SECTION_VALIDATOR, "--file", str(chinese), "--phase", "final", "--paper-type", "empirical", "--language", "zh", "--semantic-receipt", str(chinese_receipt))
            self.assertEqual(chinese_result.returncode, 0, chinese_result.stdout + chinese_result.stderr)

            no_title = manuscript.with_name("no-title.md")
            no_title.write_text(body.replace("# Title\n", ""), encoding="utf-8")
            no_title_result = run_script(SECTION_VALIDATOR, "--file", str(no_title), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertNotEqual(no_title_result.returncode, 0)
            self.assertIn("missing required title", no_title_result.stdout)

            latex = manuscript.with_suffix(".tex")
            latex.write_text("\\section{Introduction}\n", encoding="utf-8")
            latex_result = run_script(SECTION_VALIDATOR, "--file", str(latex), "--phase", "body", "--paper-type", "empirical", "--language", "en")
            self.assertNotEqual(latex_result.returncode, 0)
            self.assertIn("parses Markdown only", latex_result.stdout)

            intro_only = manuscript.with_name("intro-only-discussion-terms.md")
            intro_only.write_text(
                body.replace(
                    "text with interpretation, application boundary, and limitations",
                    "plain results",
                ).replace(
                    "## 1 Introduction\ntext",
                    "## 1 Introduction\ninterpretation, application boundary, and limitations",
                ),
                encoding="utf-8",
            )
            intro_only_result = run_script(SECTION_VALIDATOR, "--file", str(intro_only), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertNotEqual(intro_only_result.returncode, 0)
            self.assertIn("Discussion function lacks", intro_only_result.stdout)

            empty_discussion = manuscript.with_name("empty-discussion.md")
            empty_discussion.write_text(
                body.replace(
                    "## 3 Results\ntext with interpretation, application boundary, and limitations\n## 5 Conclusion",
                    "## 3 Results\nplain results\n## Discussion\n\n## 5 Conclusion",
                ),
                encoding="utf-8",
            )
            empty_discussion_result = run_script(SECTION_VALIDATOR, "--file", str(empty_discussion), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertNotEqual(empty_discussion_result.returncode, 0)
            self.assertIn("Discussion section has no body content", empty_discussion_result.stdout)

            nested = manuscript.with_name("nested.md")
            nested.write_text(body.replace("text with interpretation, application boundary, and limitations", "### Subanalysis\ninterpretation, application boundary, and limitations"), encoding="utf-8")
            nested_result = run_script(SECTION_VALIDATOR, "--file", str(nested), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertEqual(nested_result.returncode, 0, nested_result.stdout + nested_result.stderr)

            nested_results = manuscript.with_name("nested-results.md")
            nested_results.write_text(body.replace("## 3 Results", "### Results"), encoding="utf-8")
            nested_results_result = run_script(SECTION_VALIDATOR, "--file", str(nested_results), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertNotEqual(nested_results_result.returncode, 0)
            self.assertIn("missing required section: results", nested_results_result.stdout)

            trailing_hashes = manuscript.with_name("trailing-hashes.md")
            trailing_hashes.write_text(body.replace("## 1 Introduction", "## Introduction ##").replace("## 2 Materials and Methods", "## Methods ##").replace("## 3 Results", "## Results ##").replace("## 5 Conclusion", "## Conclusion ##").replace("## References", "## References ##"), encoding="utf-8")
            trailing_result = run_script(SECTION_VALIDATOR, "--file", str(trailing_hashes), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertEqual(trailing_result.returncode, 0, trailing_result.stdout + trailing_result.stderr)

            fenced = manuscript.with_name("fenced.md")
            fenced.write_text(body.replace("text with interpretation, application boundary, and limitations", "```markdown\n## Discussion\ninterpretation, application boundary, and limitations\n```\nplain results"), encoding="utf-8")
            fenced_result = run_script(SECTION_VALIDATOR, "--file", str(fenced), "--phase", "body", "--paper-type", "empirical", "--language", "en", "--discussion-integrated")
            self.assertNotEqual(fenced_result.returncode, 0)
            self.assertIn("Discussion function lacks", fenced_result.stdout)


if __name__ == "__main__":
    unittest.main()
