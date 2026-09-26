import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class InstallerContractTests(unittest.TestCase):
    def test_release_and_workflow_versions_are_validated_independently(self):
        from scripts.install_workflow import InstallError, _validate_release_metadata

        _validate_release_metadata(
            {
                "release_version": "1.1.0",
                "workflow_version": "paper-workflow-orchestrator-v1.0",
            },
            "dependency manifest",
        )
        for metadata in (
            {"release_version": "1.x.0", "workflow_version": "paper-workflow-orchestrator-v1.0"},
            {"release_version": "1.1.0", "workflow_version": "paper-workflow-orchestrator-v1"},
            {"release_version": "1.1.0"},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(InstallError):
                _validate_release_metadata(metadata, "dependency manifest")

    def test_standard_profile_contains_scientific_visualization_but_not_autoresearch(self):
        self.assertTrue((ROOT / "scripts" / "install_workflow.py").is_file(), "installer has not been implemented")
        self.assertTrue((ROOT / "dependencies.lock.json").is_file(), "dependency lock has not been implemented")
        from scripts.install_workflow import load_manifest, resolve_profile

        manifest = load_manifest(ROOT / "dependencies.lock.json")
        names = {item["name"] for item in resolve_profile(manifest, "standard")}
        self.assertIn("scientific-visualization", names)
        self.assertNotIn("autoresearch", names)

    def test_external_manifest_entries_record_license_status(self):
        from scripts.install_workflow import load_manifest

        manifest = load_manifest(ROOT / "dependencies.lock.json")
        for name, entry in manifest["skills"].items():
            if entry.get("source") == "github":
                self.assertTrue(entry.get("license"), f"missing license status for {name}")

    def test_default_target_honors_codex_home(self):
        from scripts.install_workflow import default_target

        with tempfile.TemporaryDirectory() as tmp:
            configured = Path(tmp).resolve() / "codex-home"
            with patch.dict(os.environ, {"CODEX_HOME": str(configured)}):
                self.assertEqual(default_target(), configured / "skills")

    def test_manifest_rejects_unsafe_names_and_paths_before_materialization(self):
        from scripts.install_workflow import InstallError, load_manifest

        base = json.loads((ROOT / "dependencies.lock.json").read_text(encoding="utf-8"))
        cases = [("../escape", "paper-workflow-orchestrator"), ("bad/name", "paper-workflow-orchestrator")]
        for bad_name, path_name in cases:
            manifest = json.loads(json.dumps(base))
            entry = manifest["skills"].pop("paper-workflow-orchestrator")
            entry["path"] = "../escape"
            manifest["skills"][bad_name] = entry
            for profile, names in manifest["profiles"].items():
                manifest["profiles"][profile] = [bad_name if name == "paper-workflow-orchestrator" else name for name in names]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / "manifest.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(InstallError):
                    load_manifest(path)

    def test_windows_archive_canonical_collisions_and_reserved_names_block(self):
        from scripts.install_workflow import InstallError, _download_github, validate_archive_member

        for member in ("CON", "aux.txt", "name.", "name ", "folder/PRN.log"):
            self.assertFalse(validate_archive_member(member))
        with self.assertRaises(InstallError):
            __import__("scripts.install_workflow", fromlist=["_validate_skill_name"])._validate_skill_name("con")
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp).resolve()
            commit = "3" * 40
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                root = f"repo-{commit}/"
                bundle.writestr(root, "")
                bundle.writestr(root + "skills/SKILL.md", "good")
                bundle.writestr(root + "skills/skill.md", "evil")
            entry = {"repository": "owner/repo", "commit": commit, "path": "skills"}
            with patch("scripts.install_workflow.urllib.request.urlopen", return_value=io.BytesIO(archive.getvalue())):
                with self.assertRaises(InstallError):
                    _download_github(entry, staging)

    def test_workflow_documents_figure_route_as_downstream_only(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        contract = (ROOT / "references" / "scientific-visualization-integration.md").read_text(encoding="utf-8")
        router = (ROOT / "companion-skills" / "research-skill-router" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("scientific-visualization", skill)
        self.assertIn("sole downstream primary", skill)
        self.assertIn("do not install the entire K-Dense collection", contract)
        self.assertIn("scientific-visualization` is a downstream figure skill only", router)
        self.assertIn("never infer permission", router)

    def test_readme_exposes_one_click_command(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        for content in (readme, readme_zh):
            self.assertIn("python scripts/install_workflow.py", content)
            self.assertIn("scientific-visualization", content)
            self.assertIn("python3", content)
            self.assertIn("GitHub", content)
            self.assertIn("--prune", content)

    def test_bundled_license_and_manual_copy_commands_are_repeat_safe(self):
        for path in (
            ROOT / "LICENSE",
            ROOT / "companion-skills" / "research-skill-router" / "LICENSE",
        ):
            self.assertTrue(path.is_file())
            self.assertIn("MIT License", path.read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        for content in (readme, readme_zh):
            self.assertIn("Copy-Item -Recurse -Force companion-skills\\academic-manuscript-final-editor\\*", content)
            self.assertIn("cp -R companion-skills/academic-manuscript-final-editor/.", content)

    def test_full_profile_adds_explicit_experiment_capabilities(self):
        self.assertTrue((ROOT / "scripts" / "install_workflow.py").is_file(), "installer has not been implemented")
        self.assertTrue((ROOT / "dependencies.lock.json").is_file(), "dependency lock has not been implemented")
        from scripts.install_workflow import load_manifest, resolve_profile

        manifest = load_manifest(ROOT / "dependencies.lock.json")
        names = {item["name"] for item in resolve_profile(manifest, "full")}
        self.assertIn("autoresearch", names)
        self.assertIn("ara-rigor-reviewer", names)

    def test_zip_members_reject_path_traversal(self):
        self.assertTrue((ROOT / "scripts" / "install_workflow.py").is_file(), "installer has not been implemented")
        from scripts.install_workflow import validate_archive_member

        self.assertTrue(validate_archive_member("skill/SKILL.md"))
        self.assertFalse(validate_archive_member("../escape.txt"))
        self.assertFalse(validate_archive_member("skill/../../escape.txt"))
        self.assertFalse(validate_archive_member(r"C:\escape.txt"))

    def test_safe_in_archive_symlink_is_materialized(self):
        from scripts.install_workflow import _download_github

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp).resolve()
            commit = "1" * 40
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                root = f"repo-{commit}/"
                bundle.writestr(root, "")
                bundle.writestr(root + "academic-paper/SKILL.md", "---\nname: academic-paper\n---\n")
                link = zipfile.ZipInfo(root + "skills/academic-paper")
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                bundle.writestr(link, "../academic-paper")
            payload = archive.getvalue()
            entry = {"repository": "owner/repo", "commit": commit, "path": "skills/academic-paper"}
            with patch("scripts.install_workflow.urllib.request.urlopen", return_value=io.BytesIO(payload)):
                source = _download_github(entry, staging)
            self.assertEqual(source.joinpath("SKILL.md").read_text(encoding="utf-8").splitlines()[1], "name: academic-paper")

    def test_escaping_in_archive_symlink_is_blocked(self):
        from scripts.install_workflow import InstallError, _download_github

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp).resolve()
            commit = "2" * 40
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                root = f"repo-{commit}/"
                bundle.writestr(root, "")
                link = zipfile.ZipInfo(root + "skills/unsafe")
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                bundle.writestr(link, "../../outside")
            entry = {"repository": "owner/repo", "commit": commit, "path": "skills/unsafe"}
            with patch("scripts.install_workflow.urllib.request.urlopen", return_value=io.BytesIO(archive.getvalue())):
                with self.assertRaises(InstallError):
                    _download_github(entry, staging)

    def test_compressed_download_limit_fails_before_archive_extraction(self):
        from scripts.install_workflow import InstallError, _download_github

        class OversizedResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return b"x" * 16

        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.install_workflow.MAX_ARCHIVE_COMPRESSED", 10), patch("scripts.install_workflow.urllib.request.urlopen", return_value=OversizedResponse()):
                with self.assertRaises(InstallError):
                    _download_github({"repository": "owner/repo", "commit": "4" * 40, "path": "."}, Path(tmp).resolve())

    def test_core_dry_run_does_not_write(self):
        from scripts.install_workflow import install, load_manifest

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            result = install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT, dry_run=True)
            self.assertEqual(result["status"], "dry-run")
            self.assertFalse(target.exists())

    def test_dry_run_with_missing_parent_does_not_write(self):
        from scripts.install_workflow import install, load_manifest

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "new-parent" / "skills"
            result = install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT, dry_run=True)
            self.assertEqual(result["status"], "dry-run")
            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())

    @unittest.skipIf(os.name == "nt", "creating a test symlink may require elevated Windows privileges")
    def test_install_rejects_user_controlled_symlink_parent(self):
        from scripts.install_workflow import InstallError, install, load_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            real_parent = root / "real-parent"
            real_parent.mkdir()
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(InstallError, "symlink or reparse point"):
                install(
                    load_manifest(ROOT / "dependencies.lock.json"),
                    "core",
                    linked_parent / "skills",
                    ROOT,
                    dry_run=True,
                )

    def test_core_install_and_verify(self):
        from scripts.install_workflow import install, load_manifest, verify

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            result = install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT)
            self.assertEqual(result["status"], "pass")
            manifest = load_manifest(ROOT / "dependencies.lock.json")
            self.assertEqual(verify(target, "core", manifest)["status"], "pass")
            backups = target / ".paper-workflow-backups"
            before = sorted(backups.iterdir()) if backups.exists() else []
            repeated = install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT)
            self.assertEqual(repeated["status"], "pass")
            self.assertEqual(repeated["backups"], {})
            after = sorted(backups.iterdir()) if backups.exists() else []
            self.assertEqual(after, before, "an identical install must not create a backup transaction")
            self.assertEqual(verify(target, "core", manifest)["status"], "pass")

    def test_malformed_transaction_marker_is_retained_for_recovery(self):
        from scripts.install_workflow import InstallError, _recover_transaction

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            target.mkdir()
            marker = target / ".paper-workflow-install.transaction.json"
            marker.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(InstallError):
                _recover_transaction(target)
            self.assertTrue(marker.exists(), "malformed transaction evidence must not be deleted")

    def test_incomplete_rollback_retains_marker_and_later_verify_recovers(self):
        from scripts.install_workflow import InstallError, install, verify

        mini = {
            "schema_version": "workflow-dependencies-v1",
            "profiles": {"core": ["demo-skill"], "standard": ["demo-skill"], "full": ["demo-skill"]},
            "skills": {"demo-skill": {"source": "bundled", "path": "demo-source", "license": "MIT"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "demo-source"
            source.mkdir()
            skill_file = source / "SKILL.md"
            skill_file.write_text("---\nname: demo-skill\n---\nold\n", encoding="utf-8")
            target = root / "skills"
            install(mini, "core", target, root)
            skill_file.write_text("---\nname: demo-skill\n---\nnew\n", encoding="utf-8")

            original_rename = Path.rename
            faults = {"activation": True, "restore": True}

            def faulty_rename(path, destination):
                if faults["activation"] and "paper-workflow-install-" in str(path) and str(destination).endswith("demo-skill"):
                    faults["activation"] = False
                    raise OSError("activation failure")
                if faults["restore"] and ".paper-workflow-backups" in str(path) and str(destination).endswith("demo-skill"):
                    faults["restore"] = False
                    raise OSError("restore failure")
                return original_rename(path, destination)

            try:
                with patch.object(Path, "rename", faulty_rename):
                    with self.assertRaises(InstallError):
                        install(mini, "core", target, root, update=True)
            finally:
                Path.rename = original_rename

            marker = target / ".paper-workflow-install.transaction.json"
            self.assertTrue(marker.exists(), "incomplete rollback must retain transaction evidence")
            self.assertFalse((target / "demo-skill").exists())
            self.assertEqual(verify(target, "core", mini)["status"], "pass")
            self.assertFalse(marker.exists(), "successful recovery must clear the marker")
            self.assertIn("old", (target / "demo-skill" / "SKILL.md").read_text(encoding="utf-8"))

    def test_verify_rejects_requested_profile_mismatch(self):
        from scripts.install_workflow import install, load_manifest, verify

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            manifest = load_manifest(ROOT / "dependencies.lock.json")
            install(manifest, "core", target, ROOT)
            result = verify(target, "standard", manifest)
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["errors"])

    def test_full_to_core_update_requires_explicit_prune_and_preserves_backup(self):
        from scripts.install_workflow import InstallError, install, load_manifest, verify

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            manifest = load_manifest(ROOT / "dependencies.lock.json")
            # Use a tiny local manifest to avoid network access while exercising profile transitions.
            mini = {
                "schema_version": "workflow-dependencies-v1",
                "profiles": {"core": ["research-skill-router"], "standard": ["research-skill-router", "academic-manuscript-final-editor"], "full": ["research-skill-router", "academic-manuscript-final-editor"]},
                "skills": {
                    "research-skill-router": {"source": "bundled", "path": "companion-skills/research-skill-router", "license": "MIT"},
                    "academic-manuscript-final-editor": {"source": "bundled", "path": "companion-skills/academic-manuscript-final-editor", "license": "MIT"},
                },
            }
            install(mini, "full", target, ROOT)
            with self.assertRaises(InstallError):
                install(mini, "core", target, ROOT, update=True)
            result = install(mini, "core", target, ROOT, update=True, prune=True)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(verify(target, "core", mini)["status"], "pass")
            self.assertFalse((target / "academic-manuscript-final-editor").exists())

    def test_license_text_is_present_in_bundled_installations(self):
        from scripts.install_workflow import install

        manifest = {
            "schema_version": "workflow-dependencies-v1",
            "profiles": {"core": ["research-skill-router"], "standard": ["research-skill-router"], "full": ["research-skill-router"]},
            "skills": {"research-skill-router": {"source": "bundled", "path": "companion-skills/research-skill-router", "license": "MIT"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            install(manifest, "core", target, ROOT)
            self.assertTrue((target / "research-skill-router" / "LICENSE").is_file())

    def test_keyboard_interrupt_rolls_back_partial_install(self):
        from scripts.install_workflow import InstallError, install

        mini = {
            "schema_version": "workflow-dependencies-v1",
            "profiles": {"core": ["research-skill-router", "academic-manuscript-final-editor"], "standard": ["research-skill-router", "academic-manuscript-final-editor"], "full": ["research-skill-router", "academic-manuscript-final-editor"]},
            "skills": {
                "research-skill-router": {"source": "bundled", "path": "companion-skills/research-skill-router", "license": "MIT"},
                "academic-manuscript-final-editor": {"source": "bundled", "path": "companion-skills/academic-manuscript-final-editor", "license": "MIT"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            original_rename = Path.rename
            calls = {"count": 0}

            def interrupt_on_second_source_rename(path, destination):
                if str(path).find("paper-workflow-install-") >= 0:
                    calls["count"] += 1
                    if calls["count"] == 2:
                        raise KeyboardInterrupt()
                return original_rename(path, destination)

            with patch.object(Path, "rename", interrupt_on_second_source_rename):
                with self.assertRaises(KeyboardInterrupt):
                    install(mini, "core", target, ROOT)
            self.assertFalse((target / ".paper-workflow-install.json").exists())
            self.assertFalse((target / "research-skill-router").exists())
            self.assertFalse((target / "academic-manuscript-final-editor").exists())

    def test_install_creates_requested_target_parent(self):
        from scripts.install_workflow import install, load_manifest, verify

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "new-parent" / "skills"
            result = install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(verify(target)["status"], "pass")

    def test_changed_unmanaged_destination_blocks(self):
        from scripts.install_workflow import InstallError, install, load_manifest

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "skills"
            existing = target / "paper-workflow-orchestrator"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("unmanaged", encoding="utf-8")
            with self.assertRaises(InstallError):
                install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT)

    def test_same_github_commit_can_materialize_multiple_subdirectories(self):
        from scripts.install_workflow import _download_github

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp).resolve()
            commit = "0" * 40
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                root = f"repo-{commit}/"
                bundle.writestr(root, "")
                bundle.writestr(root + "a/SKILL.md", "---\nname: a\n---\n")
                bundle.writestr(root + "b/SKILL.md", "---\nname: b\n---\n")
            payload = archive.getvalue()
            entry_a = {"repository": "owner/repo", "commit": commit, "path": "a"}
            entry_b = {"repository": "owner/repo", "commit": commit, "path": "b"}
            with patch("scripts.install_workflow.urllib.request.urlopen", return_value=io.BytesIO(payload)) as opened:
                self.assertTrue(_download_github(entry_a, staging).is_dir())
                self.assertTrue(_download_github(entry_b, staging).is_dir())
                self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
