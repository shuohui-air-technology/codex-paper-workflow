"""Pinned installation and manager-mediated figure handoff integration tests."""

import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
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
    def test_profiles_sources_and_bundled_resource_links(self):
        manifest = load_manifest(ROOT / "dependencies.lock.json")
        for profile in ("standard", "full"):
            self.assertTrue(set(NAMES) <= {x["name"] for x in resolve_profile(manifest, profile)})
        self.assertTrue(set(NAMES).isdisjoint({x["name"] for x in resolve_profile(manifest, "core")}))
        external = manifest["skills"]["nature-figure"]
        self.assertEqual(external["source"], "github")
        self.assertEqual(external["repository"], "Yuan1z0825/nature-skills")
        self.assertEqual(external["commit"], "f3941a1722e39af78b24bc7a34167b8880629545")
        self.assertEqual(external["path"], "skills/nature-figure")
        self.assertEqual(external["license"], "MIT")
        self.assertFalse((ROOT / "companion-skills/nature-figure/SKILL.md").exists())
        for name in ("reference-first-figures",):
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

    def test_adapter_is_reachable_without_modifying_the_upstream_skill(self):
        adapter = ROOT / "references/figure-implementation-adapter.md"
        self.assertTrue(adapter.is_file())
        for name in ("SKILL.md", "references/reference-led-figures.md", "references/custom-workflow-contract.md"):
            self.assertIn("figure-implementation-adapter.md", (ROOT / name).read_text(encoding="utf-8"))
        for name in ("README.md", "README.zh-CN.md"):
            self.assertNotIn("companion-skills/nature-figure", (ROOT / name).read_text(encoding="utf-8"))

    def test_studio_e2e_fixture_skills_ship_in_the_repository(self):
        """The Studio E2E fixture copies skills by id; every id must exist here.

        A fresh checkout has no empty directories, so a removed skill makes the
        fixture fail before any browser test runs. A local run can still pass
        while stale directories remain, which is why this is checked directly.
        """
        fixture = (ROOT / "studio/e2e/fixtures.ts").read_text(encoding="utf-8")
        listed = re.search(r"for \(const skillId of \[([^\]]*)\]\)", fixture)
        self.assertIsNotNone(listed, "the Studio E2E fixture no longer declares its bundled skills")
        names = re.findall(r"'([^']+)'", listed.group(1))
        self.assertTrue(names, "the Studio E2E fixture declares no skills")
        for name in names:
            folder = ROOT / "companion-skills" / name
            self.assertTrue(
                (folder / "SKILL.md").is_file(),
                f"the Studio E2E fixture copies a skill that does not ship in this repository: {name}",
            )

    def test_managed_bundled_copy_updates_to_pinned_dependency_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "old-source"
            source.mkdir()
            old_skill = "---\nname: nature-figure\ndescription: Old bundled fixture.\n---\n"
            (source / "SKILL.md").write_text(old_skill)
            (source / "workflow-handoff.md").write_text("Obsolete bundled integration.")
            target = root / "skills"
            old = {"schema_version": "workflow-dependencies-v1",
                   "profiles": {p: ["nature-figure"] for p in ("core", "standard", "full")},
                   "skills": {"nature-figure": {"source": "bundled", "path": "old-source", "license": "MIT"}}}
            install(old, "standard", target, root)
            pinned = load_manifest(ROOT / "dependencies.lock.json")["skills"]["nature-figure"]
            new = {**old, "skills": {"nature-figure": pinned}}
            self.assertEqual(verify(target, "standard", new)["status"], "blocked")
            upstream = "---\nname: nature-figure\ndescription: Pinned external fixture.\n---\n"
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("snapshot/skills/nature-figure/SKILL.md", upstream)
                bundle.writestr("snapshot/LICENSE", "MIT License\nSynthetic upstream fixture\n")
            with mock.patch("scripts.install_workflow.urllib.request.urlopen", return_value=io.BytesIO(archive.getvalue())):
                result = install(new, "standard", target, root, update=True)
            self.assertEqual(verify(target, "standard", new)["status"], "pass")
            backup = Path(result["backups"]["nature-figure"])
            self.assertEqual((backup / "SKILL.md").read_text(), old_skill)
            self.assertTrue((backup / "workflow-handoff.md").is_file())
            self.assertEqual((target / "nature-figure/SKILL.md").read_text(), upstream)
            self.assertFalse((target / "nature-figure/workflow-handoff.md").exists())

    def test_clean_pinned_install_is_receipted_and_runs_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / "project"
            project.mkdir()
            target = project / ".agents/skills"
            manifest = load_manifest(ROOT / "dependencies.lock.json")
            # The HTTP fixture makes the pinned dependency deterministic and
            # offline. Archive parsing, installation and receipt checks are real.
            selected = ["paper-workflow-orchestrator", *NAMES]
            mini = {**manifest, "profiles": {p: selected for p in ("core", "standard", "full")},
                    "skills": {name: manifest["skills"][name] for name in selected}}
            archive = io.BytesIO()
            upstream_skill = "---\nname: nature-figure\ndescription: Synthetic external fixture.\n---\n\n# Fixture\n"
            upstream_license = "MIT License\nCopyright (c) 2026 Synthetic upstream fixture\n"
            with zipfile.ZipFile(archive, "w") as bundle:
                prefix = "nature-skills-" + manifest["skills"]["nature-figure"]["commit"] + "/"
                bundle.writestr(prefix + "LICENSE", upstream_license)
                bundle.writestr(prefix + "skills/nature-figure/SKILL.md", upstream_skill)
            with mock.patch("scripts.install_workflow.urllib.request.urlopen", return_value=io.BytesIO(archive.getvalue())) as opened:
                install(mini, "standard", target, ROOT)
            self.assertEqual(opened.call_count, 1)
            self.assertIn(manifest["skills"]["nature-figure"]["commit"], opened.call_args.args[0])
            self.assertEqual((target / "nature-figure/SKILL.md").read_text(), upstream_skill)
            self.assertEqual((target / "nature-figure/UPSTREAM-LICENSE").read_text(), upstream_license)
            self.assertFalse((target / "nature-figure/references/workflow-handoff.md").exists())
            self.assertEqual(verify(target, "standard", mini)["status"], "pass")
            installed = target / "paper-workflow-orchestrator"
            self.assertTrue((installed / "scripts/build_figure_receipt.py").is_file())
            self.assertTrue((installed / "references/workflows/reference-led-figure.custom.json").is_file())
            self.assertTrue((installed / "references/figure-implementation-adapter.md").is_file())
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
