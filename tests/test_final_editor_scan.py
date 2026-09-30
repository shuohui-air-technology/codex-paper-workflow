"""Scanner coverage and evidence-free rigor detection for the final editor."""
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "companion-skills" / "academic-manuscript-final-editor" / "scripts" / "scan_manuscript_style.py"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCAN = load_module(SCANNER, "scan_manuscript_style")
CHECKER = ROOT / "companion-skills" / "academic-manuscript-final-editor" / "scripts" / "check_scan_dispositions.py"
CHECK = load_module(CHECKER, "check_scan_dispositions")


def scan(text, name="manuscript.md"):
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / name
        path.write_text(text, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = SCAN.main(["--json", str(path)])
    return code, json.loads(output.getvalue())


def findings(report, rule_id):
    return [item for item in report["findings"] if item["rule_id"] == rule_id]


def rescan(path):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        SCAN.main(["--json", str(path)])
    return json.loads(output.getvalue())


class ReferenceRegionTests(unittest.TestCase):
    def test_references_section_ends_at_the_next_heading(self):
        text = (
            "# Title\n\n门禁 first body line.\n\n"
            "## References\n\n1. Foo (2020). A cited work.\n\n"
            "## Appendix\n\n门禁 appendix line.\n"
        )
        _, report = scan(text)
        self.assertEqual(report["scanner_version"], "1.7")
        self.assertEqual([item["line"] for item in findings(report, "internal_workflow_zh")], [3, 11])

    def test_fenced_references_does_not_disable_body_scanning(self):
        text = "# Title\n\n```python\n# References\n```\n\n门禁 body after fence.\n"
        _, report = scan(text)
        self.assertEqual([item["line"] for item in findings(report, "internal_workflow_zh")], [7])

    def test_plain_text_keeps_the_references_region_open(self):
        text = "Title\n\n门禁 body before.\n\nReferences\n\n门禁 entry after.\n"
        _, report = scan(text, name="manuscript.txt")
        self.assertEqual([item["line"] for item in findings(report, "internal_workflow_zh")], [3])


class EvidenceFreeRigorTests(unittest.TestCase):
    def test_usage_clause_without_information_is_a_candidate(self):
        text = "# Title\n\n三组指标可作为环境分层抽样和后续模拟的参考。\n"
        _, report = scan(text)
        rigor = findings(report, "defensive_rigor_no_content")
        self.assertEqual(len(rigor), 1)
        self.assertEqual(rigor[0]["signals"]["strong_info"], 0)
        self.assertEqual(rigor[0]["signals"]["usage"], 1)
        self.assertEqual(rigor[0]["confidence"], "high")

    def test_sentence_with_quantities_is_not_a_rigor_candidate(self):
        text = "# Title\n\n该组的时间留出 R² 为 −0.144，跨年份水平校准较弱。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_no_content"), [])

    def test_paragraph_of_rigor_sentences_is_signalled_not_duplicated(self):
        text = (
            "# Title\n\n这些结果可作为模拟输入的参考。"
            "这些结果的统计对象由当前船队和时空范围共同界定。\n"
        )
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_block"), [])
        rigor = findings(report, "defensive_rigor_no_content")
        self.assertEqual(len(rigor), 2)
        for item in rigor:
            self.assertEqual(item["signals"]["paragraph_rigor"], 2)
            self.assertEqual(item["confidence"], "high")

    def test_anomaly_needs_a_follow_up_probe(self):
        bare = "# Title\n\n该组的空间留出 R² 为 −0.144。四档记录保留了可比较的差异。\n"
        _, report = scan(bare)
        self.assertEqual(len(findings(report, "anomaly_without_probe")), 1)

        probed = "# Title\n\n该组的空间留出 R² 为 −0.144。这可能源于 2017 年作业占比偏高。\n"
        _, report = scan(probed)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])

    def test_cross_section_repeat_ignores_markup_fragments(self):
        text = (
            "# One\n\n四档记录在作业之间保留了可比较的档位组成差异。\n\n"
            "# Two\n\n四档记录在作业之间保留了可比较的档位组成差异。"
            "<!--ref:Maunder2004SRC016--><!--anchor:section:CPUE-framework-->\n"
        )
        _, report = scan(text)
        self.assertEqual(len(findings(report, "cross_section_repeat")), 1)


    def test_scope_wall_with_only_variable_names_is_a_block(self):
        text = (
            "# 讨论\n\n解释范围首先由 CPUE 的渔业依赖观测性质决定。"
            "渔具选择性和可捕性尚未完成逐字段溯源，未测混杂仍可能影响方向性判断。\n"
        )
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_block"), [])
        rigor = findings(report, "defensive_rigor_no_content")[0]
        self.assertEqual(rigor["signals"]["strong_info"], 0)
        self.assertEqual(rigor["signals"]["paragraph_rigor"], 2)


class SectionAwareAnomalyTests(unittest.TestCase):
    def test_captions_and_summary_sections_are_exempt(self):
        caption = "# Results\n\na，2022—2023 年 GTNN 与 GAM 的 R²；负值完整保留。\n"
        _, report = scan(caption)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])

        abstract = "# Abstract\n\nGTNN 取得正 R²，SKJ-PS 的证据不一致，BET 面板未显示稳定外推。\n"
        _, report = scan(abstract)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])

    def test_anomaly_is_discharged_by_a_later_explanation_of_the_same_result(self):
        cross_section = (
            "# Results\n\nYFT-LL 的三个种子平均 ΔR² 为 −0.1304，表现为预测性能下降。\n\n"
            "# Discussion\n\n该面板的 ΔR² 下降可能源于努力量口径变化。\n"
        )
        _, report = scan(cross_section)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])

        unrelated = (
            "# Results\n\nYFT-LL 的三个种子平均 ΔR² 为 −0.1304，表现为预测性能下降。\n\n"
            "# Discussion\n\nBET 面板的波动可能来自掩码设置。\n"
        )
        _, report = scan(unrelated)
        self.assertEqual(len(findings(report, "anomaly_without_probe")), 1)

        adjacent = (
            "# Results\n\nYFT-LL 的三个种子平均 ΔR² 为 −0.1304，表现为预测性能下降。"
            "该差异可能源于掩码设置。\n"
        )
        _, report = scan(adjacent)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])


class DocxSectionTests(unittest.TestCase):
    def scan_docx(self, paragraphs):
        body = "".join(
            f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
            for style, text in paragraphs
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "manuscript.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", xml)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = SCAN.main(["--json", str(path)])
        return code, json.loads(output.getvalue())

    def test_docx_heading_styles_drive_section_repeats(self):
        _, report = self.scan_docx([
            ("Heading1", "1 引言"),
            ("BodyText", "四档记录在作业之间保留了可比较的档位组成差异。"),
            ("Heading1", "2 结果"),
            ("BodyText", "四档记录在作业之间保留了可比较的档位组成差异。"),
        ])
        self.assertEqual(len(findings(report, "cross_section_repeat")), 1)
        self.assertEqual(report["files"][0]["coverage_status"], "main-document-text-only")

class DirectionAwareAnomalyTests(unittest.TestCase):
    def test_lower_error_metrics_are_an_improvement(self):
        text = "# Results\n\nBET-LL 的 RMSE 和 MAE 同时下降，95% 区间排除 0。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])

    def test_coefficient_sign_statements_are_not_anomalies(self):
        text = "# Results\n\nYFT-LL 的三个月系数为正，YFT-PS 的一个月系数为负。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])

    def test_panel_by_panel_defect_is_one_finding_per_paragraph(self):
        text = (
            "# Results\n\nYFT-LL 的三个种子平均 ΔR² 为 −0.1304，表现为预测性能下降。"
            "YFT-PS 的三个种子平均 ΔR² 为 −0.0468，区间方向不一致。\n"
        )
        _, report = scan(text)
        anomalies = findings(report, "anomaly_without_probe")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["signals"]["anomaly_sentences"], 2)

    def test_same_defect_restated_in_another_section_is_one_event(self):
        text = (
            "# Results\n\n3–<5 yr 组的时间留出 R² 为 −0.144，跨年份水平校准较弱。\n\n"
            "# Interpretation\n\n该组的时间留出 R² 仍为 −0.144，跨年份校准较弱。\n"
        )
        _, report = scan(text)
        anomalies = findings(report, "anomaly_without_probe")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["line"], 3)
        self.assertEqual(anomalies[0]["signals"]["anomaly_sentences"], 2)
        self.assertIn("line(s): 7", anomalies[0]["context"])

    def test_numeric_probe_in_the_same_paragraph_discharges_the_anomaly(self):
        text = "# Results\n\n其余四个面板未达到稳健转换标准。阶段支持窗口不足，因此未列入转换面板。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "anomaly_without_probe"), [])


class RigorGuardTests(unittest.TestCase):
    def test_method_definition_is_not_filler(self):
        text = (
            "# Methods\n\n为概括四档记录保留的组成信息，令 \\(\\omega_b\\) 为第 b 档占 BET 总重量的比例，"
            "重量档有效数定义为 \\(N_{\\mathrm{eff}}\\)。\n"
        )
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_no_content"), [])

    def test_metric_name_counts_as_strong_information(self):
        text = "# Methods\n\n相对训练集均值基线的 RMSE skill 用于描述跨时间和空间应用时的预测支持。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_no_content"), [])

    def test_sentence_that_names_its_mechanism_is_not_filler(self):
        text = "# Discussion\n\n由于月尺度 SST 自相关较强，候选变量可能高度共线，检验的区分度较为保守。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_no_content"), [])

    def test_traceability_gap_is_a_hedged_candidate(self):
        text = "# Discussion\n\n原始捕捞量、努力量和渔具步骤尚未完成逐字段溯源，未测混杂仍可能影响方向性判断。\n"
        _, report = scan(text)
        rigor = findings(report, "defensive_rigor_no_content")
        self.assertEqual(len(rigor), 1)
        self.assertGreaterEqual(rigor[0]["signals"]["hedge"], 2)
        self.assertEqual(rigor[0]["confidence"], "high")

    def test_usage_clause_with_a_new_token_is_a_candidate(self):
        text = "# Discussion\n\n三组结果可为同类商业数据的方法比较提供证据。\n"
        _, report = scan(text)
        rigor = findings(report, "defensive_rigor_no_content")
        self.assertEqual(len(rigor), 1)
        self.assertEqual(rigor[0]["signals"]["usage"], 1)


class FindingIdTests(unittest.TestCase):
    def test_every_finding_carries_a_unique_content_id(self):
        text = "# Title\n\n门禁 first body line.\n\n## Appendix\n\n门禁 appendix line.\n"
        _, report = scan(text)
        ids = [item["finding_id"] for item in report["findings"]]
        self.assertTrue(all(item.startswith("fnd-") for item in ids))
        self.assertEqual(len(set(ids)), len(ids))

    def test_ids_are_stable_across_runs_of_the_same_file(self):
        text = "# Title\n\n三组指标可作为环境分层抽样和后续模拟的参考。\n"
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "manuscript.md"
            path.write_text(text, encoding="utf-8")
            first, second = rescan(path), rescan(path)
        self.assertEqual([item["finding_id"] for item in first["findings"]],
                         [item["finding_id"] for item in second["findings"]])
        self.assertTrue(first["findings"])

    def test_identical_findings_keep_a_deterministic_ordinal(self):
        def make():
            return SCAN.Finding("m.md", 3, 3, "internal_workflow_zh", "internal_workflow",
                                "high", "high", "门禁", "门禁", "fix it")
        findings = [make(), make()]
        SCAN.assign_finding_ids(findings)
        self.assertEqual(findings[1].finding_id, findings[0].finding_id + "-2")


class CrossSectionAggregationTests(unittest.TestCase):
    def test_judgement_repeated_in_three_sections_is_one_finding(self):
        sentence = "四档记录在作业之间保留了可比较的档位组成差异。"
        text = f"# One\n\n{sentence}\n\n# Two\n\n{sentence}\n\n# Three\n\n{sentence}\n"
        _, report = scan(text)
        repeats = findings(report, "cross_section_repeat")
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["signals"], {"sections": 3, "occurrences": 3, "ratio": 100, "containment": 100,
                                                 "cjk_skeleton": 1})
        self.assertEqual(repeats[0]["confidence"], "high")
        self.assertEqual(repeats[0]["severity"], "medium")

    def test_contained_restatement_is_a_confident_repeat(self):
        first = "五个敏感性情景的汇总期望年龄为3.313–3.517 yr，跨度为0.204 yr。"
        second = "五个敏感性情景的汇总期望年龄为3.313–3.517 yr，三组比例在各情景中保持相同排序。"
        _, report = scan(f"# Results\n\n{first}\n\n# Conclusions\n\n{second}\n")
        repeats = findings(report, "cross_section_repeat")
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["severity"], "medium")
        self.assertEqual(repeats[0]["signals"]["containment"], 82)

    def test_paraphrased_restatement_is_reported_at_low_severity(self):
        first = "EKE_100 在 0–<3 yr 组表现为负向条件关联，在 ≥5 yr 组表现为正向条件关联；3–<5 yr 组的置换支持较弱。"
        second = ("EKE_100 在 0–<3 yr 组呈负向关联，在 ≥5 yr 组呈正向关联，三个组对的差异区间均排除 0；"
                  "固定组成对照产生的组间异质性为主要分析的 3.6%。")
        _, report = scan(f"# Results\n\n{first}\n\n# Discussion\n\n{second}\n")
        repeats = findings(report, "cross_section_repeat")
        self.assertEqual(len(repeats), 1)
        self.assertEqual((repeats[0]["severity"], repeats[0]["confidence"]), ("low", "low"))
        self.assertEqual(repeats[0]["signals"]["ratio"], 38)

    def test_paraphrase_across_a_conclusion_is_genre_not_a_restatement(self):
        first = "EKE_100 在 0–<3 yr 组表现为负向条件关联，在 ≥5 yr 组表现为正向条件关联；3–<5 yr 组的置换支持较弱。"
        second = "EKE_100 在 0–<3 yr 组呈负向关联，在 ≥5 yr 组呈正向关联，三个组对的差异区间均排除 0。"
        _, report = scan(f"# Results\n\n{first}\n\n# Conclusions\n\n{second}\n")
        self.assertEqual(findings(report, "cross_section_repeat"), [])

        repeated = "五个敏感性情景的汇总期望年龄为3.313–3.517 yr，跨度为0.204 yr。"
        _, report = scan(
            f"# Results\n\n{repeated}\n\n"
            "# Conclusions\n\n五个敏感性情景的汇总期望年龄为3.313–3.517 yr，三组比例在各情景中保持相同排序。\n"
        )
        repeats = findings(report, "cross_section_repeat")
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["severity"], "medium")

    def test_abstract_definitions_and_lead_ins_are_not_restatements(self):
        sentence = "三组记录在作业之间保留了可比较的档位组成差异。"
        abstract = f"# Abstract\n\n{sentence}\n\n# Results\n\n{sentence}\n"
        self.assertEqual(findings(scan(abstract)[1], "cross_section_repeat"), [])

        lead_in = (
            "# Methods\n\n相对训练集均值基线的 RMSE skill 用于描述预测支持：\n\n"
            "# Results\n\n相对训练集均值基线的 RMSE skill 用于描述预测支持。\n"
        )
        self.assertEqual(findings(scan(lead_in)[1], "cross_section_repeat"), [])

        formula = (
            "# One\n\n其中 \\(H_i\\) 为作业 \\(i\\) 的钩数，单位为 kg RW/1,000 hooks。\n\n"
            "# Two\n\n其中年龄 \\(a\\) 的单位为 yr，体长 \\(\\bar L(a)\\) 的单位为 cm FL。\n"
        )
        self.assertEqual(findings(scan(formula)[1], "cross_section_repeat"), [])

    def test_english_manuscripts_compare_the_full_text(self):
        text = ("# One\n\nThe spatial hold-out skill stayed positive across the three age groups.\n\n"
                "# Two\n\nThe spatial hold-out skill stayed positive across three age groups.\n")
        repeats = findings(scan(text)[1], "cross_section_repeat")
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["signals"]["cjk_skeleton"], 0)
        self.assertEqual(repeats[0]["severity"], "medium")

    def test_chinese_documents_ignore_shared_identifiers_and_fragments(self):
        filler = "本研究的六个面板在各年份范围内保持了可比较的观测覆盖，未出现明显的记录缺口。"
        text = (
            f"# One\n\n{filler}\n\n观测 CPUE：SKJ-LL +6.7%、SKJ-PS +9.2%\n\n"
            f"# Two\n\n{filler}\n\n外部指数 +61.1%；GTNN：SKJ-LL −2.5%、SKJ-PS −2.6%\n"
        )
        repeats = findings(scan(text)[1], "cross_section_repeat")
        self.assertEqual([item["line"] for item in repeats], [3])
        self.assertEqual(repeats[0]["signals"]["cjk_skeleton"], 1)

    def test_repeat_of_a_reported_rigor_sentence_is_not_double_reported(self):
        sentence = "三组指标可作为环境分层抽样和后续模拟的参考。"
        _, report = scan(f"# One\n\n{sentence}\n\n# Two\n\n{sentence}\n")
        self.assertEqual(findings(report, "cross_section_repeat"), [])
        self.assertEqual(len(findings(report, "defensive_rigor_no_content")), 2)

    def test_headings_and_cross_references_are_not_compared(self):
        heading = "## 2.5 五情景年龄组包络与敏感性设置"
        text = (
            f"# One\n\n{heading}\n\n# Two\n\n{heading}\n\n"
            "正文见第 3.3 节说明各情景的设置与敏感性结果。\n\n# Three\n\n本节内容见第 3.3 节。\n"
        )
        _, report = scan(text)
        self.assertEqual(findings(report, "cross_section_repeat"), [])


class PresentationNarrationTests(unittest.TestCase):
    def test_presentation_clause_without_information_is_a_candidate(self):
        text = "# Methods\n\n3 yr边界位于该年龄区间之后，为分组提供生命史参照；结果均按数值区间报告。\n"
        _, report = scan(text)
        rigor = findings(report, "defensive_rigor_no_content")
        self.assertEqual(len(rigor), 1)
        self.assertEqual(rigor[0]["signals"]["procedural"], 1)
        self.assertEqual(rigor[0]["signals"]["strong_info"], 1)
        self.assertEqual(rigor[0]["confidence"], "high")
        # the evidence is the narration clause, so a fix cannot delete the substance
        self.assertEqual(rigor[0]["evidence"], "结果均按数值区间报告")

    def test_presentation_clause_with_a_value_is_not_a_candidate(self):
        text = "# Methods\n\n各指标均按 0.05 的分位阈值报告。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_no_content"), [])

    def test_narration_without_a_presentation_verb_is_not_a_candidate(self):
        text = "# Methods\n\n年龄与时间均按1/12 yr推进，采用保持非负的特征线更新和终端加组。\n"
        _, report = scan(text)
        self.assertEqual(findings(report, "defensive_rigor_no_content"), [])


class DispositionCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.scan_path = self.project / "editorial_scan.json"
        source = self.project / "canonical.md"
        source.write_text(
            "# Manuscript\n门禁 internal gate narration.\n三组指标可作为环境分层抽样和后续模拟的参考。\n",
            encoding="utf-8",
        )
        report = rescan(source)
        self.assertTrue(report["findings"])
        self.scan_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    def run_check(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = CHECK.main([*args, "--json"])
        raw = output.getvalue()
        return code, (json.loads(raw) if raw.strip() else None)

    def test_template_covers_every_finding_id_and_binds_the_report(self):
        out = self.project / "editorial_scan_dispositions.json"
        code, result = self.run_check("--scan", str(self.scan_path), "--template-out", str(out))
        self.assertEqual((code, result["status"], result["tier"]), (0, "pass", "itemized"))
        template = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([item["finding_id"] for item in template["dispositions"]], result["finding_ids"])
        self.assertEqual(template["disposed_count"], len(result["finding_ids"]))
        self.assertEqual(template["scanner_report_sha256"],
                         "sha256:" + hashlib.sha256(self.scan_path.read_bytes()).hexdigest())

    def test_self_check_needs_one_decision_and_evidence_per_finding(self):
        out = self.project / "dispositions.json"
        self.run_check("--scan", str(self.scan_path), "--template-out", str(out))
        code, result = self.run_check("--scan", str(self.scan_path), "--dispositions", str(out))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("decision" in error for error in result["errors"]))

        template = json.loads(out.read_text(encoding="utf-8"))
        for item in template["dispositions"]:
            item["decision"] = "accept"
            item["evidence_refs"] = ["editorial_style_ledger.yml#rule-1"]
        out.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        code, result = self.run_check("--scan", str(self.scan_path), "--dispositions", str(out))
        self.assertEqual((code, result["status"]), (0, "pass"))

        template["dispositions"] = template["dispositions"][:-1]
        template["disposed_count"] = len(template["dispositions"])
        out.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        code, result = self.run_check("--scan", str(self.scan_path), "--dispositions", str(out))
        self.assertEqual(code, 1)
        self.assertTrue(any("must match" in error for error in result["errors"]))

        template["dispositions"][0]["rule_id"] = "extra metadata"
        template["disposed_count"] = len(template["dispositions"])
        out.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        code, result = self.run_check("--scan", str(self.scan_path), "--dispositions", str(out))
        self.assertTrue(any("unsupported keys" in error for error in result["errors"]))

    def test_self_check_treats_a_report_without_ids_as_legacy(self):
        report = json.loads(self.scan_path.read_text(encoding="utf-8"))
        report["findings"] = [{key: value for key, value in item.items() if key != "finding_id"}
                              for item in report["findings"]]
        legacy = self.project / "legacy_scan.json"
        legacy.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        code, result = self.run_check("--scan", str(legacy))
        self.assertEqual((code, result["status"], result["tier"]), (0, "pass", "legacy"))
        code, result = self.run_check("--scan", str(legacy), "--template-out", str(self.project / "template.json"))
        self.assertEqual((code, result), (2, None))

    def test_repeated_content_keeps_the_deterministic_suffix(self):
        item = {"path": "m.md", "line": 3, "rule_id": "internal_workflow_zh", "evidence": "门禁"}
        base = CHECK.finding_id(item)
        tier, ids, problems = CHECK.scan_tier({"finding_count": 2, "findings": [
            {**item, "finding_id": base}, {**item, "finding_id": base + "-2"}]})
        self.assertEqual((tier, ids, problems), ("itemized", [base, base + "-2"], []))

    def test_finding_id_scheme_agrees_across_producer_checker_and_validator(self):
        from scripts.final_edit_receipt_validator import finding_id as validator_id, scanner_finding_ids
        report = json.loads(self.scan_path.read_text(encoding="utf-8"))
        for item in report["findings"]:
            self.assertEqual(CHECK.finding_id(item), item["finding_id"])
            self.assertEqual(validator_id(item), item["finding_id"])
        ids, tier = scanner_finding_ids(report)
        self.assertEqual(tier, "itemized")
        self.assertEqual(ids, [item["finding_id"] for item in report["findings"]])


if __name__ == "__main__":
    unittest.main()
