# Reference-led figure subflow

Use this optional route for requested new figures or substantive redesigns.
The reference/design Skill and the Python/R implementation Skill are separately
installed in `standard` and `full`. Select one implementation per figure:
`nature-figure` for its applicable Python/R track, or `scientific-visualization`
for the general route. A small correction keeps the established tool and skips
reference discovery. The default Official v1.0 projection is unchanged.

## Roles and actual handoff

| Task | Primary | Inputs | Outputs |
|---|---|---|---|
| Reference design | `reference-first-figures` | source data, plan, claim ledger, inspected local reference and notes | design/working notes |
| Data-panel implementation | `nature-figure` | original inputs + design, chosen backend | code, vector figure, independent preview, receipt specification |
| Visual comparison | `reference-first-figures` in bounded read-only review | same design, code, actual exports, specification, recorded human answer | versioned review notes and figure receipt |
| Evidence validation | manager's `figure-contract` adapter | figure receipt, with its dependencies still present | validator stage receipt; no generated files |

The same working record passes through design and review as successive versions;
no stage edits a claimed input in place. Preserve established backend, layout,
palette, scientific content and requested formats. Reference review findings
requiring new drawing return to the implementation node; do not run another
Skill inside its fixed claim. Existing suitable references are reusable.

The implementation declaration covers data, design, local reference materials
and any helper modules actually read. `node-result-v2` checks every reported
logical ID/path/hash against the frozen claim. If reference discovery downloads
a PDF/image to the project for later reading, declare/register it before a new
claim; do not omit it from `consumed_sources`. Remote pages inspected through
retrieval tools are recorded by URL, figure/page and viewing notes; the local
protocol does not attest to unreported reads or remote content history.

## Confirmation and repair

Show the actual exported figure and AI findings to the user. In the supplied
example, a confirmation gate binds the implementation attempt, source inputs,
code, export, preview and specification. Register the actual human review as
`human_review`, then record `approve` or `revise` through `record-approval`.
Human review names the person who inspected the figure, time, checks, findings
and dispositions; an AI is not a human reviewer. If this evidence is pending,
report a candidate and keep validation pending.

New major or scientific-meaning defects found in the later AI comparison keep
that task blocked. Record every finding and disposition, reopen implementation
and obtain review for the revised version. The earlier human answer covers the
version actually inspected; it does not dispose of newly discovered defects.

On `revise`, preserve prior receipts and explicitly `rerun-stale --node
figure-implementation`. Recreate changed outputs and the human review; the old
approval cannot release the new version. A running downstream node prevents
reopening. Changes to scientific purpose or reference design require reopening
the design stage as well, or enabling a new workflow version after review.

## Receipt adapter

`build_figure_receipt.py` accepts a JSON specification with the existing
`figure-receipt-v1` fields. For `source_files`, `code`, `figure_plan`,
`claim_evidence_matrix`, `outputs` and `preview`, supply the real relative file
paths; SHA-256 fields may be omitted. Supplied hashes are treated as expected
values and must match. Other fields use the existing
[figure contract](scientific-visualization-integration.md#figure-contract).
Visual review, claim bindings, transformations, uncertainty, missingness,
physical width and preview metadata must be supplied from actual evidence.

```bash
python3 <orchestrator-root>/scripts/build_figure_receipt.py \
  --project-root <project-root> --spec .research/figures/F001/receipt-spec.json
```

The command reads, hashes and validates files, returning a candidate under
`receipt` plus its canonical `receipt_path`. Codex writes that returned object
to the path, declares it as a node output, and submits the task result. The next
validator node runs `workflow_manager.py run-validator --node figure-check`.
The builder never invents review evidence, produces an image, advances a stage,
or silently replaces a receipt. A file change before the validator runs blocks
acceptance. It uses standard-library hashing even for R output, without creating
any Python image or violating the selected rendering backend.

## Native and mixed figures

For GIS and native-vector diagrams, use the current editor, source coordinates,
editable structure, rendered preview and real visual inspection. The existing
data/image receipt is not a conceptual-diagram certification. Do not change a
schematic's type to `data_plot` just to pass the validator.

A mixed figure may use this subflow for named quantitative panels. Their source,
code, output and QA bindings cover those panels only. A separately recorded
native-tool assembly checks the full composite and retains source files; it
does not claim whole-figure Python/R exclusivity. Keep valid map projections
and diagram connections while reusing typography and colour semantics.

## Example and installation

[reference-led-figure.custom.json](workflows/reference-led-figure.custom.json)
is a local figure workflow example for already acquired references. It is not
an extra Official v1.0 stage or a promise to complete a paper. See the
[Studio guide](../docs/workflow-studio-guide.md#参考优先绘图流程) for loading it,
mapping real inputs, waiting for review and submitting results. Install both
bundled Skills from the repository instead of using maintenance-machine paths.
