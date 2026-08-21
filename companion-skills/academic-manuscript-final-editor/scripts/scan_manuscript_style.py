#!/usr/bin/env python3
"""Locate manuscript-style review candidates without modifying the input.

Findings are prompts for editorial review, never automatic rewrite commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
BUILTIN_PROTECTED = {
    "非负", "无量纲", "无观测月份", "无观测过程月份", "零捕获", "未观测到",
    "no-observation month", "nonnegative", "dimensionless", "zero catch", "not observed",
}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    severity: str
    confidence: str
    pattern: re.Pattern[str]
    suggestion: str
    protected_exempt: bool = False


@dataclass
class Finding:
    path: str
    line: int
    paragraph: int
    rule_id: str
    category: str
    severity: str
    confidence: str
    evidence: str
    context: str
    suggestion: str


RULES = [
    Rule(
        "internal_workflow_zh", "internal_workflow", "high", "high",
        re.compile(
            r"数据提供方确认|(?:根据)?(?:项目组|研究团队|作者|导师|团队|数据提供方)(?:已)?确认|"
            r"(?:将)?(?:分析窗口|参数|阈值|配置|规则|报告组|年龄组)(?:被)?登记为|"
            r"(?:作者|导师|用户|项目组|团队)(?:已)?批准|批准(?:唯一调用|运行规格|计算规格|Q\d+)|"
            r"门禁|回执|唯一调用|失败定位|保存检查点|账目核验|核账|本地投稿材料包|"
            r"(?:预)?登记(?:门槛|配置|约束|报告年龄组)|内部(?:审批|门禁|回执|工作流|运行)(?:审查|字段|状态)?"
        ),
        "State the data definition, method, or result directly unless the decision process is scientifically relevant.",
    ),
    Rule(
        "internal_workflow_en", "internal_workflow", "high", "high",
        re.compile(
            r"\b(?:data provider confirmed|as confirmed by (?:the )?(?:project team|research team|authors?|data provider)|author approved|approval gate|stage receipt|retry allowed|"
            r"unique formal call|(?:Q\d+|internal run) failure localization|saved checkpoint|local submission package|"
            r"internal (?:workflow|approval|gate|receipt|runtime) (?:review|field|status)|registered (?:threshold|configuration|reporting group)|"
            r"(?:analysis window|parameter|threshold|configuration|rule) was registered as)\b", re.I,
        ),
        "State the data definition, method, or result directly unless the decision process is scientifically relevant.",
    ),
    Rule(
        "defensive_scope_tail_zh", "defensive_scope_tail", "medium", "medium",
        re.compile(
            r"(?:本研究|本文|该结果|这些结果|上述结果|研究结果|该估计|这些估计|上述估计|估计结果|该指标|该性质|该模型|这些模型|上述模型|模型结果|这种性质)[^。！？\n]{0,100}"
            r"(?:不是|并非|不等同于|并不等于|不代表|不能(?:被)?(?:解释|视为|用于|替代)|而不是|不作为|不进行)"
        ),
        "Check whether one positive statement can define the object and scope while preserving every material scientific limit.",
        True,
    ),
    Rule(
        "defensive_scope_tail_en", "defensive_scope_tail", "medium", "medium",
        re.compile(
            r"\b(?:this study|this paper|the result|this result|these results|our result|our results|the estimate|the estimates|this estimate|these estimates|our estimate|our estimates|the metric|the model|the models|this model|these models|our model|our models|this property|the finding|the findings|our finding|our findings|these findings)"
            r"[^.!?\n]{0,140}\b(?:is not equivalent to|(?:does|do) not (?:represent|mean|imply|constitute)|"
            r"cannot be (?:interpreted|treated|used|replaced)|rather than|was not treated as|did not include)\b", re.I,
        ),
        "Check whether one positive statement can define the object and scope while preserving every material scientific limit.",
        True,
    ),
    Rule(
        "stacked_negation_zh", "stacked_negation", "medium", "medium",
        re.compile(
            r"(?:不(?:是|代表|等同|作为|进行|解释|生成|提供|包含|使用|报告|估计|推断|进入|调整|替代|允许|支持|适用|应|需|会|能|可)[^。！？\n]{0,45}){2,}|"
            r"不(?:是|代表|等同|作为|进行|解释|生成|提供|包含|使用|报告|估计|推断|进入|调整|替代|允许|支持|适用|应|需|会|能|可)[^。！？\n]{0,55}(?:也不|且不|并不)"
        ),
        "Review stacked exclusions; state the actual operation or scope directly when that is equally precise.",
        True,
    ),
    Rule(
        "stacked_negation_en", "stacked_negation", "medium", "medium",
        re.compile(r"\bnot\b[^.!?\n]{0,90}\b(?:nor|and does not|and is not|neither)\b", re.I),
        "Review stacked exclusions; state the actual operation or scope directly when that is equally precise.",
        True,
    ),
    Rule(
        "project_defense_zh", "self_justification", "low", "low",
        re.compile(r"虽然[^。！？\n]{0,80}更复杂[^。！？\n]{0,100}(?:本文|本研究)[^。！？\n]{0,50}(?:只|仅)|为了避免(?:误写|复杂)|故不再增加"),
        "Keep this only when needed for identification, reproducibility, or interpretation; otherwise state the chosen method directly.",
    ),
    Rule(
        "project_defense_en", "self_justification", "low", "low",
        re.compile(r"\balthough\b[^.!?\n]{0,100}\bmore complex\b[^.!?\n]{0,120}\b(?:we|this study)\b[^.!?\n]{0,60}\b(?:only|limited)\b|to avoid (?:misstatement|complexity)", re.I),
        "Keep this only when needed for identification, reproducibility, or interpretation; otherwise state the chosen method directly.",
    ),
    Rule(
        "raw_field_or_artifact_zh", "raw_field_or_artifact", "medium", "medium",
        re.compile(r"原始(?:字段|列名)|字段名为|列名为|内部字段|源文件名|工作簿名|工作稿|审阅稿|SHA-?256|\.research[\\/]", re.I),
        "Confirm that this file-management detail belongs in the manuscript rather than reproducibility records.",
    ),
    Rule(
        "raw_field_or_artifact_en", "raw_field_or_artifact", "medium", "medium",
        re.compile(r"\braw (?:field|column)|\binternal field|\bsource file ?name|\bworkbook name|\bworking draft|\breview draft|SHA-?256|\.research[\\/]", re.I),
        "Confirm that this file-management detail belongs in the manuscript rather than reproducibility records.",
    ),
    Rule(
        "version_migration_zh", "version_migration_language", "medium", "medium",
        re.compile(r"旧版|旧版本|前一版本|本轮修订|修改前基线|版本迁移|(?<![A-Za-z0-9])v\d{3,}\b", re.I),
        "Describe the current method or result as it stands unless the document is explicitly version-scoped.",
    ),
    Rule(
        "version_migration_en", "version_migration_language", "medium", "medium",
        re.compile(r"\b(?:old version|previous version|this revision round|pre-revision baseline|version migration)\b|(?<![A-Za-z0-9])v\d{3,}\b", re.I),
        "Describe the current method or result as it stands unless the document is explicitly version-scoped.",
    ),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def read_docx(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        data = archive.read("word/document.xml")
    root = ET.fromstring(data)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for para in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in para.findall(".//w:t", ns)).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def read_segments(path: Path) -> tuple[list[str], bool]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return read_docx(path), False
    if suffix not in {".md", ".markdown", ".txt"}:
        raise ValueError("supported inputs are .md, .markdown, .txt, and .docx")
    return path.read_text(encoding="utf-8-sig").splitlines(), suffix in {".md", ".markdown"}


def protected_regions(lines: list[str], markdown: bool) -> set[int]:
    protected: set[int] = set()
    in_fence = False
    in_math = False
    in_references = False
    for index, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if markdown and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = not in_fence
            protected.add(index)
            continue
        if markdown and stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
            protected.add(index)
            continue
        if markdown and stripped == "$$":
            in_math = not in_math
            protected.add(index)
            continue
        if markdown and stripped == "\\[":
            in_math = True
            protected.add(index)
            continue
        if markdown and stripped == "\\]":
            protected.add(index)
            in_math = False
            continue
        if markdown and re.match(r"^\\begin\{(?:equation\*?|align\*?|alignat\*?|flalign\*?|gather\*?|multline\*?|eqnarray\*?|displaymath)\}", stripped):
            in_math = True
            protected.add(index)
            continue
        if markdown and re.match(r"^\\end\{(?:equation\*?|align\*?|alignat\*?|flalign\*?|gather\*?|multline\*?|eqnarray\*?|displaymath)\}", stripped):
            protected.add(index)
            in_math = False
            continue
        if markdown and stripped.startswith("\\[") and stripped.endswith("\\]"):
            protected.add(index)
            continue
        reference_heading = re.sub(r"^#{1,6}\s+", "", stripped)
        reference_heading = re.sub(r"\s*\{#[^}]+\}\s*$", "", reference_heading).rstrip(":：").strip()
        if re.match(r"^(?:\d+(?:\.\d+)*[.)]?\s*)?(references|bibliography|参考文献)\s*$", reference_heading, re.I):
            in_references = True
        if in_fence or in_math or in_references:
            protected.add(index)
    return protected


def excerpt(text: str, start: int, end: int, width: int) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - width):min(len(text), end + width)]).strip()


def has_scientific_negation_context(text: str, protected_terms: set[str]) -> bool:
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in protected_terms):
        return True
    return bool(re.search(
        r"零假设|显著(?:性|差异)?|置信区间|阴性结果|未观察到|未观测到|(?:无|未发现|未显示|不显示)[^。！？]{0,20}效应|效应[^。！？]{0,12}不显著|"
        r"未成年人|受试者|参与者|招募|纳入标准|排除标准|适用(?:域|范围)|定义域|低于|高于|温度|"
        r"\b(?:null hypothesis|hypothesis|statistically significant|significance|confidence interval|negative result|no events? observed|no effect|did not show an? effect|effect was not significant|"
        r"participants?|eligibility|inclusion|exclusion|younger than|older than|below|above|degrees? (?:celsius|fahrenheit)|temperature|valid domain|applicable range)\b",
        text,
        re.I,
    ))


def containing_sentence(text: str, start: int, end: int) -> str:
    left_matches = list(re.finditer(r"[。！？.!?]", text[:start]))
    left = left_matches[-1].end() if left_matches else 0
    right_match = re.search(r"[。！？.!?]", text[end:])
    right = end + right_match.end() if right_match else len(text)
    return text[left:right].strip()


def join_soft_lines(parts: list[str]) -> str:
    result = ""
    for part in parts:
        item = part.strip()
        if not result:
            result = item
        elif re.search(r"[\u4e00-\u9fff]$", result) and re.match(r"^[\u4e00-\u9fff]", item):
            result += item
        else:
            result += " " + item
    return result


def scan_units(lines: list[str], protected: set[int], markdown: bool) -> list[tuple[int, str]]:
    """Return prose units, joining soft-wrapped Markdown lines into paragraphs."""
    if not markdown:
        return [(line_no, text) for line_no, text in enumerate(lines, 1) if line_no not in protected and text.strip()]
    units: list[tuple[int, str]] = []
    start = 0
    buffer: list[str] = []

    def flush() -> None:
        nonlocal start, buffer
        if buffer:
            units.append((start, join_soft_lines(buffer)))
            start = 0
            buffer = []

    for line_no, text in enumerate(lines, 1):
        stripped = text.strip()
        structural = bool(re.match(r"^(#{1,6})\s+", stripped) or stripped.startswith("|") or re.match(r"^[-*+]\s+", stripped))
        if line_no in protected or not stripped or structural:
            flush()
            if line_no not in protected and stripped:
                units.append((line_no, text))
            continue
        if not buffer:
            start = line_no
        buffer.append(text)
    flush()
    return units


def deduplicate(findings: list[Finding]) -> list[Finding]:
    kept: dict[tuple[str, int, str, str], Finding] = {}
    for finding in findings:
        collapse_category = finding.category in {"version_migration_language", "internal_workflow"}
        rule_group = finding.category if collapse_category else finding.rule_id
        evidence_key = "" if finding.rule_id == "scientific_negation_review" or collapse_category else finding.evidence.casefold()
        key = (finding.path, finding.line, rule_group, evidence_key)
        current = kept.get(key)
        if current is None or SEVERITY_RANK[finding.severity] > SEVERITY_RANK[current.severity]:
            kept[key] = finding
    return sorted(kept.values(), key=lambda item: (item.path, item.line, -SEVERITY_RANK[item.severity], item.rule_id))


def sentence_units(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]


def is_short_sentence(text: str) -> bool:
    clean = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    if re.search(r"[\u4e00-\u9fff]", text):
        return 2 <= len(clean) <= 12
    words = re.findall(r"\b[\w'-]+\b", text)
    return 2 <= len(words) <= 6


def scan_mechanical_runs(path: Path, lines: list[str], protected: set[int]) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, text in enumerate(lines, 1):
        if line_no in protected or not text.strip():
            continue
        units = sentence_units(text)
        for pos in range(max(0, len(units) - 2)):
            run = units[pos:pos + 3]
            if len(run) == 3 and all(is_short_sentence(item) for item in run):
                evidence = " ".join(run)
                findings.append(Finding(str(path), line_no, line_no, "mechanical_short_run", "mechanical_prose", "low", "low", evidence, evidence, "Review whether this reads like a list or manufactured emphasis; merge only when the logical relation is clear."))
                break
    return findings


def scan_headings(path: Path, lines: list[str], protected: set[int]) -> list[Finding]:
    headings: list[tuple[int, int, str]] = []
    for line_no, text in enumerate(lines, 1):
        if line_no in protected:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", text)
        if match:
            headings.append((line_no, len(match.group(1)), match.group(2)))
    findings: list[Finding] = []
    for previous, current in zip(headings, headings[1:]):
        if current[1] > previous[1] + 1:
            findings.append(Finding(str(path), current[0], current[0], "heading_level_jump", "heading_hierarchy", "medium", "high", current[2], lines[current[0] - 1], "Repair the skipped heading level or remove an unnecessary layer."))
    for level in range(3, 7):
        same_level = [item for item in headings if item[1] == level]
        if len(same_level) == 1:
            line_no, _, title = same_level[0]
            findings.append(Finding(str(path), line_no, line_no, "isolated_heading_level", "heading_hierarchy", "low", "medium", title, lines[line_no - 1], "Review whether a single deep heading should be merged into its parent or matched by a real sibling structure."))
    return findings


def scan_path(path: Path, protected_terms: set[str], context_width: int) -> tuple[list[Finding], str, str]:
    before = sha256(path)
    lines, markdown = read_segments(path)
    protected = protected_regions(lines, markdown)
    findings: list[Finding] = []
    for line_no, text in scan_units(lines, protected, markdown):
        for rule in RULES:
            for match in rule.pattern.finditer(text):
                matched = match.group(0)
                local_sentence = containing_sentence(text, match.start(), match.end())
                if rule.category in {"defensive_scope_tail", "stacked_negation"} and has_scientific_negation_context(local_sentence, protected_terms):
                    findings.append(Finding(str(path), line_no, line_no, "scientific_negation_review", "scientific_negation", "info", "medium", matched, excerpt(text, match.start(), match.end(), context_width), "Preserve this negative statement if it reports a null result, exclusion rule, constraint, or other material scientific fact; review only for unnecessary repetition."))
                else:
                    findings.append(Finding(str(path), line_no, line_no, rule.rule_id, rule.category, rule.severity, rule.confidence, matched, excerpt(text, match.start(), match.end(), context_width), rule.suggestion))
    findings.extend(scan_mechanical_runs(path, lines, protected))
    if markdown:
        findings.extend(scan_headings(path, lines, protected))
    after = sha256(path)
    return deduplicate(findings), before, after


def threshold_reached(findings: Iterable[Finding], fail_on: str) -> bool:
    if fail_on == "none":
        return False
    required = SEVERITY_RANK[fail_on]
    return any(SEVERITY_RANK[item.severity] >= required for item in findings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="Markdown, text, or DOCX manuscript files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--context", type=int, default=70, help="characters of local context around a match")
    parser.add_argument("--protected-term", action="append", default=[], help="additional exact scientific term to protect; repeat as needed")
    parser.add_argument("--fail-on", choices=("none", "medium", "high"), default="none", help="return exit code 1 when a finding reaches this severity")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    protected_terms = BUILTIN_PROTECTED | set(args.protected_term)
    all_findings: list[Finding] = []
    files: list[dict[str, object]] = []
    try:
        for path in args.inputs:
            resolved = path.resolve()
            if not resolved.is_file():
                raise ValueError(f"input is not a file: {path}")
            findings, before, after = scan_path(resolved, protected_terms, max(0, args.context))
            all_findings.extend(findings)
            coverage = "main-document-text-only" if resolved.suffix.lower() == ".docx" else "complete-for-supported-text"
            files.append({"path": str(resolved), "sha256_before": before, "sha256_after": after, "unchanged": before == after, "coverage_status": coverage})
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"scan error: {exc}", file=sys.stderr)
        return 2

    payload = {"schema_version": 1, "scanner_version": "1.2", "files": files, "finding_count": len(all_findings), "findings": [asdict(item) for item in all_findings]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in all_findings:
            print(f"{item.path}:{item.line} [{item.severity}/{item.confidence}] {item.rule_id}")
            print(f"  {item.context}")
            print(f"  -> {item.suggestion}")
        print(f"findings: {len(all_findings)}; files unchanged: {all(file['unchanged'] for file in files)}")
    return 1 if threshold_reached(all_findings, args.fail_on) else 0


if __name__ == "__main__":
    raise SystemExit(main())
