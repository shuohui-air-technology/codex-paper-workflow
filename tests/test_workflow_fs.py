"""Cross-platform regression tests for project-contained artifact hashing."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts.workflow_engine.fs import (
    PathSafetyError,
    _validate_windows_project_parts,
    _windows_path_is_within,
    _windows_path_key,
    hash_project_file,
    read_project_json_object,
)


class ProjectFileHashTests(unittest.TestCase):
    def test_hash_project_file_reads_nested_unicode_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "论文" / "结果.md"
            path.parent.mkdir()
            content = "结果与证据\n".encode("utf-8")
            path.write_bytes(content)

            self.assertEqual(
                hash_project_file(root, "论文/结果.md"),
                hashlib.sha256(content).hexdigest(),
            )

    def test_hash_project_file_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            external = Path(outside) / "source.md"
            external.write_text("outside", encoding="utf-8")
            try:
                (root / "linked.md").symlink_to(external)
            except OSError as exc:
                if os.name == "nt":
                    self.skipTest(f"Windows symlink creation is unavailable: {exc}")
                raise
            with self.assertRaises(PathSafetyError):
                hash_project_file(root, "linked.md")

    def test_hash_project_file_rejects_hard_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "ordinary.md"
            original.write_text("one file", encoding="utf-8")
            alias = root / "hard-linked.md"
            try:
                os.link(original, alias)
            except OSError as exc:
                self.skipTest(f"filesystem does not support hard links: {exc}")
            with self.assertRaises(PathSafetyError):
                hash_project_file(root, "ordinary.md")

    def test_windows_final_path_check_rejects_escape_and_prefix_confusion(self):
        root = _windows_path_key(r"C:\work\project")
        self.assertTrue(
            _windows_path_is_within(root, _windows_path_key(r"C:\work\project\paper.md"))
        )
        self.assertFalse(
            _windows_path_is_within(root, _windows_path_key(r"C:\work\project-copy\paper.md"))
        )
        self.assertFalse(
            _windows_path_is_within(root, _windows_path_key(r"D:\outside\paper.md"))
        )
        self.assertEqual(
            _windows_path_key(r"\\?\UNC\server\share\project"),
            _windows_path_key(r"\\server\share\project"),
        )

    def test_windows_project_path_rejects_ads_and_reserved_names(self):
        for parts in (
            ("paper.md:secret",),
            ("CON.txt",),
            ("COM¹.log",),
            ("folder.", "paper.md"),
            ("folder ", "paper.md"),
            ("bad|name.md",),
        ):
            with self.subTest(parts=parts), self.assertRaises(PathSafetyError):
                _validate_windows_project_parts(parts)


class ProjectJSONTests(unittest.TestCase):
    def test_unicode_json_object_is_read_without_changing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "request.json"
            source.write_text('{"说明":"论文确认"}', encoding="utf-8")
            before = source.read_bytes()
            self.assertEqual(read_project_json_object(root, "request.json"), {"说明": "论文确认"})
            self.assertEqual(source.read_bytes(), before)

    def test_duplicate_nonfinite_and_nonobject_requests_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for value in ('{"x":1,"x":2}', '{"x":NaN}', '[]'):
                with self.subTest(value=value):
                    (root / "request.json").write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_project_json_object(root, "request.json")

    def test_json_size_limit_and_changed_read_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "request.json"
            source.write_text('{"x":12345}', encoding="utf-8")
            with mock.patch("scripts.workflow_engine.fs.MAX_JSON_BYTES", 8):
                with self.assertRaises(PathSafetyError):
                    read_project_json_object(root, "request.json")
            with mock.patch("scripts.workflow_engine.fs.hash_project_file", side_effect=["a" * 64, "b" * 64]):
                with self.assertRaises(PathSafetyError):
                    read_project_json_object(root, "request.json")


if __name__ == "__main__":
    unittest.main()
