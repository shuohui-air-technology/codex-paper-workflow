# Progress Schema and Reliability Rules

Use `.research/progress.md` as the canonical, append-aware project memory. Keep the headings stable so another AI can parse the file without the full conversation.

## Canonical sections

```markdown
# Paper Progress

## Project Metadata
- project_id:
- last_updated:
- current_stage:
- target_venue:
- document_format:
- workflow_version: paper-workflow-orchestrator-v0.4
- mode: guided_idea | draft_audit | write_or_revise | autonomous_experiment

## Current Snapshot
- research_question:
- selected_direction:
- current_status:
- completed_milestones:
- next_action:
- blockers:
- validity_status: pending | clear | blocked
- hub_status: ready | degraded | missing | not_applicable
- last_stage_receipt:

## Core Progress
- P001: [milestone] [evidence refs] [status]

## Core Experience
- E001: [validated practice] [evidence refs] [confidence]
- E002: [accepted prose/style practice] [humanizer pass and author preference] [confidence]

## Error Avoidance Rules
- R001:
  - error:
  - cause:
  - impact:
  - severity: critical | major | minor | unspecified
  - blocking: true | false
  - prevention_rule:
  - required_check:
  - applicable_stages:
  - status: active | superseded | resolved
- R002:
  - error: language polishing changed scientific meaning or protected text
  - cause: humanizer received unscoped manuscript content or its output was not compared
  - impact: claim/evidence drift, citation damage, or altered technical definitions
  - prevention_rule: run humanizer only after integrity; protect non-prose regions; compare claim/evidence before accepting
  - required_check: protected-region and claim/evidence diff passes
  - applicable_stages: prose naturalization, revision, finalization
  - status: active | superseded | resolved

## Decisions
- D001:
  - question:
  - chosen_option:
  - rejected_options:
  - decision_owner: user | main-model | evidence
  - evidence:

## Open Questions and Risks
- Q001: [question] [owner] [next check]
- K001: [risk] [probability/impact] [mitigation]

## Handoff Card
- next_agent_reads:
- must_not_repeat:
- active_constraints:
- resume_instruction:

## Append-only Event Log
- EVT-001: [timestamp] [stage] [type] [summary] [artifact refs]
```

Keep list IDs unique within their section. Never reuse an ID after a correction; append a new entry and reference the old one.

## Progress delta contract

Subagents return deltas; only the main model commits them:

```yaml
event_type: milestone | experience | error | decision | risk
summary: concise description
evidence_refs: []
error_id: null
proposed_rule: null
required_check: null
confidence: low | medium | high
```

For `error` deltas, `proposed_rule` and `required_check` are mandatory. Include
`severity` and `blocking` when the error can affect validity. For `decision`
deltas, include the selected and rejected options and the decision owner. Reject
a delta that has no evidence reference when it makes an externally verifiable
claim. Only the main model may change `validity_status` or commit a delta.

After the initial error entry, append a correction event containing the failed artifact, remediation, re-run result, and evidence references. An error rule is not marked `resolved` until its required check passes on the corrected artifact. Keep `Handoff Card.must_not_repeat` derived from active rules only and compact it to the most relevant active rules; the full history remains in this section and the event log.

## Commit timing

Commit a progress update for:

- project initialization;
- a completed stage and its artifacts;
- user confirmation or rejection;
- a failed validation, error, or retry;
- a new evidence-backed conclusion;
- a change of scope, venue, design, or key terminology;
- a handoff or session close.
- a stage receipt or context-pack budget failure.

Do not log every model token or routine tool call. Prefer one concise event per material change.

The `progress_manager.py record-error` event fields use bracket delimiters.
Reference values may contain `[` or `]`; the manager percent-encodes those
characters before appending the event so the record remains parseable. Other
event fields reject delimiter characters instead of writing an ambiguous record.

## Error-to-guardrail rule

When a material error occurs:

1. Record the symptom, cause, impact, and affected artifact.
2. Add an active `Error Avoidance Rule` with a concrete check.
3. Add the check to the next applicable stage prompt.
4. Re-run the failed check after correction.
5. Append the correction and evidence to the event log.

Examples of useful checks include citation-source verification, claim–evidence alignment, result-log provenance, equation/notation consistency, data-leakage review, and venue-format compliance.

## Critical validity blocker

Use `severity: critical` and `blocking: true` when a data, measurement,
identification, leakage, model-assumption, baseline, or robustness problem could
change the core conclusion. Such a rule requires `validity_status: blocked` and
blocks the abstract, final Conclusion, humanizer, peer review, and finalization.
The main model must request additional evidence, standard methods/baselines,
robustness work, or a user-approved design discussion. After correction, rerun
the required check and append a recovery event; only then may the rule be marked
`resolved` and validity return to `clear`.

## Handoff protocol

The next AI must read `Project Metadata`, `Current Snapshot`, active `Error Avoidance Rules`, `Decisions`, `Open Questions and Risks`, and `Handoff Card` before reading broad project files. It must state what it learned and which stage it will resume before changing anything.

If the progress file fails validation, do not continue from its contents. Use the validated `.bak` generation and the `progress_manager.py restore` command, or stop and ask the user to resolve the state conflict. The `.lock` file is a persistent advisory-lock container; do not delete it while another process may be active. Restore appends a recovery event and archives the replaced generation under a unique `.corrupt-*` name.

For a valid pre-v0.4 file that lacks `mode` or the newer snapshot fields, run
`progress_manager.py migrate --confirm` with a user-confirmed mode. Migration
preserves the legacy generation as `.legacy-v0.2` or `.legacy-v0.3`, adds
`validity_status: pending`, and gives legacy rules
`severity: unspecified`/`blocking: false` without guessing their importance.
If a legacy rule already explicitly has `status: active` and `blocking: true`,
migration preserves that evidence and sets `validity_status: blocked`; otherwise
it uses `pending`. It creates a validated v0.4 `.bak` recovery point and appends
a migration event; it never silently guesses the project mode or resolves a
possible blocker.
