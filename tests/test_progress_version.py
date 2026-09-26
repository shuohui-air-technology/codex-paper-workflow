import hashlib
import json
import re
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
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(
            'metadata:\n  version: "1.1.0"\n  workflow_version: "paper-workflow-orchestrator-v1.0"',
            skill,
        )

    def test_official_contract_markers_preserve_v10_bodies(self):
        projection = json.loads(
            (ROOT / "references" / "workflows" / "official-v1.0-studio-projection.json")
            .read_text(encoding="utf-8")
        )
        begin_marker = b"<!-- OFFICIAL-V1-CONTRACT:BEGIN -->\n"
        end_marker = b"<!-- OFFICIAL-V1-CONTRACT:END -->"
        paths = {
            "SKILL.md": (ROOT / "SKILL.md", "SKILL.md#body"),
            "references/stage-contracts.md": (
                ROOT / "references" / "stage-contracts.md",
                "references/stage-contracts.md#body",
            ),
            "references/progress-schema.md": (
                ROOT / "references" / "progress-schema.md",
                "references/progress-schema.md#body",
            ),
        }
        for display_path, (path, projection_key) in paths.items():
            with self.subTest(path=display_path):
                contents = path.read_bytes()
                self.assertEqual(contents.count(begin_marker), 1)
                self.assertEqual(contents.count(end_marker), 1)
                body_start = contents.index(begin_marker) + len(begin_marker)
                body_end = contents.index(end_marker)
                self.assertLess(body_start, body_end)
                body = contents[body_start:body_end]
                self.assertEqual(
                    hashlib.sha256(body).hexdigest(),
                    projection["official_contract_sections"][projection_key],
                )

    def test_release_metadata_is_v110_and_progress_schema_remains_official_v10(self):
        manifest = json.loads((ROOT / "dependencies.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_version"], "1.1.0")
        self.assertEqual(manifest["workflow_version"], "paper-workflow-orchestrator-v1.0")
        progress_schema = (ROOT / "references" / "progress-schema.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("authoritative only in Official v1.0 mode", progress_schema)

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
        self.assertIn('version: "1.1.0"', skill)
        self.assertIn('workflow_version: "paper-workflow-orchestrator-v1.0"', skill)

    def test_public_readmes_do_not_advertise_ai_handoff(self):
        for relative in ("README.md", "README.zh-CN.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8").lower()
                self.assertNotIn("handoff", text)
                self.assertNotIn("交接", text)

    def test_public_readmes_do_not_advertise_author_guided_editing(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8").lower()
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertNotIn("author-guided", english)
        self.assertNotIn("author feedback", english)
        self.assertNotIn("作者", chinese)

    def test_final_editor_user_facing_docs_use_neutral_editorial_language(self):
        skill_root = ROOT / "companion-skills" / "academic-manuscript-final-editor"
        paths = (
            skill_root / "README.md",
            skill_root / "SKILL.md",
            skill_root / "agents" / "openai.yaml",
            skill_root / "references" / "editorial-style-rules.md",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("作者", text)
                self.assertIsNone(re.search(r"\bauthors?\b", text, re.IGNORECASE))
        self.assertFalse((skill_root / "references" / "author-style-rules.md").exists())

    def test_public_readmes_distinguish_release_from_default_workflow(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn(
            "**Helping you turn any vague idea into a paper built to top-journal standards.**",
            english,
        )
        self.assertIn(
            "**帮助您将任何一个模糊的想法落地为顶刊级别的论文。**",
            chinese,
        )
        for text in (english, chinese):
            self.assertIn("Release: v1.1.0", text)
            self.assertIn("Default workflow: v1.0", text)

    def test_readmes_explain_one_advanced_workflow_studio_entry(self):
        requirements = {
            "README.md": (
                "## Custom workflow editor",
                "The official v1.0 workflow remains the default",
                "paper-workflow-orchestrator/scripts/workflow_studio.py",
                "--project .",
                "without editing JSON",
                "export `CODEX_HOME` with that same path in the shell where you launch Studio",
                "export CODEX_HOME=\"/path/to/codex-home\"",
                "Python 3.10 or later but no Node.js",
                "assets/workflow-studio.png",
            ),
            "README.zh-CN.md": (
                "## 自定义工作流编排",
                "官方 v1.0 流程仍是默认流程",
                "paper-workflow-orchestrator/scripts/workflow_studio.py",
                "--project .",
                "无需编辑 JSON",
                "在启动 Studio 的当前终端中导出同一个 `CODEX_HOME`",
                "export CODEX_HOME=\"/你的 Codex 主目录\"",
                "Python 3.10 或以上版本",
                "assets/workflow-studio.png",
            ),
        }
        for relative, required in requirements.items():
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                heading = required[0]
                self.assertEqual(text.count(heading), 1)
                for phrase in required[1:]:
                    self.assertIn(phrase, text)
        self.assertTrue((ROOT / "assets" / "workflow-studio.png").is_file())

    def test_development_guide_is_developer_focused_and_complete(self):
        guide = (ROOT / "DEVELOPMENT_GUIDE.md").read_text(encoding="utf-8")
        for required in (
            "本文面向希望理解、调试或扩展本项目的人类开发者",
            "## 2. 仓库结构",
            "## 3. 建立本地开发环境",
            "## 4. 按开发目标寻找入口",
            "## 5. Python 代码的共同模式",
            "## 6. 测试结构与运行方法",
            "## 7. 常见修改场景",
            "## 8. 安装器的安全模型",
            "## 9. 版本与兼容性",
            "## 10. 提交前检查",
            "## 11. 调试提示",
            "python -B -m unittest discover -s tests -v",
            "git diff --check",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)
        self.assertNotIn("后续 Agent", guide)
        self.assertNotIn("repository_read:", guide)
        self.assertNotIn("TBD", guide)
        self.assertNotIn("TODO", guide)
        self.assertIn("Node.js 22.12.0", guide)
        self.assertIn("npm ci --ignore-scripts", guide)
        self.assertIn("python3 scripts/verify_workflow_studio_bundle.py", guide)

    def test_release_metadata_is_bound_to_the_installer_receipt(self):
        import json

        from scripts.install_workflow import install, load_manifest, verify

        manifest = load_manifest(ROOT / "dependencies.lock.json")
        self.assertEqual(manifest.get("release_version"), "1.1.0")
        self.assertEqual(manifest.get("workflow_version"), "paper-workflow-orchestrator-v1.0")
        with TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            install(manifest, "core", target, ROOT)
            receipt = json.loads((target / ".paper-workflow-install.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt.get("release_version"), "1.1.0")
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
