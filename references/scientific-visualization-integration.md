# Scientific Visualization Integration

This contract connects the optional `scientific-visualization` downstream skill to
the paper workflow without loading it for unrelated research tasks.

## When to route

Route exactly one primary downstream skill, `scientific-visualization`, when the
frozen figure plan contains a claim-bearing data plot, statistical panel, image
measurement, or journal export/audit request. Keep it dormant for idea work,
literature, prose-only drafting, and papers with no evidence-bearing figures.
Conceptual illustrations and ML architecture diagrams require an explicitly
selected specialist; they must not be presented as data-derived results.

## Figure contract

Each figure is a directory under `.research/figures/<figure_id>/` containing a
`figure_receipt.json`. The receipt is valid only when it binds:

- `figure_id`, `figure_kind`, non-empty `claim_refs`, non-empty `source_refs`, and
  local `source_files` with SHA-256 hashes;
- the generation code path and SHA-256 hash;
- explicit transformations, uncertainty definition, and missing-data policy;
- target venue, submission phase, and physical width in millimetres;
- at least one vector output (`pdf` or `svg`) for `data_plot` figures, or a
  publication-grade raster output (`png`/`tiff`) for `image_panel` figures;
- an independent raster preview (`png`/`tiff`) with pixel dimensions, DPI, and
  a matching physical width (the preview must not be the output file itself);
- output paths and SHA-256 hashes, with paths contained by the project root;
- descriptive alt text and a human visual-review receipt with reviewer identity,
  ISO-8601 timestamp, checklist, findings, and dispositions;
- hashes for the frozen `figure_plan.yml` and `claim_evidence_matrix.yml`, plus
  explicit claim-to-source bindings that cover every declared claim and source;
- `validation_status: pass`.

The receipt records provenance; it does not certify the scientific claim. The
integrity stage must still compare the figure against the claim/evidence ledger.

The two frozen YAML/JSON inputs are relational contracts, not ID inventories.
`figure_plan.yml` must expose a `figures` (or `items`) list with one unique entry
whose `id`/`figure_id`, `claims`/`claim_refs`, and `sources`/`source_refs` exactly
match the receipt. `claim_evidence_matrix.yml` must expose claim rows such as
`{claim_id: C001, source_refs: [S001]}` (the equivalent `sources` or
`evidence_refs` keys are accepted). For every receipt claim, the matrix relation
must exactly equal the receipt's `claim_bindings`; a mere occurrence of an ID
elsewhere in either file is insufficient. Duplicate claim rows or duplicate
receipt bindings are blocked.

## Execution and gates

1. Freeze `figure_plan.yml` after the outline gate and map every headline panel
   to a claim and source artifact.
2. Dispatch figure agents only after data, terminology, palette, units, target
   size, and panel numbering are frozen. Agents return `progress_delta` and
   figure artifacts; they never edit `progress.md` directly.
3. Load `scientific-visualization` only for the figure task. Preserve raw data,
   transformations, exclusions, seeds, uncertainty, and missing observations.
4. Render at the intended physical size, inspect the preview, fix defects, and
   write the receipt. Use the bundled validator before the figure enters prose.
5. A failed receipt or a claim/evidence mismatch blocks the Results, caption,
   abstract, Conclusion, review, and finalization gates until remediated.

## Blocking findings

Treat deceptive axes, omitted negative findings, unsupported smoothing,
untraceable transformations, mismatched uncertainty, unexplained exclusions,
missing source data, hash drift, or output clipping that changes interpretation
as `critical_validity_blocker`. Cosmetic typography or spacing defects are
non-blocking only when the scientific meaning remains unchanged and a repair is
recorded. Visual-review findings use unique IDs and one-to-one dispositions;
every major or critical finding must be marked `resolved` with a reason and
evidence before the receipt can pass.

## Context and provenance boundary

Install only the `scientific-visualization` subdirectory from the upstream
repository; do not install the entire K-Dense collection. The orchestrator loads
the integration reference and a minimal figure context pack, not the complete
upstream skill, until a figure task is selected. The upstream skill's Python
3.11+ / `uv` requirements and journal guidance remain runtime prerequisites and
must be reported rather than silently installed by the core workflow.
