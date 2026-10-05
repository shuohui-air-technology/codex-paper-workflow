# Fixed-node figure implementation

Use the activated node's Skill identity, project root and frozen inputs. Record
the chosen Python/R backend and named panel scope in the shared design notes.
An established backend requires no new selection question. For planning alone,
continue planning without a backend gate. Missing selected runtime/packages
block rendering; report the missing component without cross-rendering.

Read the original source files directly from the declared paths. Preserve
transformations, missingness, units, comparisons and uncertainty definitions.
Write the plotting code, requested vector/raster exports, a separate preview,
and a figure-receipt specification listing the actual files and scientific
fields. The orchestrator's `build_figure_receipt.py` calculates hashes; it does
not invent statistics, claims or human review. A pending human review stays
pending until the configured review node receives the user's answer.

List every project file actually read in `consumed_sources`, including design
notes, datasets, helper modules and local reference images. They must match
the frozen claim's logical ID, relative path and hash. Declare extra inputs
and requeue instead of hiding reads. Keep outputs separate from claimed inputs;
a revision writes a new working version, preserving the prior receipts.

The following reference-review node checks these same actual exports. Reuse
existing QA results and one working record; avoid a second contract or parallel
`scientific-visualization` pass. For mixed figures, identify exactly which
quantitative panels this Skill created; GIS/vector assembly belongs to its own
stage. Do not label the assembled mixed figure backend-exclusive.
