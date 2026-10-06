"""Bundled installation and manager-mediated figure handoff integration tests."""

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_figure_receipt import FigureReceiptError, build_receipt
from scripts.figure_contract_validator import validate_receipt
from scripts.install_workflow import install, load_manifest, resolve_profile, verify
from scripts.workflow_engine.catalog import discover_skills, load_install_receipts
from scripts.workflow_engine.fs import PathSafetyError
from scripts.workflow_manager import WorkflowService
from tests import test_figure_workflow as figure_fixtures


ROOT = Path(__file__).resolve().parents[1]
NAMES = ("reference-first-figures", "nature-figure")


def fixture(root):
    receipt, values = figure_fixtures.FigureContractTests()._fixture(root)
    for relative, value in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    spec = copy.deepcopy(receipt)
    for field in ("code", "figure_plan", "claim_evidence_matrix", "preview"):
        spec[field].pop("sha256")
    for field in ("source_files", "outputs"):
        for item in spec[field]:
            item.pop("sha256")
    return spec


class ReceiptAdapterTests(unittest.TestCase):
    def test_actual_hashes_and_original_semantics_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = fixture(root)
            receipt = build_receipt(spec, root)
            self.assertEqual(receipt["uncertainty"], spec["uncertainty"])
            self.assertEqual(receipt["visual_review"], spec["visual_review"])
            self.assertNotIn("sha256", spec["code"])
            self.assertEqual(receipt["code"]["sha256"], "sha256:" + hashlib.sha256((root / "figure.py").read_bytes()).hexdigest())
            self.assertEqual(validate_receipt(receipt, root), [])

    def test_absent_human_review_and_unresolved_defects_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = fixture(root)
            for bad in ({}, {**spec["visual_review"], "reviewer_type": "agent"}):
                with self.subTest(review=bad), self.assertRaises(FigureReceiptError):
                    build_receipt({**spec, "visual_review": bad}, root)
            spec["visual_review"]["findings"] = [{"id": "clip", "severity": "major", "summary": "label clipped"}]
            with self.assertRaises(FigureReceiptError):
                build_receipt(spec, root)

    def test_traversal_links_hash_change_and_schematic_misclassification_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = fixture(root)
            for kind in ("schematic", "mixed"):
                with self.subTest(kind=kind), self.assertRaises(FigureReceiptError):
                    build_receipt({**spec, "figure_kind": kind}, root)
            spec["code"]["sha256"] = "sha256:" + "0" * 64
            with self.assertRaises(FigureReceiptError):
                build_receipt(spec, root)
            spec["code"] = {"path": "../outside.py"}
            with self.assertRaises(PathSafetyError):
                build_receipt(spec, root)
            (root / "linked.py").symlink_to(root / "figure.py")
            spec["code"] = {"path": "linked.py"}
            with self.assertRaises(PathSafetyError):
                build_receipt(spec, root)

    def test_cli_returns_candidate_without_writing_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = fixture(root)
            (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/build_figure_receipt.py"),
                "--project-root", str(root), "--spec", "spec.json"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "pass")
            self.assertFalse((root / ".research/figures/F001/figure_receipt.json").exists())

    def test_extreme_numbers_and_deep_nesting_return_structured_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            base = fixture(root)
            numeric = copy.deepcopy(base)
            numeric["target"]["width_mm"] = 10 ** 1000
            nested = copy.deepcopy(base)
            value = "invalid uncertainty"
            for _ in range(600):
                value = [value]
            nested["uncertainty"] = value
            for spec in (numeric, nested):
                with self.subTest(kind="number" if spec is numeric else "nesting"):
                    with self.assertRaises(FigureReceiptError):
                        build_receipt(spec, root)
                    (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
                    result = subprocess.run([sys.executable, str(ROOT / "scripts/build_figure_receipt.py"),
                        "--project-root", str(root), "--spec", "spec.json"], capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(json.loads(result.stdout)["status"], "blocked")
                    self.assertNotIn("Traceback", result.stderr + result.stdout)


class FigureSkillDistributionTests(unittest.TestCase):
    def test_profiles_and_bundled_resource_links(self):
        manifest = load_manifest(ROOT / "dependencies.lock.json")
        for profile in ("standard", "full"):
            self.assertTrue(set(NAMES) <= {x["name"] for x in resolve_profile(manifest, profile)})
        self.assertTrue(set(NAMES).isdisjoint({x["name"] for x in resolve_profile(manifest, "core")}))
        for name in NAMES:
            folder = ROOT / "companion-skills" / name
            self.assertTrue((folder / "LICENSE").is_file())
            for file in folder.rglob("*.md"):
                text = file.read_text(encoding="utf-8")
                self.assertNotIn("/Users/", text)
                for link in re.findall(r"\]\(([^)]+)\)", text):
                    if "://" in link or link.startswith("#"):
                        continue
                    target = link.split("#")[0]
                    self.assertTrue((file.parent / target).exists(), f"missing link: {file}: {link}")

    def test_clean_bundled_install_is_receipted_and_runs_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / "project"
            project.mkdir()
            target = project / ".agents/skills"
            manifest = load_manifest(ROOT / "dependencies.lock.json")
            # Exercise the real installer on the bundled subset without
            # downloading unrelated remote writing/experiment dependencies.
            selected = ["paper-workflow-orchestrator", *NAMES]
            mini = {**manifest, "profiles": {p: selected for p in ("core", "standard", "full")},
                    "skills": {name: manifest["skills"][name] for name in selected}}
            install(mini, "standard", target, ROOT)
            self.assertEqual(verify(target, "standard", mini)["status"], "pass")
            installed = target / "paper-workflow-orchestrator"
            self.assertTrue((installed / "scripts/build_figure_receipt.py").is_file())
            self.assertTrue((installed / "references/workflows/reference-led-figure.custom.json").is_file())
            catalog = discover_skills((target,), load_install_receipts((target,)))
            self.assertTrue(all(catalog.skills[name].locked for name in NAMES))
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "empty-codex")}):
                service = WorkflowService(project)
                workflow = json.loads((ROOT / "references/workflows/reference-led-figure.custom.json").read_text(encoding="utf-8"))
                validation = service.validate_document(workflow)
                self.assertEqual(validation["status"], "pass", validation)
                self.assertNotIn("risk.control_removed.figure", validation["required_warning_codes"])
                service.activate(workflow, acknowledged_warning_codes=validation["required_warning_codes"])

                spec = fixture(project)
                (project / "initial-spec.json").write_text(json.dumps(spec))
                builder = subprocess.run([sys.executable, str(installed / "scripts/build_figure_receipt.py"),
                    "--project-root", str(project), "--spec", "initial-spec.json"], capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(builder.returncode, 0, builder.stdout + builder.stderr)
                self.assertEqual(json.loads(builder.stdout)["status"], "pass")
                inputs = {"figure_plan": ".research/figure_plan.yml", "claim_evidence_matrix": ".research/claim_evidence_matrix.yml",
                          "source_data": "data.csv", "reference_image": "figure.png", "reference_notes": "references.md"}
                (project / "references.md").write_text("Synthetic test reference; backend Python; 89 mm.", encoding="utf-8")
                for key, path in inputs.items():
                    service.register_artifact(key, path, "Synthetic integration fixture.")

                def submit(node, outputs):
                    invocation = service.claim(node)
                    self.assertEqual(Path(invocation["skill_root"]), target)
                    return service.submit_result({"schema_version": "node-result-v2",
                        "run_id": invocation["run_id"], "node_id": node, "attempt": invocation["attempt"],
                        "idempotency_token": invocation["idempotency_token"], "status": "succeeded",
                        "outcome": "succeeded", "summary": "Synthetic fixture step.", "uncertainties": [],
                        "artifacts": [{"id": k, "path": v} for k, v in outputs.items()],
                        "consumed_sources": [{k: source[k] for k in ("id", "path", "sha256")}
                                             for source in invocation["input_artifacts"]]})

                (project / "design.md").write_text("Read-only synthetic design record; Python, 89 mm.", encoding="utf-8")
                submit("reference-design", {"design_notes": "design.md"})
                (project / "qa-spec.json").write_text(json.dumps(spec), encoding="utf-8")
                submit("figure-implementation", {"plot_code": "figure.py", "figure_vector": "figure.pdf",
                      "figure_preview": "figure-preview.png", "qa_spec": "qa-spec.json"})
                self.assertEqual(service.ready()["ready"], [])
                gate = next(x for x in service.summary()["nodes"] if x["node_id"] == "figure-approval")
                self.assertEqual(gate["approval_state"], "awaiting_confirmation")
                (project / "human-review.json").write_text(json.dumps(spec["visual_review"]), encoding="utf-8")
                service.register_artifact("human_review", "human-review.json", "Synthetic human-review fixture, not real publication acceptance.")
                service.record_approval("figure-approval", "approve", "Synthetic test approval.")
                self.assertEqual(service.ready()["ready"][0]["node_id"], "reference-review")
                receipt = build_receipt(spec, project)
                receipt_path = project / ".research/figures/F001/figure_receipt.json"
                receipt_path.parent.mkdir(parents=True)
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                (project / "review.md").write_text("Synthetic review; same design record, frozen files.", encoding="utf-8")
                stage = submit("reference-review", {"review_notes": "review.md", "figure_receipt": ".research/figures/F001/figure_receipt.json"})
                self.assertEqual(stage["schema_version"], "stage-receipt-v3")
                validator = service.run_validator("figure-check")
                self.assertEqual(validator["outcome"], "pass", validator)
                self.assertEqual(service.summary()["progress"]["phase"], "finished")


if __name__ == "__main__":
    unittest.main()
