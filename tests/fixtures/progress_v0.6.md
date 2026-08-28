# Paper Progress

## Project Metadata
- project_id: legacy-v0.6-fixture
- last_updated: 2025-05-01T00:00:00+00:00
- current_stage: experiments
- target_venue: legacy venue
- document_format: Word
- workflow_version: paper-workflow-orchestrator-v0.6
- mode: autonomous_experiment

## Current Snapshot
- research_question: preserve the v0.6 question
- selected_direction: preserve the v0.6 direction
- current_status: experiment blocked pending robustness check
- completed_milestones: P001
- next_action: run the required robustness check
- blockers: active model-assumption risk
- validity_status: blocked
- hub_status: ready
- last_stage_receipt: experiment-receipt-v06

## Core Progress
- P001: legacy v0.6 milestone [legacy-v06-evidence] [complete]

## Core Experience
- E001: legacy v0.6 practice [legacy-v06-evidence] [high]

## Error Avoidance Rules
- R001:
  - error: legacy v0.6 model-assumption risk
  - cause: legacy fixture
  - impact: core conclusion may change
  - severity: critical
  - blocking: true
  - prevention_rule: complete the robustness check before drafting claims
  - required_check: verify the robustness result
  - applicable_stages: experiments, integrity
  - status: active

## Decisions
- D001:
  - question: legacy v0.6 decision
  - chosen_option: preserve
  - rejected_options: discard
  - decision_owner: evidence
  - evidence: legacy-v06-evidence

## Open Questions and Risks
- Q001: legacy v0.6 question [owner] [next check]
- K001: legacy v0.6 risk [high/high] [mitigation]

## Handoff Card
- next_agent_reads: Project Metadata, Current Snapshot
- must_not_repeat: R001 — complete the robustness check before drafting claims
- active_constraints: preserve blocker and legacy fixture fields
- resume_instruction: keep validity blocked until the robustness check passes

## Append-only Event Log
- EVT-001: [2025-05-01T00:00:00+00:00] [experiments] [error] legacy v0.6 blocker [legacy-v06-evidence]
