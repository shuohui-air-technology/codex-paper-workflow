#!/usr/bin/env python3
"""Validate the structural paper-section gate and its final semantic receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SECTION_ALIASES = {
    "abstract": "abstract",
    "keywords": "keywords",
    "introduction": "introduction",
    "materials and methods": "methods",
    "materials & methods": "methods",
    "methods": "methods",
    "methodology": "methods",
    "results": "results",
    "analysis": "discussion",
    "analysis and interpretation": "discussion",
    "analysis & interpretation": "discussion",
    "discussion": "discussion",
    "results and discussion": "combined",
    "results & discussion": "combined",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "references": "references",
    "bibliography": "references",
    "appendix": "appendix",
    "appendices": "appendix",
    "摘要": "abstract",
    "关键词": "keywords",
    "关键字": "keywords",
    "引言": "introduction",
    "绪论": "introduction",
    "材料与方法": "methods",
    "材料和方法": "methods",
    "研究方法": "methods",
    "方法": "methods",
    "结果": "results",
    "结果与讨论": "combined",
    "结果和讨论": "combined",
    "讨论": "discussion",
    "结论": "conclusion",
    "参考文献": "references",
    "附录": "appendix",
}
ORDER = ["abstract", "keywords", "introduction", "methods", "results", "discussion", "conclusion", "references", "appendix"]
PAPER_TYPES = {"empirical", "theoretical", "review", "protocol"}
LANGUAGES = {"en", "zh"}


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - CLI misuse
        print(json.dumps({"valid": False, "errors": [message], "warnings": []}, ensure_ascii=False, indent=2))
        raise SystemExit(1)


def _normalise_heading(raw_heading: str) -> str:
    raw_heading = re.sub(r"\s+#+\s*$", "", raw_heading)
    raw = re.sub(r"[*_\x60]", "", raw_heading).strip().lower()
    return re.sub(r"^\d+(?:\.\d+)*\s*", "", raw).strip()


def _heading_records(text: str) -> list[tuple[int, int, int, str, str | None]]:
    records: list[tuple[int, int, int, str, str | None]] = []
    offset = 0
    fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= fence[1]:
                fence = None
            offset += len(line)
            continue
        if fence_match:
            fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            offset += len(line)
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.rstrip("\r\n"))
        if match:
            level = len(match.group(1))
            raw = _normalise_heading(match.group(2))
            records.append((offset, offset + len(line), level, raw, SECTION_ALIASES.get(raw)))
        offset += len(line)
    return records


def headings(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for _start, _end, level, raw, section in _heading_records(text):
        if level != 2:
            continue
        if section == "combined":
            found.extend([("results", raw), ("discussion", raw)])
        elif section:
            found.append((section, raw))
    return found


def section_blocks(text: str) -> list[tuple[str, str, str]]:
    """Return recognized sections while retaining nested subsection content."""
    records = _heading_records(text)
    blocks: list[tuple[str, str, str]] = []
    for index, (_start, end, level, raw, section) in enumerate(records):
        if not section:
            continue
        block_end = len(text)
        for next_start, _next_end, next_level, _next_raw, _next_section in records[index + 1:]:
            if next_level <= level:
                block_end = next_start
                break
        content = text[end:block_end].strip()
        if section == "combined":
            blocks.extend([("results", raw, content), ("discussion", raw, content)])
        else:
            blocks.append((section, raw, content))
    return blocks


def _content_for(blocks: list[tuple[str, str, str]], section: str) -> str:
    return "\n".join(content for name, _heading, content in blocks if name == section).strip()


def _without_fenced_blocks(text: str) -> str:
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= fence[1]:
                fence = None
            continue
        if fence_match:
            fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            continue
        kept.append(line)
    return "\n".join(kept)


def _discussion_function_error(
    blocks: list[tuple[str, str, str]],
    top_level_sections: list[str],
    discussion_integrated: bool,
    language: str,
) -> str | None:
    has_discussion_heading = "discussion" in top_level_sections
    discussion_content = _content_for(blocks, "discussion")
    if has_discussion_heading and not discussion_content:
        return "Discussion section has no body content"
    if has_discussion_heading:
        source = discussion_content
    elif discussion_integrated and "results" in top_level_sections:
        source = _content_for(blocks, "results")
        if not source:
            return "integrated Discussion function requires a Results or Analysis section body"
    else:
        return "Discussion heading may be omitted only with an explicitly integrated discussion function"
    source = _without_fenced_blocks(source)
    if language == "zh":
        groups = {
            "interpretation/comparison": ("解释", "机制", "比较", "对比"),
            "application boundary": ("边界", "适用", "推广", "范围"),
            "limitations": ("局限", "限制", "不足"),
        }
    else:
        source = source.lower()
        groups = {
            "interpretation/comparison": ("interpret", "explain", "mechanism", "compar"),
            "application boundary": ("boundary", "applicab", "generaliz", "scope"),
            "limitations": ("limitation", "limit", "caveat"),
        }
    missing = [name for name, terms in groups.items() if not any(term in source for term in terms)]
    if missing:
        return "Discussion function lacks: " + ", ".join(missing)
    return None


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_receipt_path(base: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts or any(char in value for char in "*?[]"):
        raise ValueError(f"{label} must be a safe relative path")
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside its receipt directory") from exc
    return resolved


def _load_semantic_receipt(
    path: Path,
    paper_type: str,
    language: str,
    method_profile: str,
    discussion_integrated: bool,
    sections: list[str],
    manuscript_path: Path,
) -> list[str]:
    if not path.is_file():
        return [f"semantic receipt does not exist: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["semantic receipt must be readable UTF-8 JSON"]
    if not isinstance(data, dict) or data.get("status") != "pass":
        return ["semantic receipt must contain status: pass"]
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("semantic receipt must contain schema_version: 1")
    verifier_id = data.get("verifier_id")
    if not isinstance(verifier_id, str) or not verifier_id.strip():
        errors.append("semantic receipt must contain a verifier_id")
    try:
        verifier_path = _resolve_receipt_path(path.parent, data.get("verifier_receipt_path"), "semantic verifier receipt")
        verifier_hash = data.get("verifier_receipt_sha256")
        if not isinstance(verifier_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", verifier_hash):
            errors.append("semantic receipt must contain a verifier_receipt_sha256")
        elif _sha256_file(verifier_path) != verifier_hash:
            errors.append("semantic verifier receipt hash does not match")
        else:
            verifier = json.loads(verifier_path.read_text(encoding="utf-8"))
            if not isinstance(verifier, dict) or verifier.get("status") != "pass":
                errors.append("semantic verifier receipt must contain status: pass")
            else:
                if verifier.get("verifier_id") != verifier_id:
                    errors.append("semantic verifier identity does not match")
                if verifier.get("manuscript_sha256") != _sha256_file(manuscript_path):
                    errors.append("semantic verifier receipt manuscript_sha256 does not match")
                if verifier.get("paper_type") != paper_type or verifier.get("language") != language:
                    errors.append("semantic verifier receipt paper_type/language does not match")
                if verifier.get("method_profile") != method_profile or verifier.get("discussion_integrated") is not discussion_integrated:
                    errors.append("semantic verifier receipt profile does not match")
                verifier_refs = verifier.get("evidence_refs")
                if not isinstance(verifier_refs, list) or not verifier_refs:
                    errors.append("semantic verifier receipt evidence_refs must be non-empty")
                verifier_checks = verifier.get("checks")
                if not isinstance(verifier_checks, dict):
                    errors.append("semantic verifier receipt must contain an independent checks object")
                else:
                    for check_name in ("discussion_function", "conclusion_function", "abstract_consistency"):
                        if verifier_checks.get(check_name) != "pass":
                            errors.append(f"semantic verifier receipt must contain {check_name}: pass")
                verifier_sections = verifier.get("sections")
                if not isinstance(verifier_sections, list) or any(not isinstance(item, str) for item in verifier_sections):
                    errors.append("semantic verifier receipt sections must be a list")
                else:
                    required_verifier_sections = {"introduction", "methods", "discussion", "conclusion"}
                    if paper_type == "empirical":
                        required_verifier_sections.add("results")
                    if not required_verifier_sections.issubset(set(verifier_sections)):
                        errors.append("semantic verifier receipt sections do not cover the required paper sections")
    except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    if data.get("paper_type") != paper_type or data.get("language") != language:
        errors.append("semantic receipt paper_type/language does not match the validator invocation")
    if data.get("method_profile") != method_profile:
        errors.append("semantic receipt method_profile does not match the validator invocation")
    if data.get("discussion_integrated") is not discussion_integrated:
        errors.append("semantic receipt discussion_integrated does not match the validator invocation")
    if data.get("validity_status") != "clear":
        errors.append("semantic receipt must contain validity_status: clear")
    if data.get("manuscript_sha256") != _sha256_file(manuscript_path):
        errors.append("semantic receipt manuscript_sha256 does not match this manuscript")
    for key in ("discussion_function", "conclusion_function", "abstract_consistency"):
        if data.get(key) != "pass":
            errors.append(f"semantic receipt must contain {key}: pass")
    refs = data.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        errors.append("semantic receipt evidence_refs must be a non-empty list")
    receipt_sections = data.get("sections")
    if not isinstance(receipt_sections, list) or any(not isinstance(item, str) for item in receipt_sections):
        errors.append("semantic receipt sections must be a list of section names")
    else:
        required_receipt_sections = {"introduction", "methods", "discussion", "conclusion"}
        if paper_type == "empirical":
            required_receipt_sections.add("results")
        if not required_receipt_sections.issubset(set(receipt_sections)):
            errors.append("semantic receipt sections do not cover the required paper sections")
    return errors


def validate(
    text: str,
    phase: str,
    method_profile: str,
    paper_type: str,
    language: str,
    validity_status: str,
    discussion_integrated: bool,
    semantic_receipt: Path | None,
    manuscript_path: Path | None = None,
) -> dict[str, object]:
    parsed = headings(text)
    sections = [section for section, _ in parsed]
    blocks = section_blocks(text)
    errors: list[str] = []
    warnings: list[str] = []
    if paper_type not in PAPER_TYPES:
        errors.append("paper_type must be explicitly recorded")
    if language not in LANGUAGES:
        errors.append("language must be explicitly recorded as en or zh")
    if validity_status == "blocked":
        errors.append("validity_status: blocked prevents section advancement")
    if not re.search(r"(?m)^#\s+\S.+$", text):
        errors.append("missing required title heading")
    required_common = ["introduction", "methods", "conclusion"]
    if paper_type == "empirical":
        required_common.insert(2, "results")
    for section in required_common:
        if section not in sections:
            errors.append(f"missing required section: {section}")
        elif not _content_for(blocks, section):
            errors.append(f"required section has no body content: {section}")
    if phase == "body" and "abstract" in sections:
        errors.append("abstract must be drafted only after the body is complete")
    if phase == "abstract" and ("abstract" not in sections or not _content_for(blocks, "abstract")):
        errors.append("abstract phase requires a non-empty abstract")
    if phase == "final":
        for section in ("abstract", "references"):
            if section not in sections:
                errors.append(f"final manuscript missing required section: {section}")
            elif not _content_for(blocks, section):
                errors.append(f"required section has no body content: {section}")
    discussion_error = _discussion_function_error(blocks, sections, discussion_integrated, language)
    if discussion_error:
        errors.append(discussion_error)
    positions = {section: sections.index(section) for section in sections}
    for earlier, later in zip(ORDER, ORDER[1:]):
        if earlier in positions and later in positions and positions[earlier] > positions[later]:
            errors.append(f"section order invalid: {earlier} must precede {later}")
    if paper_type == "empirical" and method_profile == "method-first" and "methods" in sections:
        methods_index = sections.index("methods")
        next_sections = sections[methods_index + 1:]
        if "results" not in next_sections:
            errors.append("method-first profile cannot locate a Results section after Methods")
    if method_profile == "data-first" and "methods" not in sections:
        errors.append("data-first profile requires a Methods section")
    if phase == "final":
        if semantic_receipt is None:
            errors.append("final phase requires an independent semantic receipt")
        elif manuscript_path is not None:
            errors.extend(_load_semantic_receipt(semantic_receipt, paper_type, language, method_profile, discussion_integrated, sections, manuscript_path))
    return {"valid": not errors, "errors": errors, "warnings": warnings, "sections": sections}


def main() -> int:
    parser = JSONArgumentParser(description="Validate the structural paper-section gate")
    parser.add_argument("--file", required=True)
    parser.add_argument("--phase", choices=("body", "abstract", "final"), default="body")
    parser.add_argument("--method-profile", choices=("method-first", "data-first"), default="method-first")
    parser.add_argument("--paper-type", choices=tuple(sorted(PAPER_TYPES)), required=True)
    parser.add_argument("--language", choices=tuple(sorted(LANGUAGES)), required=True)
    parser.add_argument("--validity-status", choices=("pending", "clear", "blocked"), default="clear")
    parser.add_argument("--discussion-integrated", action="store_true")
    parser.add_argument("--semantic-receipt", default="")
    args = parser.parse_args()
    result: dict[str, object]
    try:
        path = Path(args.file).expanduser().resolve()
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() not in {".md", ".markdown"}:
            raise UnicodeError("paper_section_validator parses Markdown only; use a format-specific adapter for LaTeX, DOCX, or PDF")
        semantic_path = Path(args.semantic_receipt).expanduser().resolve() if args.semantic_receipt else None
        result = validate(text, args.phase, args.method_profile, args.paper_type, args.language, args.validity_status, args.discussion_integrated, semantic_path, path)
        result["file"] = str(path)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        result = {"valid": False, "errors": [str(exc)], "warnings": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
