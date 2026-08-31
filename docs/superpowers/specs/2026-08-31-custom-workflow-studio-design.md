# Custom Workflow Studio Design

**Project:** Paper Workflow Orchestrator  
**Design status:** Approved in the architecture discussion  
**Target release:** v1.1.0  
**Compatibility baseline:** current v1.0 behavior at commit `e24c34255e3c72a329614e07c431bfa51a778c40`  
**Initial v1.0 release commit:** `73a05ff3647e44f05e30793177afcbfb9756120c`

## 1. Summary

Paper Workflow Orchestrator v1.1.0 will add an optional, local-browser
**Custom Workflow Studio** for advanced users. It will let them arrange installed
Skills as a directed acyclic graph (DAG), add or remove stages, select one primary
Skill per task stage, and define sequential, conditional, parallel, and join
behavior without manually editing configuration text.

This is an additive architecture change, not a rewrite of the official v1.0
workflow. The existing official path remains the default and retains its current
progress format, receipts, validators, gates, and user-facing behavior. The new
custom workflow control plane is entered only after an explicit advanced-user
action.

## 2. Goals

1. Preserve the official v1.0 workflow as the zero-configuration default.
2. Give advanced users a graphical way to create a genuinely executable custom
   workflow rather than merely drawing a flowchart or generating a prompt.
3. Support ordered stages, conditional branches, parallel ready sets, and joins.
4. Keep each task node bound to exactly one installed primary Skill.
5. Allow every official paper stage or gate to be removed or replaced in a
   custom workflow. The system warns about reduced control coverage but does not
   insert deleted stages back into the graph.
6. Keep the custom workflow definition, runtime state, events, artifacts, and
   receipts auditable and recoverable.
7. Keep Node.js and frontend packages out of the end-user runtime requirement.
8. Add only one focused advanced-use section to each README; do not displace the
   Router Skills framing or default quick start.

## 3. Non-goals

- Replacing the official v1.0 execution path with the new DAG engine.
- Migrating or rewriting existing `.research/progress.md` files or v1 receipts.
- Supporting cycles, feedback edges, or an indefinitely repeating stage.
- Installing Skills or downloading third-party code from the Studio.
- Executing arbitrary Python, JavaScript, shell commands, or prompt text from a
  branch condition.
- Building a native macOS application, remote SaaS, multi-user collaboration,
  cloud queue, or background daemon.
- Proving that a custom workflow is scientifically sound. The system reports
  differences and risks; the advanced user owns the chosen workflow.
- Creating a GitHub Release as part of this feature.

## 4. Fixed design decisions

- This is an **architectural, incremental extension**.
- The official v1.0 behavior is a non-negotiable compatibility requirement.
- The advanced editor is a localhost browser GUI.
- Users do not need to read or edit JSON.
- Custom graphs are DAGs. They may contain multiple entry branches but no cycle.
- A task node has one primary Skill. Multiple Skills are represented as multiple
  nodes connected sequentially or in parallel.
- Control nodes such as conditions and joins do not bind a Skill.
- The Studio lists locally installed, recognizable Skills only.
- A missing Skill may be preserved in an imported draft, but blocks activation
  and execution.
- Custom paper-safety stages are removable. Their removal produces an explicit
  warning, not an automatic repair or semantic block.
- Engine integrity, filesystem containment, valid graph structure, and evidence
  required to claim node completion remain hard runtime invariants.

## 5. Why the custom control plane is separate

The current v1.0 workflow definition is distributed across `SKILL.md`,
`references/stage-contracts.md`, `references/progress-schema.md`, and the fixed
stage identifiers in `scripts/progress_manager.py`. Its state model centers on a
single `current_stage`, while a custom DAG can have several ready, running,
blocked, or completed nodes at once.

Putting custom graph state into the existing progress format would either lose
parallel state or force a breaking schema migration. Conversely, turning the
entire official workflow into a new graph engine would make the feature close to
a rewrite and widen the regression surface unnecessarily.

The design therefore keeps two explicit paths:

```text
Ordinary user
  -> official v1.0 path
  -> existing progress, contracts, gates, and validators

Advanced user
  -> Workflow Studio
  -> custom workflow compiler and scheduler
  -> Router resolves one installed Skill for each ready task node
  -> existing Skills and validators
  -> isolated custom state and receipts
```

The official path does not load, create, or infer custom workflow state unless
the project has been explicitly switched to a custom workflow.

### 5.1 Startup and state-authority decision table

The Orchestrator checks the workflow selection before any progress file:

| Selection state | Authoritative runtime state | v1 progress behavior |
| --- | --- | --- |
| `selection.json` absent | Official v1 path | Read and update existing `.research/progress.md`; do not read custom state |
| selection is `official` | Official v1 path | Read and update existing `.research/progress.md`; do not read custom state |
| selection is a valid custom semantic revision | Custom path | Use custom `state.json` plus `events.jsonl`; do not create or update v1 `progress.md`, v1 `current_stage`, or v1 stage receipts |
| selection is corrupt or references missing custom state | Blocked | Do not fall back silently to official and do not update either state system |

Custom mode may import an existing v1 artifact or receipt only as a read-only
`path + SHA-256` reference in its own artifact registry. It never writes the
import back into v1 progress. `SKILL.md` and `references/stage-contracts.md` must
scope their current progress rules explicitly to official mode and point custom
mode to the new control-plane contract.

This separation concerns official workflow progress and stage-transition
receipts. A registered domain validator may still validate its existing
figure/final-edit/experiment receipt format. Custom mode records the validator's
receipt path and hash inside a custom node receipt without redefining or
silently rewriting that domain receipt.

## 6. Repository layout

The implementation should keep focused modules with one responsibility each:

```text
references/workflows/
  official-v1.0-studio-projection.json  # read-only acyclic Studio projection
  validator-registry.v1.json            # allowed validator identities and entrypoints

scripts/
  workflow_manager.py             # CLI used by the Orchestrator
  workflow_studio.py              # localhost Studio entrypoint
  workflow_engine/
    __init__.py
    catalog.py                    # installed-Skill discovery and metadata
    schema.py                     # document parsing and structural validation
    conditions.py                 # restricted condition AST and evaluation
    compiler.py                   # canonical execution plan and graph hash
    scheduler.py                  # ready-set, branch, parallel, and join logic
    risk.py                       # comparison with official control coverage
    store.py                      # locks, events, atomic files, and recovery
    receipts.py                   # node receipt creation and verification
    studio_server.py              # narrow localhost HTTP API

studio/                            # React/TypeScript/Vite development source
assets/workflow-studio/            # versioned prebuilt offline bundle
```

`official-v1.0-studio-projection.json` is a read-only, acyclic projection used
for Studio display, cloning, and risk comparison. It is intentionally not a
second canonical definition of official execution. The official documentation
contains revision feedback that cannot be represented as an edge in the custom
DAG. The projection therefore records:

- the exact source commit and SHA-256 of each official source file;
- a mapping from every projected node to its official stage or gate;
- projection notes for feedback behavior represented as a later run or workflow
  revision rather than a cycle; and
- explicit omissions, if any.

Tests check source hashes and mapping completeness, not semantic equality
between the projection and the official execution path. In v1.1.0 the projection
is never the execution source for the official default path.

## 7. Component responsibilities

### 7.1 Workflow Studio

- Displays the official v1.0 Studio projection as read-only and labels the
  projected treatment of official feedback behavior.
- Creates an editable custom copy only after an explicit user action.
- Provides a node palette, graph canvas, properties panel, and validation area.
- Supports mouse and keyboard operations for adding, selecting, connecting,
  moving, duplicating, disabling, and deleting nodes.
- Saves drafts separately from activation.
- Never executes a Skill or decides that a node succeeded.

### 7.2 Skill catalog

- Resolves Skill roots in this order: explicit repeatable `--skills-root` values
  used for development and tests; roots recorded by the workflow installation;
  then the configured Codex user Skills root, with the normal
  `${CODEX_HOME}/skills` location or `~/.codex/skills` fallback.
- Parses only the metadata needed for display and resolution, such as name and
  description; discovery does not execute Skill content.
- Refreshes when Studio opens and on explicit refresh.
- Marks a reference as missing if the named Skill is no longer installed.
- Does not install, update, download, or silently substitute a Skill.

A recognized Skill has a valid `SKILL.md`, a normalized metadata name matching
its catalog ID, and a resolved path contained by an allowed Skill root. A
symlink is accepted only when its resolved target remains inside that same root.
Two copies with the same catalog ID and different content are `ambiguous` and
cannot be selected. Byte-identical duplicates collapse to one identity.

The identity bound to a compiled node and receipt contains the catalog ID,
resolved root-relative path, `SKILL.md` SHA-256, and the installation tree hash
when a valid install receipt provides one. Without an install receipt the engine
computes a local tree hash and reports the Skill as unlocked. A changed identity
blocks a new attempt until the workflow is revalidated; it is never treated as
the previously compiled Skill.

Any installed Skill may be used as a task node. A Skill without structured
artifact metadata remains selectable, but the Studio reports a risk warning and
uses node-scoped output declarations supplied through GUI controls.

Validator nodes use a separate shipped `validator-registry.v1.json`. Each entry
contains a stable validator ID, script path contained by the installed
Orchestrator, input and outcome schema versions, and content hash. The Studio
cannot turn an arbitrary command or path into a validator node.

### 7.3 Validator and compiler

- Treats edges as the single source of graph dependencies. It does not duplicate
  a second complete dependency list on each node.
- Validates the document schema, identifiers, references, conditions, joins,
  artifacts, Skill availability, and parallel write sets.
- Rejects cycles through deterministic graph validation.
- Produces a canonical execution plan and semantic SHA-256 hash.
- Separates canvas-only changes from behavior-changing changes.

### 7.4 Scheduler

- Computes the current ready set from an immutable compiled plan and run state.
- Activates conditional edges from recorded outcomes only.
- Applies join semantics and downstream failure propagation.
- Prevents nodes with overlapping protected write sets from running in parallel.
- Does not invoke Skills directly. It supplies ready nodes to the Codex
  Orchestrator, which invokes the selected Skill under existing permissions.

### 7.5 Router and Orchestrator

The core Router Skills separation remains intact:

- The custom graph determines which node or nodes are ready.
- The Router resolves and loads exactly one installed primary Skill for a task
  node; it does not re-route that node to an unconfigured substitute.
- The Orchestrator owns global transitions, optional subagent dispatch, receipt
  submission, user decisions, and state updates.
- A downstream Skill returns bounded results and cannot mark another node
  complete or rewrite global workflow state.

### 7.6 Node invocation and result submission protocol

The control plane exposes a small JSON CLI used by the Orchestrator, not by the
downstream Skill:

```text
workflow_manager.py ready --project . --json
workflow_manager.py claim --project . --node NODE_ID --json
workflow_manager.py submit-result --project . --result RESULT_FILE --json
workflow_manager.py record-decision --project . --decision DECISION_FILE --json
workflow_manager.py retry --project . --node NODE_ID --json
```

`claim` atomically verifies that the node is ready, changes it to `running`, and
returns a `node-invocation-v1` envelope containing the run ID, workflow hash,
node ID, attempt, random idempotency token, resolved Skill identity, input
artifacts and hashes, expected outputs, allowed project root, and declared
outcomes.

The Orchestrator invokes that one Skill with the bounded node contract and then
submits a `node-result-v1` envelope. A Skill that does not natively return this
shape is wrapped by the Orchestrator: the wrapper may organize the Skill's real
summary, uncertainties, and produced artifact paths, but may not invent an
artifact, outcome, or completion claim.

`submit-result` verifies the invocation identity, idempotency token, current
attempt, input hashes, result schema, declared outcome, and project-contained
paths. The store computes output hashes itself. An exact duplicate submission is
idempotent; a stale attempt or mismatched token is rejected.

A task succeeds only when the submitted result is valid and every required
output is present and verified. A task with no file output still requires a
structured result summary in its receipt. Task results may select only outcomes
declared by that node. Validator outcomes come only from the registered
validator adapter. Condition outcomes come only from the condition engine.
Project booleans and user decisions can be created only through explicit,
event-recorded manager operations; an arbitrary Skill cannot change them
implicitly.

### 7.7 State and receipt store

- Uses project-root containment, file locks, temporary files, atomic replacement,
  backups, and append-only events.
- Treats structured JSON state as authoritative.
- Generates a Markdown summary for humans and agents, but never reads manual
  summary edits back into machine state.

## 8. Workflow document model

The GUI writes JSON because Python can read it with the standard library and the
user is not expected to edit it. A representative document is:

```json
{
  "schema_version": "paper-workflow-custom-v1",
  "workflow_id": "my-paper-flow",
  "document_revision": 8,
  "semantic_revision": 3,
  "derived_from": null,
  "settings": {
    "max_parallelism": 3
  },
  "nodes": [
    {
      "id": "directions",
      "type": "task",
      "display_name": "Clarify the research direction",
      "entry": true,
      "skill_ref": "clarify-research-idea",
      "inputs": [],
      "outputs": ["idea_brief"],
      "failure_policy": "block"
    }
  ],
  "edges": [],
  "ui": {
    "positions": {
      "directions": {"x": 120, "y": 80}
    }
  }
}
```

For a workflow copied from the Studio projection, `derived_from` instead contains
the projection ID and its generated SHA-256. The schema reference and tests must
also include complete valid examples for every node and edge type.

`document_revision` increments on every saved draft and supports optimistic
concurrency between browser tabs. `semantic_revision` and the compiled graph
hash change only when execution behavior changes. Moving a card on the canvas
therefore does not invalidate receipts or risk acknowledgements.

The canonical semantic hash includes node identifiers and types, Skill or
validator bindings, inputs, outputs, conditions, failure policies, edges, join
settings, and execution settings. It excludes coordinates and other visual-only
state.

## 9. Node and edge semantics

### 9.1 Task node

- Binds exactly one `skill_ref`.
- Declares logical input and output artifacts.
- Uses `block` as the default failure policy.
- May use `skip_branch` only when the user selects that policy explicitly. A
  skipped task deactivates all of its outgoing edges for that run; it does not
  report a successful outcome.
- A manual retry creates a new attempt for the same node and run.

### 9.2 Condition node

A condition node has no Skill. Its predicate is a restricted, serialized
abstract syntax tree built by GUI controls. Supported facts are:

- predecessor status or named outcome;
- a named user decision;
- the existence or verified state of a registered artifact; and
- a registered project boolean.

Every condition has an explicit edge marked `default`. When the node runs, all
edges matching the selected named outcome become active; if none match, the
default edge becomes active. All other outgoing edges become inactive.
Arbitrary source code, shell, templates, and free-form executable expressions
are invalid.

### 9.3 Join node

The first release supports:

- `all_active`: wait for every upstream branch that was actually activated;
  branches excluded by conditions do not keep the join waiting.
- `any_success`: release after the first successful active upstream branch. The
  graph must declare which output mapping the join exposes. Other running
  branches are not force-killed; their later results are recorded as auxiliary
  results and do not replace the chosen join output.

`all_active` is the default because its result is more deterministic.

### 9.4 Validator node

- Binds a known project validator rather than a Skill.
- Produces a named `pass`, `fail`, or `blocked` outcome after the validator
  process itself completes. A validator crash is a node execution failure, not
  a `fail` scientific outcome.
- Remains removable or replaceable in a custom graph.

### 9.5 Edges

Edges are the only dependency source. Each compiled edge has one runtime state:

| Edge state | Meaning |
| --- | --- |
| `waiting` | The source cannot yet determine this edge |
| `satisfied` | The source completed with the edge's required status or outcome |
| `inactive` | A condition selected another branch, or the edge belongs to a disabled node |
| `failed` | The source reached a terminal execution failure that cannot satisfy this edge |

An unconditional task or join edge requires `succeeded`. An edge from a
condition names one declared outcome or `default`. An edge from a validator may
name `pass`, `fail`, or `blocked`. Task execution failures do not activate a
failure edge in v1 of the custom schema; they follow the task's `block` or
`skip_branch` policy.

For an ordinary task, condition, or validator with several incoming edges, the
dependency rule is always `all_active`: wait while any non-inactive edge is
`waiting`; become ready when every non-inactive edge is `satisfied`; become
blocked if any required edge is `failed`. A zero-incoming node is a root only
when it is explicitly marked as an entry node.

A join applies its own rule:

| Join mode | Ready/success rule | Failure rule |
| --- | --- | --- |
| `all_active` | Every non-inactive incoming edge is satisfied | Any required incoming edge fails |
| `any_success` | The first incoming edge becomes satisfied; its declared output mapping is frozen as the winner | All incoming edges become terminal without a satisfied edge |

Late successful results after an `any_success` join are retained as auxiliary
artifacts and cannot replace the frozen winner or its downstream inputs.

Disabled nodes and all their incident edges are excluded from the compiled graph.
The compiler does not invent a bypass edge. If disabling a node leaves another
node without an incoming path and that node is not explicitly marked as an entry,
activation fails until the user reconnects the graph.

Edge references must resolve to existing nodes. A DAG may have several explicit
entry nodes and terminal nodes. Any source type, trigger, status, or join
combination not covered by these rules is activation-blocking rather than left
for the scheduler to guess.

## 10. Compilation and activation

Saving and activation are separate operations:

1. The Studio may save a structurally incomplete draft so the user can continue
   later.
2. Activation invokes backend validation and compilation.
3. Blocking errors prevent activation, not ordinary draft saving.
4. High-risk warnings require explicit acknowledgement bound to the semantic
   hash. They do not force official stages back into the graph.
5. A successful compiler run stores the canonical plan, semantic hash, Skill
   resolution results, risk report, and deterministic topological information.
6. Activating a custom workflow writes an explicit selection record. Saving a
   draft alone never changes the project's active workflow.
7. Deactivation restores the official default without deleting custom data.

Only one workflow run may be active in a project at a time. A new draft can be
edited while a run is active, but a different semantic revision cannot replace
that run silently. The user must finish, stop, or archive the existing run and
start a new one.

## 11. Runtime algorithm

For each custom run:

1. Load the active compiled plan and verify its semantic hash.
2. Load the last valid state snapshot and replay later append-only events.
3. Evaluate condition outcomes from recorded, side-effect-free facts.
4. Mark condition-excluded branches as skipped for this run.
5. Compute nodes whose activated predecessors, join policy, inputs, Skill
   resolution, and write-set constraints are satisfied.
6. Return the ready set in deterministic order. Independent nodes may be
   dispatched to Codex subagents up to the configured and platform limits.
7. Before dispatch, atomically record the node attempt as `running` with its
   input hashes and idempotency key.
8. On return, validate the structured result, output artifacts, hashes, and
   receipt before marking the node `succeeded`.
9. Propagate failure, skip, block, or stale state to dependent nodes according
   to the compiled plan.
10. Append an event, atomically update the snapshot, and regenerate the readable
    summary for every accepted state transition.

The state set is:

```text
pending | ready | running | succeeded | failed | blocked | skipped | stale
```

There is no automatic unbounded retry. A retry is a deliberate new attempt.
Changing nodes, Skill bindings, edges, conditions, or semantic settings creates
a new semantic revision rather than a feedback edge.

## 12. Project storage

```text
.research/custom-workflow/
  workflow.json                    # latest GUI document
  revisions/                       # immutable semantic revision snapshots
  selection.json                   # official or explicit custom selection
  state.json                       # authoritative active-run snapshot
  events.jsonl                     # append-only transitions and decisions
  artifacts.json                   # logical artifact registry and hashes
  summary.md                       # generated readable projection
  receipts/
    {run-id}/
      {node-id}-attempt-{number}.json
```

The selection file is absent or selects `official` until the advanced user
explicitly activates a custom workflow. Each Orchestrator entry announces the
selected workflow name, semantic revision, and hash.

A node receipt binds at least:

- receipt schema version;
- workflow ID, semantic revision, and semantic hash;
- run ID, node ID, and attempt number;
- resolved Skill or validator identity;
- input artifact identifiers and hashes;
- output artifact identifiers and hashes;
- status or named outcome;
- start and completion timestamps; and
- structured error or uncertainty data when present.

## 13. Crash recovery and stale propagation

Every event contains a monotonically increasing `event_seq`, run ID, semantic
workflow hash, `previous_event_hash`, and its own hash over canonical event
content. The state snapshot stores `last_applied_event_seq` and
`last_applied_event_hash`.

An event line is appended and flushed before the corresponding snapshot is
atomically replaced. On restart, recovery verifies the snapshot boundary and
replays only a contiguous, hash-linked suffix. An exact duplicate event is an
idempotent no-op. A sequence gap, conflicting duplicate, hash mismatch, or
invalid non-tail line blocks the run without rewriting evidence. A truncated
final JSONL line is first preserved in a timestamped recovery copy; recovery may
return to the last verified event, but any possibly affected running node is
marked `blocked` and the incident is shown to the user.

An interrupted `running` node is never guessed to be successful:

1. Verify whether a complete receipt exists.
2. Verify the workflow hash, attempt, inputs, outputs, and artifact hashes.
3. Restore `succeeded` only when all completion evidence agrees.
4. Otherwise mark the node `blocked` and offer a deliberate retry.

When a semantic revision changes a node or an upstream artifact, affected nodes
and their descendants become `stale`. Unaffected upstream receipts may be
referenced by hash. A new graph never silently continues an old run state.

## 14. Studio interaction design

The approved desktop layout contains:

- a top bar with project, current mode, clone, validate, and save actions;
- a searchable left node library;
- a central draggable graph canvas;
- a right properties panel for the selected node; and
- a persistent validation and risk area.

Typical flow:

1. From the research project, run `python3 scripts/workflow_studio.py --project .`.
2. The browser opens the official v1.0 Studio projection in read-only mode.
3. Choose **Copy as custom workflow** or create a blank custom workflow.
4. Add task, condition, join, or validator nodes.
5. Select an installed Skill for each task node and connect edges.
6. Receive immediate advisory feedback while editing.
7. Save the draft.
8. Choose **Validate and activate** for authoritative backend validation.
9. Review and acknowledge any high-risk differences.
10. Start or resume the custom workflow through the Orchestrator.

Keyboard-accessible node operations and a textual graph outline are required;
drag-and-drop is not the only editing path.

## 15. Frontend and packaging

The Studio frontend uses React, TypeScript, Vite, and `@xyflow/react`. React Flow
provides the node, edge, viewport, selection, and keyboard foundations needed by
the approved editor layout. See the official documentation at
<https://reactflow.dev/>.

- Node.js is a development and build dependency only.
- Dependency versions and integrity data are locked.
- The repository stores frontend source plus a reproducible prebuilt bundle.
- End users launch the bundle through Python 3.10+ standard-library code.
- The runtime bundle is offline and contains no CDN, telemetry, or remote font
  request.
- A dependency-license inventory accompanies the bundle.
- The Studio bundle is installed with the Orchestrator in every install profile;
  the set of selectable Skills naturally reflects what that profile installed.

## 16. Local HTTP security

`workflow_studio.py` starts a narrow local service that:

- binds only to `127.0.0.1` on a random available port;
- creates a high-entropy per-session token;
- performs same-origin and CSRF checks on state-changing requests;
- applies a CSP that disallows remote executable resources;
- limits request body sizes and accepted content types;
- exposes only catalog, projection, load, validate, compile, save, activate, and
  deactivate operations;
- provides no arbitrary file browser, command runner, network downloader, or
  Skill installer;
- resolves and validates every project-relative path against the selected
  project root, including symlink and traversal checks;
- uses optimistic concurrency so an old browser tab cannot overwrite a newer
  document revision; and
- stops on explicit shutdown or an idle timeout.

Frontend checks improve interaction speed, but Python validation is authoritative
for every activation and state-changing operation.

## 17. Validation, warnings, and runtime errors

### 17.1 Activation-blocking errors

- unsupported or malformed schema;
- duplicate node or edge IDs;
- missing node references or self-dependencies;
- a cycle of any length;
- invalid or non-exhaustive conditions;
- an ambiguous join waiting set or output mapping;
- a missing or unrecognizable Skill;
- an input artifact with no valid source;
- unsafe parallel overlap in declared or protected write sets;
- a workflow/state semantic-hash mismatch; or
- a required engine or filesystem security invariant that cannot be satisfied.

### 17.2 Non-blocking workflow-risk findings

- removal or replacement of official integrity, citation, experiment-contract,
  figure, final-edit, or final-audit controls;
- a Skill with unknown structured inputs or outputs;
- a delivery path without a validator;
- an `any_success` join that ignores later branch results;
- use of an unlocked or changed local Skill identity; or
- a graph that cannot be projected meaningfully to a single v1 stage.

High-risk findings require explicit acknowledgement for the current semantic
hash. After acknowledgement, they do not prevent custom activation.

### 17.3 Runtime failures

A failed Skill, missing receipt, changed artifact hash, validator failure, or
filesystem error records the current node as `failed` or `blocked`. Downstream
nodes do not start. The engine never substitutes another Skill, invents a
result, or treats file existence alone as proof of completion.

Every error shown in the Studio or CLI includes the node, stable error code,
human explanation, affected operation, recovery guidance, and whether any file
was written.

## 18. v1.0 compatibility contract

1. No custom selection means the existing official path is used exactly as
   before.
2. Existing `.research/progress.md`, `.bak`, `.legacy-*`, install receipts, and
   validator receipts remain readable and are not automatically rewritten.
3. The existing `progress_manager.py migrate` behavior remains available.
4. The v1 `workflow_version` and normalized official stage identifiers are not
   changed merely because the project release becomes v1.1.0.
5. Custom state is stored under `.research/custom-workflow/` and never becomes
   an implicit extension of the v1 Markdown schema.
6. A custom workflow may reference an old artifact or receipt by path and
   SHA-256 without reissuing it as a new v1 receipt.
7. The Studio projection is tested for source-file hashes, stage/gate mapping
   completeness, and explicit feedback-loop projection notes.
8. All existing tests pass without relying on a custom workflow file.

## 19. Documentation and release positioning

The feature is presented as an advanced capability, not a second main product
story.

- `README.md`: add one **Advanced: Build Your Own Workflow** section.
- `README.zh-CN.md`: add the matching **高级用法：自由编排工作流** section.
- Each section uses a real implementation screenshot and a short
  launch-copy-edit-validate-activate sequence.
- Both READMEs state that the official workflow remains the default and that no
  JSON editing is required.
- `DEVELOPMENT_GUIDE.md` explains the frontend build, Python components, tests,
  and bundle verification for human contributors. It must not read as a prompt
  or instruction set for a future agent.
- Runtime and development dependencies are labeled separately so the existing
  Python-standard-library runtime claim remains precise.
- The project release becomes v1.1.0; the official workflow identity and legacy progress
  schema retain their v1.0 identities.
- Publishing to GitHub requires a final user review and separate approval. No
  GitHub Release is created automatically.

## 20. Test strategy

### 20.1 Existing regression suite

- Keep all current 51 tests passing.
- Preserve legacy progress and receipt fixtures byte-for-byte unless a fixture
  change is explicitly required and reviewed.
- Add source-hash and mapping-completeness tests for the official v1.0 Studio
  projection.

### 20.2 Graph and compiler tests

- valid sequential, multi-entry, branch, parallel, and join graphs;
- self-loop, two-node loop, and arbitrary-depth cycle rejection;
- dangling and duplicate identifiers;
- condition exhaustiveness and restricted-AST rejection;
- deterministic compilation and semantic hashing;
- visual-only changes leaving the semantic hash unchanged;
- Skill-root priority, symlink containment, duplicate identity, missing Skill,
  changed tree hash, and catalog refresh;
- validator-registry identity and arbitrary-command rejection;
- ordinary multi-input `all_active`, disabled-node edge removal, explicit entry
  nodes, and every legal source/trigger combination;
- output-source and parallel write-conflict validation; and
- risk differences that warn without forcing official stages back in.

### 20.3 Scheduler and state tests

- ready-set calculation;
- `all_active` with condition-skipped branches;
- `any_success` output selection and auxiliary late results;
- failure, skip, block, manual retry, and stale propagation;
- invocation claim tokens, stale attempts, structured wrappers, and idempotent
  result submission;
- one active run per project;
- official/custom selection precedence and corrupt-selection failure closing;
- append-before-snapshot recovery;
- event sequence and hash-chain replay, gaps, conflicting duplicates, and a
  truncated final line;
- interrupted running-node receipt reconciliation;
- corrupted events, state, receipts, and artifact hashes; and
- idempotent handling of duplicate result submission.

### 20.4 Studio and security tests

- component tests for palette, canvas, inspector, validation, and mode changes;
- browser end-to-end tests for clone, edit, connect, save, reopen, validate, and
  activate;
- keyboard editing and accessible labels;
- optimistic concurrency between two tabs;
- invalid token, Origin, CSRF, content type, and oversized requests;
- path traversal, symlink escape, and cross-project access rejection;
- CSP and a network-denied offline run; and
- clean shutdown and idle timeout.

### 20.5 Installation and usability tests

- build the frontend reproducibly from locked dependencies;
- verify the committed bundle matches a clean build;
- install each existing profile and confirm Studio assets are present;
- launch Studio from a clean directory with Python 3.10+ and no Node runtime;
- verify Chrome and Safari on macOS, with portable Python tests on Linux and
  Windows where platform behavior differs; and
- confirm that default official use neither creates nor reads custom state.

## 21. Acceptance criteria

The feature is complete only when all of the following are true:

1. A new user can continue using the official workflow with no new step and no
   behavior regression.
2. An advanced user can launch a local browser Studio with one documented
   command and never edit JSON manually.
3. The user can graphically add, remove, replace, disable, and reorder task
   stages and can construct sequential, conditional, parallel, and join flows.
4. Every task node resolves exactly one installed primary Skill.
5. A valid saved custom graph produces a real ready set and runtime state; the
   GUI is not merely a diagram exporter.
6. Cycles and unsafe runtime structures cannot be activated.
7. Removing official paper controls produces a recorded warning but remains
   possible after explicit acknowledgement.
8. Custom state survives restart, detects interrupted or stale evidence, and
   never guesses completion.
9. The Studio runs offline for end users without Node.js.
10. Existing tests, new Python tests, frontend tests, security tests, clean
    installation, and offline browser tests all pass.

## 22. Release checklist

Before asking for publishing approval:

- independent subagent reviews cover code quality, security, compatibility,
  README readability, installation, and downloadable repository integrity;
- a clean clone or archive can install every documented profile, launch the
  Studio offline, and execute the test suite;
- generated frontend assets match a clean locked build;
- the user reviews the implemented UI and bilingual README sections; and
- no GitHub Release is created. A remote push remains a separate user-approved
  action.

## 23. Implementation sequencing constraint

Implementation must proceed in compatibility-first slices:

1. Lock the current baseline and add official-source hashes plus projection
   mapping tests.
2. Add the data model, validator, compiler, and risk report without executing a
   custom workflow.
3. Add isolated store, receipts, scheduler, and CLI with fake-node tests.
4. Add condition, parallel, join, retry, recovery, and stale semantics.
5. Add the Studio frontend and local server against the tested backend.
6. Add Orchestrator integration behind explicit custom activation.
7. Add bilingual README and contributor-guide updates.
8. Run independent review and full release verification before asking for
   publishing approval.

Each slice must keep the official default tests green. No slice may make custom
state an implicit input to the official workflow.
