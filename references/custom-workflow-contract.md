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
reconciled, execution remains blocked. The supported recovery command is:

```text
python <orchestrator-root>/scripts/workflow_manager.py recover \
  --project <project-root> --confirm-interrupted
```

Use it only after confirming that no task or validator from the run is still
executing. Recovery replays and validates the durable event log, restores
receipt projections, and marks uncertain running work as `blocked`; it never
turns an interrupted attempt into a successful result and never starts a new
task. A `blocked` recovery result preserves the evidence for inspection. Do not
edit `.research/custom-workflow/` state, event, or receipt files by hand.

## Run one task node

For a ready custom run, follow this order:

```text
workflow_manager.py summary
workflow_manager.py ready
workflow_manager.py claim --node <node-id>
invoke exactly the one Skill named by the returned node-invocation-v1 or node-invocation-v2
workflow_manager.py submit-result --result <project-relative-result.json>
```

`ready` is the source of eligible node IDs. Claim only nodes listed there and
respect the manager-enforced `max_parallelism`. The manager-generated invocation
binds the workflow/run, attempt, selected Skill
identity, declared inputs and outputs, and an attempt-bound idempotency token.
Newly activated plans use `node-invocation-v2`; it also names the activated
Skill root and requires a `node-result-v2` result. For each claim, invoke only
that Skill; do not disclose the token in reports or receipts, and do not invent
a result or claim an output file that the Skill did not actually create.

For a reference-led figure implementation node, the controller reads
[the figure implementation adapter](figure-implementation-adapter.md) and passes
its applicable requirements in the task brief. The installed third-party Skill
remains unchanged. Its reusable examples do not replace this run's fixed
inputs, approved design, output scope or confirmation requirements.

Write the actual result using the schema named by the invocation. A
`node-result-v2` task result includes `consumed_sources`, a sorted list of the
project-relative files actually used, each with its SHA-256 hash. Every listed
source must be one of the frozen claim inputs with the same path and hash; an
undeclared or changed source is rejected. The manager persists that list in a
`stage-receipt-v3`, so recovery can replay the same check. Existing v1 runs
continue to accept `node-result-v1` and write `stage-receipt-v2`.

This declared-source check records what the executor reports it read. Without
an OS-level file-access sandbox, it cannot prove that an executor did not read
an additional unreported file. Do not write or edit run state, receipts, or
event logs directly. After result submission, ask the manager for readiness
again; it resolves eligible condition and join nodes from recorded facts and
outcomes.

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

To use a confirmed bundle as a declared external input, run
`workflow_manager.py register-confirmed-input --project <project-root>
--artifact-id <declared-id> --role <role-id> --expect-catalog-revision <revision>`.
It verifies the snapshot and binds its entrypoint (or `--file <relative-path>`)
and hash inside the run transaction. Later adoption never rewrites that binding.
See [material management](materials.md) for version differences on resume.

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

Approval gates are condition nodes configured to wait for an explicit user
answer. After the source stage succeeds, the manager keeps the gate waiting
until the user accepts or requests revision. Record the answer with a JSON file
containing `node_id`, `action` (`approve` or `revise`), and
`provenance_summary`, then run:

```text
workflow_manager.py record-approval --approval <project-relative-approval.json>
```

The approval is bound to the source stage's attempt and output hashes. A source
rerun, output change, or revision request invalidates the old approval. To run a
stale source again, inspect the preserved receipt and explicitly call
`workflow_manager.py rerun-stale --node <source-node-id>`; the manager refuses
to reopen a closure while a descendant is running. A gate's approval edge only
controls readiness; the original artifact edges remain responsible for passing
files to the downstream stage.

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
