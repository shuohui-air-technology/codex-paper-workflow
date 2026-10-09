# Confirmed Artifact Lifecycle

## Contents

- [Authorities and directories](#authorities-and-directories)
- [Resume and resolve](#resume-and-resolve)
- [Confirm an accepted stage output](#confirm-an-accepted-stage-output)
- [Official and custom evidence](#official-and-custom-evidence)
- [Failures, withdrawal, and recovery](#failures-withdrawal-and-recovery)

## Authorities and directories

For working candidate registration, content differences and batch decisions,
use [material management](materials.md). It reuses this catalog as the single
adoption authority. Input-role adoption records supplied material selection;
output-role adoption retains the checked-stage provenance below. The combined
`material_manager.py resume` resolves mode before progress and returns verified
bindings rather than guessing a version from dates, filenames or generated views.

Keep three separate facts:

| Fact | Authority |
|---|---|
| Current official research stage, next action, active rules | Validated `.research/progress.md` |
| Execution state and frozen inputs of an activated custom run | Validated custom state, events, and receipts |
| Latest user-confirmed version of a logical artifact role | `.research/confirmed-artifacts/catalog.json` |

Confirmation records adoption of a checked output. Continue to apply the
selected workflow's existing scientific checks, integrity gates, and user
decisions before confirmation. Neither a file's existence nor a successful
copy establishes scientific validity.

```text
project/
├── work/                         # illustrative working location; keep existing project paths
├── artifacts/
│   ├── INDEX.md                  # generated readable view of current confirmations
│   └── current/<type>/<role>/    # generated copies of the latest confirmed bundle
└── .research/confirmed-artifacts/
    ├── catalog.json              # single durable confirmation commit point
    ├── versions/<role>/<version>/ # immutable snapshots, including older versions
    └── projection-backups/        # retained display generations and conflict-repair backups
```

Use a stable logical role such as `main-manuscript`, `literature-matrix`, or
`figure-1`; use an explicit type such as `manuscripts`, `literature`, or
`figures`. A revised version retains its role. Distinct language editions or
different figures use distinct roles. Record originating workflow, run, task,
attempt, and evidence in provenance; reusing a role across runs requires a new
explicit confirmation against the current catalog revision.

Preserve working source files. Existing task receipts, figure checks, and
final-edit records may bind their original paths. Keep snapshot paths stable;
new approvals create new versions and change the catalog's current selection.
The generated `current` tree is for convenient viewing and delivery, while
machine consumers resolve the catalog to verified immutable snapshots.

## Resume and resolve

First resolve official versus custom mode through the workflow manager. Then
read the mode's compact progress summary and the confirmation catalog summary.
Use `artifact_manager.py status --project <root> --json` when the exact current
roles or catalog revision are needed. Metadata-only status explicitly leaves
file-content verification unchecked; `status --verify` checks current files.

Resolve only the roles needed for the next task:

```text
python <skill-root>/scripts/artifact_manager.py resolve --project <root> --artifact-id main-manuscript --expect-revision <catalog-revision> --json
```

Use the returned version, snapshot path, and file list in the context pack.
For an editable revision, create a working copy and leave the confirmed
snapshot intact. A revision conflict requires rereading the current catalog.
A missing, withdrawn, or invalid confirmation requires a user decision or
repair; do not silently choose an older snapshot or a filename containing
`final`, `latest`, or a more recent timestamp.

Custom claim inputs remain bound to their declared paths and hashes, including
claims created after a confirmation. Use the claim's inputs during execution.
To use a confirmed snapshot as an input to a new custom run, declare and
register it with the existing `register-artifact` protocol. Confirmation alone
does not rewrite graph dependencies or a run's artifact registry.

## Confirm an accepted stage output

After actual stage checks and user adoption, construct one project-relative
request file. Keep its `operation_id` stable when retrying the same operation.
Create a new operation ID for a new approval. Obtain hashes from actual files,
the catalog revision from `status`, and the official progress digest from
`progress_manager.py summary`.

Example for official mode (replace every placeholder with verified values):

```json
{
  "schema_version": "confirmed-artifact-request-v1",
  "operation_id": "confirm-main-manuscript-v1",
  "artifact_id": "main-manuscript",
  "artifact_type": "manuscripts",
  "expected_catalog_revision": 0,
  "entrypoint": "paper.md",
  "files": [
    {
      "source_path": "work/paper.md",
      "relative_path": "paper.md",
      "sha256": "<actual SHA-256>"
    }
  ],
  "confirmation": "<the user's actual adoption decision>",
  "expected_progress_sha256": "<digest from the current official summary>",
  "evidence": [
    {"path": ".research/stage_receipts/draft-check.json", "sha256": "<actual evidence SHA-256>"}
  ]
}
```

List every file required for a multi-file deliverable. Preserve relative layout
with `relative_path`, for example `paper.tex`, `figures/figure1.pdf`, and
`references.bib`. Set `entrypoint` to a listed file. Check dependencies before
declaring the bundle complete; the tool copies only the explicit list and does
not discover remote assets or rewrite embedded paths.

```text
python <skill-root>/scripts/artifact_manager.py accept --project <root> --request <relative-request.json> --confirm --json
```

This one action records adoption, stores the immutable bundle, and refreshes
the corresponding current folder and index. Keep regular working changes out
of this operation until their next explicit acceptance.

## Official and custom evidence

In official mode, the manager holds the workflow-mode lock and progress lock,
validates the progress document, and checks the requested progress digest and
evidence hashes. The caller must actually run and interpret the relevant stage
validators. Active blocking validity rules prevent new official confirmations.
Evidence hashing confirms the supplied evidence's identity; it
does not interpret every possible scientific report format.

In custom mode, omit `expected_progress_sha256`. Add `source_artifact_id` to
each file, naming its exact registered output in the active run. The manager
requires a verified artifact from a currently successful task attempt and a
committed receipt binding that output's path and hash. A working file without
such a receipt remains an unconfirmed working output. Additional verification
evidence may be supplied in `evidence`.

Confirmation uses the lock order workflow mode/run → official progress when
applicable → confirmation catalog. The catalog revision protects against
another confirmation replacing the one the caller inspected. Confirmation and
stage execution remain separate commits with separate meanings; do not report
either one as evidence that the other succeeded.

## Failures, withdrawal, and recovery

Check `committed` and `projection_pending`, not only process exit status.
If the catalog commit succeeded but display generation failed, the confirmation
is durable. Retry the same operation ID to inspect the original commit without
creating a second approval; use `repair` to rebuild the display. The idempotent
acceptance branch does not repair display files. Different content under the
same operation ID is rejected.

Replacing or withdrawing display files first retains their old file instances
under `projection-backups`; publishing uses exclusive creation rather than
overwriting a newly saved file. Report any returned `projection_backups` or
repair `backups` locations. Preserve these backups: an editor with an already
open file can still save changes into the retained file instance. An interrupted
display update may need repair; the verified snapshot remains the read source.

Changed working source bytes do not invalidate an intact confirmed snapshot.
Changed snapshot bytes do invalidate that confirmation's readable evidence.
Custom execution may independently detect drift in its original input paths;
handle that through custom recovery and rerun rules.

Use `repair --project <root> --expected-revision <revision> --confirm` to rebuild
missing, unmodified display files. If a user edited the generated current tree,
preserve and inspect those changes first. Only with explicit authorization use
`--backup-conflicts` to preserve conflicting files before rebuilding. Report
the returned backup location.

Use `withdraw --project <root> --artifact-id <role> --expected-revision <revision>
--operation-id <unique-id> --reason <reason> --confirm` when the user withdraws
an adoption. Withdrawal retains the history and clears the effective current
version. An older version becomes current again only through a new explicit
confirmation.
