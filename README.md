![Paper Workflow Orchestrator Banner](assets/banner.png)

# Paper Workflow Orchestrator

**From vague idea to high-quality paper — a complete workflow controller.**

[简体中文](README.zh-CN.md) | **English**

> Helping you turn any vague idea into a top-tier journal-level paper.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)
![Codex Skill](https://img.shields.io/badge/Codex-Skill-8A2BE2.svg)
![Dependencies](https://img.shields.io/badge/Dependencies-None-success.svg)

## Overview

Paper Workflow Orchestrator is a Codex skill that organizes the full research-to-paper pipeline — from idea, literature, and study design through experimentation, writing, integrity audit, peer review, revision, and AI handoff — into a stage-gated, evidence-tracked process.

The main model acts as editor-in-chief: it owns routing, the evidence ledger, prompt design, conflict resolution, and final synthesis. Codex-internal subagents execute only bounded, independently reviewable tasks. Every stage ends with a confirmation gate; the workflow never advances silently.

## Features

Each feature maps to a concrete component in this repository:

| Feature | Implementation |
|---|---|
| 13-stage gated workflow with a user confirmation gate at every stage | `SKILL.md`, `references/stage-contracts.md` |
| Four entry modes: guided idea, draft audit, write/revise, experiment | `SKILL.md` |
| Durable, concurrency-safe append-only progress memory and evidence ledger (v0.4) | `scripts/progress_manager.py`, `references/progress-schema.md` |
| Critical validity blockers that stop the abstract, review, and finalization | `references/progress-schema.md` |
| Bounded subagent delegation with per-stage context packs and dispatch receipts | `references/stage-contracts.md` |
| Author-guided final-edit stage gated by validated protected receipts | `references/final-editor-integration.md`, `scripts/final_edit_receipt_validator.py` |
| Bundled companion skill: author-voice final editing in Revise / Audit / Learn modes | `companion-skills/academic-manuscript-final-editor/SKILL.md` |
| Read-only whole-manuscript style scan with four severity levels and CI exit codes | `companion-skills/academic-manuscript-final-editor/scripts/scan_manuscript_style.py` |
| Author-style editing rules for full-draft revision and bilingual sync | `companion-skills/academic-manuscript-final-editor/references/author-style-rules.md` |
| Format-safe humanizer adapter that fails closed on any missing contract | `scripts/humanizer_preflight.py`, `references/humanizer-adapter.md` |
| Experiment-contract validation required before any autonomous run | `scripts/experiment_contract_validator.py` |
| Paper structure gates: required sections and their order | `scripts/paper_section_validator.py`, `references/paper-section-contract.md` |
| Zero runtime dependencies (Python stdlib only) | all `scripts/*.py` |

## Workflow Stages

The orchestrator routes one primary downstream skill per stage. Each stage produces required artifacts and ends with a user gate.

1. **Intake** — diagnose input, confirm entry mode and constraints
2. **Directions** — explore 3–5 candidate research directions
3. **Literature** — discover and triage verified sources
4. **Topic** — gap / contribution / feasibility verdict and question lock
5. **Design** — falsifiable study design and experiment matrix
6. **Architecture** — freeze paper structure, section profile, outline
7. **Drafting** — bounded internal agents write sections; main model synthesizes
8. **Integrity** — audit citations, numbers, claims, leakage, reproducibility
9. **Review & Revision** — peer-review simulation, revision matrix, and substantive revision
10. **Author-Guided Final Edit** — learn from author feedback, scan full-text analogues, and validate protected content
11. **Naturalization** — optional format-safe humanizer pass with claim/evidence diff
12. **Final Editorial Audit** — read-only author-voice verification after naturalization
13. **Finalize** — final integrity audit, rendering, handoff card, and delivery

At every gate the user receives completed work, a progress snapshot, remaining risks, 2–5 next-step options (one marked **Recommended**), and the exact confirmation needed to continue. The abstract is written only after the body, results, interpretation, and conclusion are stable; a conclusion is always mandatory.

## Project Structure

```
paper-workflow-orchestrator/
├── SKILL.md                          # Orchestrator skill definition & routing
├── agents/
│   └── openai.yaml                   # Agent interface declaration
├── assets/                           # Logo and README banner
├── references/
│   ├── paper-section-contract.md     # Title/abstract/methods/results/conclusion contract
│   ├── progress-schema.md            # Progress memory v0.4 schema & error rules
│   ├── stage-contracts.md            # Stage table, delegation & acceptance predicates
│   ├── final-editor-integration.md   # Author-guided final-edit handshake
│   └── humanizer-adapter.md          # Format-safe humanizer adapter protocol
├── scripts/
│   ├── progress_manager.py           # Progress init/validate/migrate/record/restore
│   ├── humanizer_preflight.py        # Humanizer preflight (fail-closed)
│   ├── paper_section_validator.py    # Section order & required-section checks
│   ├── final_edit_receipt_validator.py # Protected final-edit receipt validator
│   └── experiment_contract_validator.py  # Bounded experiment contract validator
└── companion-skills/
    └── academic-manuscript-final-editor/  # Companion skill for the final-edit stage
```

## Installation

This repository contains two skills, installed separately:

1. **`paper-workflow-orchestrator`** — the main workflow controller. Copy the skill files from the repository root into your Codex skills folder (`CODEX_HOME/skills`, defaults to `~/.codex/skills` when `CODEX_HOME` is unset).
2. **`academic-manuscript-final-editor`** — the companion skill used by the author-guided final-edit stage. Copy `companion-skills/academic-manuscript-final-editor/` into the same skills folder as its own directory.

**Windows PowerShell** (run from the repository root):

```powershell
$skillsHome = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills' }
$main = Join-Path $skillsHome 'paper-workflow-orchestrator'
New-Item -ItemType Directory -Force -Path $main | Out-Null
Copy-Item -Recurse -Force SKILL.md, agents, assets, references, scripts $main
Copy-Item -Recurse -Force companion-skills\academic-manuscript-final-editor (Join-Path $skillsHome 'academic-manuscript-final-editor')
```

**macOS / Linux** (run from the repository root):

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$SKILLS_HOME/paper-workflow-orchestrator"
cp -R SKILL.md agents assets references scripts "$SKILLS_HOME/paper-workflow-orchestrator/"
cp -R companion-skills/academic-manuscript-final-editor "$SKILLS_HOME/"
```

Reload Codex or refresh the skills list after installing.

The author-guided final-edit stage requires the companion `academic-manuscript-final-editor` skill, version `2.1.0` or newer, with `capability_schema: final-editor-v1` in its YAML frontmatter; the matching version is bundled in `companion-skills/`. If it is unavailable or incompatible, the orchestrator reports the missing capability and asks whether to install it or explicitly skip that stage.

## Usage

Trigger the full workflow with:

```
Use paper-workflow-orchestrator to run a gated, evidence-tracked research-to-paper workflow.
```

This skill is a workflow controller, not a single-task tool. When you only need a literature matrix, study design, prose polishing, or citation audit, let `research-skill-router` select a narrower dedicated skill instead of loading the full pipeline.

All scripts use only the Python standard library (Python 3.10+); no third-party packages are required.

## Safety Boundaries

- `autoresearch` is never auto-loaded and never starts unattended experiments without an explicit, complete, validated contract.
- Subagents cannot change the research direction, edit the final manuscript, write `progress.md` directly, or fabricate citations, numbers, or results.
- The humanizer cannot bypass the format adapter, protected manifest, claim/evidence diff, integrity receipt, or rollback target.
- The final editor stays dormant until substantive revision and integrity pass; it produces a candidate and bound rollback receipt rather than silently overwriting the canonical manuscript.
- DOCX, PDF, and LaTeX require a format-specific adapter; when parsing is insufficient the stage stays `blocked`.
- Critical validity problems cannot be concealed by rewriting the abstract, conclusion, or prose style.

## Contributing

Contributions are welcome. Please keep all scripts dependency-free (Python standard library only) and describe how you verified your changes. Do not commit personal papers, `.research/`, `.paper/`, experiment data, credentials, or locally generated caches.

## License

This project is licensed under the [MIT License](LICENSE).
