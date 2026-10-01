import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_workflow_studio_bundle import verify_bundle


ROOT = Path(__file__).resolve().parents[1]


class WorkflowStudioBundleTests(unittest.TestCase):
    def make_bundle(self, root, *, html=None, css="body { color: #123; }", javascript="const help = 'https://example.org/docs';", extra_files=None):
        assets = root / "assets"
        assets.mkdir(parents=True)
        (root / "index.html").write_text(
            html or '<html><head><link rel="stylesheet" href="./assets/app.css"></head><body><script type="module" src="./assets/app.js"></script></body></html>',
            encoding="utf-8",
        )
        (assets / "app.css").write_text(css, encoding="utf-8")
        (assets / "app.js").write_text(javascript, encoding="utf-8")
        licenses = {
            "schema_version": "workflow-studio-third-party-licenses-v1",
            "packages": [{
                "name": "example-runtime",
                "version": "1.2.3",
                "license": "MIT",
                "homepage": "https://example.org",
                "repository": "https://github.com/example/runtime",
            }],
        }
        (root / "THIRD_PARTY_LICENSES.json").write_text(json.dumps(licenses), encoding="utf-8")
        for relative, content in (extra_files or {}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.write_manifest(root)
        return root

    @staticmethod
    def write_manifest(root):
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != "bundle-manifest.json":
                relative = path.relative_to(root).as_posix()
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "workflow-studio-bundle-v1",
            "release_version": "1.1.0",
            "files": files,
        }
        (root / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_committed_bundle_hashes_and_offline_policy_pass(self):
        result = verify_bundle(ROOT / "assets" / "workflow-studio")
        self.assertEqual(result["status"], "pass", result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertGreater(result["file_count"], 2)
        self.assertRegex(result["aggregate_sha256"], r"^[0-9a-f]{64}$")

    def test_manifest_detects_modified_runtime_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle")
            (root / "assets" / "app.js").write_text("const changed = true;", encoding="utf-8")
            result = verify_bundle(root)
            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("SHA-256 does not match" in error for error in result["errors"]))

    def test_manifest_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle")
            (root / "bundle-manifest.json").write_text(
                '{"schema_version":"workflow-studio-bundle-v1",'
                '"schema_version":"workflow-studio-bundle-v1",'
                '"release_version":"1.1.0","files":{}}',
                encoding="utf-8",
            )
            result = verify_bundle(root)
            self.assertTrue(any("duplicate JSON member: schema_version" in error for error in result["errors"]))

    def test_manifest_rejects_paths_that_are_unsafe_on_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle")
            manifest_path = root / "bundle-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["C:/outside.js"] = "0" * 64
            manifest["files"]["assets/CON.txt"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = verify_bundle(root)
            unsafe = [error for error in result["errors"] if "unsafe or reserved file path" in error]
            self.assertEqual(len(unsafe), 2)

    def test_manifest_must_be_a_regular_non_symlink_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle")
            manifest_path = root / "bundle-manifest.json"
            outside_manifest = Path(temporary) / "outside-manifest.json"
            outside_manifest.write_bytes(manifest_path.read_bytes())
            manifest_path.unlink()
            try:
                manifest_path.symlink_to(outside_manifest)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symbolic links are unavailable on this host: {exc}")
            result = verify_bundle(root)
            self.assertTrue(any("symbolic links are not allowed" in error for error in result["errors"]))

    def test_unlisted_runtime_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle")
            (root / "extra.css").write_text("body{}", encoding="utf-8")
            result = verify_bundle(root)
            self.assertTrue(any("unlisted runtime file: extra.css" in error for error in result["errors"]))

    def test_root_relative_html_resources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(
                Path(temporary) / "bundle",
                html='<html><script src="/assets/app.js"></script></html>',
            )
            result = verify_bundle(root)
            self.assertTrue(any("must be relative to the bundle" in error for error in result["errors"]))

    def test_remote_stylesheet_fonts_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(
                Path(temporary) / "bundle",
                css="@font-face { src: url('https://fonts.example.invalid/a.woff2'); }",
            )
            result = verify_bundle(root)
            self.assertTrue(any("external resource is not allowed" in error for error in result["errors"]))

    def test_runtime_network_calls_and_dom_resource_assignments_are_rejected(self):
        for javascript in (
            "fetch('https://telemetry.example.invalid/collect')",
            "new WebSocket('//socket.example.invalid/feed')",
            "import('/outside/chunk.js')",
            "const image = document.createElement('img'); image.src = 'https://images.example.invalid/p.png';",
            "navigator.sendBeacon('./collect', payload)",
        ):
            with self.subTest(javascript=javascript), tempfile.TemporaryDirectory() as temporary:
                root = self.make_bundle(Path(temporary) / "bundle", javascript=javascript)
                result = verify_bundle(root)
                self.assertEqual(result["status"], "fail")

    def test_runtime_json_configuration_cannot_select_remote_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(
                Path(temporary) / "bundle",
                extra_files={"runtime.json": '{"font_url":"https://fonts.example.invalid/font.woff2"}'},
            )
            result = verify_bundle(root)
            self.assertTrue(any("external resource is not allowed" in error for error in result["errors"]))

    def test_license_attribution_urls_and_help_links_are_not_runtime_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(
                Path(temporary) / "bundle",
                javascript="const help = 'https://example.org/docs';",
                html='<html><body><a href="https://example.org/help">Help</a><script type="module" src="./assets/app.js"></script><link rel="stylesheet" href="./assets/app.css"></body></html>',
            )
            result = verify_bundle(root)
            self.assertEqual(result["status"], "pass", result["errors"])

    def test_source_maps_are_rejected_even_when_manifested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle", extra_files={"assets/app.js.map": "{}"})
            result = verify_bundle(root)
            self.assertTrue(any("source-map files are not allowed" in error for error in result["errors"]))

    def test_symbolic_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_bundle(Path(temporary) / "bundle")
            target = root / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symbolic links are unavailable on this host: {exc}")
            result = verify_bundle(root)
            self.assertTrue(any("symbolic links are not allowed" in error for error in result["errors"]))

    def test_core_install_contains_launcher_engine_projection_and_verified_bundle(self):
        from scripts.install_workflow import install, load_manifest

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "skills"
            manifest = load_manifest(ROOT / "dependencies.lock.json")
            install(manifest, "core", target, ROOT)
            installed = target / "paper-workflow-orchestrator"
            self.assertTrue(installed.joinpath("scripts/workflow_studio.py").is_file())
            self.assertTrue(installed.joinpath("scripts/workflow_engine/studio_server.py").is_file())
            self.assertTrue(installed.joinpath("references/workflows/official-v1.0-studio-projection.json").is_file())
            bundle = installed / "assets" / "workflow-studio"
            self.assertTrue(bundle.joinpath("index.html").is_file())
            self.assertEqual(verify_bundle(bundle)["status"], "pass")
            self.assertFalse(installed.joinpath("studio").exists(), "development sources are not part of the user installation")

    def test_workflow_orchestrator_is_in_every_install_profile(self):
        from scripts.install_workflow import load_manifest

        manifest = load_manifest(ROOT / "dependencies.lock.json")
        for profile in ("core", "standard", "full"):
            with self.subTest(profile=profile):
                self.assertIn("paper-workflow-orchestrator", manifest["profiles"][profile])


if __name__ == "__main__":
    unittest.main()
