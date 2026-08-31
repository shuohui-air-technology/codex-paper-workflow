import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_engine.catalog import (
    CatalogError,
    discover_skills,
    load_validator_registry,
    resolve_skill_roots,
    tree_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def write_skill(root: Path, name: str, body: str = "instructions") -> Path:
    path = root / name
    path.mkdir(parents=True)
    path.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


class WorkflowCatalogTests(unittest.TestCase):
    def test_different_duplicate_skill_ids_are_ambiguous(self):
        """Catches root-priority silently selecting different Skill content."""
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first"
            second = base / "second"
            write_skill(first, "sample", "first")
            write_skill(second, "sample", "second")
            result = discover_skills((first, second), install_receipts={})
            self.assertNotIn("sample", result.skills)
            self.assertEqual(result.errors[0].code, "catalog.ambiguous_skill")

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_symlink_escaping_root_is_not_discovered(self):
        """Catches a direct-child link resolving beyond its collection root."""
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "skills"
            outside = base / "outside"
            root.mkdir()
            target = write_skill(outside, "escaped")
            root.joinpath("escaped").symlink_to(target, target_is_directory=True)
            result = discover_skills((root,), install_receipts={})
            self.assertNotIn("escaped", result.skills)
            self.assertEqual(result.errors[0].code, "catalog.symlink_escape")

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_internal_direct_child_symlink_is_discovered_but_nested_link_is_rejected(self):
        """Catches accepting link-bearing Skill trees after allowing safe child aliases."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            target = write_skill(root / "stored", "alias")
            root.joinpath("alias").symlink_to(target, target_is_directory=True)
            target.joinpath("nested-link").symlink_to(target / "SKILL.md")
            result = discover_skills((root,), install_receipts={})
            self.assertNotIn("alias", result.skills)
            self.assertEqual(result.errors[0].code, "catalog.symlink_in_tree")

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_internal_direct_child_symlink_is_a_valid_skill_alias(self):
        """Catches rejecting a direct child alias whose target remains under the root."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            target = write_skill(root / "stored", "alias")
            root.joinpath("alias").symlink_to(target, target_is_directory=True)
            result = discover_skills((root,), install_receipts={})
            self.assertEqual(result.skills["alias"].relative_path, "alias")
            self.assertFalse(result.errors)

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_symlinked_skill_file_is_reported_as_an_in_tree_link(self):
        """Catches silently skipping a Skill whose entrypoint is redirected."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            skill = root / "linked-skill"
            skill.mkdir(parents=True)
            source = root / "source.md"
            source.write_text("---\nname: linked-skill\n---\n", encoding="utf-8")
            skill.joinpath("SKILL.md").symlink_to(source)
            result = discover_skills((root,), install_receipts={})
            self.assertNotIn("linked-skill", result.skills)
            self.assertEqual(result.errors[0].code, "catalog.symlink_in_tree")

    def test_identical_duplicate_uses_first_root(self):
        """Catches a stable duplicate identity losing declared root priority."""
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first"
            second = base / "second"
            write_skill(first, "sample")
            write_skill(second, "sample")
            result = discover_skills((first, second), install_receipts={})
            self.assertEqual(result.skills["sample"].root, first.resolve())
            self.assertFalse(result.errors)

    def test_tree_digest_literal_receipt_marks_skill_locked(self):
        """Catches changing the installer-compatible tree hash byte format."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            skill = write_skill(root, "locked")
            skill.joinpath("references").mkdir()
            skill.joinpath("references/note.txt").write_text("pine\n", encoding="utf-8")
            expected = "sha256:6c86f439621beb7e9c6427d7e94bd9e76a06527c71e50b324c32d2cd59660630"
            self.assertEqual(tree_sha256(skill), expected)
            receipt = {"skills": {"locked": {"tree_hash": expected}}}
            result = discover_skills((root,), install_receipts={root.resolve(): receipt})
            self.assertTrue(result.skills["locked"].locked)
            self.assertEqual(
                result.skills["locked"].skill_sha256,
                "981e23293ea9f9e34ac5ee486967146f399e8387a2ba60837d7d302590b57820",
            )
            self.assertFalse(result.warnings)

    def test_digit_leading_installer_skill_is_discovered_and_locked(self):
        """Catches rejecting a receipt-bound Skill ID the installer accepts."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            write_skill(root, "3d-skill", "digit")
            expected = "sha256:135b095f366a600e5dc09289d898da4e823057f6ac3db874f3d0a6b46fac1678"
            result = discover_skills(
                (root,),
                install_receipts={root.resolve(): {"skills": {"3d-skill": {"tree_hash": expected}}}},
            )
            self.assertTrue(result.skills["3d-skill"].locked)
            self.assertFalse(result.errors)

    def test_installer_invalid_underscore_and_overlength_ids_are_rejected(self):
        """Catches catalog IDs drifting wider than the installer contract."""
        for name in ("under_score", "a" * 129):
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                root = Path(temporary) / "skills"
                write_skill(root, name)
                result = discover_skills((root,), install_receipts={})
                self.assertNotIn(name, result.skills)
                self.assertEqual(result.errors[0].code, "catalog.invalid_frontmatter")

    def test_unrecorded_skill_is_reported_unlocked(self):
        """Catches a mutable local Skill being silently treated as installer-locked."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            write_skill(root, "local")
            result = discover_skills((root,), install_receipts={})
            self.assertFalse(result.skills["local"].locked)
            self.assertEqual(result.warnings[0].code, "catalog.unlocked_skill")

    def test_root_resolution_preserves_explicit_then_install_then_codex_priority(self):
        """Catches an environment default displacing an explicitly selected root."""
        explicit = Path("/tmp/explicit")
        install = Path("/tmp/install")
        resolved = resolve_skill_roots((explicit, explicit), install, {"CODEX_HOME": "/tmp/codex"})
        self.assertEqual(resolved, (explicit, install, Path("/tmp/codex/skills")))

    def test_validator_registry_loads_current_hashed_scripts(self):
        """Catches registry entries that no longer bind to the repository scripts."""
        validators = load_validator_registry(ROOT / "references/workflows/validator-registry.v1.json", ROOT)
        self.assertEqual(set(validators), {
            "experiment-contract",
            "figure-contract",
            "final-edit-receipt",
            "humanizer-preflight",
            "paper-section",
        })
        self.assertEqual(validators["paper-section"].script, ROOT / "scripts/paper_section_validator.py")

    def test_validator_registry_rejects_tampered_script(self):
        """Catches accepting a registry hash after validator contents change."""
        with TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            script = repository / "scripts/check.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('changed')\n", encoding="utf-8")
            registry = repository / "validator-registry.v1.json"
            registry.write_text(json.dumps({
                "schema_version": "paper-workflow-validator-registry-v1",
                "validators": {"check": {
                    "script": "scripts/check.py",
                    "sha256": hashlib.sha256(b"print('expected')\\n").hexdigest(),
                    "adapter": "paper_section_v1",
                    "input_schema": "paper_section_v1",
                    "control_tags": [],
                    "outcomes": ["pass", "fail", "blocked"],
                }},
            }), encoding="utf-8")
            with self.assertRaises(CatalogError) as caught:
                load_validator_registry(registry, repository)
            self.assertEqual(caught.exception.code, "validator.hash_mismatch")

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_validator_registry_rejects_symlinked_script(self):
        """Catches a hash-valid script path redirecting execution through a link."""
        with TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            target = repository / "target.py"
            target.parent.mkdir(parents=True)
            target.write_text("print('safe')\n", encoding="utf-8")
            script = repository / "scripts/check.py"
            script.parent.mkdir()
            script.symlink_to(target)
            registry = repository / "validator-registry.v1.json"
            registry.write_text(json.dumps({
                "schema_version": "paper-workflow-validator-registry-v1",
                "validators": {"check": {
                    "script": "scripts/check.py",
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "adapter": "paper_section_v1",
                    "input_schema": "paper_section_v1",
                    "control_tags": [],
                    "outcomes": ["pass", "fail", "blocked"],
                }},
            }), encoding="utf-8")
            with self.assertRaises(CatalogError) as caught:
                load_validator_registry(registry, repository)
            self.assertEqual(caught.exception.code, "validator.symlink")


if __name__ == "__main__":
    unittest.main()
