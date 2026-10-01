"""Functional tests for the hash-locked validator scripts.

``references/workflows/validator-registry.v1.json`` pins the bytes of
``scripts/paper_section_validator.py`` and ``scripts/humanizer_preflight.py``, so
this module adds direct functional coverage without editing either script.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import humanizer_preflight
from scripts import paper_section_validator


EN_BODY = """# A Reproducible Study

## Introduction
This study motivates the problem and states the hypothesis in enough detail.

## Methods
We describe the materials and the analysis pipeline used for the experiment.

## Results
The measured outcomes are reported with supporting tables and figures.

## Discussion
We interpret the findings and explain the mechanism, then compare them with prior work.
We discuss the boundary and applicability scope, and close with the limitation and caveat.

## Conclusion
We conclude that the approach is effective within the studied scope.
"""

ZH_BODY = """# 可复现研究

## 引言
本研究说明问题背景并提出假设。

## 方法
我们介绍材料与实验分析流程。

## 结果
测量结果以表格与图形给出。

## 讨论
我们解释结果背后的机制并进行比较，同时讨论适用边界与推广范围，指出局限与不足。

## 结论
结论认为该方案在研究范围内有效。
"""


class PaperSectionValidatorTests(unittest.TestCase):
    """Exercise the structural and semantic gate exposed by ``validate``."""

    def validate(self, text, **overrides):
        kwargs = {
            "phase": "body",
            "method_profile": "method-first",
            "paper_type": "empirical",
            "language": "en",
            "validity_status": "pending",
            "discussion_integrated": False,
            "semantic_receipt": None,
            "manuscript_path": None,
        }
        kwargs.update(overrides)
        return paper_section_validator.validate(text, **kwargs)

    def test_english_empirical_body_is_valid(self):
        result = self.validate(EN_BODY)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["valid"])

    def test_chinese_empirical_body_is_valid(self):
        result = self.validate(ZH_BODY, language="zh")
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["valid"])

    def test_missing_title_and_required_sections_are_reported(self):
        result = self.validate("")
        self.assertFalse(result["valid"])
        self.assertIn("missing required title heading", result["errors"])
        self.assertIn("missing required section: introduction", result["errors"])

    def test_blocked_validity_status_stops_advancement(self):
        result = self.validate(EN_BODY, validity_status="blocked")
        self.assertIn(
            "validity_status: blocked prevents section advancement",
            result["errors"],
        )

    def test_abstract_is_forbidden_during_body_phase(self):
        text = EN_BODY.replace(
            "## Introduction",
            "## Abstract\nA premature abstract body.\n\n## Introduction",
        )
        result = self.validate(text)
        self.assertIn("abstract must be drafted only after the body is complete", result["errors"])

    def test_missing_discussion_function_is_reported(self):
        text = "\n".join(
            line
            for line in EN_BODY.splitlines()
            if line.strip() != "## Discussion" and "interpret the findings" not in line
        )
        result = self.validate(text)
        self.assertIn(
            "Discussion heading may be omitted only with an explicitly integrated discussion function",
            result["errors"],
        )

    def test_data_first_profile_requires_methods_section(self):
        text = (
            "# Title\n\n## Introduction\nIntro body text.\n\n"
            "## Results\nResults body text.\n\n## Conclusion\nConclusion body text.\n"
        )
        result = self.validate(text, method_profile="data-first")
        self.assertIn("data-first profile requires a Methods section", result["errors"])

    def test_final_phase_requires_semantic_receipt(self):
        text = EN_BODY.replace(
            "## Introduction",
            "## Abstract\nA complete abstract body.\n\n## Introduction",
        ) + "\n## References\nReference entries.\n"
        result = self.validate(text, phase="final")
        self.assertIn("final phase requires an independent semantic receipt", result["errors"])


class HumanizerPreflightTests(unittest.TestCase):
    """Exercise the fail-closed primitives and CLI of the humanizer preflight."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def test_detect_format_uses_suffix_and_aliases(self):
        self.assertEqual(humanizer_preflight.detect_format(Path("paper.md"), ""), "markdown")
        self.assertEqual(humanizer_preflight.detect_format(Path("paper.markdown"), ""), "markdown")
        self.assertEqual(humanizer_preflight.detect_format(Path("paper.tex"), "latex"), "latex")
        self.assertEqual(humanizer_preflight.detect_format(Path("paper.pdf"), ".PDF"), "pdf")

    def test_detect_format_rejects_conflicting_override(self):
        with self.assertRaises(humanizer_preflight.PreflightError):
            humanizer_preflight.detect_format(Path("paper.md"), "latex")

    def test_detect_format_rejects_unknown_suffix(self):
        with self.assertRaises(humanizer_preflight.PreflightError):
            humanizer_preflight.detect_format(Path("paper.txt"), "")

    def test_sha256_file_matches_hex_digest(self):
        path = self.root / "sample.md"
        path.write_bytes(b"canonical bytes")
        expected = "sha256:" + hashlib.sha256(b"canonical bytes").hexdigest()
        self.assertEqual(humanizer_preflight.sha256_file(path), expected)

    def test_verify_immutable_copy_accepts_identical_copy(self):
        original = self.root / "manuscript.md"
        original.write_bytes(b"canonical bytes")
        copy = self.root / "copy.md"
        copy.write_bytes(b"canonical bytes")
        humanizer_preflight.verify_immutable_copy(
            original, copy, humanizer_preflight.sha256_file(original)
        )

    def test_verify_immutable_copy_rejects_tampering(self):
        original = self.root / "manuscript.md"
        original.write_bytes(b"canonical bytes")
        copy = self.root / "copy.md"
        copy.write_bytes(b"tampered bytes")
        with self.assertRaises(humanizer_preflight.PreflightError):
            humanizer_preflight.verify_immutable_copy(
                original, copy, humanizer_preflight.sha256_file(original)
            )

    def test_verify_humanizer_skill_reads_declared_version(self):
        skill = self.root / "SKILL.md"
        skill.write_text('name: humanizer\n  version: "3.4.5"\n', encoding="utf-8")
        digest, version = humanizer_preflight.verify_humanizer_skill(skill)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(version, "3.4.5")

    def test_verify_humanizer_skill_rejects_other_package(self):
        skill = self.root / "SKILL.md"
        skill.write_text('name: something-else\n  version: "1.0"\n', encoding="utf-8")
        with self.assertRaises(humanizer_preflight.PreflightError):
            humanizer_preflight.verify_humanizer_skill(skill)

    def test_main_fails_closed_when_input_is_missing(self):
        argv = [
            "humanizer_preflight",
            "--input", str(self.root / "missing.md"),
            "--candidate", str(self.root / "candidate.md"),
            "--adapter-contract", str(self.root / "contract.json"),
            "--immutable-copy", str(self.root / "copy.md"),
            "--protected-manifest", str(self.root / "manifest.json"),
            "--claim-diff", str(self.root / "claim.json"),
            "--integrity-report", str(self.root / "integrity.json"),
        ]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            code = humanizer_preflight.main()
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["canonical_mutation_allowed"])


if __name__ == "__main__":
    unittest.main()