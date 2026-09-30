#!/usr/bin/env python3
"""Locate manuscript-style review candidates without modifying the input.

Findings are prompts for editorial review, never automatic rewrite commands.
Every finding carries a stable content-derived ``finding_id`` so a dispositions
receipt can bind one decision and its evidence to one exact candidate.
"""

from __future__ import annotations

import argparse
import difflib
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
    signals: dict[str, int] | None = None
    finding_id: str = ""


FINDING_ID_PREFIX = "fnd-"
CLAUSE_SEPARATORS = "。！？；;，,"


def finding_id(finding: Finding) -> str:
    """Derive the ID that one disposition may cite for this finding.

    The scheme is a contract with ``scripts/final_edit_receipt_validator.py``,
    which recomputes the ID from the report instead of trusting the declaration.
    Change both sides together.
    """
    payload = {
        "path": finding.path, "line": finding.line,
        "rule_id": finding.rule_id, "evidence": finding.evidence,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return FINDING_ID_PREFIX + hashlib.sha256(raw).hexdigest()[:20]


def assign_finding_ids(findings: list[Finding]) -> None:
    """Give every finding its ID; a repeated ID keeps a deterministic ordinal."""
    seen: dict[str, int] = {}
    for finding in findings:
        base = finding_id(finding)
        seen[base] = seen.get(base, 0) + 1
        finding.finding_id = base if seen[base] == 1 else f"{base}-{seen[base]}"


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


DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_heading_styles(path: Path) -> set[str]:
    """Resolve which DOCX style ids act as headings.

    Style ids in Chinese Word files are often numeric, so the style name and the
    outline level from ``word/styles.xml`` decide, not the id spelling.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/styles.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError, OSError):
        return set()
    headings: set[str] = set()
    for style in root.iter(DOCX_NS + "style"):
        style_id = style.attrib.get(DOCX_NS + "styleId", "")
        if not style_id:
            continue
        name_node = style.find(DOCX_NS + "name")
        name = "" if name_node is None else name_node.attrib.get(DOCX_NS + "val", "")
        outline = style.find("./" + DOCX_NS + "pPr/" + DOCX_NS + "outlineLvl")
        is_heading = outline is not None or bool(
            re.match(r"^(?:heading|title|标题|题名)\s*[1-9]?$", (name or style_id).strip(), re.I)
        )
        if is_heading:
            headings.add(style_id)
    return headings


def read_docx_with_sections(path: Path) -> tuple[list[str], list[str]]:
    """Return paragraph text plus the section label each paragraph belongs to.

    Heading paragraphs supply the label; they are not returned as prose, so the
    section-aware detectors can tell a result from an abstract or a caption.
    """
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    heading_styles = docx_heading_styles(path)
    texts: list[str] = []
    labels: list[str] = []
    current = "document"
    for paragraph in root.iter(DOCX_NS + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(DOCX_NS + "t")).strip()
        style = paragraph.find("./" + DOCX_NS + "pPr/" + DOCX_NS + "pStyle")
        style_name = "" if style is None else style.attrib.get(DOCX_NS + "val", "")
        is_heading = style_name in heading_styles or bool(
            re.match(r"^(?:heading|title|标题|题名)\s*[1-9]?$", style_name.strip(), re.I)
        )
        if text and is_heading:
            current = text
            continue
        if text:
            texts.append(text)
            labels.append(current)
    return texts, labels


def markdown_section_labels(lines: list[str]) -> list[str]:
    labels: list[str] = []
    current = "document"
    fenced = False
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
        heading = None if fenced else MARKDOWN_HEADING.match(stripped)
        if heading:
            current = reference_heading_text(heading.group(2)) or current
        labels.append(current)
    return labels


def read_segments(path: Path) -> tuple[list[str], bool, list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        texts, labels = read_docx_with_sections(path)
        return texts, False, labels
    if suffix not in {".md", ".markdown", ".txt"}:
        raise ValueError("supported inputs are .md, .markdown, .txt, and .docx")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return lines, suffix in {".md", ".markdown"}, markdown_section_labels(lines)


SECTION_ROLES = (
    ("abstract", re.compile(r"摘\s*要|abstract|关键词|keywords", re.I)),
    ("methods", re.compile(r"方\s*法|材料|methods?|materials?|数据来源", re.I)),
    ("results", re.compile(r"结\s*果|results?", re.I)),
    ("closing", re.compile(r"讨\s*论|结\s*论|discussion|conclusions?|总结|小结", re.I)),
    ("references", re.compile(r"参考文献|references|bibliography", re.I)),
)
CAPTION_START = re.compile(r"^\s*(?:[a-fA-F]\s*[，,、]|图\s*\d|表\s*\d|Fig\.?\s*\d|Table\s*\d)")


def section_role(label: str) -> str:
    for role, pattern in SECTION_ROLES:
        if pattern.search(label):
            return role
    return "body"


def is_caption(text: str) -> bool:
    return bool(CAPTION_START.match(text))


REFERENCE_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s*)?(references|bibliography|参考文献)\s*$", re.I
)
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def reference_heading_text(value: str) -> str:
    text = re.sub(r"^#{1,6}\s+", "", value.strip())
    text = re.sub(r"\s*\{#[^}]+\}\s*$", "", text)
    return text.rstrip(":：").strip()


def protected_regions(lines: list[str], markdown: bool) -> set[int]:
    """Mark the lines the scanner must not read as manuscript prose.

    A references region stays open until the next heading of the same or a
    higher level, so appendices, supplementary material, and acknowledgements
    that follow the bibliography are still scanned. Fences and math blocks can
    neither open a references region nor keep one open.
    """
    protected: set[int] = set()
    in_fence = False
    in_math = False
    references_level: int | None = None
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
        heading_level: int | None = None
        if markdown and not in_fence and not in_math:
            heading = MARKDOWN_HEADING.match(stripped)
            if heading:
                heading_level = len(heading.group(1))
        if references_level is not None and heading_level is not None and heading_level <= references_level:
            references_level = None
        if not in_fence and not in_math and REFERENCE_HEADING.match(reference_heading_text(stripped)):
            # Plain-text segments have no heading structure, so the region keeps
            # the historical behaviour and stays open to the end of the file.
            references_level = heading_level if heading_level is not None else 6
        if in_fence or in_math or references_level is not None:
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
    lines, markdown, labels = read_segments(path)
    protected = protected_regions(lines, markdown)
    findings: list[Finding] = []
    units = scan_units(lines, protected, markdown)
    for line_no, text in units:
        for rule in RULES:
            for match in rule.pattern.finditer(text):
                matched = match.group(0)
                local_sentence = containing_sentence(text, match.start(), match.end())
                if rule.category in {"defensive_scope_tail", "stacked_negation"} and has_scientific_negation_context(local_sentence, protected_terms):
                    findings.append(Finding(str(path), line_no, line_no, "scientific_negation_review", "scientific_negation", "info", "medium", matched, excerpt(text, match.start(), match.end(), context_width), "Preserve this negative statement if it reports a null result, exclusion rule, constraint, or other material scientific fact; review only for unnecessary repetition."))
                else:
                    findings.append(Finding(str(path), line_no, line_no, rule.rule_id, rule.category, rule.severity, rule.confidence, matched, excerpt(text, match.start(), match.end(), context_width), rule.suggestion))
    findings.extend(scan_mechanical_runs(path, lines, protected))
    findings.extend(scan_defensive_rigor(path, units, protected_terms, context_width))
    findings.extend(scan_anomaly_without_probe(path, units, protected_terms, context_width, labels))
    findings.extend(scan_cross_section_repeats(path, lines, protected, markdown, context_width, labels))
    if markdown:
        findings.extend(scan_headings(path, lines, protected))
    after = sha256(path)
    ordered = deduplicate(findings)
    rigor_evidence = {finding.evidence for finding in ordered if finding.category == "defensive_rigor"}
    ordered = [finding for finding in ordered
               if not (finding.category == "redundant_restatement" and finding.evidence in rigor_evidence)]
    assign_finding_ids(ordered)
    return ordered, before, after


RIGOR_USAGE = re.compile(
    r"可作为|可用于|可供|可沿用|可生成|用于评估|用于描述|用于概括|用于检验|共同界定|共同评估|提供证据"
)
RIGOR_HEDGE = (
    "适用于", "在一定程度上", "原则上", "有待", "尚需", "不排除", "据此解释为", "据此解读",
    "分别描述", "分别解释", "分别判断", "尚未完成", "逐字段溯源", "并不必然", "须结合",
    "解释尺度", "解释范围", "解释限", "限于", "需注意", "需要说明", "参考", "概括",
    "尚未", "仍可能", "可能影响", "保守", "谨慎",
)
RIGOR_STRONG_INFO = (
    re.compile(r"\d"), re.compile(r"%(?![a-zA-Z])"), re.compile(r"°"), re.compile(r"[⁻²]"),
    re.compile(r"(?<![A-Za-z])(?:Q\d+|R²|R2|RMSE|MAE|skill|P\s*[<=])(?![A-Za-z])"),
    re.compile(r"et al\.|\(\d{4}\)|（\d{4}）"), re.compile(r"表\s*\d|图\s*\d|Fig\.?\s*\d|Table\s*\d"),
    re.compile(r"(?:[一二三四五六七八九十]|数|若干)(?:个|种|条|项|层|级|段|篇|次|尾|年度?)"),
    re.compile(r"负值|为负|下降|降低|偏低|较弱|差异显著|相关显著"),
)
RIGOR_WEAK_INFO = (
    re.compile(r"(?<![A-Za-z])(?:SST|OHC|EKE|MLD|CPUE|GG|RW|PDE|RMSE|BAI|GTNN|GAM|RF)(?![A-Za-z])"),
    re.compile(r"分位|区间|中位数|比例|bootstrap|置换|留出|置信|回归|样条|矩阵|包络|标准化"),
)
RIGOR_INFO = (
    re.compile(r"\d"), re.compile(r"%(?![a-zA-Z])"), re.compile(r"\b(?:yr|kg|cm|mm|d|h)\b"),
    re.compile(r"°"), re.compile(r"[⁻²]"),
    re.compile(r"(?<![A-Za-z])(?:SST|OHC|EKE|MLD|CPUE|GG|RW|PDE|RMSE|R²|R2|Q\d+)(?![A-Za-z])"),
    re.compile(r"et al\.|\(\d{4}\)|（\d{4}）"), re.compile(r"表\s*\d|图\s*\d"),
    re.compile(r"正向|负向|高于|低于|增加|降低|增大|减小|排除0"),
    re.compile(r"分位|区间|中位数|比例|bootstrap|置换|留出|置信|回归|样条|矩阵|包络"),
    re.compile(r"(?:[一二三四五六七八九十]|数|若干)(?:个|种|条|项|层|级|段|篇|次|尾|年度?)"),
)
ANOMALY_MARK = re.compile(
    # A direction word is an anomaly only when a performance metric binds it:
    # "RMSE 和 MAE 同时下降" is an improvement, "一个月系数为负" is a value.
    r"(?:R²|R2|ΔR²|RMSE|MAE|skill|校准|预测|精度|性能|表现|基线|基准|证据|支持|标准|区间)"
    r"[^。！？\n]{0,24}(?:为负|负值|下降|降低|偏低|较弱|较差|欠佳|未达到|未优于|偏离|波动较大|不一致)|"
    r"(?:下降|降低|偏低|较弱|较差|欠佳|未达到|未优于|偏离|波动较大|不一致)"
    r"[^。！？\n]{0,24}(?:R²|R2|ΔR²|RMSE|MAE|skill|校准|预测|精度|性能|基线|基准|证据|支持|标准|区间)|"
    r"(?:R²|R2|ΔR²)\s*(?:为|=|均为|仍为|接近|呈)\s*[−-]\s*0?\.\d|"
    r"(?:下降|降低)\s*[−-]\s*\d"
)
NEGATIVE_METRIC = re.compile(
    r"(?:R²|R2|ΔR²)\s*(?:为|=|均为|仍为|接近|呈)\s*[−-]?\s*0?\.\d"
    r"|(?:R²|R2|ΔR²)[^。！？\n]{0,12}(?:均为负|为负)"
    r"|(?:下降|降低)\s*[−-]\s*\d"
)
BASELINE_SHORTFALL = re.compile(r"未优于|低于基线|低于[^。！？\n]{0,6}基线|负增益|变差|更差|不如")
IMPROVEMENT_EFFECT = re.compile(r"误差|RMSE|MAE|偏差|损失|同时下降|均下降")
PROBE_MARK = re.compile(
    r"原因|由于|可能|或许|机制|源于|归因|假设|推测|待查|值得进一步|尚不清楚|因为|受[^。！？]{0,12}影响"
    r"|很可能|提示[^。！？]{0,6}(?:可能|来自|源于)|与[^。！？]{0,12}(?:有关|相关)"
    # Numeric and procedural explanations also discharge an anomaly.
    r"|区间排除\s*0|排除\s*0|分位|中位数|窗口不足|样本不足|样本有限|观测缺失"
    r"|由[^。！？]{0,10}主导|共线"
    r"|尚未|未完成|未能完成|缺乏|缺失|不足|受限于|限制|妨碍|难以区分|无法区分|不易区分"
)
ENTITY_TOKEN = re.compile(r"[A-Z]{2,6}[-–][A-Z]{2,4}|ΔR²|R²|R2(?![A-Za-z0-9])|RMSE|MAE|skill")
DEFINITION_MARK = re.compile(
    r"定义为|定义如下|记作|记为|令[^。！？\n]{0,20}为|以[^。！？\n]{0,25}为(?:响应|解释|预测)?变量|"
    r"构成[^。！？\n]{0,12}(?:包络|矩阵|序列|集合|指标)|\bdefined as\b|\blet\s+[A-Za-z]\b"
)
CAUSAL_MARK = re.compile(
    r"由于|因为|源于|归因于|取决于|由[^。！？\n]{0,14}决定|导致|使得|会影响"
)
RIGOR_PROCEDURAL = re.compile(
    r"(?:均按|统一按|一律按)[^。！？；\n]{0,14}(?:报告|呈现|给出|标注|保存)|"
    r"按[^。！？；\n]{0,6}口径(?:报告|给出)"
)
CROSS_REFERENCE = re.compile(r"见第\s*\d|详见|参见|[Ss]ee (?:Section|§)|第\s*\d+(?:\.\d+)*\s*节")
REPEAT_THRESHOLD = 0.62
REPEAT_POSSIBLE = 0.38
REPEAT_CONTAINMENT = 0.70
CJK_DOCUMENT_SHARE = 0.15
GENRE_SECTION = re.compile(r"引言|前言|绪论|结论|总结|小结|introduction|conclusions?", re.I)


def states_a_mechanism(sentence: str) -> bool:
    """A sentence that names why something happens carries information.

    "由于月尺度 SST 自相关较强…区分较为保守" and "解释范围首先由 CPUE 的渔业
    依赖观测性质决定" cite a concrete cause with a named factor; the rubric's
    zero-information test cannot be met, so they are not rigor candidates.
    """
    return bool(CAUSAL_MARK.search(sentence))


def is_definition_sentence(sentence: str) -> bool:
    """A method sentence that introduces notation or builds a quantity is content.

    "令 … 为第 b 档占比", "有效数定义为", "以 CPUE 为响应变量",
    "构成组成包络", and any sentence carrying an equals sign define the study's
    own machinery; they are not defensive filler even without a number.
    """
    return bool(re.search(r"=|＝|\\\(", sentence) or DEFINITION_MARK.search(sentence))


def clause_containing(text: str, start: int, end: int) -> str:
    """The clause holding a match, so one phrase is judged on its own content."""
    left = max(text.rfind(separator, 0, start) for separator in CLAUSE_SEPARATORS)
    right = min((position for position in (text.find(separator, end) for separator in CLAUSE_SEPARATORS) if position != -1), default=len(text))
    return text[left + 1:right].strip()


def procedural_evidence(sentence: str) -> str:
    """The narration clause itself, so an editor cannot delete adjacent substance.

    "3 yr边界位于该年龄区间之后，…；结果均按数值区间报告" carries its only science
    in the first clause; pointing the finding at the second one keeps the fix local.
    """
    match = RIGOR_PROCEDURAL.search(sentence)
    clause = clause_containing(sentence, match.start(), match.end()) if match else sentence
    return (clause or sentence)[:120]


def procedural_clauses(sentence: str) -> int:
    """Count presentation-narration clauses that report nothing themselves.

    "结果均按数值区间报告" only says how results are formatted. A clause carrying
    a number, unit, or metric is a real method or result statement and is not
    counted, even when the surrounding sentence holds one.
    """
    counted = 0
    for match in RIGOR_PROCEDURAL.finditer(sentence):
        clause = clause_containing(sentence, match.start(), match.end())
        if not any(pattern.search(clause) for pattern in RIGOR_STRONG_INFO):
            counted += 1
    return counted


def rigor_signals(text: str) -> tuple[int, int, int, bool]:
    """Return the auditable feature vector behind a defensive-rigor candidate."""
    hedge = sum(text.count(token) for token in RIGOR_HEDGE)
    strong = sum(len(pattern.findall(text)) for pattern in RIGOR_STRONG_INFO)
    weak = sum(len(pattern.findall(text)) for pattern in RIGOR_WEAK_INFO)
    return hedge, strong, weak, bool(RIGOR_USAGE.search(text))


def scan_defensive_rigor(
    path: Path, units: list[tuple[int, str]], protected_terms: set[str], context_width: int
) -> list[Finding]:
    """Flag rigor language that carries no scientific information.

    A candidate is a usage/defensive clause ("可作为…参考", "用于评估…"), a
    presentation-narration clause ("结果均按数值区间报告"), or a hedged sentence
    whose feature vector holds no strong information (no number, unit, interval,
    direction, citation, or figure/table reference). Weak information (a bare
    variable name) does not rescue a filler clause. Captions
    and references are skipped, and so are sentences that define the study's own
    quantities or name a mechanism. The feature vector travels with the finding so
    a disposition can cite it.
    """
    findings: list[Finding] = []
    per_unit: dict[int, int] = {}
    for unit_index, (line_no, text) in enumerate(units):
        for sentence in sentence_units(text):
            if len(sentence) < 10 or sentence.startswith("注：") or is_caption(sentence):
                continue
            if is_definition_sentence(sentence) or states_a_mechanism(sentence):
                continue
            if has_scientific_negation_context(sentence, protected_terms):
                continue
            hedge, strong, weak, usage = rigor_signals(sentence)
            procedural = procedural_clauses(sentence)
            if not (procedural or (strong == 0 and (usage or hedge >= 1))):
                continue
            per_unit[unit_index] = per_unit.get(unit_index, 0) + 1
            findings.append(Finding(
                str(path), line_no, unit_index, "defensive_rigor_no_content", "defensive_rigor",
                "medium", "high" if usage or procedural or hedge >= 2 else "medium",
                procedural_evidence(sentence) if procedural and strong else sentence[:120],
                excerpt(text, text.find(sentence), text.find(sentence) + len(sentence), context_width),
                "State the concrete limit, condition, or next test; drop the clause if it only asserts caution, usefulness, or presentation.",
                {"hedge": hedge, "strong_info": strong, "weak_info": weak, "usage": int(usage),
                 "procedural": procedural, "chars": len(sentence)},
            ))
    # A paragraph dominated by framing is reported once, as a signal on the
    # sentences already listed, so the same paragraph is not counted twice.
    crowded = {unit_index for unit_index, count in per_unit.items() if count >= 2}
    if crowded:
        for finding in findings:
            if finding.rule_id != "defensive_rigor_no_content" or finding.paragraph not in crowded:
                continue
            count = per_unit[finding.paragraph]
            finding.signals = {**(finding.signals or {}), "paragraph_rigor": count}
            if count >= 2:
                # The count raises confidence; the sentence keeps its own advice,
                # because a paragraph can hold framing and substance together.
                finding.confidence = "high"
    return findings


def anomaly_context(sentence: str) -> bool:
    """Return True when a sentence reports a degraded result, not a plain value.

    A direction word alone is ambiguous: "RMSE 和 MAE 同时下降" is an
    improvement, "一个月系数为负" states a value, and "R² 均为负" is a defect.
    Only the last shape is an anomaly a reader needs explained.
    """
    if ANOMALY_MARK.search(sentence) is None:
        return False
    if IMPROVEMENT_EFFECT.search(sentence) and not BASELINE_SHORTFALL.search(sentence) and not NEGATIVE_METRIC.search(sentence):
        return False
    return True


def entity_tokens(text: str) -> set[str]:
    """Panel codes and metric names that tie a probe to the result it explains."""
    return {match.group(0) for match in ENTITY_TOKEN.finditer(text)}


def anomaly_value_key(sentence: str) -> frozenset[str]:
    """Negative metric values a sentence cites, used to group one defect's reports.

    "R² 为 −0.144" in a results section and the same figure restated in an
    interpretation section describe one defect; the shared value is what makes
    them one event rather than two.
    """
    return frozenset(re.sub(r"\s+", "", match.group(0)) for match in re.finditer(r"[−-]\s*\d+\.\d+", sentence))


def anomaly_is_probed(
    entries: list[tuple[str, int, int, str]], index: int, sentence: str
) -> bool:
    """A probe is an explanation in this sentence, its paragraph, or a later one.

    Abstracts, methods, closing sections, references, and captions are exempt at
    the caller: those surfaces defer mechanisms by design. A later explanation
    must name the same panel or metric, because a mechanism for a different
    result does not discharge this anomaly.
    """
    if PROBE_MARK.search(sentence):
        return True
    unit_index = entries[index][1]
    tokens = entity_tokens(sentence)
    for _, other_unit, _, other in entries[index + 1:]:
        if PROBE_MARK.search(other) is None:
            continue
        if other_unit == unit_index:
            return True
        if tokens and tokens & entity_tokens(other):
            return True
    return False


def scan_anomaly_without_probe(
    path: Path, units: list[tuple[int, str]], protected_terms: set[str], context_width: int,
    labels: list[str],
) -> list[Finding]:
    """Flag an anomaly that no part of the manuscript tries to explain.

    One defect is reported once: sentences that share a paragraph collapse into a
    single finding, and a defect restated elsewhere with the same value joins the
    first report instead of opening a second one.
    """
    entries: list[tuple[str, int, int, str]] = []
    for unit_index, (line_no, text) in enumerate(units):
        label = labels[line_no - 1] if 0 <= line_no - 1 < len(labels) else "document"
        for sentence in sentence_units(text):
            entries.append((label, unit_index, line_no, sentence))

    unresolved: dict[int, list[tuple[int, str]]] = {}
    for index, (label, unit_index, line_no, sentence) in enumerate(entries):
        if len(sentence) < 10 or is_caption(sentence):
            continue
        if section_role(label) in {"abstract", "methods", "closing", "references"}:
            continue
        if not anomaly_context(sentence):
            continue
        if has_scientific_negation_context(sentence, protected_terms):
            continue
        if anomaly_is_probed(entries, index, sentence):
            continue
        unresolved.setdefault(unit_index, []).append((line_no, sentence))

    reports = [
        (unit_index, line_no, sentence, anomaly_value_key(sentence))
        for unit_index, items in sorted(unresolved.items())
        for line_no, sentence in items
    ]
    events: list[dict[str, object]] = []
    for unit_index, line_no, sentence, values in reports:
        for event in events:
            same_paragraph = any(unit_index == other_unit for other_unit, _, _ in event["items"])
            if same_paragraph or (values and values & event["values"]):
                event["values"] |= values
                event["items"].append((unit_index, line_no, sentence))
                break
        else:
            events.append({"values": set(values), "items": [(unit_index, line_no, sentence)]})

    findings: list[Finding] = []
    for event in events:
        items = event["items"]
        unit_index, line_no, first = items[0]
        context = excerpt(units[unit_index][1], 0, len(first), context_width)
        elsewhere = sorted({units[other_unit][0] for other_unit, other_line, _ in items[1:]})
        if elsewhere:
            context = f"{context} | same defect restated at line(s): {', '.join(str(item) for item in elsewhere)}"
        findings.append(Finding(
            str(path), line_no, unit_index, "anomaly_without_probe", "unanexplained_anomaly",
            "medium", "medium", first[:120], context,
            "Follow the anomaly with the most likely mechanism, an explicit alternative, or a note that the cause is unresolved.",
            {"anomaly_sentences": len(items)},
        ))
    return findings


def normalize_for_repeat(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"[\d.,%–\-−/]+", "#", text)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text)


@dataclass
class RepeatCluster:
    """One judgement and the sentences that restate it."""

    items: list[tuple[str, int, str]]
    kind: str = ""
    ratio: float = 0.0
    containment: float = 0.0


def cjk_share(text: str) -> float:
    """Share of Chinese characters, used to decide how sentences are compared."""
    content = [character for character in text if not character.isspace()]
    if not content:
        return 0.0
    return sum("\u4e00" <= character <= "\u9fff" for character in content) / len(content)


def repeat_skeleton(text: str) -> str:
    """The Chinese content of a sentence: shared Latin identifiers are entities."""
    return re.sub(r"[A-Za-z]+", "", normalize_for_repeat(text))


def comparison_text(text: str, cjk_document: bool) -> str:
    """Compare Chinese content in a Chinese manuscript, raw text elsewhere."""
    return repeat_skeleton(text) if cjk_document else normalize_for_repeat(text)


def repeat_metrics(first: str, second: str, cjk_document: bool = False) -> tuple[float, float]:
    """Return (character ratio, shared-bigram containment of the shorter side).

    Containment catches a sentence that a later one repeats almost entirely
    before adding material, which a plain ratio discounts for length. In a
    Chinese manuscript both metrics run on the Chinese skeleton, so a shared
    identifier, a table cell, or a Latin abbreviation cannot make two sentences
    look like a restatement.
    """
    a, b = comparison_text(first, cjk_document), comparison_text(second, cjk_document)
    if len(a) < 12 or len(b) < 12:
        return 0.0, 0.0
    grams_a = {a[index:index + 2] for index in range(len(a) - 1)}
    grams_b = {b[index:index + 2] for index in range(len(b) - 1)}
    shorter = min(len(grams_a), len(grams_b))
    containment = len(grams_a & grams_b) / shorter if shorter else 0.0
    return difflib.SequenceMatcher(None, a, b).ratio(), containment


def repeat_verdict(first: str, second: str, cjk_document: bool = False, genre_pair: bool = False) -> tuple[str, float, float]:
    """Classify a pair as a restatement, a possible one, or unrelated.

    Verbatim restatement and near-complete containment are confident repeats.
    Lower character similarity is only a possible paraphrase, reported at low
    confidence for editorial triage, because sentences that merely share entities
    can look alike. Pairs across an introduction or conclusion are not compared
    at the possible level: a thesis and its summary are genre, not a restatement,
    and only a confident repeat (a verbatim figure or phrase) is flagged there.
    """
    ratio, containment = repeat_metrics(first, second, cjk_document)
    if ratio >= REPEAT_THRESHOLD or containment >= REPEAT_CONTAINMENT:
        return "repeat", ratio, containment
    if ratio >= REPEAT_POSSIBLE and not genre_pair:
        return "possible", ratio, containment
    return "", ratio, containment


def repeat_clusters(entries: list[tuple[str, int, str]], cjk_document: bool = False) -> list[RepeatCluster]:
    """Group sentences that restate one judgement, keeping the first occurrence.

    A cluster carries its strongest pair verdict, so a confident repeat and a
    possible paraphrase stay distinguishable in the finding.
    """
    clusters: list[RepeatCluster] = []
    for entry in entries:
        for cluster in clusters:
            genre_pair = bool(GENRE_SECTION.search(cluster.items[0][0]) or GENRE_SECTION.search(entry[0]))
            kind, ratio, containment = repeat_verdict(cluster.items[0][2], entry[2], cjk_document, genre_pair)
            if not kind:
                continue
            cluster.items.append(entry)
            if kind == "repeat" or not cluster.kind:
                cluster.kind = kind
            cluster.ratio = max(cluster.ratio, ratio)
            cluster.containment = max(cluster.containment, containment)
            break
        else:
            clusters.append(RepeatCluster(items=[entry]))
    return clusters


def scan_cross_section_repeats(
    path: Path, lines: list[str], protected: set[int], markdown: bool, context_width: int,
    labels: list[str],
) -> list[Finding]:
    """Flag a judgement that later sections restate instead of advancing.

    Section labels come from Markdown headings or DOCX heading styles, so the
    same check works for both formats. Headings, cross-references, definition
    lead-ins, and abstracts are structure, pointers, or genre-required overview
    rather than restated judgements, so they are not compared. Restatements are
    clustered and reported once with the sections they span: one judgement
    repeated in three sections is a single finding, and a paraphrase that only
    shares entities stays at low severity.
    """
    entries: list[tuple[str, int, str]] = []
    for line_no, raw in enumerate(lines, 1):
        stripped = re.sub(r"<!--.*?-->", "", raw).strip()
        if line_no in protected or not stripped or stripped.startswith(("|", "**表", "注：")) or is_caption(stripped):
            continue
        if markdown and MARKDOWN_HEADING.match(stripped):
            continue
        section = labels[line_no - 1] if 0 <= line_no - 1 < len(labels) else "document"
        if section_role(section) in {"abstract", "references"}:
            continue
        for sentence in sentence_units(stripped):
            if len(sentence) < 16 or CROSS_REFERENCE.search(sentence) or re.search(r"[：:]\s*$", sentence):
                continue
            if is_definition_sentence(sentence):
                continue
            entries.append((section, line_no, sentence))
    cjk_document = cjk_share("".join(sentence for _, _, sentence in entries)) >= CJK_DOCUMENT_SHARE
    findings: list[Finding] = []
    for cluster in repeat_clusters(entries, cjk_document):
        if not cluster.kind:
            continue
        sections = {section for section, _, _ in cluster.items}
        if len(sections) < 2:
            continue
        first_section, line_no, sentence = cluster.items[0]
        other = next(item[2] for item in cluster.items[1:] if item[0] != first_section)
        locations = " | ".join(f"{name} L{line}" for name, line, _ in cluster.items[:6])
        confident = cluster.kind == "repeat"
        findings.append(Finding(
            str(path), line_no, line_no, "cross_section_repeat", "redundant_restatement",
            "medium" if confident else "low",
            ("high" if len(sections) >= 3 else "medium") if confident else "low",
            sentence[:120],
            f"{'restated' if confident else 'possibly restated'} in {len(sections)} sections: {locations}; other occurrence: {excerpt(other, 0, min(80, len(other)), context_width)}",
            "Keep the first statement and replace each later occurrence with what its section adds; a judgement repeated across sections reads as filler."
            if confident else
            "Verify that this is the same judgement as the other occurrence rather than a shared entity; keep one and let the other advance, or dispose of the candidate.",
            {"sections": len(sections), "occurrences": len(cluster.items),
             "ratio": int(round(cluster.ratio * 100)), "containment": int(round(cluster.containment * 100)),
             "cjk_skeleton": int(cjk_document)},
        ))
    return findings


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

    payload = {"schema_version": 1, "scanner_version": "1.7", "files": files, "finding_count": len(all_findings), "findings": [asdict(item) for item in all_findings]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in all_findings:
            print(f"{item.path}:{item.line} [{item.severity}/{item.confidence}] {item.rule_id} {item.finding_id}")
            print(f"  {item.context}")
            print(f"  -> {item.suggestion}")
        print(f"findings: {len(all_findings)}; files unchanged: {all(file['unchanged'] for file in files)}")
    return 1 if threshold_reached(all_findings, args.fail_on) else 0


if __name__ == "__main__":
    raise SystemExit(main())
