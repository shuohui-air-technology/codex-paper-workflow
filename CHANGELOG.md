# Changelog

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
