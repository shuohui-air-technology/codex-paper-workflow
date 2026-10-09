# Project-owned figure implementation adapter

The controller reads this adapter before dispatching a reference-led plotting
task and includes the applicable requirements in its task brief. These are
project integration requirements; external Skills remain unchanged at their
pinned upstream versions. The brief accompanies the manager invocation rather
than changing a claim, installed Skill, validator or official stage contract.

## Design and rendering scope

Use the approved design and its original scientific materials. Preserve panel
comparisons, layout, colour semantics, units, missingness, transformations and
uncertainty definitions. Upstream house palettes, hero layouts and quick-start
settings fill unset choices; an established design and current venue requirements
govern the deliverable. Reuse one working design/QA record.

Record the selected Python/R backend and named panel scope in the design notes.
A clear existing language-specific workflow supplies the selection. If actual
rendering needs a backend and none has been selected, obtain the user's choice
before rendering. Planning alone needs no rendering gate. Missing runtime or
packages block rendering; report the specific dependency without cross-rendering.
Generate plots, previews, exports and rendering QA in the selected backend.

Retain GIS and native-vector tools for their own panels and assembly. A plotting
node may implement explicitly named quantitative panels of a mixed figure; its
backend/QA claims cover only those deliverables. Preserve the full composite's
editable sources and record assembly checks separately. For exact physical
dimensions, export without tight cropping or measure and correct the actual
output width; quick-start arguments alone are not a measurement.

## Fixed-node inputs and outputs

Use the activated node's Skill identity, project root and frozen input paths.
Implement only its declared scope; a request for another Skill goes to a separate
configured node. Read original sources directly. List every project file read in
`consumed_sources`, including design notes, data, helpers and local references,
with the claim's logical ID, path and hash. Additional materials require explicit
registration and a new claim before use. Write new candidates separately from
claimed inputs, retaining the prior version and receipts on revision.

Produce the declared plotting code, actual exports, independent preview and
figure-receipt specification. Fill scientific/review fields from real evidence.
The orchestrator's `build_figure_receipt.py` supplies hashes and validates fields;
it does not generate statistics or human-review evidence. Receipts retain
project-relative provenance paths and hashes; public prose omits private absolute
paths. Keep human review pending until the configured confirmation/review step
receives and records the user's actual inspection.

The configured reference-review node checks these same exports. Reuse recorded
QA results and the working design rather than invoking an overlapping plotting
Skill inside the claim. Follow [reference-led-figures.md](reference-led-figures.md)
for confirmation, revision, receipt construction and final validation.
