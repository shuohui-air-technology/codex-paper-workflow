# Custom Workflow Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated, auditable DAG control plane that can validate, schedule, persist, and resume custom workflows while leaving the official v1.0 path unchanged.

**Architecture:** A new Python-standard-library package under `scripts/workflow_engine/` owns custom workflow documents, installed-Skill identities, compilation, risk reporting, scheduling, receipts, and crash-safe state. `scripts/workflow_manager.py` exposes the JSON protocol used by the Codex Orchestrator. Official mode continues to use the existing v1 progress machinery; custom mode uses only `.research/custom-workflow/` state.

**Tech Stack:** Python 3.10+ standard library, `dataclasses`, `enum`, `json`, `hashlib`, `pathlib`, `argparse`, `unittest`

**Spec:** `docs/superpowers/specs/2026-08-31-custom-workflow-studio-design.md`

## Global Constraints

- Preserve `workflow_version: paper-workflow-orchestrator-v1.0` and all existing v1 progress and receipt readers.
- After startup resolves a missing or `official` selection, the ordinary
  Orchestrator execution path must never read or create custom draft/run state.
  A user who explicitly launches Studio may inspect an existing custom draft
  while official mode remains selected; that inspection does not activate it
  or make it an execution authority.
- Valid custom graphs are DAGs: sequential, conditional, parallel, and join execution are supported; cycles are activation-blocking.
- Every activatable task node binds exactly one installed primary Skill; a saved
  draft may keep `skill_ref: null` until the user selects one. Control nodes
  always bind no Skill.
- The custom editor/runtime may use installed Skills only and must never download, install, or silently substitute a Skill.
- Custom workflows may remove every official paper stage or gate. Missing controls produce warnings and explicit acknowledgement, not automatic reinsertion.
- Arbitrary Python, JavaScript, shell, templates, and free-form executable conditions are forbidden.
- Runtime code remains Python 3.10+ standard-library-only.
- Structured JSON is authoritative; generated Markdown summaries are read-only projections.
- Use TDD for every behavior, run the existing suite after each task, and commit only the files listed for that task.
- Do not change release metadata to v1.1.0 in this plan; the Studio/release plan performs the coordinated version bump.
- Do not push or create a GitHub Release.

---

## File map

| Path | Responsibility |
| --- | --- |
| `references/workflows/official-v1.0-studio-projection.json` | Read-only, acyclic projection with source hashes, stage mapping, and feedback-loop notes |
| `references/workflows/validator-registry.v1.json` | Fixed validator IDs, scripts, schemas, and content hashes |
| `references/custom-workflow-contract.md` | Human-readable Orchestrator protocol for official/custom selection, claims, results, and recovery |
| `scripts/workflow_engine/schema.py` | Immutable document types, parsing, canonical semantic payload, and semantic hash |
| `scripts/workflow_engine/catalog.py` | Installed-Skill discovery, duplicate handling, identities, and validator registry loading |
| `scripts/workflow_engine/conditions.py` | Restricted condition AST validation and evaluation |
| `scripts/workflow_engine/compiler.py` | Graph validation, topology, artifact/write-set checks, and compiled plans |
| `scripts/workflow_engine/risk.py` | Non-blocking comparison with projected official controls |
| `scripts/workflow_engine/scheduler.py` | Pure node/edge state machine and ready-set calculation |
| `scripts/workflow_engine/fs.py` | Project containment, locking, durable append, and atomic replacement |
| `scripts/workflow_engine/store.py` | Selection, acknowledgements, drafts, revisions, snapshots, events, artifact projection, and recovery |
| `scripts/workflow_engine/receipts.py` | Invocation/result envelopes, output hashing, receipt validation, and idempotency |
| `scripts/workflow_engine/validators.py` | Fixed registered-validator adapters and bounded subprocess execution |
| `scripts/workflow_manager.py` | JSON CLI used by the Orchestrator |
| `tests/test_workflow_*.py` | Unit, integration, security, compatibility, and recovery tests |

---

### Task 1: Lock the official projection and validator identities

**Files:**
- Create: `references/workflows/official-v1.0-studio-projection.json`
- Create: `references/workflows/validator-registry.v1.json`
- Create: `tests/test_workflow_projection.py`

**Interfaces:**
- Consumes: current v1.0 source commit `e24c34255e3c72a329614e07c431bfa51a778c40`
- Produces: projection JSON with `schema_version`, `source_commit`, `source_files`, `official_contract_sections`, `nodes`, `edges`, `stage_map`, `control_tags`, and `projection_notes`; validator registry with `schema_version` and `validators`

- [ ] **Step 1: Write the failing projection contract tests**

```python
import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NORMALIZED_STAGES = {
    "intake", "directions", "literature", "topic", "design", "draft_audit",
    "venue_outline", "outline", "drafting", "scientific_figures",
    "abstract_title_keywords", "integrity", "review", "revision",
    "author_guided_final_editing", "prose_naturalization",
    "final_editorial_audit", "experiments", "finalize",
}


class WorkflowProjectionTests(unittest.TestCase):
    def test_projection_is_bound_to_the_reviewed_v10_sources(self):
        data = json.loads((ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text())
        self.assertEqual(data["schema_version"], "paper-workflow-studio-projection-v1")
        self.assertEqual(data["source_commit"], "e24c34255e3c72a329614e07c431bfa51a778c40")
        self.assertEqual(data["source_files"], {
            "SKILL.md": "d9d95b736f6450421294954e871179c209f2a5ac015f2f057e147a5bd22b4240",
            "references/stage-contracts.md": "e6780f2f00a81a695ba7b6c8b44d3ee3adced6eef7ea795cdf391113b19deff9",
            "references/progress-schema.md": "44c868ca0160906e74a8e61ee413f27bd4b23e151b7d486bc25b5516d45a3c7f",
        })
        self.assertEqual(data["official_contract_sections"], {
            "SKILL.md#body": "4387f4201b8b3e5ff38c49dc555f30397d164b60ce393cdb66b8d223f9cf9786",
            "references/stage-contracts.md#body": "e6780f2f00a81a695ba7b6c8b44d3ee3adced6eef7ea795cdf391113b19deff9",
            "references/progress-schema.md#body": "44c868ca0160906e74a8e61ee413f27bd4b23e151b7d486bc25b5516d45a3c7f",
        })
        self.assertEqual(set(data["stage_map"]), NORMALIZED_STAGES)
        self.assertTrue(any(note["kind"] == "feedback_as_new_revision" for note in data["projection_notes"]))

    def test_validator_registry_contains_only_repository_scripts(self):
        data = json.loads((ROOT / "references/workflows/validator-registry.v1.json").read_text())
        self.assertEqual(data["schema_version"], "paper-workflow-validator-registry-v1")
        expected = {
            "experiment-contract", "figure-contract", "final-edit-receipt",
            "humanizer-preflight", "paper-section",
        }
        self.assertEqual(set(data["validators"]), expected)
        for entry in data["validators"].values():
            path = ROOT / entry["script"]
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
```

- [ ] **Step 2: Run the tests and confirm the fixtures are absent**

Run: `python3 -B -m unittest tests.test_workflow_projection -v`
Expected: `ERROR` with `FileNotFoundError` for `official-v1.0-studio-projection.json`.

- [ ] **Step 3: Create the projection with complete stage coverage**

Use this exact top-level shape and include one `stage_map` entry for every value in `NORMALIZED_STAGES`:

```json
{
  "schema_version": "paper-workflow-studio-projection-v1",
  "projection_id": "official-v1.0",
  "source_commit": "e24c34255e3c72a329614e07c431bfa51a778c40",
  "source_files": {
    "SKILL.md": "d9d95b736f6450421294954e871179c209f2a5ac015f2f057e147a5bd22b4240",
    "references/stage-contracts.md": "e6780f2f00a81a695ba7b6c8b44d3ee3adced6eef7ea795cdf391113b19deff9",
    "references/progress-schema.md": "44c868ca0160906e74a8e61ee413f27bd4b23e151b7d486bc25b5516d45a3c7f"
  },
  "official_contract_sections": {
    "SKILL.md#body": "4387f4201b8b3e5ff38c49dc555f30397d164b60ce393cdb66b8d223f9cf9786",
    "references/stage-contracts.md#body": "e6780f2f00a81a695ba7b6c8b44d3ee3adced6eef7ea795cdf391113b19deff9",
    "references/progress-schema.md#body": "44c868ca0160906e74a8e61ee413f27bd4b23e151b7d486bc25b5516d45a3c7f"
  },
  "nodes": [],
  "edges": [],
  "stage_map": {},
  "control_tags": ["citation", "experiment_contract", "figure", "final_edit", "final_audit", "integrity"],
  "projection_notes": [
    {
      "kind": "feedback_as_new_revision",
      "official_from": "review",
      "official_to": "design",
      "studio_behavior": "Create a new semantic revision; do not create a cycle."
    }
  ]
}
```

Use this reviewed projection mapping so the implementation does not infer a
second official workflow from prose:

| Projection node | `projection_kind` | Normalized stages | Suggested installed Skill IDs |
| --- | --- | --- | --- |
| `intake` | `orchestrator` | `intake` | none |
| `directions` | `task` | `directions` | `clarify-research-idea` |
| `literature` | `task` | `literature` | `research-hub` |
| `topic` | `task` | `topic` | `gap-to-topic` |
| `design` | `task` | `design` | `research-design-helper` |
| `draft-audit` | `task` | `draft_audit` | `paper-memory-builder` |
| `venue-outline` | `task` | `venue_outline`, `outline` | `ml-paper-writing`, `academic-paper` |
| `drafting` | `task` | `drafting` | `ml-paper-writing`, `academic-paper` |
| `scientific-figures` | `task` | `scientific_figures` | `scientific-visualization` |
| `abstract-title-keywords` | `orchestrator` | `abstract_title_keywords` | none |
| `integrity` | `gate` | `integrity` | none |
| `review` | `task` | `review` | `academic-paper-reviewer` |
| `revision` | `task` | `revision` | `ml-paper-writing`, `academic-paper` |
| `final-editing` | `task` | `author_guided_final_editing` | `academic-manuscript-final-editor` |
| `prose-naturalization` | `task` | `prose_naturalization` | `humanizer` |
| `final-editorial-audit` | `gate` | `final_editorial_audit` | `academic-manuscript-final-editor` |
| `experiments` | `task` | `experiments` | `autoresearch` |
| `finalize` | `delivery` | `finalize` | none |

Each projection node has exactly `id`, `display_name`, `projection_kind`,
`official_stage_ids`, `suggested_skill_ids`, `suggested_validator_ids`,
`inputs`, `outputs`, `write_scopes`, and `control_tags`. Use logical artifact
IDs from the current stage table; use `canonical_manuscript` as a protected
write scope for drafting/revision/final-edit/naturalization nodes. The only
non-empty projected validator suggestion is `final-edit-receipt` for
`final-editorial-audit`; other gates remain explicitly unbound in a clone.
Each edge has
exactly `id`, `source`, `target`, and `projection_note`. `stage_map` maps every
normalized stage ID to exactly one projection node ID. Keep the projection
acyclic: represent review-to-design feedback, failed-audit revision loops, and
later experiment reruns only in `projection_notes`, never as back edges. The
Studio clone step must ask the user to choose one currently installed Skill
when a projected task has multiple or unavailable suggestions; do not invent a
binding for `intake`, `abstract-title-keywords`, `integrity`, or `finalize`.

- [ ] **Step 4: Create the validator registry from actual script hashes**

Run this read-only command to obtain the five exact hashes, then write them into the registry entries:

```bash
shasum -a 256 \
  scripts/experiment_contract_validator.py \
  scripts/figure_contract_validator.py \
  scripts/final_edit_receipt_validator.py \
  scripts/humanizer_preflight.py \
  scripts/paper_section_validator.py
```

Each entry must have exactly `script`, `sha256`, `adapter`, `input_schema`,
`control_tags`, and `outcomes`; use outcomes `pass`, `fail`, and `blocked`. Adapter IDs are fixed to
`experiment_contract_v1`, `figure_contract_v1`, `final_edit_receipt_v1`,
`humanizer_preflight_v1`, and `paper_section_v1`. They name code-owned argument
builders, not commands supplied by a workflow document.

Use these registry tags: `experiment-contract` → `experiment_contract` and
`figure-contract` → `figure`; the other three registry entries use an empty
tag list because passing one receipt/section/preflight validator alone is not
equivalent to the complete integrity, final-edit, or final-audit control.
Citation and those broader controls remain projection-derived. This
conservative mapping may warn for a custom replacement, which the advanced
user can acknowledge; it must not suppress a warning on partial evidence.

- [ ] **Step 5: Run projection and existing tests**

Run: `python3 -B -m unittest tests.test_workflow_projection -v`
Expected: all projection tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: the original 51 tests plus the new projection tests pass.

- [ ] **Step 6: Commit the projection boundary**

```bash
git add references/workflows/official-v1.0-studio-projection.json \
  references/workflows/validator-registry.v1.json \
  tests/test_workflow_projection.py
git commit -m "test: lock official workflow studio projection"
```

---

### Task 2: Add the versioned workflow schema and semantic hash

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/workflow_engine/__init__.py`
- Create: `scripts/workflow_engine/schema.py`
- Create: `tests/fixtures/workflow_valid_linear.json`
- Create: `tests/fixtures/workflow_valid_branch_join.json`
- Create: `tests/fixtures/workflow_valid_installed_core.json`
- Create: `tests/test_workflow_schema.py`

**Interfaces:**
- Consumes: JSON objects produced by the future Studio
- Produces: `WorkflowError`, `WorkflowIssue`, `NodeSpec`, `EdgeSpec`, `WorkflowDocument`, `parse_workflow(value)`, `load_workflow(path)`, `behavior_payload(document)`, `document_payload(document)`, and `document_sha256(document)`

- [ ] **Step 1: Write schema and hash tests**

```python
import copy
import json
import unittest
from pathlib import Path

from scripts.workflow_engine.schema import WorkflowError, document_sha256, parse_workflow


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSchemaTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())

    def test_visual_position_does_not_change_semantic_hash(self):
        original = parse_workflow(self.value)
        moved = copy.deepcopy(self.value)
        moved["ui"]["positions"]["directions"] = {"x": 900, "y": 700}
        self.assertEqual(document_sha256(original), document_sha256(parse_workflow(moved)))

    def test_duplicate_node_id_is_rejected_with_stable_code(self):
        duplicate = copy.deepcopy(self.value)
        duplicate["nodes"].append(copy.deepcopy(duplicate["nodes"][0]))
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(duplicate)
        self.assertEqual(caught.exception.code, "schema.duplicate_node_id")

    def test_unbound_task_is_a_saveable_but_incomplete_draft(self):
        draft = copy.deepcopy(self.value)
        draft["nodes"][0]["skill_ref"] = None
        document = parse_workflow(draft)
        self.assertIsNone(document.nodes[0].skill_ref)

    def test_control_node_cannot_bind_a_skill(self):
        invalid = copy.deepcopy(self.value)
        invalid["nodes"][0]["type"] = "join"
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(invalid)
        self.assertEqual(caught.exception.code, "schema.control_skill_forbidden")
```

- [ ] **Step 2: Run the schema test and verify import failure**

Run: `python3 -B -m unittest tests.test_workflow_schema -v`
Expected: `ERROR` with `ModuleNotFoundError: scripts.workflow_engine`.

- [ ] **Step 3: Implement immutable schema types**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "paper-workflow-custom-v1"
NODE_TYPES = frozenset({"task", "condition", "join", "validator"})


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str, *, node_id: str = "", edge_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.node_id = node_id
        self.edge_id = edge_id


@dataclass(frozen=True)
class WorkflowIssue:
    severity: str
    code: str
    message: str
    node_id: str = ""
    edge_id: str = ""


@dataclass(frozen=True)
class NodeSpec:
    id: str
    type: str
    display_name: str
    entry: bool
    enabled: bool
    skill_ref: str | None
    validator_ref: str | None
    origin_projection_node_id: str | None
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    outcomes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    failure_policy: str
    condition_cases: tuple[Mapping[str, Any], ...]
    join_mode: str


@dataclass(frozen=True)
class EdgeSpec:
    id: str
    source: str
    target: str
    trigger: str
    output_map: Mapping[str, str]


@dataclass(frozen=True)
class WorkflowDocument:
    schema_version: str
    workflow_id: str
    document_revision: int
    semantic_revision: int
    derived_from: Mapping[str, Any] | None
    max_parallelism: int
    external_inputs: tuple[str, ...]
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    ui: Mapping[str, Any] = field(compare=False, hash=False)
```

Implement strict object-key, identifier, enum, duplicate, and scalar-size
validation in `parse_workflow`. Reject unknown top-level and node fields so
schema changes require a new version. Structural parsing deliberately permits
`skill_ref: null` on a task and `validator_ref: null` on a validator so Studio
can save an incomplete draft. It must still reject a task with a validator, a
validator with a Skill, and every control node carrying either binding. The
compiler, not the parser, turns an unbound executable node into an
activation-blocking issue. `origin_projection_node_id` is nullable provenance,
not a capability claim: it is allowed only when document `derived_from`
contains the matching projection ID/hash, and compilation verifies that the
referenced projection node exists.

- [ ] **Step 4: Implement canonical semantic hashing**

```python
import hashlib
import json


def document_payload(document: WorkflowDocument) -> dict[str, object]:
    return {
        "semantic_revision": document.semantic_revision,
        **behavior_payload(document),
    }


def behavior_payload(document: WorkflowDocument) -> dict[str, object]:
    return {
        "schema_version": document.schema_version,
        "workflow_id": document.workflow_id,
        "derived_from": document.derived_from,
        "max_parallelism": document.max_parallelism,
        "external_inputs": list(document.external_inputs),
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
                "entry": node.entry,
                "enabled": node.enabled,
                "skill_ref": node.skill_ref,
                "validator_ref": node.validator_ref,
                "origin_projection_node_id": node.origin_projection_node_id,
                "inputs": list(node.inputs),
                "outputs": list(node.outputs),
                "outcomes": list(node.outcomes),
                "write_scopes": list(node.write_scopes),
                "failure_policy": node.failure_policy,
                "condition_cases": list(node.condition_cases),
                "join_mode": node.join_mode,
            }
            for node in sorted(document.nodes, key=lambda item: item.id)
        ],
        "edges": [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "trigger": edge.trigger,
                "output_map": dict(sorted(edge.output_map.items())),
            }
            for edge in sorted(document.edges, key=lambda item: item.id)
        ],
    }


def document_sha256(document: WorkflowDocument) -> str:
    raw = json.dumps(document_payload(document), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Add complete valid fixtures**

Create a linear fixture with two task nodes (`directions` then `design`) and one
unconditional edge. Create a branch fixture with one condition, two task
branches, one `all_active` join, explicit `default`, and unique logical outputs.
Both fixtures must include every schema field accepted by `parse_workflow` and
no undocumented field. Task `outcomes` contains at least `succeeded`;
validators use registry outcomes; conditions derive outcomes from cases; joins
declare no custom outcome. `write_scopes` uses normalized logical resource IDs,
including protected scopes such as `canonical_manuscript` where applicable.
Create `workflow_valid_installed_core.json` as a one-node, no-file-output task
bound to `paper-workflow-orchestrator`; it is used only to prove that an
installed core archive can parse imports, discover a present Skill, and run the
manager `validate` command.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -B -m unittest tests.test_workflow_schema -v`
Expected: all schema tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass and `ProgressVersionTests.test_current_workflow_version_is_v10` remains green.

- [ ] **Step 7: Commit the schema**

```bash
git add scripts/__init__.py scripts/workflow_engine/__init__.py scripts/workflow_engine/schema.py \
  tests/fixtures/workflow_valid_linear.json \
  tests/fixtures/workflow_valid_branch_join.json \
  tests/fixtures/workflow_valid_installed_core.json \
  tests/test_workflow_schema.py
git commit -m "feat: add custom workflow schema"
```

---

### Task 3: Resolve installed Skills and registered validators

**Files:**
- Create: `scripts/workflow_engine/catalog.py`
- Create: `tests/test_workflow_catalog.py`

**Interfaces:**
- Consumes: explicit Skill roots, installer receipt, environment mapping, and `validator-registry.v1.json`
- Produces: `SkillIdentity`, `ValidatorIdentity`, `CatalogResult`, `resolve_skill_roots(explicit, install_target, environ)`, `discover_skills(roots, install_receipts)`, and `load_validator_registry(path, repository_root)`

- [ ] **Step 1: Write catalog identity and ambiguity tests**

```python
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_engine.catalog import CatalogError, discover_skills


def write_skill(root: Path, name: str, body: str = "instructions") -> Path:
    path = root / name
    path.mkdir(parents=True)
    path.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


class WorkflowCatalogTests(unittest.TestCase):
    def test_different_duplicate_skill_ids_are_ambiguous(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first"
            second = base / "second"
            write_skill(first, "sample", "first")
            write_skill(second, "sample", "second")
            result = discover_skills((first, second), install_receipts={})
            self.assertNotIn("sample", result.skills)
            self.assertEqual(result.errors[0].code, "catalog.ambiguous_skill")

    def test_symlink_escaping_root_is_not_discovered(self):
        if os.name == "nt":
            self.skipTest("symlink creation requires platform privileges")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "skills"
            outside = base / "outside"
            root.mkdir()
            target = write_skill(outside, "escaped")
            root.joinpath("escaped").symlink_to(target, target_is_directory=True)
            result = discover_skills((root,), install_receipts={})
            self.assertNotIn("escaped", result.skills)
            self.assertEqual(result.errors[0].code, "catalog.symlink_escape")
```

- [ ] **Step 2: Run the catalog test and verify import failure**

Run: `python3 -B -m unittest tests.test_workflow_catalog -v`
Expected: `ERROR` because `scripts.workflow_engine.catalog` does not exist.

- [ ] **Step 3: Implement stable Skill identities**

```python
from dataclasses import dataclass
from pathlib import Path

from .schema import WorkflowIssue


@dataclass(frozen=True)
class SkillIdentity:
    catalog_id: str
    root: Path
    relative_path: str
    skill_sha256: str
    tree_sha256: str
    locked: bool


@dataclass(frozen=True)
class CatalogResult:
    skills: dict[str, SkillIdentity]
    errors: tuple[WorkflowIssue, ...]
    warnings: tuple[WorkflowIssue, ...]
```

Parse `name:` only from the first YAML frontmatter block; do not import or execute Skill code. Resolve every candidate path and require it to remain under its root. Collapse duplicate IDs only when both `skill_sha256` and `tree_sha256` match; otherwise emit `catalog.ambiguous_skill` and omit the ID.

Implement `tree_sha256(path)` as a public helper in `catalog.py` using the same
algorithm recorded by installation receipts: sort every regular file by POSIX
relative path, update SHA-256 with `relative_utf8 + NUL + bytes + NUL`, reject
every symlink/reparse point, and return the `sha256:<hex>` form. Do not import
the installer's private `_tree_hash`. Add a golden test that builds a small
Skill tree, asserts a literal precomputed `sha256:<hex>` digest before writing
that literal into a receipt, and proves discovery marks the same identity as
locked. Task 6 of the Studio/release plan additionally checks a real
installer-produced receipt. Treat each allowed root as a collection of direct child
Skill directories; the child directory/catalog ID and frontmatter `name` must
match exactly.

- [ ] **Step 4: Implement root priority and receipt binding**

```python
def resolve_skill_roots(
    explicit: tuple[Path, ...],
    install_target: Path | None,
    environ: dict[str, str],
) -> tuple[Path, ...]:
    ordered = list(explicit)
    if install_target is not None:
        ordered.append(install_target)
    codex_root = Path(environ["CODEX_HOME"]) if environ.get("CODEX_HOME") else Path.home() / ".codex"
    ordered.append(codex_root / "skills")
    unique: list[Path] = []
    for path in ordered:
        expanded = path.expanduser()
        if expanded not in unique:
            unique.append(expanded)
    return tuple(unique)
```

`discover_skills` accepts a mapping from resolved collection root to its already
validated `.paper-workflow-install.json` content. When the receipt for that
root contains a matching tree hash, set `locked=True`; otherwise compute the
tree hash and emit `catalog.unlocked_skill`.

- [ ] **Step 5: Implement the validator registry loader**

Add `ValidatorIdentity` with `validator_id`, `script`, `sha256`, `adapter`,
`input_schema`, `control_tags`, and `outcomes`. Resolve each script under the repository root,
reject symlinks and traversal, recompute SHA-256, reject an adapter outside the
five fixed IDs, and raise `CatalogError("validator.hash_mismatch", ...)` when
content differs.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -B -m unittest tests.test_workflow_catalog -v`
Expected: ambiguity, symlink, root-priority, receipt, and validator tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 7: Commit catalog discovery**

```bash
git add scripts/workflow_engine/catalog.py tests/test_workflow_catalog.py
git commit -m "feat: discover installed workflow skills"
```

---

### Task 4: Validate restricted conditions and compile DAGs

**Files:**
- Create: `scripts/workflow_engine/conditions.py`
- Create: `scripts/workflow_engine/compiler.py`
- Create: `scripts/workflow_engine/risk.py`
- Create: `tests/test_workflow_compiler.py`

**Interfaces:**
- Consumes: `WorkflowDocument`, `CatalogResult`, validator identities, and official projection control tags
- Produces: `ConditionFacts`, `evaluate_condition(cases, facts)`, `CompiledNode`, `CompiledEdge`, `CompiledPlan`, `CompileResult`, and `compile_workflow(document, catalog, validators, projection)`

- [ ] **Step 1: Write failing condition, cycle, and warning tests**

```python
import copy
import json
import unittest
from pathlib import Path

from scripts.workflow_engine.compiler import compile_workflow
from scripts.workflow_engine.schema import parse_workflow


ROOT = Path(__file__).resolve().parents[1]


class WorkflowCompilerTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text())

    def test_cycle_is_activation_blocking(self):
        value = copy.deepcopy(self.value)
        value["edges"].append({
            "id": "back-edge", "source": "join", "target": "condition",
            "trigger": "succeeded", "output_map": {},
        })
        result = compile_workflow(parse_workflow(value), self.catalog, self.validators, self.projection)
        self.assertIsNone(result.plan)
        self.assertIn("graph.cycle", {issue.code for issue in result.errors})

    def test_removed_integrity_control_warns_but_compiles(self):
        result = compile_workflow(parse_workflow(self.value), self.catalog, self.validators, self.projection)
        self.assertIsNotNone(result.plan)
        self.assertIn("risk.control_removed.integrity", {issue.code for issue in result.warnings})

    def test_unbound_task_is_activation_blocking(self):
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["type"] == "task")["skill_ref"] = None
        result = compile_workflow(parse_workflow(value), self.catalog, self.validators, self.projection)
        self.assertIsNone(result.plan)
        self.assertIn("catalog.skill_required", {issue.code for issue in result.errors})
```

In `setUp`, construct real `SkillIdentity` objects for every fixture `skill_ref`, an empty validator map, and load the projection JSON. Do not mock compiler internals.

- [ ] **Step 2: Run the compiler tests and confirm the module is missing**

Run: `python3 -B -m unittest tests.test_workflow_compiler -v`
Expected: `ERROR` because `compiler.py` does not exist.

- [ ] **Step 3: Implement the restricted condition evaluator**

```python
from dataclasses import dataclass
from typing import Mapping

from .schema import WorkflowError


@dataclass(frozen=True)
class ConditionFacts:
    node_statuses: Mapping[str, str]
    node_outcomes: Mapping[str, str]
    decisions: Mapping[str, object]
    artifact_states: Mapping[str, str]
    project_booleans: Mapping[str, bool]


def evaluate_predicate(expression: object, facts: ConditionFacts) -> bool:
    if not isinstance(expression, dict) or not isinstance(expression.get("op"), str):
        raise WorkflowError("condition.invalid_ast", "condition must be a serialized operation")
    operation = expression["op"]
    if operation == "status_is":
        return facts.node_statuses.get(str(expression.get("node"))) == expression.get("value")
    if operation == "outcome_is":
        return facts.node_outcomes.get(str(expression.get("node"))) == expression.get("value")
    if operation == "decision_is":
        return facts.decisions.get(str(expression.get("name"))) == expression.get("value")
    if operation == "artifact_state_is":
        return facts.artifact_states.get(str(expression.get("artifact"))) == expression.get("value")
    if operation == "fact_is":
        return facts.project_booleans.get(str(expression.get("name"))) is expression.get("value")
    if operation == "all":
        return all(evaluate_predicate(item, facts) for item in expression.get("args", []))
    if operation == "any":
        return any(evaluate_predicate(item, facts) for item in expression.get("args", []))
    if operation == "not":
        return not evaluate_predicate(expression.get("arg"), facts)
    raise WorkflowError("condition.unsupported_op", f"unsupported condition operation: {operation}")


def evaluate_condition(cases: tuple[Mapping[str, object], ...], facts: ConditionFacts) -> str:
    for case in cases:
        if evaluate_predicate(case["when"], facts):
            return str(case["outcome"])
    return "default"
```

Validate AST keys and operand types during compilation, including non-empty `all`/`any` arguments and exactly one default edge per condition node.

- [ ] **Step 4: Implement deterministic graph compilation**

Use Kahn's algorithm with lexicographically sorted node IDs. Exclude disabled
nodes and incident edges before validation. Require explicit `entry=True` for
every zero-incoming enabled node. Require every enabled node to be reachable
from an entry and able to reach an enabled terminal. Validate source/target IDs,
source-trigger combinations, declared outcomes, input producers or declared
`external_inputs`, Skill identities, validator identities, output mappings, and
conflicting parallel `write_scopes`. Treat two unordered nodes that share any
write scope as activation-blocking; protected scope names cannot be renamed
away through an edge output map.

```python
@dataclass(frozen=True)
class CompiledNode:
    id: str
    type: str
    entry: bool
    skill: SkillIdentity | None
    validator: ValidatorIdentity | None
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    outcomes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    failure_policy: str
    condition_cases: tuple[Mapping[str, object], ...]
    join_mode: str


@dataclass(frozen=True)
class CompiledEdge:
    id: str
    source: str
    target: str
    trigger: str
    output_map: Mapping[str, str]


@dataclass(frozen=True)
class CompiledPlan:
    workflow_id: str
    semantic_revision: int
    document_sha256: str
    semantic_sha256: str
    nodes: Mapping[str, CompiledNode]
    edges: Mapping[str, CompiledEdge]
    incoming: Mapping[str, tuple[str, ...]]
    outgoing: Mapping[str, tuple[str, ...]]
    topological_order: tuple[str, ...]
    max_parallelism: int


@dataclass(frozen=True)
class CompileResult:
    plan: CompiledPlan | None
    errors: tuple[WorkflowIssue, ...]
    warnings: tuple[WorkflowIssue, ...]
```

Return errors instead of a plan when any structural issue exists. Never mutate or repair the document.

After validation, compute `CompiledPlan.semantic_sha256` from the canonical
document payload plus every resolved Skill identity (`catalog_id`,
root-relative path, `SKILL.md` hash, tree hash, locked flag), every validator
identity (ID, adapter, script hash, input schema, outcomes), and the canonical
compiled topology. `document_sha256` changes only with saved document semantics;
the compiled semantic hash also changes when a bound local capability changes.
Selection, acknowledgements, run state, invocations, and receipts bind the
compiled semantic hash. Draft revision filenames use `document_sha256` because
an incomplete draft may not compile.

- [ ] **Step 5: Implement risk comparison as warnings only**

`risk.py` compares projected `control_tags` with server-derived coverage. A
custom document never supplies editable control tags. Coverage comes only from
an enabled cloned node whose `origin_projection_node_id` resolves and whose
current binding, inputs, outputs, write scopes, and projected adjacent control
edges remain compatible with that projection node, or from a fixed validator
registry entry's `control_tags`. Reject duplicate use of one
`origin_projection_node_id`. A changed binding or material rewiring on a cloned
control emits `risk.control_replaced.<tag>`; deleting/disabling it emits
`risk.control_removed.<tag>`. A newly added arbitrary task cannot self-assert a
control tag. These checks are deliberately conservative: uncertain replacement
coverage remains a warning that the user may acknowledge. The compiler must
still return a plan when its only findings are risk warnings.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -B -m unittest tests.test_workflow_compiler -v`
Expected: condition, cycle, missing reference, input source, disabled node, deterministic order, write conflict, and risk warning tests pass.

Include a compiler test that keeps the workflow document byte-identical while
changing one bound Skill tree: `document_sha256` stays equal and compiled
`semantic_sha256` changes. Task 7 adds the integration assertion that a
previously active run cannot claim another node after that identity change
until revalidation/new activation.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 7: Commit compilation**

```bash
git add scripts/workflow_engine/conditions.py scripts/workflow_engine/compiler.py \
  scripts/workflow_engine/risk.py tests/test_workflow_compiler.py
git commit -m "feat: compile custom workflow dags"
```

---

### Task 5: Implement the pure scheduler state machine

**Files:**
- Create: `scripts/workflow_engine/scheduler.py`
- Create: `tests/test_workflow_scheduler.py`

**Interfaces:**
- Consumes: immutable `CompiledPlan` and `RunState`
- Produces: `NodeStatus`, `EdgeStatus`, `ArtifactRuntime`, `NodeRuntime`, `EdgeRuntime`, `RunState`, `ControlTransition`, `initial_run(plan, run_id)`, `refresh_ready(plan, state)`, `condition_facts(state)`, `stabilize_control_nodes(plan, state)`, `claim_transition(plan, state, node_id, token)`, `result_transition(plan, state, result)`, and `retry_transition(plan, state, node_id)`

- [ ] **Step 1: Write scheduler transition tests**

```python
import unittest

from scripts.workflow_engine.scheduler import (
    EdgeStatus, NodeStatus, initial_run, refresh_ready, result_transition,
    stabilize_control_nodes,
)


class WorkflowSchedulerTests(unittest.TestCase):
    def test_all_active_ignores_condition_inactive_branch(self):
        state = initial_run(self.plan, "run-001")
        state = self.complete(state, "condition", outcome="has_sources")
        state = self.complete(state, "literature")
        refreshed = refresh_ready(self.plan, state)
        self.assertEqual(refreshed.edges["condition-to-design"].status, EdgeStatus.INACTIVE)
        self.assertEqual(refreshed.nodes["join"].status, NodeStatus.READY)

    def test_any_success_freezes_first_output_source(self):
        state = initial_run(self.any_plan, "run-002")
        state = self.complete(state, "branch-a", outputs={"draft": "a.md"})
        self.assertEqual(state.nodes["join"].selected_inputs["draft"], "a.md")
        state = self.complete(state, "branch-b", outputs={"draft": "b.md"})
        self.assertEqual(state.nodes["join"].selected_inputs["draft"], "a.md")

    def test_verified_artifact_state_selects_condition_branch(self):
        state = initial_run(self.artifact_condition_plan, "run-003")
        state = self.register_artifact(state, "sources", state="verified")
        stabilized, transitions = stabilize_control_nodes(
            self.artifact_condition_plan,
            refresh_ready(self.artifact_condition_plan, state),
        )
        self.assertEqual(stabilized.nodes["has-sources"].outcome, "verified")
        self.assertEqual(transitions[0].node_id, "has-sources")
```

Build plans through `compile_workflow` in `setUp`; do not hand-construct impossible compiled graphs.

- [ ] **Step 2: Run scheduler tests and confirm import failure**

Run: `python3 -B -m unittest tests.test_workflow_scheduler -v`
Expected: `ERROR` because `scheduler.py` does not exist.

- [ ] **Step 3: Implement explicit state enums and immutable runtime values**

```python
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    STALE = "stale"


class EdgeStatus(str, Enum):
    WAITING = "waiting"
    SATISFIED = "satisfied"
    INACTIVE = "inactive"
    FAILED = "failed"


@dataclass(frozen=True)
class EdgeRuntime:
    status: EdgeStatus
    selected_output_map: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRuntime:
    artifact_id: str
    path: str
    sha256: str
    state: str
    producer_node_id: str
    producer_attempt: int


@dataclass(frozen=True)
class NodeRuntime:
    status: NodeStatus
    attempt: int = 0
    outcome: str = ""
    claim_token_hash: str = ""
    selected_inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    auxiliary_outputs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    winner_edge_id: str = ""


@dataclass(frozen=True)
class RunState:
    run_id: str
    workflow_id: str
    semantic_sha256: str
    nodes: Mapping[str, NodeRuntime]
    edges: Mapping[str, EdgeRuntime]
    artifacts: Mapping[str, ArtifactRuntime]
    decisions: Mapping[str, object]
    project_booleans: Mapping[str, bool]


@dataclass(frozen=True)
class ControlTransition:
    node_id: str
    event_type: str
    outcome: str
    edge_updates: Mapping[str, EdgeStatus]
```

Use `dataclasses.replace` and copied dictionaries for transitions so callers cannot observe half-applied state.

- [ ] **Step 4: Implement ready-set and edge-state rules exactly**

For ordinary nodes, require all non-inactive incoming edges to be satisfied. A
failed required edge blocks the node. A non-entry node whose incoming edges are
all inactive becomes `SKIPPED` and recursively makes its outgoing edges
inactive; it must not become ready by vacuous truth. For `all_active`, apply the
same rule while ignoring inactive branches, and skip the join when every branch
is inactive. For `any_success`, succeed on the first satisfied edge, freeze the
declared output map, and retain later outputs in `auxiliary_outputs` without
replacing the winner. If every active edge becomes terminal without a success,
block the join; if every edge is inactive, skip it. Apply condition outcomes to
matching/default edges. A skipped task makes every outgoing edge inactive.
For an `any_success` join, set `winner_edge_id` with the unique first
satisfaction, preserve it through late auxiliary results and stale state, and
reject a simultaneous first satisfaction. Source invalidation cuts the old
route and stales the frozen join rather than selecting another edge.

Even when graph dependencies are satisfied, a node remains `PENDING` with
`runtime.external_artifact_missing` guidance until every referenced declared
external input has a verified `ArtifactRuntime`. Registering that artifact
refreshes the ready set; a missing external file is never treated as empty
input or activation success.

`condition_facts(state)` derives node statuses/outcomes, decisions, project
booleans, and artifact states from the same immutable snapshot; it never checks
file existence as a substitute for a verified artifact. `result_transition`
adds an artifact only after the manager has checked its project-contained path
and SHA-256. A later hash mismatch marks that artifact `stale` and propagates
staleness before any artifact predicate is evaluated.

`stabilize_control_nodes(plan, state)` returns the final immutable state plus an ordered tuple
of `ControlTransition` records; it repeatedly applies deterministic,
side-effect-free transitions until no condition or join can advance and
recomputes `condition_facts` after each transition. A ready condition evaluates
its restricted AST from recorded facts, succeeds with the selected named
outcome, and sets non-selected edges inactive. A ready join succeeds
automatically after applying its declared join rule and input selection. It
never returns condition or join nodes to the Orchestrator as executable work.
Validator nodes remain in the ready set for the registered-validator runner.

```python
def ready_node_ids(plan: CompiledPlan, state: RunState) -> tuple[str, ...]:
    return tuple(
        node_id
        for node_id in plan.topological_order
        if state.nodes[node_id].status is NodeStatus.READY
    )
```

Keep the result sorted by compiled topology and cap claims through `plan.max_parallelism`; do not make scheduler ordering depend on dictionary insertion.

- [ ] **Step 5: Implement claim, result, retry, and stale transitions**

`claim_transition` accepts only `READY`, increments `attempt`, stores the SHA-256 of the random claim token, and returns `RUNNING`. `result_transition` accepts only the current running attempt and declared outcome. A failed task with `failure_policy: block` remains `FAILED` and fails required outgoing edges; with `skip_branch` it records the failure receipt, transitions to `SKIPPED`, and deactivates its outgoing edges. `retry_transition` accepts only `FAILED` or `BLOCKED` and returns the node to `READY` after rechecking inputs. Add `mark_descendants_stale(plan, state, changed_node_ids)` using the compiled outgoing adjacency.

Restrict `claim_transition` and `node-invocation-v1` to task nodes. Validator
nodes enter `RUNNING` only through the registered-validator transition in Task
7; condition and join nodes never receive an attempt token.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -B -m unittest tests.test_workflow_scheduler -v`
Expected: all state table, parallel, condition, verified-artifact condition,
join, failure, skip, retry, and stale tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 7: Commit the scheduler**

```bash
git add scripts/workflow_engine/scheduler.py tests/test_workflow_scheduler.py
git commit -m "feat: schedule custom workflow nodes"
```

---

### Task 6: Add crash-safe custom workflow storage

**Files:**
- Create: `scripts/workflow_engine/fs.py`
- Create: `scripts/workflow_engine/store.py`
- Create: `tests/test_workflow_store.py`

**Interfaces:**
- Consumes: `WorkflowDocument`, `CompiledPlan`, `RunState`, and project root
- Produces: `Selection`, `WorkflowEvent`, `RecoveryResult`, `WorkflowTransaction`, `WorkflowStore`, `WorkflowStore.locked_run()`, `resolve_project_path(root, relative)`, `atomic_write_json(path, value)`, and `append_event(path, event)`

- [ ] **Step 1: Write selection, concurrency, and recovery tests**

```python
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_engine.store import StoreError, WorkflowStore


class WorkflowStoreTests(unittest.TestCase):
    def test_missing_selection_is_official_without_custom_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            self.assertEqual(store.read_selection().mode, "official")
            self.assertFalse(root.joinpath(".research/custom-workflow").exists())

    def test_stale_document_revision_cannot_overwrite_newer_draft(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.save_draft(self.document, expected_document_revision=0)
            with self.assertRaises(StoreError) as caught:
                store.save_draft(self.document, expected_document_revision=0)
            self.assertEqual(caught.exception.code, "store.revision_conflict")

    def test_event_hash_gap_blocks_recovery(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(self.plan, "run-001")
            events = store.paths.events
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_seq": 99, "previous_event_hash": "bad"}) + "\n")
            result = store.recover()
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.code, "events.sequence_gap")
```

- [ ] **Step 2: Run store tests and verify import failure**

Run: `python3 -B -m unittest tests.test_workflow_store -v`
Expected: `ERROR` because `store.py` does not exist.

- [ ] **Step 3: Implement containment, locking, and atomic writes**

`resolve_project_path` must reject absolute paths, `..`, symlink components, and resolved paths outside the selected project. Implement a persistent `.lock` file with `fcntl.flock` on POSIX and `msvcrt.locking` on Windows, following the existing `progress_manager.py` approach. Write JSON to a same-directory temporary file, flush, `os.fsync`, and replace atomically; retain a validated `.bak` generation before replacing material state.

```python
def atomic_write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
```

- [ ] **Step 4: Implement selection and isolated paths**

`WorkflowStore.read_selection()` must read `.research/custom-workflow/selection.json` without creating its parent. Missing selection returns `Selection(mode="official")`. A corrupt selection raises `StoreError("selection.invalid", ...)`; it must not fall back. `activate_custom` writes workflow ID, semantic revision, semantic hash, monotonically increasing `selection_revision`, sorted `acknowledged_warning_codes`, `acknowledged_semantic_sha256`, and UTC `acknowledged_at`. The same data is recorded in the activation event. `deactivate_custom` writes an explicit official selection with a new selection revision without deleting custom data.

An acknowledgement is valid only when its semantic hash and exact required
high-risk warning-code set match a fresh compilation. A changed semantic hash,
new warning, removed warning, or corrupt acknowledgement requires a new
activation acknowledgement; no code is inherited by name alone. Add restart
tests proving the recorded acknowledgement remains auditable but cannot be
reused for changed semantics.

`save_draft` is the authority for revisions: it ignores browser-proposed
revision increments, increments `document_revision` on every successful save,
and increments `semantic_revision` only when the canonical behavior payload
(schema, settings, nodes, edges, and bindings, excluding UI and both revision
counters) differs from the previous saved draft. It returns the normalized
saved document. A UI-only save keeps the semantic revision and hash unchanged.
On each new semantic revision, write one immutable
`revisions/{semantic_revision}-{document_sha256}.json` snapshot before updating
`workflow.json`; never overwrite an existing revision path with different
bytes. Refuse activation when another run is active unless it has reached a
terminal archived/stopped state. Regenerate `summary.md` after accepted runtime
transitions and never parse manual edits back into state.

The context manager returned by `locked_run()` acquires the project lock before
loading plan/state and holds it through event append plus snapshot replacement.
`WorkflowTransaction.commit_control_transitions` accepts the ordered records returned by
`stabilize_control_nodes`, appends one hash-linked event per condition/join
transition, and replaces the snapshot once with the final state while holding
the project lock. Recovery must replay the same ordered events to the same
state.

Keep verified `ArtifactRuntime` records inside the event-recoverable run state.
Regenerate `artifacts.json` as a structured projection after accepted result,
stale, and recovery transitions; never use that projection alone to prove
completion. Add a store integration test that registers an artifact, restarts
the store, verifies its bytes/hash, and proves an `artifact_state_is` condition
selects the same branch. If the file bytes change after restart, recovery marks
the artifact and dependent nodes stale before condition stabilization.

- [ ] **Step 5: Implement hash-linked events and snapshots**

Each event contains `event_seq`, `run_id`, `semantic_sha256`, `event_type`, `payload`, `previous_event_hash`, and `event_hash`. Compute `event_hash` from canonical JSON excluding itself. Snapshots contain `last_applied_event_seq` and `last_applied_event_hash`. Append and fsync the event before atomically replacing the snapshot.

Recovery replays only the continuous verified suffix. Preserve a truncated final line under `recovery/events-truncated-{time_ns}.jsonl`, restore only through the last valid event, and block any possibly affected running node. Gaps, conflicting duplicates, or hash mismatches block the run without rewriting evidence.
The custom state codec requires `winner_edge_id`; old pre-release states
without it fail closed. Replay derives the live edge's historical
source/attempt/path/hash witness from an authorized completion event and
compares current bytes at the live boundary, including when the current
artifact registry was overwritten by another branch. This does not alter the
separate official v1.0 storage format.

- [ ] **Step 6: Run focused, platform, and full tests**

Run: `python3 -B -m unittest tests.test_workflow_store -v`
Expected: selection, containment, optimistic concurrency, semantic-versus-UI
revision assignment, immutable revision snapshot, one-active-run protection,
artifact restart/hash-change behavior, generated summary/artifact projection,
atomic backup, event chain, truncated tail, and hash-gap tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass on the current platform.

- [ ] **Step 7: Commit the store**

```bash
git add scripts/workflow_engine/fs.py scripts/workflow_engine/store.py \
  tests/test_workflow_store.py
git commit -m "feat: persist custom workflow state"
```

---

### Task 7: Enforce invocation and result receipts through the manager CLI

Implementation is reviewed in sequential slices: **7A** provides strict task
invocation/result/receipt codecs, task service operations, claim/completion event
authority, receipt projection recovery, and adversarial replay tests; **7B**
adds the approved validator form/adapter contract; **7C** adds stale-rerun,
remaining service operations, and the full JSON CLI. The command examples below
describe the integrated Task 7 target, not the availability of the 7A slice.

Task **7B1** adds the strict GUI-form schema, code-owned property metadata,
canonical document/compiled identity, store codecs, and activation block for an
enabled custom `humanizer-preflight`. Task **7B2** adds the four runnable
adapters, receipt normalization, and an independent direct-run humanizer guard
before subprocess or event writes. The first-release form contract is recorded
in the Task 7 validator-form design supplement.

**Files:**
- Create: `scripts/workflow_engine/receipts.py`
- Create: `scripts/workflow_engine/validators.py`
- Create: `scripts/workflow_manager.py`
- Create: `tests/test_workflow_manager.py`

**Interfaces:**
- Consumes: compiled plan, `WorkflowStore`, installed catalog, claim token, and `node-result-v1`
- Produces: `node-invocation-v1`, `node-result-v1` validation, `stage-receipt-v2`, bounded registered-validator execution, and CLI commands `validate`, `activate`, `ready`, `claim`, `submit-result`, `run-validator`, `register-artifact`, `record-decision`, `record-fact`, `retry`, `summary`, and `deactivate`

- [ ] **Step 1: Write a failing claim-and-submit integration test**

```python
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_engine.receipts import sha256_file
from scripts.workflow_manager import WorkflowManagerError, WorkflowService


class WorkflowManagerTests(unittest.TestCase):
    def test_claim_and_submit_bind_the_current_attempt_and_hash_outputs(self):
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            service = self.make_service(project)
            service.activate(self.document, acknowledged_warning_codes=())
            invocation = service.claim("directions")
            output = project / "idea.md"
            output.write_text("evidence-backed idea", encoding="utf-8")
            result = {
                "schema_version": "node-result-v1",
                "run_id": invocation["run_id"],
                "node_id": "directions",
                "attempt": invocation["attempt"],
                "idempotency_token": invocation["idempotency_token"],
                "status": "succeeded",
                "outcome": "succeeded",
                "summary": "Produced the idea brief.",
                "artifacts": [{"id": "idea_brief", "path": "idea.md"}],
                "uncertainties": [],
            }
            receipt = service.submit_result(result)
            self.assertEqual(receipt["schema_version"], "stage-receipt-v2")
            self.assertEqual(receipt["output_artifacts"][0]["sha256"], sha256_file(output))
            self.assertEqual(service.submit_result(result), receipt)
```

- [ ] **Step 2: Run manager tests and verify import failure**

Run: `python3 -B -m unittest tests.test_workflow_manager -v`
Expected: `ERROR` because `scripts.workflow_manager` does not exist.

- [ ] **Step 3: Implement envelope validation and receipt creation**

```python
INVOCATION_SCHEMA = "node-invocation-v1"
RESULT_SCHEMA = "node-result-v1"
RECEIPT_SCHEMA = "stage-receipt-v2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
```

Validate exact keys, bounded text, current run/node/attempt, declared outcome, token hash, project-contained artifact paths, required logical artifact IDs, and input hashes. Compute every output hash in the manager. Store only the token hash in state and receipts. An exact repeated submission returns the existing receipt; a changed payload for the same token raises `receipt.idempotency_conflict`.

Every `stage-receipt-v2` contains exactly: schema version, workflow ID,
semantic revision/hash, run ID, node ID/type, attempt, resolved Skill or
validator identity, claim token SHA-256, input artifact target/source IDs/paths/hashes, output artifact
IDs/paths/hashes, status, named outcome, start/completion UTC timestamps,
summary, uncertainties, and an error field (`null` or structured `code/message`). Receipt creation and
artifact-registry updates occur in one locked transition; event recovery can
reconstruct the same `ArtifactRuntime` entries from the receipt hashes.

- [ ] **Step 4: Implement `WorkflowService`**

```python
class WorkflowService:
    def __init__(self, project_root: Path, *, skill_roots: tuple[Path, ...] = ()) -> None:
        self.project_root = project_root.resolve()
        self.store = WorkflowStore(self.project_root)
        self.skill_roots = skill_roots

    def ready(self) -> dict[str, object]:
        with self.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
            refreshed = refresh_ready(plan, state)
            stabilized, controls = stabilize_control_nodes(plan, refreshed)
            if controls:
                transaction.commit_control_transitions(controls, stabilized)
        return {
            "status": "pass",
            "ready": [
                {"node_id": node_id, "node_type": plan.nodes[node_id].type}
                for node_id in ready_node_ids(plan, stabilized)
            ],
        }

    def claim(self, node_id: str) -> dict[str, object]:
        token = secrets.token_urlsafe(32)
        with self.store.locked_run() as transaction:
            plan, state = transaction.load_active_run()
            updated = claim_transition(plan, state, node_id, token)
            transaction.commit_transition("node_claimed", updated)
        return build_invocation(plan, updated, node_id, token)
```

Implement `validate_document`, `load_draft`, `save_draft`, `activate`,
`submit_result`, `run_validator`, `register_artifact`, `record_decision`, `record_fact`, `retry`, `summary`, and
`deactivate` through the same store lock and event transition path. `activate`
must reject compiler errors and require the exact set of high-risk warning
codes to be acknowledged for the semantic hash. `save_draft` returns the
store-normalized document and its authoritative revision counters.
After an accepted task result, registered-validator result, registered external
artifact, recorded decision, or recorded fact, refresh readiness and persist every automatic condition/join
transition before returning the response.

- [ ] **Step 5: Run only registered validator adapters**

`validators.py` maps the five fixed adapter IDs to code-owned argument builders.
Each builder accepts only project-contained, hash-verified logical artifacts
declared in the compiled validator node. Launch exactly
`[sys.executable, registered_script, *adapter_arguments]` with `shell=False`, a
project-root working directory, a minimal inherited environment, a 120-second
timeout, and 1 MiB stdout/stderr capture limits. Parse the validator's JSON
result and accept only `pass`, `fail`, or `blocked`; a timeout, crash, malformed
JSON, hash change, or undeclared artifact is a validator execution failure.

`WorkflowService.run_validator(node_id)` atomically claims only a ready
validator node, releases the project lock while the fixed subprocess runs,
then reacquires the lock and verifies the same run/node/attempt before recording its real outcome and
hashed domain receipt in `stage-receipt-v2`, and stabilizes downstream control
nodes. It never accepts a command, script path, or adapter name from the GUI or
workflow document.

- [ ] **Step 6: Add a strict JSON CLI**

Each command prints one JSON object to stdout and returns exit code `0` for a
valid result (including non-blocking risk warnings), `2` for blocking
validation findings or blocked runtime state, and `1` for malformed input or an
unexpected runtime error. Keep human diagnostics inside JSON; do not mix prose
into stdout. `record-decision` accepts only a bounded JSON object with a
decision name/value and provenance summary. `record-fact` accepts only a
registered project-boolean name and a JSON boolean. Both append events; neither
accepts expressions or commands.

`register-artifact` accepts a bounded JSON file containing exactly a logical
artifact ID, project-relative path, and provenance summary. The manager rejects
symlink/traversal paths, computes SHA-256 itself, records producer type
`external`, and appends the registration event. Re-registering an ID with
changed bytes marks dependent completed nodes stale; the caller cannot submit a
trusted hash or verification state. It requires an active custom run and an
artifact ID listed in that workflow's `external_inputs`; it cannot add an
undeclared dependency after activation.

Every command requires `--project`; catalog-dependent commands accept
repeatable `--skills-root` for development/tests before installed/default roots.
`validate` requires `--workflow`, `submit-result` requires `--result`, and
artifact/decision/fact payloads use explicit JSON file arguments. Resolve every input
file under the selected project root and reject symlink/traversal paths.

Use absolute `scripts.workflow_engine` imports. At the top of each directly
executable repository CLI, before those imports, add the repository/installed
Skill root to `sys.path` only when `__package__` is empty:

```python
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```

This supports both `python3 scripts/workflow_manager.py ...` and
`python3 -m scripts.workflow_manager ...` without changing import behavior when
tests import the module. Do not depend on the caller's current directory.

Run example after implementation:

```bash
python3 scripts/workflow_manager.py validate \
  --project . \
  --workflow tests/fixtures/workflow_valid_linear.json
```

Expected JSON keys: `status`, `errors`, `warnings`, `document_sha256`, and
compiled `semantic_sha256` (the latter is `null` when blocking findings prevent
compilation).

- [ ] **Step 7: Run focused and full tests**

Run: `python3 -B -m unittest tests.test_workflow_manager -v`
Expected: draft revision, activation, acknowledgement, control stabilization,
ready, claim, result, registered validator, validator timeout/crash, duplicate,
stale attempt, changed Skill/validator identity blocking, external artifact
registration/hash-change staleness, decision, retry,
summary, and deactivation tests pass.

Add black-box `subprocess.run` tests for direct-file and `-m` invocation of
`validate`, `summary`, `ready`, `claim`, and `submit-result`. Cover success,
blocked validation, malformed JSON, and unknown command. For every case assert
the documented exit code, `json.loads(stdout)` consumes the entire stdout,
stderr contains no protocol data, and running from a directory outside the
repository still resolves imports.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 8: Commit the control protocol**

```bash
git add scripts/workflow_engine/receipts.py scripts/workflow_engine/validators.py \
  scripts/workflow_manager.py \
  tests/test_workflow_manager.py
git commit -m "feat: add custom workflow manager protocol"
```

---

### Task 8: Integrate custom selection with the Orchestrator contract

**Files:**
- Create: `references/custom-workflow-contract.md`
- Modify: `SKILL.md:18-120`
- Modify: `references/stage-contracts.md:1-20`
- Modify: `references/progress-schema.md:1-12`
- Modify: `agents/openai.yaml:1-7`
- Modify: `tests/test_progress_version.py:54-149`
- Create: `tests/test_custom_workflow_contract.py`

**Interfaces:**
- Consumes: `workflow_manager.py summary`, `ready`, `claim`, `submit-result`, `run-validator`, `register-artifact`, `record-decision`, and `record-fact`
- Produces: explicit official/custom startup decision, bounded node invocation steps, and official-only scoping for v1 progress rules

- [ ] **Step 1: Write failing contract tests**

```python
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CustomWorkflowContractTests(unittest.TestCase):
    def test_orchestrator_checks_selection_before_progress(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("workflow_manager.py summary", skill)
        self.assertIn("selection.json is absent or selects official", skill)
        self.assertIn("custom state is the only runtime authority", skill)
        self.assertIn("do not update .research/progress.md", skill)

    def test_custom_contract_names_every_manager_transition(self):
        contract = (ROOT / "references/custom-workflow-contract.md").read_text(encoding="utf-8")
        for command in ("ready", "claim", "submit-result", "run-validator", "register-artifact", "record-decision", "record-fact", "retry", "deactivate"):
            self.assertIn(f"workflow_manager.py {command}", contract)
        self.assertIn("node-invocation-v1", contract)
        self.assertIn("node-result-v1", contract)
        self.assertIn("stage-receipt-v2", contract)
```

- [ ] **Step 2: Run the contract tests and verify they fail**

Run: `python3 -B -m unittest tests.test_custom_workflow_contract -v`
Expected: failures because the custom contract and startup wording do not exist.

- [ ] **Step 3: Write the custom workflow contract**

Document this exact startup table:

```text
selection absent/official -> use existing progress.md path; never read custom state
selection custom and valid -> use state.json + events.jsonl only; never update progress.md/current_stage
selection corrupt/missing target -> blocked; never fall back silently
```

Then document the task sequence `summary -> ready -> claim -> invoke exactly
one Skill -> wrap real result -> submit-result`, the validator sequence `ready
-> run-validator`, and automatic condition/join stabilization. State that
existing project files enter only through `register-artifact`; project booleans
and user decisions enter only through event-recorded `record-fact` and
`record-decision` manager commands. Validator-specific legacy receipts are
referenced by path and hash rather than rewritten.

- [ ] **Step 4: Scope `SKILL.md` and existing references by mode**

At Orchestrator entry, check selection before reading `progress.md`. Add the mode
selector and **Custom workflow mode** material outside an explicit
official-contract marker. Wrap the current `SKILL.md` body beginning at
`# Paper Workflow Orchestrator`, the complete current `stage-contracts.md`, and
the complete current `progress-schema.md` in `OFFICIAL-V1-CONTRACT:BEGIN/END`
markers without changing any byte inside each marked body. Introduce an
external heading/note explaining that those unchanged bodies are authoritative
only in **Official v1.0 mode**. Link custom mode to the new contract and require
manager-mediated transitions. Do not change current stage IDs or
`workflow_version` metadata.

Update the agent description to mention optional custom DAG orchestration without implying that ordinary use requires Studio or configuration.

- [ ] **Step 5: Extend compatibility tests without weakening existing assertions**

Keep `ProgressVersionTests.test_current_workflow_version_is_v10`. Add assertions that release metadata is still `1.0.0` during this plan and that `references/progress-schema.md` explicitly states it is authoritative only for official mode.

Add a helper that extracts bytes strictly between each official marker and
asserts the three SHA-256 values in
`official-v1.0-studio-projection.json`. This is the live drift guard for the
official contract; the historical full-file hashes remain source provenance.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -B -m unittest tests.test_custom_workflow_contract tests.test_progress_version -v`
Expected: all contract and v1 compatibility tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 7: Commit Orchestrator integration**

```bash
git add references/custom-workflow-contract.md SKILL.md \
  references/stage-contracts.md references/progress-schema.md \
  agents/openai.yaml tests/test_progress_version.py \
  tests/test_custom_workflow_contract.py
git commit -m "feat: route explicit custom workflows"
```

---

### Task 9: Prove a complete custom DAG run and official-mode isolation

**Files:**
- Create: `tests/test_custom_workflow_end_to_end.py`
- Modify: `DEVELOPMENT_GUIDE.md:100-190`

**Interfaces:**
- Consumes: all engine and manager interfaces from Tasks 1-8
- Produces: one restart-safe end-to-end test and human developer commands for the engine checkpoint

- [ ] **Step 1: Write the end-to-end test**

```python
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_manager import WorkflowService


class CustomWorkflowEndToEndTests(unittest.TestCase):
    def test_parallel_branch_join_survives_service_restart(self):
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            service = self.make_service(project)
            service.activate(self.branch_document, acknowledged_warning_codes=self.warning_codes)
            self.assertEqual(
                {item["node_id"] for item in service.ready()["ready"]},
                {"literature", "design"},
            )
            for node_id in ("literature", "design"):
                invocation = service.claim(node_id)
                self.submit_text_result(service, invocation, f"{node_id}.md")

            restarted = self.make_service(project)
            ready_after_restart = restarted.ready()["ready"]
            self.assertEqual([item["node_id"] for item in ready_after_restart], ["drafting"])

    def test_official_selection_does_not_create_custom_state(self):
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            service = self.make_service(project)
            summary = service.summary()
            self.assertEqual(summary["mode"], "official")
            self.assertFalse(project.joinpath(".research/custom-workflow").exists())
```

Use an end-to-end fixture with two explicit task entry nodes (`literature` and
`design`), one `all_active` join, and downstream `drafting`. The join advances
through automatic control-node stabilization after restart; it is never claimed
as a Skill task. Test helpers create real installed Skill fixtures and real
result files under the temporary project; do not patch hashing or state
transitions.

- [ ] **Step 2: Run the end-to-end test and fix only integration defects**

Run: `python3 -B -m unittest tests.test_custom_workflow_end_to_end -v`
Expected before integration fixes: failures identifying mismatched interface assumptions.
Expected after fixes: both end-to-end tests pass.

- [ ] **Step 3: Add a human-developer engine section**

Add `## 自定义工作流引擎` to `DEVELOPMENT_GUIDE.md` after the Python test section. Explain component responsibilities, official/custom state isolation, and these exact commands:

```bash
python3 scripts/workflow_manager.py validate --project . --workflow tests/fixtures/workflow_valid_linear.json
python3 scripts/workflow_manager.py summary --project .
python3 -B -m unittest tests.test_custom_workflow_end_to_end -v
```

Keep the guide addressed to human contributors. Do not use “agent should”, prompt-like imperatives, `TODO`, or `TBD`.

- [ ] **Step 4: Run the complete Python suite and static checks**

Run: `python3 -B -m unittest discover -s tests -v`
Expected: every old and new test passes.

Run: `git diff --check`
Expected: no output.

Run: `python3 -m compileall -q scripts tests`
Expected: exit code `0`.

- [ ] **Step 5: Commit the engine checkpoint**

```bash
git add tests/test_custom_workflow_end_to_end.py DEVELOPMENT_GUIDE.md \
  scripts/workflow_engine scripts/workflow_manager.py
git commit -m "test: verify custom workflow engine end to end"
```

The final `git add` deliberately includes engine files only if Task 9 needed integration corrections. Review `git diff --cached --name-only` before committing; it must not include Studio or release files.

---

## Engine-plan completion gate

Run all of the following from the repository root:

```bash
python3 -B -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
git diff --check
git status --short
```

Expected:

- all original and new Python tests pass;
- compilation and whitespace checks return exit code `0`;
- no custom workflow file is created in the repository by official-mode tests;
- `workflow_version` remains `paper-workflow-orchestrator-v1.0`;
- release metadata remains `1.0.0`; and
- the worktree is clean after the final task commit.
