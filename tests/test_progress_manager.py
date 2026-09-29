import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import progress_manager as progress


ROOT = Path(__file__).resolve().parents[1]


class ProgressManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="progress-manager-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.md"
        self.path.write_text(progress.template("progress-test"), encoding="utf-8")

    def record_error(self, *, rule="check the inputs", **overrides):
        values = dict(
            file=str(self.path), stage="intake", error="input check failed",
            cause="incomplete material", impact="requires another check",
            severity="minor", blocking="false", rule=rule,
            check="rerun the input check", stages="intake", refs="inputs.md",
        )
        values.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(progress.cmd_record_error(types.SimpleNamespace(**values)), 0)

    def summary(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(progress.cmd_summary(types.SimpleNamespace(file=str(self.path))), 0)
        return json.loads(output.getvalue())

    def test_record_error_preserves_literal_backslashes_in_rule_and_handoff(self):
        for rule in (r"Use C:\new\results", r"Keep \alpha unchanged", r"Check the regex \d+", r"Do not expand \1 or \g<name>"):
            with self.subTest(rule=rule):
                self.path.write_text(progress.template("progress-test"), encoding="utf-8")
                self.record_error(rule=rule)
                text = progress.read_text(self.path)
                self.assertTrue(progress.validate_text(text)["valid"])
                self.assertIn(f"  - prevention_rule: {rule}\n", text)
                self.assertIn(f"- must_not_repeat: R001 — {rule}\n", text)
                self.assertNotIn("\x07", text)

    def test_upsert_preserves_literal_backslashes_and_only_changes_top_level_field(self):
        document = progress.template("progress-test").replace(
            "- next_action:", "  - next_action: nested example\n- next_action:", 1,
        )
        value = r"Review C:\new with \alpha and regex \d+"
        changed = progress._upsert_field(document, "## Current Snapshot", "next_action", value)
        self.assertIn("  - next_action: nested example\n", changed)
        self.assertIn(f"\n- next_action: {value}\n", changed)
        self.assertTrue(progress.validate_text(changed)["valid"])

    def test_rules_stay_in_their_section_with_an_intervening_extension(self):
        extension = "## Diagnostic Notes\nKeep this extension and its details.\n\n"
        document = progress.read_text(self.path).replace("## Decisions", extension + "## Decisions")
        self.path.write_text(document, encoding="utf-8")
        self.assertTrue(progress.validate_text(document)["valid"])

        self.record_error()
        self.record_error(rule="also inspect the result")
        text = progress.read_text(self.path)
        self.assertEqual(progress._section(text, "## Diagnostic Notes"), extension)
        self.assertEqual(self.summary()["active_rule_ids"], ["R001", "R002"])
        self.assertIn("- R001:", progress._section(text, "## Error Avoidance Rules"))
        self.assertIn("- R002:", progress._section(text, "## Error Avoidance Rules"))
        backup = progress.read_text(Path(str(self.path) + ".bak"))
        self.assertTrue(progress.validate_text(backup)["valid"])
        self.assertEqual(progress._active_rule_ids(backup), ["R001"])

    def test_blocking_rule_with_an_extension_updates_snapshot_and_handoff(self):
        document = progress.read_text(self.path).replace("## Decisions", "## Project Notes\nOptional notes.\n\n## Decisions")
        self.path.write_text(document, encoding="utf-8")
        self.record_error(severity="critical", blocking="true")
        summary = self.summary()
        self.assertEqual(summary["validity_status"], "blocked")
        self.assertEqual(summary["active_rule_ids"], ["R001"])

    def test_summary_reads_top_level_fields_not_nested_examples(self):
        document = progress.read_text(self.path).replace(
            "- current_stage: intake", "  - current_stage: drafting\n- current_stage: intake",
        ).replace(
            "- next_action:", "  - next_action: an indented example\n- next_action:", 1,
        ).replace(
            "- mode: guided_idea", "  - mode: autonomous_experiment\n- mode: guided_idea",
        )
        self.path.write_text(document, encoding="utf-8")
        summary = self.summary()
        self.assertEqual(summary["current_stage"], "intake")
        self.assertEqual(summary["mode"], "guided_idea")
        self.assertEqual(summary["next_action"], "diagnose the user's materials and confirm the entry stage")

    def test_summarize_text_is_pure_and_matches_cli_without_file(self):
        text = progress.read_text(self.path)
        before = {path.name: path.read_bytes() for path in self.path.parent.iterdir()}
        output = io.StringIO()
        with mock.patch("builtins.open", side_effect=AssertionError("unexpected file access")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("unexpected path access")), \
                mock.patch.object(progress, "atomic_write", side_effect=AssertionError("unexpected write")), \
                contextlib.redirect_stdout(output):
            result = progress.summarize_text(text)
            normalized = progress.summarize_text("\ufeff" + text.replace("\n", "\r\n"))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(result, normalized)
        self.assertNotIn("file", result)
        self.assertEqual(result["project_id"], "progress-test")
        self.assertEqual(result["document_sha256"], progress.document_sha256(text))
        cli = self.summary()
        self.assertEqual(cli.pop("file"), str(self.path.resolve()))
        self.assertEqual(result, cli)
        self.assertEqual({path.name: path.read_bytes() for path in self.path.parent.iterdir()}, before)

    def test_summarize_text_invalid_result_matches_existing_cli_behavior(self):
        text = "invalid progress document\n"
        self.path.write_text(text, encoding="utf-8")
        result = progress.summarize_text(text)
        self.assertEqual(set(result), {"valid", "validation_errors", "validation_warnings"})
        self.assertFalse(result["valid"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(progress.cmd_summary(types.SimpleNamespace(file=str(self.path))), 1)
        cli = json.loads(output.getvalue())
        self.assertEqual(cli.pop("file"), str(self.path.resolve()))
        self.assertEqual(result, cli)

    def test_top_level_invalid_stage_cannot_be_hidden_by_a_nested_valid_value(self):
        document = progress.read_text(self.path).replace(
            "- current_stage: intake", "  - current_stage: intake\n- current_stage: invalid-stage",
        )
        result = progress.validate_text(document)
        self.assertFalse(result["valid"])
        self.assertIn("invalid current_stage: invalid-stage", result["errors"])

    def test_rule_status_is_read_from_its_nested_fields(self):
        self.record_error(severity="critical", blocking="true")
        document = progress.read_text(self.path).replace("- R001:\n", "- R001:\n- status: resolved\n")
        self.assertTrue(progress.validate_text(document)["valid"])
        self.assertEqual(progress._active_rule_ids(document), ["R001"])

    def test_decision_owner_is_read_from_its_nested_fields(self):
        decision = """- D001:
- decision_owner: invalid-example
  - question: proceed?
  - chosen_option: wait
  - rejected_options: advance
  - decision_owner: user
  - evidence: user-confirmation

"""
        document = progress.read_text(self.path).replace(
            "## Decisions\n\n", "## Decisions\n\n" + decision,
        )
        self.assertTrue(progress.validate_text(document)["valid"])

    def test_restore_preserves_literal_rules_from_a_valid_backup(self):
        rule = r"Keep \alpha unchanged"
        self.record_error(rule=rule)
        self.record_error(rule="inspect the second input")
        self.path.write_text("corrupt progress\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(progress.cmd_restore(types.SimpleNamespace(file=str(self.path))), 0)
        restored = progress.read_text(self.path)
        self.assertTrue(progress.validate_text(restored)["valid"])
        self.assertIn(f"- must_not_repeat: R001 — {rule}\n", restored)
        self.assertIn("[recovery] restored from validated backup generation", restored)
        self.assertEqual(len(list(self.path.parent.glob("progress.md.corrupt-*"))), 1)


class ProgressSnapshotUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="progress-snapshot-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / ".research" / "progress.md"
        self.path.parent.mkdir()
        self.path.write_text(progress.template("snapshot-test"), encoding="utf-8")

    def digest(self):
        return hashlib.sha256(progress.read_text(self.path).encode("utf-8")).hexdigest()

    def arguments(self, **changes):
        values = dict(project=str(self.root), summary="记录当前进度", refs="notes.md", expected_sha256=self.digest())
        values.update(changes)
        return types.SimpleNamespace(**values)

    def update(self, **changes):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(progress.cmd_update_snapshot(self.arguments(**changes)), 0)

    def assert_unchanged_after(self, changes, exception=progress.ProgressError):
        original = self.path.read_bytes()
        backup = Path(str(self.path) + ".bak")
        backup.write_text("keep the previous backup", encoding="utf-8")
        with self.assertRaises(exception):
            progress.cmd_update_snapshot(self.arguments(**changes))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(backup.read_text(encoding="utf-8"), "keep the previous backup")

    def test_summary_reports_digest_of_canonical_utf8_document(self):
        text = progress.read_text(self.path)
        self.path.write_bytes(("\ufeff" + text.replace("\n", "\r\n")).encode("utf-8"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(progress.cmd_summary(types.SimpleNamespace(file=str(self.path))), 0)
        self.assertEqual(json.loads(output.getvalue())["document_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_updates_named_fields_preserves_extensions_and_appends_evidence(self):
        extension = "## 研究补充\n保持这个扩展章节。\n\n"
        original = progress.read_text(self.path).replace("## Core Progress", extension + "## Core Progress")
        self.path.write_text(original, encoding="utf-8")
        self.update(
            current_stage="literature", entry_mode="write_or_revise", research_question="模型能否解释观测差异？",
            selected_direction="检验已有研究材料", current_status="资料已整理", completed_milestones="研究问题已确认",
            next_action=r"读取 C:\研究\draft.md 并核查 \alpha", blockers="等待补充资料", hub_status="degraded",
            last_stage_receipt="receipts/intake.json", summary="整理材料并进入文献阶段", refs="报告[1].md",
        )
        text = progress.read_text(self.path)
        self.assertTrue(progress.validate_text(text)["valid"])
        self.assertEqual(progress._section(text, "## 研究补充"), extension)
        self.assertIn("- current_stage: literature\n", text)
        self.assertIn("- mode: write_or_revise\n", text)
        self.assertIn("- hub_status: degraded\n", text)
        self.assertIn("- last_stage_receipt: receipts/intake.json\n", text)
        self.assertIn(r"- resume_instruction: 读取 C:\研究\draft.md 并核查 \alpha" + "\n", text)
        self.assertIn("[literature] [milestone] 整理材料并进入文献阶段 [报告%5B1%5D.md]", text)
        self.assertEqual(progress.read_text(Path(str(self.path) + ".bak")), original)
        self.assertFalse((self.root / ".research/custom-workflow/selection.json").exists())

    def test_explicit_resume_instruction_takes_priority(self):
        self.update(next_action="继续查找文献", resume_instruction="先确认新增资料的来源")
        text = progress.read_text(self.path)
        self.assertIn("- next_action: 继续查找文献\n", text)
        self.assertIn("- resume_instruction: 先确认新增资料的来源\n", text)
        self.assertIn("- next_agent_reads: Project Metadata, Current Snapshot, active Error Avoidance Rules, Decisions, Open Questions and Risks", text)
        self.assertIn("- active_constraints: use Codex-internal subagents; verify evidence before formal prose", text)

    def test_stale_digest_and_empty_update_do_not_change_main_or_backup(self):
        for changes in ({"expected_sha256": "0" * 64, "current_status": "changed"}, {}):
            with self.subTest(changes=changes):
                self.assert_unchanged_after(changes)

    def test_invalid_existing_document_is_not_modified(self):
        self.path.write_text("invalid progress document\n", encoding="utf-8")
        self.assert_unchanged_after({"next_action": "must not apply"})

    def test_snapshot_paths_reject_symlinks_without_touching_targets(self):
        from scripts.workflow_engine.fs import PathSafetyError

        for suffix in ("", ".lock", ".bak"):
            with self.subTest(suffix=suffix), TemporaryDirectory(prefix="progress-path-test-") as directory:
                project = Path(directory).resolve() / "project"
                progress_path = project / ".research/progress.md"
                progress_path.parent.mkdir(parents=True)
                original = progress.template("path-test")
                progress_path.write_text(original, encoding="utf-8")
                external = Path(directory).resolve() / "external.md"
                external.write_text(original if not suffix else "keep external contents", encoding="utf-8")
                link = Path(str(progress_path) + suffix)
                if not suffix:
                    link.unlink()
                try:
                    link.symlink_to(external)
                except OSError as exc:
                    if os.name == "nt":
                        self.skipTest(f"Windows symlink creation is unavailable: {exc}")
                    raise
                external_before = external.read_bytes()
                with self.assertRaises(PathSafetyError):
                    progress.cmd_update_snapshot(self.arguments(
                        project=str(project), expected_sha256=progress.document_sha256(original), next_action="must not apply",
                    ))
                self.assertTrue(link.is_symlink())
                self.assertEqual(external.read_bytes(), external_before)
                self.assertEqual(progress_path.read_text(encoding="utf-8"), original)

    def test_rejects_invalid_fields_and_event_text_without_writes(self):
        for changes in (
            {"current_stage": "unknown"}, {"entry_mode": "custom"}, {"hub_status": "unknown"},
            {"next_action": ""}, {"next_action": "line one\nline two"},
            {"next_action": "review", "summary": "ambiguous [summary]"},
            {"next_action": "review", "refs": ""}, {"expected_sha256": "not-a-digest", "next_action": "review"},
        ):
            with self.subTest(changes=changes):
                self.assert_unchanged_after(changes)

    def test_same_read_digest_cannot_commit_twice(self):
        digest = self.digest()
        self.update(next_action="first update", expected_sha256=digest)
        main_before = self.path.read_bytes()
        backup = Path(str(self.path) + ".bak")
        backup_before = backup.read_bytes()
        with self.assertRaisesRegex(progress.ProgressError, "changed|sha256|conflict"):
            progress.cmd_update_snapshot(self.arguments(next_action="stale update", expected_sha256=digest))
        self.assertEqual(self.path.read_bytes(), main_before)
        self.assertEqual(backup.read_bytes(), backup_before)

    def test_active_blocking_rule_and_pending_validation_are_not_cleared(self):
        with contextlib.redirect_stdout(io.StringIO()):
            progress.cmd_record_error(types.SimpleNamespace(
                file=str(self.path), stage="intake", error="invalid data", cause="missing data", impact="conclusion affected",
                severity="critical", blocking="true", rule="verify the dataset", check="recheck data", stages="all", refs="data.csv",
            ))
        original_rules = progress._section(progress.read_text(self.path), "## Error Avoidance Rules")
        self.update(current_status="正在补充证据", next_action="核对新数据")
        text = progress.read_text(self.path)
        self.assertEqual(progress._section(text, "## Error Avoidance Rules"), original_rules)
        self.assertIn("- validity_status: blocked\n", text)
        self.assertEqual(progress._active_rule_ids(text), ["R001"])

    def test_restore_can_return_to_the_generation_before_snapshot_update(self):
        original = progress.read_text(self.path)
        self.update(next_action="new action")
        with contextlib.redirect_stdout(io.StringIO()):
            progress.cmd_restore(types.SimpleNamespace(file=str(self.path)))
        restored = progress.read_text(self.path)
        self.assertEqual(progress._section_field(progress._section(restored, "## Current Snapshot"), "next_action"),
                         progress._section_field(progress._section(original, "## Current Snapshot"), "next_action"))
        self.assertIn("[recovery] restored from validated backup generation", restored)

    def test_failed_main_replace_keeps_original_and_a_valid_backup(self):
        original = self.path.read_bytes()
        replace = progress._replace_with_retry

        def fail_main(source, destination):
            if Path(destination) == self.path:
                raise OSError("simulated replace failure")
            return replace(source, destination)

        with mock.patch.object(progress, "_replace_with_retry", side_effect=fail_main):
            with self.assertRaises(OSError):
                progress.cmd_update_snapshot(self.arguments(next_action="new action"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(Path(str(self.path) + ".bak").read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_custom_or_invalid_selection_cannot_modify_official_progress(self):
        from scripts.workflow_engine.store import Selection, StoreError

        custom = Selection("custom", 1, "custom-test", 1, "a" * 64, (), "a" * 64, "2026-09-29T00:00:00Z").to_payload()
        selection = self.root / ".research/custom-workflow/selection.json"
        selection.parent.mkdir(parents=True)
        for contents in (json.dumps(custom), "{corrupt"):
            with self.subTest(contents=contents):
                selection.write_text(contents, encoding="utf-8")
                self.assert_unchanged_after({"next_action": "must not apply"}, StoreError)

    def test_cli_accepts_direct_script_and_module_entry_points(self):
        for invocation in ([str(ROOT / "scripts/progress_manager.py")], ["-m", "scripts.progress_manager"]):
            with self.subTest(invocation=invocation):
                command = [sys.executable, "-B", *invocation, "update-snapshot", "--project", str(self.root),
                           "--expected-sha256", self.digest(), "--summary", "记录进度", "--refs", "notes.md",
                           "--entry-mode", "draft_audit", "--next-action", "审核当前稿件"]
                result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("- mode: draft_audit\n", progress.read_text(self.path))
                self.assertIn("- next_action: 审核当前稿件\n", progress.read_text(self.path))


class ProgressFileSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="progress-safety-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.path = self.project / ".research" / "progress.md"
        self.path.parent.mkdir(parents=True)
        self.original = progress.template("safety-test")
        self.path.write_text(self.original, encoding="utf-8")

    def update_command(self):
        return [sys.executable, "-B", str(ROOT / "scripts/progress_manager.py"),
                "update-snapshot", "--project", str(self.project),
                "--expected-sha256", progress.document_sha256(self.original),
                "--summary", "Record progress", "--refs", "notes.md",
                "--next-action", "Review the new material"]

    def test_hardlinked_empty_lock_is_rejected_without_any_external_write(self):
        from scripts.artifact_manager import ArtifactService

        external = self.root / "external-empty-file"
        external.touch()
        os.link(external, progress._lock_path(self.path))
        result = subprocess.run(self.update_command(), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("single-link regular file", result.stderr)
        self.assertEqual(external.read_bytes(), b"")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertFalse(Path(str(self.path) + ".bak").exists())
        service = ArtifactService(self.project)
        request = {
            "schema_version": "confirmed-artifact-request-v1", "operation_id": "unsafe-lock-test",
            "artifact_id": "manuscript", "artifact_type": "manuscripts", "expected_catalog_revision": 0,
            "entrypoint": "paper.md", "files": [{"source_path": "paper.md", "relative_path": "paper.md",
                "sha256": "0" * 64}], "confirmation": "Explicit test confirmation.",
            "expected_progress_sha256": progress.document_sha256(self.original),
        }
        with self.assertRaisesRegex(progress.ProgressError, "single-link regular file"):
            service.accept(request, confirmed=True)
        self.assertEqual(external.read_bytes(), b"")
        self.assertFalse((self.path.parent / "confirmed-artifacts" / "catalog.json").exists())

    def test_public_reader_and_summary_reject_linked_progress_files(self):
        external = self.root / "external-progress.md"
        external.write_text(self.original, encoding="utf-8")
        for kind in ("hardlink", "symlink"):
            with self.subTest(kind=kind):
                self.path.unlink()
                try:
                    if kind == "hardlink":
                        os.link(external, self.path)
                    else:
                        self.path.symlink_to(external)
                except OSError as exc:
                    if os.name == "nt" and kind == "symlink":
                        self.skipTest(f"Windows symlink creation is unavailable: {exc}")
                    raise
                with self.assertRaisesRegex(progress.ProgressError, "single-link regular file"):
                    progress.read_text(self.path)
                output = io.StringIO()
                with self.assertRaises(progress.ProgressError), contextlib.redirect_stdout(output):
                    progress.cmd_summary(types.SimpleNamespace(file=str(self.path)))
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(external.read_text(encoding="utf-8"), self.original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes require os.mkfifo")
    def test_fifo_progress_and_lock_fail_fast_in_subprocesses(self):
        for suffix in ("", ".lock"):
            with self.subTest(suffix=suffix):
                target = Path(str(self.path) + suffix)
                if target.exists():
                    target.unlink()
                os.mkfifo(target)
                try:
                    result = subprocess.run(self.update_command(), capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("single-link regular file", result.stderr)
                    self.assertFalse(Path(str(self.path) + ".bak").exists())
                finally:
                    target.unlink()
                self.path.write_text(self.original, encoding="utf-8")

    def test_reader_rejects_directories_and_oversized_documents(self):
        with self.assertRaisesRegex(progress.ProgressError, "single-link regular file"):
            progress.read_text(self.path.parent)
        with mock.patch.object(progress, "MAX_PROGRESS_BYTES", 16):
            with self.assertRaisesRegex(progress.ProgressError, "exceeds 16 bytes"):
                progress.read_text(self.path)
            with self.assertRaisesRegex(progress.ProgressError, "exceeds 16 bytes"):
                progress.atomic_write(self.path, "x" * 17)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertFalse(Path(str(self.path) + ".bak").exists())

    @unittest.skipIf(os.name == "nt", "replacing an open file is a POSIX race fixture")
    def test_replacing_progress_during_a_read_is_rejected(self):
        original_read = os.read
        replacement = self.path.with_name("replacement.md")
        replacement.write_text(self.original, encoding="utf-8")
        replaced = False

        def replace_during_read(descriptor, size):
            nonlocal replaced
            data = original_read(descriptor, size)
            if not replaced:
                os.replace(replacement, self.path)
                replaced = True
            return data

        with mock.patch.object(progress.os, "read", side_effect=replace_during_read):
            with self.assertRaisesRegex(progress.ProgressError, "changed while open|regular file"):
                progress.read_text(self.path)

    @unittest.skipIf(os.name == "nt", "replacing an open file is a POSIX race fixture")
    def test_replacing_lock_after_open_is_rejected_before_initialization(self):
        lock = progress._lock_path(self.path)
        lock.touch()
        original_open = os.open
        retired = lock.with_name("retired-lock")

        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if Path(path) == lock:
                os.replace(lock, retired)
                replacement = original_open(lock, os.O_RDWR | os.O_CREAT, 0o600)
                os.close(replacement)
            return descriptor

        with mock.patch.object(progress.os, "open", side_effect=replace_after_open):
            with self.assertRaisesRegex(progress.ProgressError, "changed while open"):
                with progress.progress_lock(self.path):
                    self.fail("a replaced lock must not authorize an update")
        self.assertEqual(retired.read_bytes(), b"")
        self.assertEqual(lock.read_bytes(), b"")


class ProgressConfirmedArtifactSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="progress-artifact-summary-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.path = self.project / ".research" / "progress.md"
        progress.atomic_write(self.path, progress.template("summary-test"))

    def summary(self, *, expected_code=0, path=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(progress.cmd_summary(types.SimpleNamespace(file=str(path or self.path))), expected_code)
        return json.loads(output.getvalue())

    def file_bytes(self):
        return {path.relative_to(self.project): path.read_bytes()
                for path in self.project.rglob("*") if path.is_file()}

    def test_missing_catalog_reports_empty_compact_summary_without_creating_files(self):
        before = self.file_bytes()
        result = self.summary()
        self.assertTrue(result["valid"])
        self.assertEqual(result["confirmed_artifacts"]["catalog_revision"], 0)
        self.assertEqual(result["confirmed_artifacts"]["current_count"], 0)
        self.assertEqual(self.file_bytes(), before)
        self.assertFalse((self.path.parent / "confirmed-artifacts").exists())

    def test_corrupt_catalog_invalidates_summary_without_faking_empty_counts(self):
        catalog = self.path.parent / "confirmed-artifacts" / "catalog.json"
        catalog.parent.mkdir()
        catalog.write_text("{corrupt catalog", encoding="utf-8")
        before = self.file_bytes()
        result = self.summary(expected_code=1)
        self.assertFalse(result["valid"])
        self.assertNotIn("confirmed_artifacts", result)
        self.assertTrue(any("confirmed artifact catalog is invalid" in error
                            for error in result["validation_errors"]))
        self.assertEqual(self.file_bytes(), before)

    def test_standalone_summary_does_not_guess_a_project_catalog(self):
        standalone = self.project / "progress.md"
        standalone.write_text(progress.read_text(self.path), encoding="utf-8")
        for path in (standalone, self.path.with_name("notes.md")):
            if not path.exists():
                path.write_text(progress.read_text(self.path), encoding="utf-8")
            with self.subTest(path=path), mock.patch(
                "scripts.confirmed_artifacts.confirmed_artifact_summary",
                side_effect=AssertionError("standalone summary must not inspect a project catalog"),
            ):
                self.assertNotIn("confirmed_artifacts", self.summary(path=path))

    def test_confirmed_and_withdrawn_versions_appear_in_read_only_compact_summary(self):
        from scripts.artifact_manager import ArtifactService

        source = self.project / "paper.md"
        source.write_text("An accepted manuscript.\n", encoding="utf-8")
        evidence = self.project / "checks.json"
        evidence.write_text('{"status":"pass"}\n', encoding="utf-8")
        service = ArtifactService(self.project)
        service.accept({
            "schema_version": "confirmed-artifact-request-v1", "operation_id": "confirm-summary",
            "artifact_id": "manuscript", "artifact_type": "manuscripts", "expected_catalog_revision": 0,
            "entrypoint": "paper.md", "files": [{"source_path": "paper.md", "relative_path": "paper.md",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
            "confirmation": "The user accepts this checked manuscript.",
            "expected_progress_sha256": progress.document_sha256(progress.read_text(self.path)),
            "evidence": [{"path": "checks.json", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
        }, confirmed=True)
        before = self.file_bytes()
        result = self.summary()["confirmed_artifacts"]
        self.assertEqual((result["catalog_revision"], result["current_count"], result["withdrawn_count"]), (1, 1, 0))
        self.assertEqual(set(result), {"catalog_revision", "current_count", "withdrawn_count", "index_path", "verification", "projection_pending"})
        self.assertEqual(self.file_bytes(), before)
        service.catalog.withdraw("manuscript", 1, "withdraw-summary", "The user withdrew the approval.")
        result = self.summary()["confirmed_artifacts"]
        self.assertEqual((result["catalog_revision"], result["current_count"], result["withdrawn_count"]), (2, 0, 1))


if __name__ == "__main__":
    unittest.main()
