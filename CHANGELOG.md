# Changelog

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

The release version is recorded as `1.0.0`; the progress schema identifies the
workflow as `paper-workflow-orchestrator-v1.0`.
