import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


class ProgressVersionTests(unittest.TestCase):
    def test_current_workflow_version_is_v10(self):
        from scripts import progress_manager as progress

        self.assertEqual(progress.CURRENT_WORKFLOW_VERSION, "paper-workflow-orchestrator-v1.0")
        document = progress.template("version-test")
        self.assertIn("workflow_version: paper-workflow-orchestrator-v1.0", document)
        self.assertTrue(progress.validate_text(document)["valid"])

    def test_migrate_legacy_versions_to_v10_keeps_legacy_backups(self):
        from scripts import progress_manager as progress

        fixtures = {
            "v0.2": ("intake", "pending"),
            "v0.3": ("drafting", "pending"),
            "v0.4": ("literature", "pending"),
            "v0.5": ("design", "pending"),
            "v0.6": ("experiments", "blocked"),
        }
        for suffix, (expected_stage, expected_validity) in fixtures.items():
            legacy_version = f"paper-workflow-orchestrator-{suffix}"
            with self.subTest(legacy_version=legacy_version), TemporaryDirectory() as tmp:
                path = Path(tmp) / "progress.md"
                fixture = ROOT / "tests" / "fixtures" / f"progress_{suffix}.md"
                legacy = fixture.read_text(encoding="utf-8")
                path.write_text(legacy, encoding="utf-8")
                args = types.SimpleNamespace(
                    file=str(path),
                    mode="guided_idea",
                    current_stage=expected_stage if suffix == "v0.3" else "",
                    confirm=True,
                )

                self.assertEqual(progress.cmd_migrate(args), 0)
                migrated = progress.read_text(path)
                self.assertIn("workflow_version: paper-workflow-orchestrator-v1.0", migrated)
                self.assertIn("migrated progress state to v1.0", migrated)
                self.assertIn(f"project_id: legacy-{suffix}-fixture", migrated)
                self.assertIn(f"legacy {suffix} milestone", migrated)
                self.assertIn(f"validity_status: {expected_validity}", migrated)
                self.assertTrue(Path(str(path) + f".legacy-{suffix}").is_file())
                self.assertTrue(progress.validate_text(migrated)["valid"])

    def test_public_docs_identify_v10_as_current_workflow_version(self):
        for relative in (
            "README.md",
            "README.zh-CN.md",
            "DEVELOPMENT_GUIDE.md",
            "references/progress-schema.md",
            "CHANGELOG.md",
        ):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("v1.0", text)
        self.assertIn("v1.0.0", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn("v1.0", (ROOT / "companion-skills" / "academic-manuscript-final-editor" / "README.md").read_text(encoding="utf-8"))
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('version: "1.0.0"', skill)
        self.assertIn('workflow_version: "paper-workflow-orchestrator-v1.0"', skill)

    def test_public_readmes_do_not_advertise_ai_handoff(self):
        for relative in ("README.md", "README.zh-CN.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8").lower()
                self.assertNotIn("handoff", text)
                self.assertNotIn("交接", text)

    def test_public_readmes_use_v10_release_subtitles(self):
        self.assertIn(
            "**Helping you turn any vague idea into a paper built to top-journal standards.**",
            (ROOT / "README.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "**帮助您将任何一个模糊的想法落地为顶刊级别的论文。**",
            (ROOT / "README.zh-CN.md").read_text(encoding="utf-8"),
        )

    def test_development_guide_is_a_complete_entrypoint(self):
        guide = (ROOT / "DEVELOPMENT_GUIDE.md").read_text(encoding="utf-8")
        for required in (
            "## 2. 开始开发前的阅读顺序",
            "## 4. 目录与职责",
            "## 5. 不可破坏的工作流约束",
            "## 7. 开发与测试规则",
            "## 9. 版本升级清单",
            "## 10. Git 与发布流程",
            "python -B -m unittest discover -s tests -v",
            "git archive --format=zip",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)
        self.assertNotIn("TBD", guide)
        self.assertNotIn("TODO", guide)

    def test_release_metadata_is_bound_to_the_installer_receipt(self):
        import json

        from scripts.install_workflow import install, load_manifest, verify

        manifest = load_manifest(ROOT / "dependencies.lock.json")
        self.assertEqual(manifest.get("release_version"), "1.0.0")
        self.assertEqual(manifest.get("workflow_version"), "paper-workflow-orchestrator-v1.0")
        with TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            install(manifest, "core", target, ROOT)
            receipt = json.loads((target / ".paper-workflow-install.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt.get("release_version"), "1.0.0")
            self.assertEqual(receipt.get("workflow_version"), "paper-workflow-orchestrator-v1.0")
            self.assertEqual(verify(target, "core", manifest)["status"], "pass")

    def test_migration_fails_closed_without_overwriting_backups(self):
        from scripts import progress_manager as progress

        legacy_version = "paper-workflow-orchestrator-v0.4"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.md"
            legacy = progress.template("collision-test").replace(
                progress.CURRENT_WORKFLOW_VERSION, legacy_version
            )
            existing_legacy = Path(str(path) + ".legacy-v0.4")
            existing_legacy.write_text("keep this legacy generation", encoding="utf-8")
            existing_bak = Path(str(path) + ".bak")
            existing_bak.write_text("keep this recovery generation", encoding="utf-8")
            path.write_text(legacy, encoding="utf-8")
            args = types.SimpleNamespace(
                file=str(path),
                mode="guided_idea",
                current_stage="",
                confirm=True,
            )

            self.assertEqual(progress.cmd_migrate(args), 0)
            self.assertEqual(existing_legacy.read_text(encoding="utf-8"), "keep this legacy generation")
            self.assertGreaterEqual(len(list(Path(tmp).glob("progress.md.legacy-v0.4*"))), 2)
            previous = list(Path(tmp).glob("progress.md.bak-previous-*"))
            self.assertGreaterEqual(len(previous), 1)
            self.assertEqual(previous[0].read_text(encoding="utf-8"), "keep this recovery generation")
            self.assertIn("workflow_version: paper-workflow-orchestrator-v1.0", existing_bak.read_text(encoding="utf-8"))

    def test_migration_rejects_missing_rule_status_before_creating_backups(self):
        from scripts import progress_manager as progress

        legacy_version = "paper-workflow-orchestrator-v0.5"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.md"
            legacy = progress.template("missing-status-test").replace(
                progress.CURRENT_WORKFLOW_VERSION, legacy_version
            )
            rule = """- R001:
  - error: legacy rule without status
  - cause: legacy fixture
  - impact: unknown
  - severity: unspecified
  - blocking: false
  - prevention_rule: preserve the rule until reviewed
  - required_check: review the legacy rule
  - applicable_stages: intake
"""
            legacy = legacy.replace("\n## Decisions\n", f"\n{rule}\n## Decisions\n")
            path.write_text(legacy, encoding="utf-8")
            args = types.SimpleNamespace(
                file=str(path),
                mode="guided_idea",
                current_stage="",
                confirm=True,
            )

            with self.assertRaises(progress.ProgressError) as context:
                progress.cmd_migrate(args)
            self.assertIn("status", str(context.exception))
            self.assertFalse(list(Path(tmp).glob("progress.md.legacy-*")))
            self.assertFalse(Path(str(path) + ".bak").exists())


if __name__ == "__main__":
    unittest.main()
