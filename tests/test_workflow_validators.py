"""Fixed custom-validator adapter and process-boundary tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.workflow_engine.catalog import load_validator_registry
from scripts.workflow_engine.compiler import CompiledNode
from scripts.workflow_engine.validators import (
    ValidatorError,
    _capture_bounded,
    build_validator_argv,
    normalize_validator_output,
    run_validator,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "references/workflows/validator-registry.v1.json"
PAPER_OPTIONS = {
    "phase": "body", "paper_type": "empirical", "language": "en",
    "method_profile": "method-first", "validity_status": "clear",
    "discussion_integrated": False,
}


class ValidatorAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = load_validator_registry(REGISTRY, ROOT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        (self.project / "input.json").write_text("{}", encoding="utf-8")
        (self.project / "paper.md").write_text(
            "# A paper\n## Introduction\nBackground.\n## Methods\nProcedure.\n"
            "## Results\nFindings.\n## Discussion\nThe mechanism explains scope and limitations.\n"
            "## Conclusion\nConclusion.\n", encoding="utf-8",
        )

    def node(self, validator_id, roles, options=None, *, identity=None):
        return CompiledNode(
            id="check", type="validator", entry=True, skill=None,
            validator=identity or self.registry[validator_id],
            validator_config={"input_roles": roles, "options": options if options is not None else {}},
            inputs=tuple(roles.values()), outputs=(), outcomes=("pass", "fail", "blocked"),
            write_scopes=(), failure_policy="block", condition_cases=(), join_mode="",
        )

    def claims(self, **ids_to_paths):
        return tuple({"id": artifact_id, "source_id": artifact_id,
                      "path": relative, "sha256": hashlib.sha256((self.project / relative).read_bytes()).hexdigest()}
                     for artifact_id, relative in ids_to_paths.items())

    def test_exact_argv_for_four_adapters(self):
        cases = (
            ("experiment-contract", {"contract": "a"}, {"a": "input.json"},
             ["--contract", str(self.project / "input.json")]),
            ("figure-contract", {"receipt": "a"}, {"a": "input.json"},
             ["--receipt", str(self.project / "input.json"), "--project-root", str(self.project)]),
            ("final-edit-receipt", {"receipt": "a"}, {"a": "input.json"},
             ["--receipt", str(self.project / "input.json")]),
            ("paper-section", {"file": "a"}, {"a": "paper.md"},
             ["--file", str(self.project / "paper.md"), "--phase", "body", "--paper-type", "empirical",
              "--language", "en", "--method-profile", "method-first", "--validity-status", "clear"]),
        )
        for validator_id, roles, paths, expected in cases:
            with self.subTest(validator_id=validator_id):
                node = self.node(validator_id, roles, PAPER_OPTIONS if validator_id == "paper-section" else {})
                self.assertEqual(build_validator_argv(node, self.claims(**paths), self.project, ROOT),
                                 (sys.executable, str(self.registry[validator_id].script), *expected))

    def test_paper_true_and_final_flags(self):
        (self.project / "semantic.json").write_text("{}", encoding="utf-8")
        options = {**PAPER_OPTIONS, "phase": "final", "discussion_integrated": True}
        node = self.node("paper-section", {"file": "a", "semantic_receipt": "b"}, options)
        argv = build_validator_argv(node, self.claims(a="paper.md", b="semantic.json"), self.project, ROOT)
        self.assertEqual(argv[-3:], ("--discussion-integrated", "--semantic-receipt", str(self.project / "semantic.json")))

    def test_reject_bad_roles_options_claims_and_identity(self):
        valid = self.node("experiment-contract", {"contract": "a"})
        bad_nodes = (
            self.node("experiment-contract", {"contract": "a", "extra": "b"}),
            self.node("experiment-contract", {}),
            self.node("experiment-contract", {"contract": "a"}, {"arbitrary_flag": "yes"}),
            self.node("paper-section", {"file": "a"}, {}),
            self.node("paper-section", {"file": "a"}, {**PAPER_OPTIONS, "phase": "final"}),
        )
        for node in bad_nodes:
            with self.assertRaises(ValidatorError):
                build_validator_argv(node, self.claims(a="input.json"), self.project, ROOT)
        bad_claim = ({**self.claims(a="input.json")[0], "sha256": "0" * 64},)
        with self.assertRaises(ValidatorError):
            build_validator_argv(valid, bad_claim, self.project, ROOT)
        with self.assertRaises(ValidatorError):
            build_validator_argv(valid, self.claims(a="input.json") + self.claims(b="input.json"), self.project, ROOT)
        with self.assertRaises(ValidatorError):
            build_validator_argv(valid, ({**self.claims(a="input.json")[0], "path": self.project / "input.json"},), self.project, ROOT)
        with self.assertRaises(ValidatorError):
            build_validator_argv(self.node("humanizer-preflight", {}), (), self.project, ROOT)
        with self.assertRaises(ValidatorError):
            build_validator_argv(self.node("bogus", {}, identity=self.registry["experiment-contract"]), (), self.project, ROOT)

    def test_real_script_positive_and_negative_shapes(self):
        for validator_id, role in (("experiment-contract", "contract"),
                                   ("figure-contract", "receipt"), ("final-edit-receipt", "receipt")):
            with self.subTest(validator_id=validator_id):
                result = run_validator(self.node(validator_id, {role: "a"}),
                                       self.claims(a="input.json"), self.project, ROOT)
                self.assertEqual(result.outcome, "blocked")
        paper = run_validator(self.node("paper-section", {"file": "a"}, PAPER_OPTIONS),
                              self.claims(a="paper.md"), self.project, ROOT)
        self.assertEqual(paper.outcome, "pass")
        (self.project / "paper.md").write_text("# A paper\n", encoding="utf-8")
        failed = run_validator(self.node("paper-section", {"file": "a"}, PAPER_OPTIONS),
                               self.claims(a="paper.md"), self.project, ROOT)
        self.assertEqual(failed.outcome, "fail")

    def test_real_experiment_and_figure_pass(self):
        from scripts.experiment_contract_validator import _scope_hash
        from tests.test_figure_workflow import FigureContractTests

        contract = {
            "objective": "Improve the measured response", "metric": "accuracy", "direction": "maximize",
            "baseline": "current model", "budget": 100, "max_runs": 2, "max_wall_time": 60,
            "stop_conditions": ["after two runs"], "data_code_scope": ["data/"],
            "network_scope": "none", "write_and_commit_policy": "no_auto_commit",
            "report_destination": "local-only", "validation_status": "pass",
            "approved_by": "orchestrator", "user_confirmation": "recorded",
            "stage_receipt": "stage.json", "stage_receipt_sha256": "pending", "validity_status": "clear",
        }
        stage = {"status": "confirmed", "approved_by": "orchestrator",
                 "user_confirmation": "recorded", "validity_status": "clear",
                 "scope_hash": _scope_hash(contract),
                 "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        stage_raw = json.dumps(stage).encode()
        (self.project / "stage.json").write_bytes(stage_raw)
        contract["stage_receipt_sha256"] = "sha256:" + hashlib.sha256(stage_raw).hexdigest()
        (self.project / "input.json").write_text(json.dumps(contract), encoding="utf-8")
        experiment = run_validator(self.node("experiment-contract", {"contract": "a"}),
                                   self.claims(a="input.json"), self.project, ROOT)
        self.assertEqual(experiment.outcome, "pass", experiment)

        receipt, files = FigureContractTests()._fixture(self.project)
        for relative, raw in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        figure_path = self.project / ".research/figures/F001/figure_receipt.json"
        figure_path.parent.mkdir(parents=True, exist_ok=True)
        figure_path.write_text(json.dumps(receipt), encoding="utf-8")
        figure = run_validator(self.node("figure-contract", {"receipt": "a"}),
                               self.claims(a=".research/figures/F001/figure_receipt.json"), self.project, ROOT)
        self.assertEqual(figure.outcome, "pass", figure)

    def test_final_edit_real_invalid_mode_is_domain_blocked(self):
        (self.project / "input.json").write_text(json.dumps({"mode": "unsupported"}), encoding="utf-8")
        result = run_validator(self.node("final-edit-receipt", {"receipt": "a"}),
                               self.claims(a="input.json"), self.project, ROOT)
        self.assertEqual(result.outcome, "blocked", result)
        node = self.node("final-edit-receipt", {"receipt": "a"})
        paths = {"receipt": self.project / "input.json"}
        for mode in ("revise", "audit", "learn"):
            normal_pass = {"status": "pass", "receipt": str(paths["receipt"]), "mode": mode, "errors": []}
            self.assertEqual(normalize_validator_output(node, 0, json.dumps(normal_pass).encode(), paths).outcome, "pass")

    def test_real_final_edit_learn_pass(self):
        from scripts.final_edit_receipt_validator import CHECKS, calc_scope

        def write(name, value):
            path = self.project / name
            raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            path.write_bytes(raw)
            return "sha256:" + hashlib.sha256(raw).hexdigest()

        canonical = self.project / "canonical.md"
        canonical_sha = write("canonical.md", "# Manuscript\nText.\n")
        editor_sha = write("editor.md", "---\nname: academic-manuscript-final-editor\nmetadata:\n  version: 2.1.0\n  capability_schema: final-editor-v1\n---\n")
        checks = {name: "pass" for name in CHECKS}
        scope = calc_scope(canonical_sha, ["canonical.md"], ["all"], "learn")
        integrity_sha = write("integrity.json", {"status": "pass", "validity_status": "clear", "canonical_sha256": canonical_sha})
        auth_sha = write("auth.json", {"status": "confirmed", "approved_by": "user", "canonical_sha256": canonical_sha,
                                       "scope_hash": scope, "mode": "learn",
                                       "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        scanner_sha = write("scanner.json", {"schema_version": 1, "scanner_version": "test-v1", "finding_count": 0,
                                             "files": [{"path": str(canonical), "sha256_before": canonical_sha,
                                                        "sha256_after": canonical_sha, "unchanged": True}]})
        ledger_sha = write("ledger.json", {"rule_count": 1, "rules": ["style"]})
        dispositions_sha = write("dispositions.json", {"status": "pass", "scanner_report_sha256": scanner_sha,
                                                       "finding_count": 0, "disposed_count": 0})
        script = ROOT / "scripts/final_edit_receipt_validator.py"
        verifier_sha = write("verifier.json", {"status": "pass", "canonical_sha256": canonical_sha,
                                                 "verifier_id": "test-verifier", "evidence_refs": ["test"],
                                                 "implementation_path": str(script), "implementation_version": "protected-content-v1",
                                                 "implementation_sha256": "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest(),
                                                 "protected_checks": checks})
        stage_sha = write("stage.json", {"status": "confirmed", "approved_by": "orchestrator",
                                           "validity_status": "clear", "canonical_sha256": canonical_sha,
                                           "scope_hash": scope, "integrity_report_sha256": integrity_sha,
                                           "editor_skill_sha256": editor_sha, "editor_capability_schema": "final-editor-v1",
                                           "editor_version": "2.1.0"})
        receipt = {
            "schema_version": 1, "mode": "learn", "validation_status": "pass", "validity_status": "clear",
            "canonical_mutation_allowed": False, "canonical_sha256": canonical_sha,
            "authorized_paths": ["canonical.md"], "authorized_sections": ["all"], "scope_hash": scope,
            "apply_decision": "pending", "citation_numbering_policy": "preserve",
            "bilingual_parity_status": "not_applicable", "style_evidence_refs": ["test"],
            "protected_checks": checks, "editor_capability_schema": "final-editor-v1", "editor_version": "2.1.0",
        }
        bindings = {
            "canonical": ("canonical.md", canonical_sha), "scanner_report": ("scanner.json", scanner_sha),
            "stage_receipt": ("stage.json", stage_sha), "integrity_report": ("integrity.json", integrity_sha),
            "edit_authorization": ("auth.json", auth_sha), "editor_skill": ("editor.md", editor_sha),
            "author_style_ledger": ("ledger.json", ledger_sha),
            "finding_dispositions": ("dispositions.json", dispositions_sha),
            "protected_verifier_receipt": ("verifier.json", verifier_sha),
        }
        for name, (path, sha) in bindings.items():
            receipt[name + "_path"] = path
            receipt[name + "_sha256"] = sha
        write("input.json", receipt)
        result = run_validator(self.node("final-edit-receipt", {"receipt": "a"}),
                               self.claims(a="input.json"), self.project, ROOT)
        self.assertEqual(result.outcome, "pass", result)

    def test_identity_symlink_and_path_tamper(self):
        node = self.node("experiment-contract", {"contract": "a"})
        changed = replace(node.validator, sha256="0" * 64)
        with self.assertRaises(ValidatorError) as caught:
            build_validator_argv(replace(node, validator=changed), self.claims(a="input.json"), self.project, ROOT)
        self.assertEqual(caught.exception.code, "validator.identity_changed")
        moved = replace(node.validator, script=self.project / "input.json")
        with self.assertRaises(ValidatorError):
            build_validator_argv(replace(node, validator=moved), self.claims(a="input.json"), self.project, ROOT)
        (self.project / "link.json").symlink_to(self.project / "input.json")
        with self.assertRaises(ValidatorError):
            build_validator_argv(node, self.claims(a="input.json")[:-1] +
                                 ({"id": "a", "source_id": "a", "path": "link.json", "sha256": hashlib.sha256(b"{}").hexdigest()},),
                                 self.project, ROOT)

    def test_paper_classifier_boundaries(self):
        normal = {"valid": False, "errors": ["missing required title heading"],
                  "warnings": [], "sections": [], "file": str(self.project / "paper.md")}
        node = self.node("paper-section", {"file": "a"}, PAPER_OPTIONS)
        paths = {"file": self.project / "paper.md"}
        self.assertEqual(normalize_validator_output(node, 1, json.dumps(normal).encode(), paths).outcome, "fail")
        normal["errors"] = ["missing required title heading", "validity_status: blocked prevents section advancement"]
        self.assertEqual(normalize_validator_output(node, 1, json.dumps(normal).encode(), paths).outcome, "blocked")
        normal["errors"] = ["unexpected new rule"]
        self.assertIsNone(normalize_validator_output(node, 1, json.dumps(normal).encode(), paths).outcome)
        normal["errors"] = ["missing required title heading"]
        for rc, raw in ((0, json.dumps(normal).encode()), (1, b'{"valid":false,"valid":false}'),
                        (1, json.dumps(normal).encode() + b" trailing"),
                        (1, b'{"valid":false,"errors":[NaN]}'),
                        (2, json.dumps(normal).encode()), (1, b"")):
            self.assertIsNone(normalize_validator_output(node, rc, raw, paths).outcome)

    def test_real_final_paper_nested_verifier_path_is_domain_blocked(self):
        manuscript = (
            "# A paper\n## Abstract\nSummary.\n## Introduction\nBackground.\n"
            "## Methods\nProcedure.\n## Results\nFindings.\n"
            "## Discussion\nThe mechanism explains scope and limitations.\n"
            "## Conclusion\nConclusion.\n## References\nReference.\n"
        )
        (self.project / "paper.md").write_bytes(manuscript.encode("utf-8"))
        receipt = {
            "status": "pass", "schema_version": 1, "verifier_id": "independent-checker",
            "verifier_receipt_path": "../outside.json", "verifier_receipt_sha256": "sha256:" + "0" * 64,
            "paper_type": "empirical", "language": "en", "method_profile": "method-first",
            "discussion_integrated": False, "validity_status": "clear",
            "manuscript_sha256": "sha256:" + hashlib.sha256(manuscript.encode()).hexdigest(),
            "discussion_function": "pass", "conclusion_function": "pass", "abstract_consistency": "pass",
            "evidence_refs": ["source-1"],
            "sections": ["introduction", "methods", "results", "discussion", "conclusion"],
        }
        encoded_receipt = json.dumps(receipt).replace("../outside.json", "\\u002e\\u002e/outside.json")
        (self.project / "semantic.json").write_text(encoded_receipt, encoding="utf-8")
        self.assertEqual(json.loads(encoded_receipt)["verifier_receipt_path"], "../outside.json")
        options = {**PAPER_OPTIONS, "phase": "final"}
        node = self.node("paper-section", {"file": "paper", "semantic_receipt": "semantic"}, options)
        claims = self.claims(paper="paper.md", semantic="semantic.json")
        argv = build_validator_argv(node, claims, self.project, ROOT)
        rc, stdout, _stderr = _capture_bounded(argv, self.project)
        self.assertEqual(rc, 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["errors"], ["semantic verifier receipt must be a safe relative path"])
        self.assertEqual(run_validator(node, claims, self.project, ROOT).outcome, "blocked")
        paths = {"file": self.project / "paper.md", "semantic_receipt": self.project / "semantic.json"}
        for message in ("semantic verifier receipt must be a non-empty relative path",
                        "semantic verifier receipt must be a safe relative path",
                        "semantic verifier receipt resolves outside its receipt directory"):
            payload["errors"] = ["missing required title heading", message]
            self.assertEqual(normalize_validator_output(node, 1, json.dumps(payload).encode(), paths).outcome,
                             "blocked")
        payload["errors"] = ["new unrelated validator message"]
        self.assertIsNone(normalize_validator_output(node, 1, json.dumps(payload).encode(), paths).outcome)

    def test_result_path_binding_and_status_contradiction(self):
        mappings = (
            ("experiment-contract", {"contract": "a"}, {"contract": str(self.project / "other.json"),
                                     "status": "blocked", "errors": ["invalid"]}, "contract"),
            ("final-edit-receipt", {"receipt": "a"}, {"receipt": str(self.project / "other.json"),
                                      "mode": "learn", "status": "blocked", "errors": ["invalid"]}, "receipt"),
        )
        for validator_id, roles, payload, role in mappings:
            node = self.node(validator_id, roles)
            paths = {role: self.project / "input.json"}
            self.assertIsNone(normalize_validator_output(node, 1, json.dumps(payload).encode(), paths).outcome)
            payload[role] = str(paths[role])
            self.assertIsNone(normalize_validator_output(node, 0, json.dumps(payload).encode(), paths).outcome)
        figure = self.node("figure-contract", {"receipt": "a"})
        paths = {"receipt": self.project / "input.json"}
        self.assertIsNone(normalize_validator_output(figure, 1, b'{"status":"pass","errors":[]}', paths).outcome)
        self.assertIsNone(normalize_validator_output(figure, 0, b'{"status":"blocked","errors":["x"]}', paths).outcome)

    def test_stream_limits_timeout_and_process_cleanup(self):
        started = []
        original_popen = subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.append(process)
            return process

        noisy = [sys.executable, "-c", "import sys;sys.stdout.write('x'*1100000)"]
        with patch("scripts.workflow_engine.validators.subprocess.Popen", side_effect=tracked_popen):
            with self.assertRaises(ValidatorError) as caught:
                _capture_bounded(noisy, self.project, timeout=5)
        self.assertEqual(caught.exception.code, "validator.output_limit")
        noisy[2] = "import sys;sys.stderr.write('x'*1100000)"
        with patch("scripts.workflow_engine.validators.subprocess.Popen", side_effect=tracked_popen):
            with self.assertRaises(ValidatorError) as caught:
                _capture_bounded(noisy, self.project, timeout=5)
        self.assertEqual(caught.exception.code, "validator.output_limit")
        with patch("scripts.workflow_engine.validators.subprocess.Popen", side_effect=tracked_popen):
            with self.assertRaises(ValidatorError) as caught:
                _capture_bounded([sys.executable, "-c", "import time;time.sleep(5)"], self.project, timeout=.1)
        self.assertEqual(caught.exception.code, "validator.timeout")
        self.assertEqual(len(started), 3)
        for process in started:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_one_byte_over_cap_stops_live_child_promptly_on_each_stream(self):
        started = []
        original_popen = subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.append(process)
            return process

        for fd in (1, 2):
            with self.subTest(stream=fd):
                command = [sys.executable, "-c",
                           f"import os,time;os.write({fd},b'x'*1048577);time.sleep(5)"]
                start = time.monotonic()
                with patch("scripts.workflow_engine.validators.subprocess.Popen", side_effect=tracked_popen):
                    with self.assertRaises(ValidatorError) as caught:
                        _capture_bounded(command, self.project, timeout=3)
                elapsed = time.monotonic() - start
                self.assertEqual(caught.exception.code, "validator.output_limit")
                self.assertLess(elapsed, 1.5, "output cap must stop a live child before the deadline")
                self.assertIsNotNone(started[-1].poll())
                self.assertTrue(started[-1].stdout.closed and started[-1].stderr.closed)

    def test_stderr_only_and_crash_are_execution_failures(self):
        figure = self.node("figure-contract", {"receipt": "a"})
        paths = {"receipt": self.project / "input.json"}
        self.assertIsNone(normalize_validator_output(figure, 1, b"", paths).outcome)
        self.assertIsNone(normalize_validator_output(figure, -9, b'{"status":"blocked","errors":["x"]}', paths).outcome)

    def test_humanizer_has_zero_subprocess(self):
        with patch("subprocess.Popen") as popen:
            with self.assertRaises(ValidatorError):
                run_validator(self.node("humanizer-preflight", {}), (), self.project, ROOT)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
