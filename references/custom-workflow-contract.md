# Custom Workflow Mode Contract

This contract applies only after the user has explicitly selected a custom
workflow in Workflow Studio. The official v1.0 workflow remains the default.

## Resolve the mode first

Before reading `progress.md`, invoke the installed manager's non-repairing mode
summary for the selected project:

```text
python <orchestrator-root>/scripts/workflow_manager.py summary --project <project-root> --json
```

Use the returned `mode`; do not infer it by inspecting workflow files yourself.

| Selection and manager result | Required behavior |
|---|---|
| No selector or activation journal has been recorded, or resolved mode is `official` | Use the existing Official v1.0 path and its `progress.md`. Do not inspect saved custom drafts or custom run files. |
| Selector projection is missing but a valid activation journal exists | Use the mode resolved from that journal; do not infer Official v1.0 from the missing projection. |
| Selection is custom and summary returns `mode: custom` | Use the selected custom workflow through `workflow_manager.py`; do not read or update `progress.md` or `current_stage`. |
| A present selector or activation journal is corrupt/conflicting, or a custom selection's required plan/run/evidence is missing or inconsistent | Stop and report the manager error. Never fall back silently to Official v1.0 or custom mode. |

The manager validates the activation journal and selection projection without
repairing them during mode resolution. In custom mode it then verifies the
activated plan, event history, and state snapshot against one another, checks
recorded artifact hashes against project files, and verifies stored receipt
projections against result events. A saved `workflow.json` is an editable Studio
draft, not authority to execute a workflow. If required state cannot be
reconciled, execution remains blocked. This release has no general-purpose
`recover` CLI command. `deactivate` returns to Official v1.0 only when the
manager can validate the selector and any persisted run state; a failed summary
does not guarantee that deactivation will succeed. Preserve the evidence and
stop for a supported recovery procedure rather than editing state files.

## Run one task node

For a ready custom run, follow this order:

```text
workflow_manager.py summary
workflow_manager.py ready
workflow_manager.py claim --node <node-id>
invoke exactly the one Skill named by the returned node-invocation-v1
workflow_manager.py submit-result --result <project-relative-result.json>
```

`ready` is the source of eligible node IDs. Claim only nodes listed there and
respect the manager-enforced `max_parallelism`. The manager-generated
`node-invocation-v1` binds the workflow/run, attempt, selected Skill identity,
declared inputs and outputs, and an attempt-bound idempotency token. For each
claim, invoke only that Skill; do not disclose the token in reports or receipts,
and do not invent a result or claim an output file that the Skill did not
actually create.

Write the actual result using the `node-result-v1` schema, then submit it. The
manager verifies the claim, inputs, output paths and hashes, and writes the
`stage-receipt-v2`. Do not write or edit run state, receipts, or event logs
directly. After result submission, ask the manager for readiness again; it
resolves eligible condition and join nodes from recorded facts and outcomes.

## Run a validator node

Validator nodes are checks, not Skills that generate manuscript files. When a
validator node appears in `ready`, invoke only:

```text
workflow_manager.py run-validator --node <node-id>
```

The manager resolves the node against the project's registered validator
catalog, checks its declared inputs, and runs only the approved adapter. Do not
substitute a shell command or run a validator directly to advance custom run
state. `humanizer-preflight` is unavailable in the first custom-mode release;
an enabled instance blocks activation, while a disabled instance remains only
as a dormant draft node and can never run.

## Register inputs and record branch facts

Existing project files become workflow inputs only through
`workflow_manager.py register-artifact`, which records the declared ID,
project-relative path, content hash, and provenance summary. Project facts and
user decisions that control conditions enter the run only through
`workflow_manager.py record-fact` and `workflow_manager.py record-decision`,
with their provenance summaries. Project facts are booleans; decisions use the
bounded JSON values allowed by the condition contract. The manager records
changes as events and re-evaluates dependent conditions. Repeating the same
value is idempotent; changing a value invalidates affected downstream work and
requires the manager to revalidate its lineage.

## Failure, retry, stale work, and exit

- Use `workflow_manager.py retry --node <node-id>` for a failed task or
  validator when the manager marks it retryable.
- Use `workflow_manager.py rerun-stale --node <node-id>` only after reviewing
  why prior evidence became stale. A stale result is not retried implicitly.
- Use `workflow_manager.py deactivate` to explicitly return to Official v1.0.
  Deactivation does not delete the custom draft or its historical run data.
- If any manager command reports a blocked or integrity error, stop that branch
  and preserve the evidence. Do not bypass the manager by editing
  `.research/custom-workflow/` or by switching to `progress.md`.

The custom manager is the sole runtime authority while custom mode is selected.
Never update `.research/progress.md` or its `current_stage` to mirror custom
nodes; the official progress schema remains specific to Official v1.0.
