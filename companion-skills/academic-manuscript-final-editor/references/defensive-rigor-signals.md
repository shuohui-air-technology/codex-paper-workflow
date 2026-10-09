# Evidence-free rigor signals

Candidate locators for language that performs rigor instead of stating it. Every
hit is a review prompt: the editor keeps, rewrites, or records a disposition.

## Zero-information defensive language

A scanner's sparse feature vector locates candidates; it does **not** establish
that a sentence has no scientific information. The lexical filter looks for
digits, units, variable or metric names (`RMSE`, `MAE`, `skill`), intervals,
directions, citations, and table/figure references. Their absence is not grounds
for deletion: unresolved provenance, confounding, and observation-versus-target
distinctions can be essential without these markers. Their presence does not
make an otherwise redundant sentence useful. Decide by its information gain
relative to the surrounding definitions and argument.

Three scanner shapes:

1. **Usage/external-validity clause** — "可作为…参考", "可用于检验…", "可沿用…",
   "提供证据", "共同界定使用范围". Review whether usefulness is specified or merely
   asserted; the scanner's confidence label is not an editorial verdict.
2. **Hedged filler** — limits and cautions ("限于", "据此解释为", "分别判断",
   "尚未完成逐字段溯源", "并不必然", "在…范围内") with a sparse feature vector.
   In particular, unfinished provenance is an unresolved substantive issue, not
   filler to hide. Record it or retain it in the appropriate disclosure.
3. **Presentation narration** — a clause that only says how material is formatted
   or stored ("结果均按数值区间报告", "…均按面板保存"). It is judged inside its own
   clause: "年龄与时间均按 1/12 yr 推进" carries a value and is real method text,
   and a narration clause that holds no number, unit, or metric is a candidate even
   when the surrounding sentence holds one.

Recorded evidence: `hedge`, `strong_info`, `weak_info`, `usage`, `procedural`, `chars`.

Not a candidate:

- a limitation that names the concrete quantity, level, or test ("32.5 m 层级占
  68.5%", "时间留出 R² 为 −0.144"); "更长序列可用于检验" is a candidate, but
  "更长序列需覆盖 2019–2024" states a testable condition;
- a sentence that defines notation or builds a quantity ("令 … 为第 b 档占比",
  "有效数定义为…", "以 CPUE 为响应变量", "构成组成包络"), or that carries an
  equals sign or display-math fragment;
- a sentence that names why something happens ("由于月尺度 SST 自相关较强…区分
  较为保守", "解释范围首先由 CPUE 的渔业依赖观测性质决定"): a named cause is
  information, not filler.

## Paragraph-level dilution

Two or more zero-information rigor sentences in one paragraph are not reported as
an extra finding: each of those sentences carries `paragraph_rigor` (the paragraph's
count) and high confidence, so the paragraph judgement stays visible once per
sentence instead of being counted twice. The confidence lift, not a block-level
severity, carries the dilution signal, and each sentence keeps its own advice so a
paragraph that also contains real interpretation is not misdescribed as pure
framing. Keep the one limit that changes a reader's decision.

## Anomaly without a probe

Two gates decide whether a defect report is a candidate.

1. **It must report a degraded result, not a value.** A direction word counts only
   when a performance metric or an explicit baseline shortfall binds it: "RMSE 和
   MAE 同时下降" and "区间排除 0" are improvements, "一个月系数为负" states a
   value, while "R² 均为负" and "未优于季节基线" are defects.
2. **No part of the manuscript may explain it.** A probe is a mechanism, an
   alternative, or an explicit unresolved-cause note ("可能", "源于", "窗口不足",
   "排除 0", "由…主导", "尚未"). A probe in the same sentence or paragraph counts
   directly; a probe elsewhere counts when it names the same panel or metric.
   One defect is then reported once: sentences in one paragraph collapse into a
   single finding, and a defect restated elsewhere with the same negative value
   joins the first report (the repeated line is named in the context). The number
   of affected sentences travels in `anomaly_sentences`.

Abstracts, methods, closing sections, references, and captions are exempt: those
surfaces defer mechanisms by design.

## Cross-section restatement

A judgement restated in another section instead of advanced becomes
`cross_section_repeat`. Comparison is normalised (digits, parentheses, markup and
punctuation removed) and judged by two metrics: character sequence ratio and the
share of the shorter sentence's character bigrams contained in the other.

In a Chinese manuscript (`cjk_skeleton: 1`) both metrics run on the Chinese
content only, so a shared identifier ("EKE_100", "RMSE"), a table fragment, or a
Latin abbreviation cannot make two sentences look like a restatement; a pair whose
Chinese content is too short to compare is not compared at all. Manuscripts
without enough Chinese are compared on the full text (`cjk_skeleton: 0`).

- **Confident repeat** — ratio ≥ 0.62 or containment ≥ 0.70 (a later sentence
  repeats the first almost entirely before adding material). Reported at medium
  severity, high confidence when three or more sections are involved.
- **Possible paraphrase** — ratio ≥ 0.38, calibrated on reviewed manuscripts.
  Reported at low severity and low confidence: the band still admits sentences
  that share a template or a background fact, so the editor verifies before
  acting. This band does not compare pairs across an introduction or conclusion,
  because a thesis and its summary are genre rather than a restatement; a
  confident repeat (a verbatim figure or phrase) is still flagged in those
  sections, so a conclusion that reproduces a number verbatim is reported while a
  reworded callback is not; that boundary is deliberate.

Restatements are clustered and reported once, carrying `sections`, `occurrences`,
`ratio`, `containment`, and `cjk_skeleton`. Headings, cross-references ("见第 3.3
节"), definition lead-ins ("…用于描述预测支持："), definition sentences, formula
notation, abstracts, references, and captions are structure, genre, or machinery
rather than restated judgements and are not compared. Only different sections are
compared, so a section that quotes itself is not flagged, and a sentence already
reported as `defensive_rigor` is not reported again as a repeat.

## Dispositions

Each finding needs its own decision and evidence. Keeping a usage clause is
allowed when a journal or funder expects an availability statement; record the
reason instead of deleting silently.
