# Material roles and difference-based adoption

## Purpose and authority

`scripts/material_manager.py` connects working candidates to the existing confirmed
artifact catalog. A **role** describes a stable purpose (`main-manuscript`,
`figure-2`); a **candidate** names one explicitly declared bundle for that role.
Different filenames, copies and dates do not define which content is adopted.

| Record | What it establishes |
|---|---|
| `.research/materials/registry.json` | Working role/candidate/file associations |
| `.research/materials/last-scan.json` | Observed hashes and discovery scopes at the last saved scan |
| `.research/materials/INDEX.md` | Generated reading view, not an adoption authority |
| `.research/materials/reviews/*.json` | A proposed decision bound to candidate bytes and catalog/registry revisions |
| `.research/confirmed-artifacts/catalog.json` | The single authority for adopted versions |

`progress.md` continues to record official progress; custom state continues to
record execution and frozen inputs. Registration, scans and adoption do not
advance progress or manufacture successful stage receipts. Existing confirmed
roles appear automatically without migration or a new approval.

## Register a working candidate

Explain the proposed role, kind and file bundle in the conversation first. The
agent prepares project-relative JSON (for example `requests/register.json`):

```json
{
  "schema_version": "material-role-v1",
  "expected_registry_revision": 0,
  "role_id": "source-notes",
  "candidate_id": "working",
  "kind": "input",
  "artifact_type": "materials",
  "entrypoint": "notes.md",
  "files": [
    {"source_path": "inputs/notes.md", "relative_path": "notes.md"}
  ]
}
```

```bash
python3 <orchestrator-root>/scripts/material_manager.py register \
  --project <project-root> --request requests/register.json --json
```

Use the current `registry_revision` from `review` or `resume` as the expected
revision. Re-registering identical paths, layout and bytes is idempotent. A new
candidate uses a different `candidate_id`; changing one candidate updates that
candidate, retaining other candidates. Roles have stable kind and artifact type.

- `input` is supplied research material and must use type `materials`. Adopting
  it records the user's material selection, not a verified research result.
- `output` is a checked stage deliverable. It retains the official progress and
  verification-evidence requirements, or the custom run's committed receipt and
  exact registered-output bindings. It cannot be downgraded to an input role.
- A multi-file bundle preserves the explicit `relative_path` layout, with the
  entrypoint included in the file list. No undeclared dependency is collected.
- Custom outputs also supply `source_artifact_id` for each file, as required by
  [confirmed-artifact provenance](confirmed-artifacts.md).

Working paths must be ordinary files inside the project. Traversal, linked
paths, special files and generated confirmation/material/runtime directories
are rejected. Original working files stay in place.

## Scan, review and resume

```bash
# Refresh the inventory intentionally; persist discovery scope and reading view.
python3 <orchestrator-root>/scripts/material_manager.py scan \
  --project <project-root> --scope manuscripts --scope figures --json

# Read-only full differences; save a review only when proposing a decision.
python3 <orchestrator-root>/scripts/material_manager.py review \
  --project <project-root> \
  --output .research/materials/reviews/decision.json --json

# Daily resume: compact progress, differences and verified adopted bindings.
python3 <orchestrator-root>/scripts/material_manager.py resume \
  --project <project-root> --role main-manuscript --role figure-2 --json
```

Without `--scope`, later reviews/resumes reuse the last saved scan's scopes; a
project without a scan starts with `.`. Every registered candidate is still
checked, including ones outside the discovery scope. Set explicit scopes to
avoid repeatedly walking large unrelated directories. `resume` and unsaved
`review` are read-only, including for a project with no metadata yet.

The manager hashes actual file contents rather than trusting dates or sizes.
Adopted equality includes content hashes, bundle-relative layout and entrypoint;
changing only a working source location does not require another approval.

| Candidate state | Handling |
|---|---|
| `unchanged` | Reuse the current confirmation, with no extra version or approval |
| `new` / `changed` | Show changed files; ask to select and adopt if needed |
| `historical` | Keep as an alternative without prompting repeatedly; explicit adoption can restore it |
| `unavailable` | Report missing, unsafe or ambiguous sources; do not guess |

An exact, unique same-content location can resolve a missing working file. Two
matching locations are reported as ambiguous. Identical candidates need only one
selection; distinct new contents require choosing a candidate, not “the newest
filename”. Added, modified, missing, moved, duplicate and unregistered files are
discovery hints, never automatic role assignments or approvals.

A saved `scan` also records a uniquely verified candidate relocation in the
working registry, without changing its adopted version. Later edits at the new
location remain the same candidate. Read-only `resume`/`review` do not persist
relocations: save a scan before continuing to edit moved working material.

Discovery excludes generated snapshots, runtime metadata, dependency/cache
directories and progress projections. It bounds discovery to 10,000 files, 2 GiB
of observed content and 40,000 visited entries; individual files also obey the
existing 512 MiB limit. Errors or limits set `discovery.complete: false`. Missing
file conclusions are suppressed when a baseline is incomplete or scopes change.
Narrow the scope and scan again rather than treating such a result as exhaustive.

Text previews are advisory UTF-8 diffs for eligible files up to 128 KiB, bounded
to 80 lines per file and 20,000 characters per candidate. Resume further limits
preview and discovery lists. Binary and large files retain hash/file differences;
use the relevant visual/data review when judging their meaning.

`ready_to_read` means the requested adopted snapshot bindings can be verified.
It is not a statement that the workflow is ready to execute or that the research
is valid. Unrelated candidate changes do not block reading an intact adopted role.
Required missing/withdrawn/damaged snapshots are reported in `issues`.

## Adopt only the selected differences

Show the saved review's selected candidate, changed file list and checks. After
the user explicitly agrees, submit one or more role/candidate selections:

```bash
python3 <orchestrator-root>/scripts/material_manager.py accept \
  --project <project-root> \
  --review .research/materials/reviews/decision.json \
  --select main-manuscript=working --select figure-2=revised \
  --decision "采用本次审阅的主稿与图 2，作为接下来使用的版本。" \
  --confirm --evidence requests/verification-evidence.json --json
```

`--evidence` is needed for official output adoption, not input-only adoption.
Its file contains `{"evidence": [{"path": "checks/result.json", "sha256": "..."}]}`;
the actual bounded check records must satisfy the existing provenance contract.
Official outputs also bind the progress digest captured by the review. Custom
outputs require verified successful receipts; a candidate registration cannot
substitute for their output artifact IDs or validation.

The manager rechecks selected bytes, layout, registry/catalog revisions and
output workflow binding before preparing snapshots. Changed selected files,
stale checks or selection revisions require a refreshed review and decision.
All changed selections prepare first, then commit their pointers together in one
catalog write. Failure before that write adopts none of the batch. Unreferenced
prepared snapshots may remain for diagnosis; they are not current versions.
Unchanged selections create no snapshots and require no new decision.

Retry the same saved review, selections, decision and evidence after an uncertain
response. The deterministic operation IDs return a fully committed batch without
creating extra versions; altered retry data is rejected. Snapshot/current-view
failures are reported separately from the durable commit. The material reading
view is refreshed after a new commit; if `material_index_pending` is reported,
use `scan` to rebuild it. An idempotent retry does not repair views.

Do not rewrite `.research/progress.md` to maintain material versions. Update
progress separately through its digest-checked manager when the next action changes.

## Feed a confirmed version into a custom run

```bash
python3 <orchestrator-root>/scripts/workflow_manager.py register-confirmed-input \
  --project <project-root> --artifact-id supplied-notes --role source-notes \
  --expect-catalog-revision <revision> --json
```

The artifact ID must be an external input declared in the selected plan. The
entrypoint is used by default; `--file <bundle-relative-path>` selects another
declared bundle file. Snapshot resolution verifies the complete bundle, and
registration checks the selected hash again inside the run transaction.

Later adoption does not silently change an already registered run input.
`resume.run_input_differences` lists frozen and current versions with
`retain_frozen_run_input`. Follow the explicit custom input-update/invalidation
procedure or start a new run when choosing to use the newly adopted content.
Claims, result sources and approval gates retain their existing contracts.
