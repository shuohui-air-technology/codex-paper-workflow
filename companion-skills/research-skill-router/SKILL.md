---
name: research-skill-router
description: Use when an academic, AI, machine-learning, literature, experiment, or paper task could match two or more installed research skills, or when the user asks which research skill to use. Select one primary skill and keep overlapping skills from loading together; do not use for a narrowly scoped task with an obvious single skill.
---

# Research Skill Router

Route the task before doing substantive work. Use the smallest useful skill set: one primary skill, plus a secondary skill only when the primary output explicitly depends on it. Do not read every candidate `SKILL.md`.

## Routing table

| User intent | Primary skill |
|---|---|
| Vague AI/ML idea, innovation, task definition, anchor papers, novelty attack | `clarify-research-idea` |
| Decide whether one proposed gap/topic is worth pursuing | `gap-to-topic` |
| Topic chosen; sharpen RQ, mechanism, identifiability, validation, risks | `research-design-helper` |
| Discover papers, ingest PDFs, organize Zotero/Obsidian/NotebookLM | `research-hub` |
| Compare a known set of papers by method/data/claims/limitations | `literature-triage-matrix` |
| Summarize papers already ingested into a research-hub cluster | `paper-summarize` |
| Existing manuscript/draft audit, review, or citation/claim integrity check | `paper-workflow-orchestrator` |
| Build reusable memory from the user's own manuscript and figures | `paper-memory-builder` |
| Draft or revise an AI/ML conference paper | `ml-paper-writing` |
| Scientifically stable manuscript; apply author feedback across the full draft, learn scoped author rules, or synchronize bilingual final versions | `academic-manuscript-final-editor` |
| Generic AI-pattern or mechanical-prose cleanup after scientific content is frozen | `humanizer` |
| Claim-bearing scientific figures or figure audit after inputs are frozen | `scientific-visualization` |
| New scientific figure or substantive redesign requiring inspected published references | `reference-first-figures` |
| Python/R manuscript figure implementation, statistics or export QA with an established design | `nature-figure` |
| Explicit end-to-end gated paper workflow, progress memory, AI handoff, or workflow audit | `paper-workflow-orchestrator` |
| General research-to-paper, systematic review, citation, or manuscript workflow | `academic-paper` (the `academic-research-suite` compatibility alias) |
| Review an Agent-Native Research Artifact after structural validation | `ara-rigor-reviewer` |
| Verify a NotebookLM brief against its source bundle | `notebooklm-brief-verifier` |
| Create or refresh `.research/` project manifests | `research-context-compressor` |
| Orient in a project that already has `.research/` manifests | `research-project-orienter` |
| Start or run any autonomous experiment/research loop | `paper-workflow-orchestrator` |

## Selection protocol

1. Classify the user's immediate stage: workflow design/audit, idea, gap, design, discovery, comparison, experiment, writing, review, or project memory.
2. When multiple rows appear to match, choose the most specific user intent. An explicit end-to-end workflow, progress-memory, handoff, workflow-audit, or draft-audit request takes precedence over the generic research-to-paper row. Every autonomous-experiment request routes through the orchestrator safety gate; `autoresearch` is a downstream runner, never a top-level bypass.
3. Select exactly one primary skill from the table.
4. State the route in one sentence: `Primary skill: <name> — <reason>.`
5. Load only that skill's `SKILL.md`. Load a secondary skill only if the primary workflow names a concrete handoff or missing artifact.
6. If the request spans stages, run them sequentially; do not activate all stages in one turn without a user-approved pipeline.

## Anti-overlap rules

- `clarify-research-idea` → `gap-to-topic` → `research-design-helper` is a sequence, not three simultaneous skills.
- Use either `ml-paper-writing` or `academic-paper`/`academic-research-suite` for drafting; use both only when one is explicitly handling a distinct handoff.
- Use either `ara-rigor-reviewer` or the academic manuscript reviewer workflow; do not run both for the same review pass.
- `academic-manuscript-final-editor` and `humanizer` are serial, not simultaneous. Use the final editor for author-evidence generalization and whole-manuscript final editing; use Humanizer afterward only for generic AI-pattern cleanup with protected content isolated. If both are requested inside a paper project, route through `paper-workflow-orchestrator`, then run a read-only final-editor audit after Humanizer.
- Do not route an initial draft, method change, external citation verification, or layout-only request to `academic-manuscript-final-editor`.
- `research-hub` requires the CLI runtime. Check `research-hub describe --json` and `research-hub doctor` before executing searches, ingestion, or vault changes. If unavailable, use a prompt-only skill and say so.
- `paper-workflow-orchestrator` owns multi-stage progress, gates, subagent contracts, and handoff state when explicitly selected; dispatch its downstream skills sequentially rather than loading `academic-research-suite` as a competing top-level controller. Existing draft audits and workflow audits must not fall through to a generic writing route.
- `scientific-visualization` is a downstream figure skill only. Load it after claims, source data, transformations, units, uncertainty, missing-data policy, panel numbering, palette, and target size are frozen; never load it alongside a competing writing skill for the same task, and never use it to make a conceptual diagram look like data evidence.
- Keep `autoresearch` dormant unless the user explicitly asks for autonomous, iterative experimentation and the orchestrator has recorded a complete, user-confirmed bounded contract with `validation_status: pass`, `approved_by: orchestrator`, `user_confirmation: recorded`, `report_destination: local-only`, and a stage receipt. Invoke it only through the orchestrator's bounded local wrapper; never infer permission for indefinite loops, background operation, cron, external reports, or unattended changes.

## Output contract

For figure tasks, pick the most specific route: a small correction retains the
current tool; new/reference-led design uses `reference-first-figures`; a ready
Python/R implementation uses `nature-figure`; general scientific visualization
uses `scientific-visualization`. These are alternative primary implementations,
not parallel reviews. A named handoff shares the established design, backend,
source data and QA trail. GIS/native-vector work keeps its current tools; mixed
figures may hand only named quantitative panels to `nature-figure`. Inside a
custom workflow, route a different Skill through its own configured node.

Before acting, provide:

```text
Primary skill: <name>
Why: <one sentence>
Not loading: <one or two overlapping skills, if relevant>
```

If the task is genuinely ambiguous, ask one concise routing question or offer at most two candidate routes. Do not produce a full research report while routing is unresolved.
