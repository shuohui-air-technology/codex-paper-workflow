# Format-safe Humanizer Adapter

This is a protocol, not permission to rewrite a manuscript in place. `humanizer` is a prose editor, not a LaTeX, Word, PDF, citation, or scientific-claim parser. The current workflow package ships this protocol, not a universal format parser. A prose-naturalization stage may run only when a format-specific adapter is available and its preflight passes; otherwise the stage is explicitly `blocked` and the original draft remains authoritative.

## Preconditions

Run the adapter only after an integrity report has passed. Identify the selected format, create an immutable copy of the canonical draft, and retain the adapter-produced immutable candidate. Record the original and candidate paths, SHA-256 checksums, selected sections, author voice sample (if any), venue style, and rollback path plus rollback checksum in the stage receipt. Preflight requires `--candidate`; a `ready` result is never issued for an unbound candidate.

If the selected format cannot be parsed safely, stop with `status: blocked`; do not call native humanizer file mode and do not mutate the canonical draft. Plain Markdown prose may be processed only when protected spans can be enumerated reliably; Markdown is not an unconditional safe branch. LaTeX, DOCX, and PDF require a format-specific adapter that understands their structure; extracted plain text alone is not sufficient. The adapter contract must be schema version 2, identify a contract ID, adapter version, parser version, real entrypoint, and an entrypoint inside the project workspace; bind the entrypoint and every evidence artifact to SHA-256 hashes, bind the immutable copy, candidate, protected manifest, claim diff, integrity report, and rollback target (including its SHA-256 checksum) to the preflight invocation, declare `extract_protected`, `reassemble`, `verify_protected`, `verify_claims`, and `rollback` operations, declare all protected-span kinds, and provide an executed, bounded self-test command whose first two arguments are the current Python interpreter and the hashed adapter entrypoint (no `-c`, `-m`, or shell evaluation), with `--self-test`, `{input}`, `{candidate}`, `{contract_id}`, and `{self_test_report}` bindings. Its runtime JSON must match the passing report, exact input/candidate/rollback hashes, contract ID, entrypoint hash, humanizer hash/version, protected-operation booleans, report provenance, and offset mapping before preflight can pass. Adapter offsets require a hashed, input-bound mapping receipt. The claim diff requires a candidate-bound verifier receipt, claim inventory hash/count, grounded old/new excerpts, and protected-field inventories. The integrity receipt must bind `schema_version: 1`, `validation_status: pass`, `validity_status: clear`, `receipt_id`, the manifest and claim-diff hashes, and the exact input and candidate hashes.

The adapter contract must also set `network_scope: none`; the preflight refuses a contract that permits network access during the self-test.

## Protected manifest

Extract each protected span into `protected_manifest.json` with:

```json
{
  "id": "PROT-001",
  "kind": "math | code | table | figure_caption | citation | bibliography | url | doi | quote | technical_term | data | number | unit | statistical_qualifier | negative_finding | modality | negation | scope | frontmatter | label",
  "source_offset": [0, 0],
  "canonical_hash": "sha256:...",
  "exact_text": "..."
}
```

Protect equations and math delimiters, code, tables, figure/table labels and captions, bibliography entries, citation keys and targets, URLs/DOIs, quotations, numbers and units, statistical qualifiers, negative findings, modality/negation, domain terms, frontmatter, and user-marked text. For LaTeX, also protect `\\label`, `\\ref`, `\\tag`, citation commands, math token sequences, and macro definitions. For Word, preserve OOXML runs and fields rather than editing raw extracted text. Do not use an exact-byte comparison as a substitute for a format-aware parser; allow only documented surrounding prose whitespace changes.

The manifest is machine-checked before a call: `schema_version: 1`, exact `format`, `input_sha256`, `offset_unit: utf8_byte | adapter`, unique, ordered, non-overlapping span IDs, a supported `kind`, exact text, and a matching `sha256:` hash. Every span also carries `candidate_exact_text`, `candidate_span_sha256`, and an ordered `candidate_offset`; the candidate protected text must be byte/adapter-grounded after reassembly and must equal the original protected `exact_text` (formatting changes belong outside the protected span). With `utf8_byte`, each span must also match the canonical input offsets. Adapter-specific offsets require a separate JSON mapping receipt with its SHA-256 hash, `status: pass`, the exact input and candidate hashes, the canonical manifest binding hash (computed after removing only `mapping_receipt` and `mapping_receipt_sha256` from the manifest, avoiding a self-referential hash), `protected_span_count`, `offset_mapping_verified: true`, `candidate_protected_verified: true`, and a runtime self-test binding to that receipt; the mapping receipt is hashed again after the self-test.

Replace protected spans with opaque placeholders that cannot be mistaken for ordinary prose, preserving order and one-to-one IDs. Do not send the original protected text to the humanizer unless the user explicitly asks for it.

## Bounded humanizer call

Send only prose chunks with stable section/chunk IDs, a locked glossary, the target venue/register, and the author voice sample as style-only input. Use embedded mode so the canonical file is never rewritten. Keep chunks small enough to fit the current context budget and include nearby paragraph context only when needed for continuity.

The prompt must say:

- preserve every fact, result, citation, number, caveat, modality, negation, scope, and technical definition;
- do not add facts, citations, examples, interpretations, or stylistic personality to technical prose;
- do not alter placeholders or their order;
- return only the rewritten chunk plus its chunk ID.

## Verification artifacts

Before applying any candidate, emit `claim_evidence_diff.json`. The claim inventory must record claim ID, evidence references, entities, numeric/unit tokens, modality, negation, scope, statistical qualifiers, citation keys, and equation/label hashes. Each entry records the old excerpt, new excerpt, status, and verifier.

The machine-readable diff must contain `schema_version: 1`, the exact `input_sha256`, `original_sha256`, and `candidate_sha256`, `status: pass`, `grounding_mode: text | adapter`, `claim_count`, a matching `claim_inventory_sha256`, a candidate-bound independent `verifier_receipt` plus separately hashed `verifier_receipt_path`, and a non-empty `claims` list. The separately hashed verifier must bind the selected format, provide non-empty evidence references, set `grounded_excerpts: true` for adapter mode, and provide a complete, hash-matching verified-claim ledger; its claim IDs must exactly equal the diff claim IDs. Each claim must have a unique `claim_id`, non-empty evidence refs, old/new excerpts with matching hashes (grounded directly for text formats or by the adapter verifier for DOCX/PDF), a verifier, `status: pass`, `semantic_checks: true`, inventories for entities, numeric/units, modality, negation, scope, statistical qualifiers, citations, and equations/labels, and a `protected_field_ledger` proving each inventory was independently checked (including verified-empty inventories); an absent or failing claim is a block, not a warning.

Accept only when all of the following pass:

1. placeholder count, order, IDs, and canonical hashes match;
2. protected text, equations, labels, citations, URLs/DOIs, numbers, units, and glossary terms are unchanged;
3. every claim, caveat, evidence mapping, modality, negation, and scope survives;
4. no new fact, result, citation, or scientific interpretation appears;
5. selected sections and chunk IDs match the user's requested scope;
6. the candidate passes a lightweight post-edit integrity check and the humanizer's no-em/en-dash rule outside protected spans when the venue permits it.

Any mismatch is a failed pass. Keep the original immutable, retain the rejected candidate and diff for audit, record an error-to-guardrail event in `progress.md`, and ask whether to retry with a narrower scope. The preflight runs the self-test only on temporary input/candidate probes, uses a minimal environment, and re-hashes every bound artifact after execution; any mutation blocks the stage. Apply a verified candidate only after the user confirms the exact sections and preserves the original as rollback.
