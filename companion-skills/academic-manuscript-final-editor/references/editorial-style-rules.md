# Feedback-guided final editing rules

Read this reference for whole-manuscript revision, comment generalization, or bilingual synchronization. These are editorial decision rules, not a list of words to delete.

## Learn narrowly from supplied feedback

- Treat an explicit replacement as canonical for that passage unless it creates a factual contradiction.
- Classify the inferred rule as passage-local, section-local, project-wide, or reusable across projects. Only the last class belongs in a durable cross-project editorial profile.
- Keep project facts local. A preferred parameter, data interpretation, deferred declaration, target journal, or model boundary does not become a universal writing rule.
- Record deletion status as permanent, current-version only, deferred to submission, or moved to supplementary/audit material. Do not restore deleted prose during smoothing or translation unless its status requires later restoration.
- A useful project ledger records: original wording, revised wording, issue type, rule scope, analogous locations, bilingual action, restoration status, and completion state.

## Write from the finished scientific argument

- Organize the paper around its final research question and evidence. Do not falsify preregistration, hide exploratory status, suppress negative findings, or erase integrity-relevant changes.
- Remove narration about attempts, debugging, approvals, migration from an old draft, or what the team originally hoped to prove when those details have no scientific role.
- Move purely internal contract, gate, receipt, retry, checkpoint, bookkeeping, file-path, and local-package details to project records.
- Protocols, preregistrations, reproducibility appendices, and audit papers may legitimately require process terms. Judge the document's purpose before editing.

## State scope positively without erasing limits

Prefer one direct statement that defines what an output is, the data and assumptions behind it, and where it applies.

Weak pattern:

> The estimate is a conditional summary. It is not an individual measurement and does not represent the full population.

Preferred pattern:

> The estimate summarizes the sampled observations under the stated model and observation conditions.

Use the preferred form only when it preserves every material boundary. Keep explicit negation when the negative fact is itself a result or operational rule, for example:

- no events were observed;
- a parameter must be nonnegative;
- records without effort cannot be standardized;
- the null hypothesis was not rejected;
- an intervention did not change the measured outcome.

Never use a word-counting rule to remove negation. Look for repeated defensive tails, stacked exclusions, and prose about what the work does not claim after its scope has already been stated clearly.

## Remove self-defense and editorial residue

- Delete side remarks whose only purpose is to say that more complex methods exist or to defend why the paper did not use them. Keep a method-choice explanation when it is needed for identification, reproducibility, or interpretation.
- Replace statements that merely report an approval or registered rule with the actual data definition or analytical rule when the decision history is not itself evidence.
- Omit redundant raw columns, internal file names, variable names, version IDs, hashes, and accounting checks from ordinary prose. Retain reproducibility information in the appropriate methods, data, code, or supplement section.
- Replace implementation warnings such as "do not apply twice" with the mathematical definition and unit convention that make the operation unambiguous.
- Do not delete ethics, conflicts, funding, data provenance, or required journal statements by default. Their inclusion and timing depend on the study and submission stage.

## Judge meaning before polishing

Read [readability-examples.md](readability-examples.md) when a passage is grammatically sound but contributes little or is hard to unpack.

- Compare a candidate sentence with its nearby definitions and premises. If it merely restates a consequence already explicit there, delete it unless it supplies useful navigation, contrast, or interpretation. A methods definition and a short abstract reminder can serve different readers; similarity alone is not a defect.
- A replacement must either preserve needed information more clearly or remove repetition. "共同构建解释对象界定" becoming "共同界定解释范围" is still empty if the preceding sentences already state each analysis's task. Do not add a generic concluding sentence after deleting one.
- Distinguish repeated self-defense from a substantive limit: sample support, negative controls, observation-versus-target distinctions, unresolved provenance, and identified confounding may change the inference even without numbers. Retain a necessary limit where it governs the claim; do not soften it merely to use positive wording.
- Unpack nominal chains by identifying who or what does what to which object. Replace "实施……的开展" with the operation; replace abstract "支持" only when the supplied evidence specifies the comparison. Never infer causal effects, validation, or mechanisms from an attractive verb.
- Separate paragraphs by the question they answer. Prediction performance, directional identification, and external-index comparability have different limitations. Preserve logical connections and citation scope; paragraph splitting is not evidence repair.
- Check a difficult term against definitions, equations, captions, and supplied context. Preserve a defined technical name; explain its concrete operation on first use when useful. For an undefined compound, identify the missing quantity or relation and request clarification instead of guessing its meaning or declaring it fabricated.

## Preserve the paper's altitude

- Decide whether a definition belongs by audience and journal. Avoid dictionary-like explanations that interrupt a specialist introduction; move elementary teaching to a guide or glossary when appropriate.
- Do not advertise or belittle the contribution. Let the gap, method, and evidence establish its scale.
- Combine tightly related short sentences with a natural causal or contrastive link. Before merging, verify that citation scope does not expand to a claim the source does not support.
- Preserve a short sentence when it carries real emphasis or improves comprehension. Remove vague pronouns when the referent can be named directly.

## Use structure only when it helps retrieval

- A short introduction often reads better as a continuous problem, prior work, gap, and objective sequence than as generic subsections.
- Headings should name actual method modules or result objects. Treat a lone deep heading or a generic heading as a review candidate, not an automatic deletion.
- Put stable cross-item mappings in tables when comparison matters. Do not turn argumentative prose into tables.
- Put consecutive equations on separate display lines when inline mathematics interrupts reading. Preserve definitions, units, sources, numbering, and cross-references.
- Captions and surrounding prose should explain what the figure encodes. Redraw a figure only with explicit authorization, frozen source results, and a complete figure-text integrity check.

## Synchronize languages semantically

- The language designated for the current revision round controls synchronization. If none is designated, use the project version most recently edited. This is not always Chinese.
- Transfer each sentence's function: claim, qualification, transition, definition, or interpretation. Do not preserve an awkward source-language shape merely for word-level similarity.
- Compare numbers, signs, units, equations, citations, figure/table numbers, uncertainty, causal strength, sample scope, and conclusion scope after synchronization.
- Do not add an explanation to the translation that was removed from the canonical version.

## Protect document integrity

- Before and after editing, compare the scientific payload and document inventory: headings, paragraphs, equations, tables, figures, captions, references, and notes.
- Respect a document-wide spacing rule. Do not vary line spacing paragraph by paragraph merely to force pagination.
- Render every page of changed DOCX and PDF artifacts. Check equations, tables, figures, captions, headers, footers, page breaks, overflow, unusual whitespace, and missing glyphs.
- Preserve a recoverable baseline for formal or versioned artifacts. An explicit instruction to overwrite an ordinary working copy still takes precedence.

## Final acceptance checklist

- Every supplied comment is applied or explicitly unresolved.
- Analogous wording was searched across the full draft.
- Rule scope and any deferred restoration status are recorded.
- Scientific payload matches the frozen baseline.
- Necessary limits remain without repeated defensive wording.
- Deleted restatements were not replaced by abstract filler; difficult prose names its actual object and operation or records the missing definition.
- Purely internal workflow language is absent from ordinary prose.
- Bilingual versions have matching scientific meaning.
- Artifact rendering has no important defect.
- Independent review, when required, reached Critical=0 and Important=0 or reports a concrete blocker.
