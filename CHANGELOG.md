# Changelog

## Unreleased

- Flags evidence-free rigor, unexplained anomalies, and cross-section restatements in the
  final-editor scanner with auditable signal vectors, direction-aware anomaly detection,
  document-level probes, and one report per defect instead of one per sentence: a
  paragraph dominated by framing is signalled on its sentences rather than counted again.
- Catches paraphrased cross-section restatements through containment and a low-severity
  possible-paraphrase band, and presentation narration such as "结果均按数值区间报告".
  Chinese manuscripts are compared on their Chinese content skeleton, so shared identifiers
  and table fragments no longer look like restatements, and headings, cross-references,
  definition lead-ins, definitions, and abstracts are not compared at all.
- Adds a final-editor dispositions helper that writes the per-finding skeleton and self-checks
  decisions, evidence, IDs, and bindings before the orchestrator gate runs.
- Gives every scanner finding a content-derived `finding_id` and requires itemized
  dispositions with a decision and evidence per finding; count-only legacy receipts still
  validate as a separate tier and the two tiers cannot be mixed.
- Organizes the documentation around default use, visual customization, and
  development, with a three-stage Studio walkthrough.
- Adds connected stage creation, undo/redo for unsaved edits, automatic canvas
  layout, and a permanent workflow-settings entry.
- Moves advanced stage settings into a collapsible section, suggests available
  validator inputs, and links validation findings to the affected stage.
- Shows setup steps and a copyable Codex continuation prompt after activation.
- Preserves artifact handoffs when inserting stages, invalidates checks after
  refreshing Skills, and improves compact-screen and short-window layouts.
- Adds digest-checked Official v1 snapshot updates with a single progress event
  and consistent next-action handoff, while preserving validity and rule status.
- Preserves literal paths and nested rule fields in progress updates, rejects
  linked or special progress files, and bounds reads before taking further action.
- Shares a read-only execution-progress summary between the custom CLI and its
  Markdown projection, and exposes recovery with explicit interruption confirmation.
- Keeps user-accepted artifacts in versioned snapshots with a compact confirmation
  index and type-organized current copies, while preserving original working files
  and existing receipt bindings. Adoption records actual verification evidence
  and a user decision separately from progress updates and task completion.
- Adds metadata-only artifact status, hash-verified snapshot resolution,
  withdrawal without fallback, and explicit backup of edited views during repair.
- Documents the short resume path through mode, current progress, and confirmed
  artifacts, with historical detail retrieved when needed.

## v1.1.0

This release adds an optional visual editor for advanced users while retaining
the official v1.0 workflow as the default.

- Adds the local Workflow Studio for arranging task, condition, parallel, join,
  and validator stages through a graphical interface.
- Adds a durable custom DAG runtime with explicit validation, risk review,
  activation, and state separate from the official progress workflow.
- Packages the Studio as a deterministic offline bundle with a generated
  third-party license inventory, file hashes, and an offline-resource verifier.
- Keeps the normal Skill runtime on Python's standard library; Node.js is only
  required when developing or rebuilding the Studio frontend.
- Bounds local Skill catalog scans and hashes files incrementally so oversized
  or unusually large Skill trees cannot consume unbounded resources.
- Marks all-active joins with missing or conflicting outputs as blocked, and
  allows an attempted `skip_branch` task failure to be retried in the same run
  when no dependent descendant work has started.
- Adds frontend unit tests, browser end-to-end coverage, and CI checks for the
  reproducible runtime bundle.

## v1.0.0

This is the first stable release of Paper Workflow Orchestrator.

- Adds the gated research-to-paper workflow with durable progress memory,
  evidence tracking, user confirmation gates, and Codex-internal delegation.
- Adds claim-bound scientific figure routing and fail-closed figure receipts.
- Adds the cross-platform `core`, `standard`, and `full` Skill installer with
  pinned dependencies, license metadata, archive safety, backups, and recovery.
- Adds paper-section, humanizer, final-editor, and bounded-experiment checks.
- Documents installation, runtime prerequisites, safety boundaries, and
  third-party license conditions in English and Simplified Chinese.
- Normalizes temporary-directory fixtures before installer tests, keeps
  user-controlled symlink parents blocked, and adds Linux, macOS, and Windows
  CI coverage for Python 3.10 and 3.13.
- Uses neutral final-editing terminology across the main workflow and bundled
  Final Editor while preserving existing v1.0 machine identifiers for receipt
  and progress compatibility.

The release version is recorded as `1.0.0`; the progress schema identifies the
workflow as `paper-workflow-orchestrator-v1.0`.
