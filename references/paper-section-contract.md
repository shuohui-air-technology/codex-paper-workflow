# Paper Section Contract

This contract defines the default structure for a complete research paper. The
orchestrator adapts it to the selected venue, language, document format, and
study type, but must not silently remove a required semantic function.

## Section order and status

| Component | Status | Writing time | Gate |
|---|---|---|---|
| Title | required | provisional at outline; final after results and conclusion | reject overclaiming titles |
| Abstract | required | only after the body, results, interpretation, and conclusion are stable | author confirmation plus consistency audit |
| Keywords | required when the venue requests them | after the abstract | terminology and searchability check |
| 1 Introduction | required | after the evidence set and research question are locked | concise gap, innovation, and RQ check |
| 2 Materials and Methods | required | after the design is locked | reproducibility and no-results-in-method check |
| 3 Results | required for empirical work | after verified analysis logs | claim-evidence and uncertainty check |
| 4 Discussion | optional as a standalone heading | after results | semantic discussion function must still exist |
| 5 Conclusion | required | after results and discussion/interpretation | direct answer, contribution, and validity-boundary check |
| References | required for cited work | continuously, final audit at the end | source, DOI, title, and claim-support audit |
| Appendix | optional | after the main text | no indispensable evidence hidden outside the body |

The default manuscript is concise, evidence-led, and suitable for a top-tier
venue. Venue instructions override word counts and heading names, but not the
requirements that the abstract follow the body, the conclusion exist, and
critical validity problems block advancement.

## Title

Generate two to five candidate titles during the outline stage. Each title
must identify the object, method or comparison, and setting without claiming
more generality than the evidence supports. Select the final title only after
the results and conclusion are stable. If a title implies causality, novelty,
or broad transfer, the claim-evidence ledger must explicitly support it.

## Abstract

Write the abstract from the accepted body rather than from the original idea.
It should contain, in compact form:

1. the problem and why it matters;
2. the objective or research question;
3. the data/material and method;
4. the most important verified results, including uncertainty when relevant;
5. the conclusion and bounded contribution or application.

Do not add citations, new numbers, unreported methods, unverified explanations,
or stronger claims than the main text. The main model proposes two or three
abstract versions, discusses the intended emphasis with the user, records the
choice in `progress.md`, and then runs an abstract-to-body consistency check.
Any material change to results, discussion, or conclusion invalidates the
abstract and triggers regeneration.

## Keywords

Select venue-compatible, stable terms after the abstract. Prefer the research
object, task, method, data setting, and application domain over promotional
phrases. Record terminology decisions so later agents use the same names.

## 1 Introduction

Keep the introduction short, normally four focused paragraphs or an equivalent
venue-specific structure:

1. define the research object and its importance;
2. synthesize how prior work approached similar problems, including data,
   methods, and strategies;
3. identify a specific, evidence-supported limitation in the target setting;
4. state the research question, the finite innovation, the main contributions,
   and the scope of the claims.

Do not use a long paper-by-paper catalogue or an unsupported statement such as
“no study has ever”. The novelty claim should be a defensible improvement in a
specific subproblem, not a revolutionary claim unless the evidence truly
supports one.

## 2 Materials and Methods

Choose one profile at the architecture gate:

### Method-first profile (default for AI/ML and model-centric work)

1. task definition, notation, assumptions, and formulas;
2. established industry or field-standard methods and baselines;
3. the proposed method and the limited, clearly isolated innovation;
4. data/material source, sampling, preprocessing, splits, training or analysis;
5. validation, robustness, ablation, uncertainty, reproducibility, and ethics.

Keep equations concentrated here. Use a method diagram, pipeline, or design
figure to explain the innovation. Do not place experimental results or
interpretive conclusions in this section.

### Data-first profile

Use this for short observational, descriptive, or material-led studies:

1. study object, area, time range, and material source;
2. preprocessing and variable definitions;
3. the standard analysis method and assumptions;
4. the finite modification or extension introduced by this study;
5. validation, robustness, and reproducibility.

In both profiles, state what was done, why it answers the research question,
and how another researcher can reproduce it. Never hide required methods in an
appendix merely to shorten the main text.

## 3 Results

Organize results by research question, hypothesis, or pre-registered analysis
order. Every central statement must map to a result artifact, table, figure,
statistical output, or verified experiment log. Report effect direction,
uncertainty, relevant negative or null findings, and deviations from the
planned analysis. Keep method descriptions brief and do not introduce a new
model or unverified mechanism in the results.

## 4 Discussion (optional heading)

Use a separate Discussion heading when interpretation, comparison, limitations,
and implications are substantial. If the venue combines sections, place the
same semantic functions in “Results and Discussion”, “Analysis”, or another
clearly named section. Record the paper type and manuscript language in the
section receipt. The workflow fails the paper if the heading is omitted and
those functions are absent. Discussion must distinguish what the data show
from what is a plausible interpretation, compare with verified literature,
state the application boundary, and address limitations proportionately. The
final gate also requires a schema-versioned semantic receipt bound to the
manuscript hash and a separately hashed verifier receipt with an identity,
profile binding, independent pass checks for discussion/conclusion/abstract
consistency, required-section coverage, and evidence references.

## 5 Conclusion

The conclusion is mandatory and is not a mechanical copy of the abstract. It
should:

1. answer the research question directly;
2. summarize two to four central findings without repeating every number;
3. emphasize the substantive, finite contribution and its valid applications;
4. state the scope boundary and give only limited, natural follow-up work.

Limitations normally occupy about two or three sentences and must not dominate
the contribution. If a limitation could change the core conclusion, it is not a
routine limitation: record a `critical_validity_blocker`, set the project to
`blocked`, and require additional data, standard baselines, robustness work, or
research-design discussion before drafting a final conclusion or abstract.

## References and Appendix

Formal references must be traceable to a source record and must support the
claim for which they are cited. The appendix may contain supplementary proofs,
algorithms, parameters, extra experiments, or reproducibility details, but the
main argument must remain understandable without it.

## Section-level handoff and agent rules

Each section receipt records its status, evidence refs, unresolved risks, and
the exact user confirmation needed. Writing agents receive only the section's
context pack and active progress guardrails. They return prose or structured
evidence plus a `progress_delta`; they never edit the canonical draft or
`progress.md`. The main model resolves conflicts, performs the evidence check,
and serially commits accepted progress updates.
