# Workflow Studio and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a local, browser-based Workflow Studio that lets advanced users edit and activate real custom DAG workflows without hand-editing JSON, while ordinary users keep the official v1.0 workflow as the unchanged default.

**Architecture:** A Python 3.10+ standard-library server exposes a narrow token-protected localhost API over the engine completed in `2026-08-31-custom-workflow-engine.md`. A React/TypeScript/Vite frontend uses `@xyflow/react` for the graph editor. The repository commits both frontend source and a verified offline bundle under the Orchestrator Skill, so installed users need Python and a browser but not Node.js.

**Tech Stack:** Python 3.10+ standard library; React 19.2.8; TypeScript 7.0.2; Vite 8.2.2; `@xyflow/react` 12.11.5; Vitest 4.1.11; Testing Library; Playwright 1.62.1; Node.js 22.12+ for development only

**Spec:** `docs/superpowers/specs/2026-08-31-custom-workflow-studio-design.md`

**Prerequisite:** Complete `docs/superpowers/plans/2026-08-31-custom-workflow-engine.md`, confirm its completion gate is green, and begin this plan from a clean worktree.

## Global Constraints

- The official workflow remains selected when custom selection is absent or explicitly `official`; opening Studio alone must not change that selection.
- Studio may save structurally incomplete drafts. Only authoritative Python validation can compile or activate a workflow.
- An activatable task node has exactly one locally installed primary Skill. Studio never downloads, installs, updates, executes, or substitutes a Skill.
- Sequential, conditional, parallel, and join graphs are supported; cycles are never activatable.
- Official control stages may be removed in a custom graph. The UI records warnings and requires acknowledgement for high-risk codes without silently restoring stages.
- Bind only to `127.0.0.1` on a random port. Protect every API call with a high-entropy session token and every state-changing request with exact-origin and CSRF checks.
- Expose no shell runner, arbitrary filesystem browser, URL fetcher, package installer, or user-supplied validator command.
- The committed runtime bundle contains no CDN, telemetry, remote font, or other network dependency.
- Node.js and npm packages are development dependencies only. Installed end users launch Studio through Python 3.10+.
- The project release becomes `1.1.0`, but `workflow_version` remains `paper-workflow-orchestrator-v1.0` and legacy progress files retain their v1.0 identity.
- README changes are limited to one matching advanced-workflow section in each language plus the release/default-workflow badges needed to avoid version ambiguity.
- Do not push, publish, or create a GitHub Release in this plan. A remote push requires a separate final user approval.
- Use TDD for each behavior, run focused tests before the full suite, and commit only the files listed for the current task.

---

## File map

| Path | Responsibility |
| --- | --- |
| `scripts/workflow_engine/studio_server.py` | Authenticated localhost HTTP service, API dispatch, CSP, request limits, idle shutdown |
| `scripts/workflow_studio.py` | End-user launcher, random port/token creation, browser opening, lifecycle |
| `scripts/verify_workflow_studio_bundle.py` | Recompute bundle hashes and reject remote runtime references |
| `tests/test_workflow_studio_server.py` | API, authentication, same-origin, request-limit, containment, and lifecycle tests |
| `tests/test_workflow_studio_bundle.py` | Offline asset manifest and installer-presence tests |
| `studio/package.json` / `studio/package-lock.json` | Exact frontend development dependency lock |
| `studio/vite.config.ts` / `studio/tsconfig*.json` | Deterministic build and TypeScript configuration |
| `studio/src/types.ts` | Workflow, projection, catalog, issue, and API types |
| `studio/src/api.ts` | Session-token bootstrap and typed same-origin API client |
| `studio/src/workflow.ts` | Projection cloning, immutable graph edits, revision classification |
| `studio/src/App.tsx` | Mode, loading, selection, validation, save, activation, and conflict state |
| `studio/src/components/*` | Top bar, node palette, canvas, inspector, outline, validation/risk panels |
| `studio/src/nodes/*` | Task, condition, join, and validator node renderers |
| `studio/src/**/*.test.tsx` | Component and interaction tests |
| `studio/e2e/workflow-studio.spec.ts` | Browser clone/edit/save/reopen/activate/offline flow |
| `studio/playwright.config.ts` | Chromium and WebKit projects against the Python launcher |
| `assets/workflow-studio/` | Versioned offline production bundle and hash manifest |
| `assets/workflow-studio.png` | Real implementation screenshot used by both READMEs |
| `README.md` / `README.zh-CN.md` | One advanced-workflow section in each language |
| `DEVELOPMENT_GUIDE.md` | Human developer build, test, bundle, and architecture guide |
| `CHANGELOG.md`, `SKILL.md`, `dependencies.lock.json` | Coordinated v1.1.0 project release metadata |
| `.github/workflows/tests.yml` | Python matrix, locked frontend build, bundle, and browser checks |

---

### Task 1: Add the authenticated localhost Studio service

**Files:**
- Create: `scripts/workflow_engine/studio_server.py`
- Create: `scripts/workflow_studio.py`
- Create: `tests/test_workflow_studio_server.py`

**Interfaces:**
- Consumes: `WorkflowService`, `WorkflowStore`, installed-Skill catalog, official Studio projection, and prebuilt asset root
- Produces: `StudioConfig`, `StudioApplication`, `create_server(config)`, `serve(config)`, and launcher flags `--project`, repeatable `--skills-root`, `--no-browser`, `--idle-timeout`, and test-only `--port`

- [ ] **Step 1: Write failing authentication and API contract tests**

```python
import http.client
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_engine.studio_server import StudioConfig, create_server


class WorkflowStudioServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.assets = Path(self.temporary.name) / "assets"
        self.project.mkdir()
        self.assets.mkdir()
        self.assets.joinpath("index.html").write_text("<main>studio</main>", encoding="utf-8")
        config = StudioConfig(
            project_root=self.project,
            asset_root=self.assets,
            host="127.0.0.1",
            port=0,
            session_token="s" * 64,
            csrf_token="c" * 64,
            idle_timeout_seconds=30,
            open_browser=False,
        )
        self.server = create_server(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def api(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Authorization": "Bearer " + "s" * 64}
        request_headers.update(headers or {})
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def test_bootstrap_requires_session_token(self):
        status, data = self.api("GET", "/api/bootstrap", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(data["errors"][0]["code"], "http.invalid_session")

    def test_state_change_requires_origin_csrf_and_json(self):
        status, data = self.api("PUT", "/api/workflow", body={})
        self.assertEqual(status, 403)
        self.assertEqual(data["errors"][0]["code"], "http.origin_required")
```

Add focused tests for exact `Host`, exact `Origin`, CSRF, `Content-Type`, body larger than 2 MiB, malformed JSON, unknown routes, path traversal in static URLs, two-tab revision conflicts, idle shutdown, and explicit shutdown. Use a real loopback HTTP server; do not mock request validation.
Also call bootstrap, catalog, projection, and workflow GETs with no prior custom
selection and assert `.research/custom-workflow/` is still absent; an explicit
read-only Studio session must not create execution state.

- [ ] **Step 2: Run the server test and confirm the module is absent**

Run: `python3 -B -m unittest tests.test_workflow_studio_server -v`
Expected: `ERROR` with `ModuleNotFoundError` for `studio_server`.

- [ ] **Step 3: Implement the immutable server configuration and response envelope**

```python
from dataclasses import dataclass
from pathlib import Path


MAX_JSON_BODY = 2 * 1024 * 1024


@dataclass(frozen=True)
class StudioConfig:
    project_root: Path
    asset_root: Path
    host: str = "127.0.0.1"
    port: int = 0
    session_token: str = ""
    csrf_token: str = ""
    idle_timeout_seconds: int = 900
    open_browser: bool = True
    skill_roots: tuple[Path, ...] = ()


def envelope(*, status: str, data=None, errors=(), warnings=(), wrote_files=False):
    return {
        "status": status,
        "data": data,
        "errors": list(errors),
        "warnings": list(warnings),
        "wrote_files": wrote_files,
    }
```

Every issue sent to the browser has stable `code`, `message`, `operation`, `recovery`, `node_id`, and `edge_id` keys. Do not include absolute Skill paths, tokens, tracebacks, or file contents in API errors.
Catalog responses expose catalog ID, display metadata, root-relative path,
content hashes, and locked/ambiguous state, but never an absolute filesystem
path.

- [ ] **Step 4: Implement the exact API surface**

| Method | Route | Write behavior |
| --- | --- | --- |
| `GET` | `/api/bootstrap` | none; returns project label, mode, document revision, CSRF token, and request limit |
| `GET` | `/api/catalog` | none; refreshes recognizable installed Skills and registered validators |
| `GET` | `/api/projection` | none; returns the read-only official projection plus its server-computed canonical SHA-256 |
| `GET` | `/api/workflow` | none; returns saved custom draft or `null` |
| `POST` | `/api/validate` | none; parses and compiles submitted draft, returning errors, warnings, document hash, and compiled semantic hash |
| `POST` | `/api/compile` | none; returns a sanitized topology/ready-plan preview only after validation passes |
| `PUT` | `/api/workflow` | saves a structurally valid draft using `expected_document_revision` |
| `POST` | `/api/activate` | reloads the saved revision, revalidates it, and activates only with exact risk acknowledgements |
| `POST` | `/api/deactivate` | writes explicit official selection without deleting custom data |
| `POST` | `/api/shutdown` | stops this server session after responding |

Do not accept a filesystem path in any route body. `/api/activate` accepts only `workflow_id`, `expected_document_revision`, `semantic_sha256`, and `acknowledged_warning_codes`; it activates the server-loaded draft rather than a browser-supplied replacement.

`GET /api/workflow` is available only because launching Studio is an explicit
advanced-user action; it may inspect a deactivated saved draft while the mode
badge remains official. This does not weaken the ordinary Orchestrator rule:
after official selection, Orchestrator execution never reads that draft or
custom run state.

- [ ] **Step 5: Enforce loopback and browser security**

Require:

- `Host` exactly matching `127.0.0.1:<actual-port>`;
- `Authorization: Bearer <session-token>` on every `/api/` route;
- `Origin: http://127.0.0.1:<actual-port>`, `X-Workflow-CSRF`, and `Content-Type: application/json` on every state-changing request;
- `Content-Length` present and no larger than `MAX_JSON_BODY` for JSON writes;
- resolved static paths contained by `asset_root`, with no symlink component;
- `Cache-Control: no-store` for API and HTML;
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and this CSP:

```text
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none';
base-uri 'none'; frame-ancestors 'none'; form-action 'none'
```

The launch URL uses `#session=<token>`, never a query parameter. The frontend moves it to `sessionStorage` and clears the fragment immediately. Do not log either token.

- [ ] **Step 6: Implement the launcher and lifecycle**

Resolve `--project` and reject a symlinked or missing directory. Resolve assets relative to the installed Orchestrator root. Generate independent 256-bit URL-safe session and CSRF tokens with `secrets.token_urlsafe(32)`. Bind port `0` by default, print one JSON startup record to stdout, open the browser unless `--no-browser`, and shut down after explicit `/api/shutdown`, `KeyboardInterrupt`, or the configured idle timeout. Unit-test `main(argv, asset_root_override=temporary_assets)` with a temporary `index.html`; the first real-bundle launch occurs after Task 2 builds the production assets.

Use the same direct-file bootstrap contract as `workflow_manager.py`: when
`__package__` is empty, add the installed Orchestrator root to `sys.path`, then
use absolute `scripts.workflow_engine` imports. Unit-test the bootstrap through
`main(..., asset_root_override=...)` in Task 1. Do not expose an asset-root CLI
flag, because serving arbitrary frontend code would give that code the session
token. Task 2 adds the direct-file black-box test after the real bundle exists.

- [ ] **Step 7: Run focused and full Python tests**

Run: `python3 -B -m unittest tests.test_workflow_studio_server -v`
Expected: all server/API/security tests pass.

Run: `python3 -B -m unittest discover -s tests -v`
Expected: every engine, legacy, installer, and server test passes.

- [ ] **Step 8: Commit the server boundary**

```bash
git add scripts/workflow_engine/studio_server.py scripts/workflow_studio.py \
  tests/test_workflow_studio_server.py
git commit -m "feat: add local workflow studio server"
```

---

### Task 2: Scaffold the locked frontend and typed API client

**Files:**
- Create: `studio/package.json`
- Create: `studio/package-lock.json`
- Create: `studio/tsconfig.json`
- Create: `studio/tsconfig.app.json`
- Create: `studio/tsconfig.node.json`
- Create: `studio/vite.config.ts`
- Create: `studio/index.html`
- Create: `studio/src/main.tsx`
- Create: `studio/src/styles.css`
- Create: `studio/src/types.ts`
- Create: `studio/src/api.ts`
- Create: `studio/src/test/setup.ts`
- Create: `studio/src/api.test.ts`

**Interfaces:**
- Consumes: the Task 1 API envelope and engine document schema
- Produces: `ApiClient`, `consumeSessionToken()`, typed API methods, and a deterministic Vite build rooted at `assets/workflow-studio/`

- [ ] **Step 1: Write the failing token and API client tests**

```typescript
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, consumeSessionToken } from './api';

describe('Studio API client', () => {
  beforeEach(() => {
    sessionStorage.clear();
    history.replaceState(null, '', '/#session=' + 's'.repeat(64));
  });

  it('consumes the fragment once and removes it from browser history', () => {
    expect(consumeSessionToken()).toBe('s'.repeat(64));
    expect(location.hash).toBe('');
    expect(sessionStorage.getItem('workflow-studio-session')).toBe('s'.repeat(64));
  });

  it('adds bearer, origin-derived API path, and csrf headers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'pass', data: {}, errors: [], warnings: [], wrote_files: false }))
    );
    const client = new ApiClient('token', fetchMock);
    client.setCsrfToken('csrf');
    await client.saveWorkflow({ workflow: {}, expected_document_revision: 1 });
    const [url, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init.headers);
    expect(url).toBe('/api/workflow');
    expect(init.method).toBe('PUT');
    expect(headers.get('Authorization')).toBe('Bearer token');
    expect(headers.get('X-Workflow-CSRF')).toBe('csrf');
    expect(headers.get('Content-Type')).toBe('application/json');
  });
});
```

- [ ] **Step 2: Create exact package metadata and lock it**

Use package name `paper-workflow-studio`, component version `1.1.0`,
`private: true`, `type: "module"`, and Node engine `>=22.12.0`. Define
`dev: "vite"`, `typecheck: "tsc -b --pretty false"`, `test: "vitest"`,
`build: "vite build"`, and `e2e: "playwright test"`. Task 6 adds the license
and manifest generators after those files exist. Add exact direct versions:

```json
{
  "dependencies": {
    "@xyflow/react": "12.11.5",
    "react": "19.2.8",
    "react-dom": "19.2.8"
  },
  "devDependencies": {
    "@playwright/test": "1.62.1",
    "@testing-library/jest-dom": "7.0.1",
    "@testing-library/react": "16.3.3",
    "@testing-library/user-event": "14.6.6",
    "@types/node": "22.20.1",
    "@types/react": "19.2.18",
    "@types/react-dom": "19.2.5",
    "@vitejs/plugin-react": "6.1.1",
    "jsdom": "30.0.1",
    "typescript": "7.0.2",
    "vite": "8.2.2",
    "vitest": "4.1.11"
  }
}
```

Run from `studio/`: `npm install --package-lock-only --ignore-scripts`
Expected: `package-lock.json` is created with `lockfileVersion: 3` and no unpinned direct dependency.

- [ ] **Step 3: Configure deterministic output and tests**

Set Vite `base: './'`, `build.outDir: '../assets/workflow-studio'`, `emptyOutDir: true`, and disable sourcemaps in the committed runtime bundle. Configure Vitest with `jsdom`, `src/test/setup.ts`, and stable `ResizeObserver`/`matchMedia` stubs needed by React Flow. TypeScript uses strict mode and no emit.

- [ ] **Step 4: Define exact shared browser types**

Mirror `paper-workflow-custom-v1` without weakening it to `any`. Include
discriminated node types `task`, `condition`, `join`, and `validator`; nullable
draft bindings and `origin_projection_node_id` provenance; inputs, outputs, declared outcomes, write scopes, condition
AST, join mode, and workflow-level declared `external_inputs`; projection nodes; catalog entries; `WorkflowIssue`;
`ValidationResult`; and the uniform API envelope. Keep UI position fields
separate from semantic fields.

- [ ] **Step 5: Implement the same-origin client**

`ApiClient` uses relative `/api/...` URLs only, sends the bearer token on every request, adds CSRF and JSON headers only where required, rejects a non-JSON response, maps `409` to a revision-conflict result, and never retries a state-changing request automatically.

- [ ] **Step 6: Run frontend foundation checks**

Run from `studio/`:

```bash
npm ci --ignore-scripts
npm run typecheck
npm test -- --run
npm run build
```

Expected: typecheck and tests pass; `assets/workflow-studio/index.html` and local JS/CSS assets are produced; built HTML contains no CDN URL.

Then run from the repository root:

```bash
python3 scripts/workflow_studio.py --project . --no-browser --idle-timeout 2
```

Expected: one JSON object containing `status: "ready"`, a tokenized loopback URL, and the project path; the process exits after the idle timeout without a traceback.

Repeat the command from a temporary working directory using the absolute path
to `scripts/workflow_studio.py`; assert it resolves imports/assets without
`PYTHONPATH`, stdout is one parseable JSON object, and stderr has no traceback.

- [ ] **Step 7: Commit the frontend foundation and first bundle**

```bash
git add studio/package.json studio/package-lock.json studio/tsconfig.json \
  studio/tsconfig.app.json studio/tsconfig.node.json studio/vite.config.ts \
  studio/index.html studio/src/main.tsx studio/src/styles.css studio/src/types.ts \
  studio/src/api.ts studio/src/test/setup.ts studio/src/api.test.ts \
  assets/workflow-studio
git commit -m "build: scaffold workflow studio frontend"
```

---

### Task 3: Build the accessible graph editor

**Files:**
- Create: `studio/src/workflow.ts`
- Create: `studio/src/workflow.test.ts`
- Create: `studio/src/App.tsx`
- Create: `studio/src/App.test.tsx`
- Create: `studio/src/components/StudioHeader.tsx`
- Create: `studio/src/components/NodePalette.tsx`
- Create: `studio/src/components/WorkflowCanvas.tsx`
- Create: `studio/src/components/NodeInspector.tsx`
- Create: `studio/src/components/GraphOutline.tsx`
- Create: `studio/src/components/ValidationPanel.tsx`
- Create: `studio/src/nodes/TaskNode.tsx`
- Create: `studio/src/nodes/ConditionNode.tsx`
- Create: `studio/src/nodes/JoinNode.tsx`
- Create: `studio/src/nodes/ValidatorNode.tsx`
- Modify: `studio/src/styles.css`

**Interfaces:**
- Consumes: bootstrap, catalog, projection, and saved draft responses
- Produces: official read-only view, blank/custom copy creation, immutable graph operations, four node editors, mouse connection, keyboard connection, and textual graph outline

- [ ] **Step 1: Write failing projection-clone and immutable edit tests**

```typescript
import { describe, expect, it } from 'vitest';
import { cloneProjection, deleteNode, moveNode } from './workflow';

describe('workflow graph operations', () => {
  it('creates an incomplete saveable draft without inventing missing skills', () => {
    const draft = cloneProjection(projection, catalog, 'my-paper-flow');
    expect(draft.derived_from?.projection_id).toBe('official-v1.0');
    expect(draft.nodes.find((node) => node.id === 'intake')?.skill_ref).toBeNull();
    expect(draft.document_revision).toBe(0);
  });

  it('moving a node changes UI only', () => {
    const moved = moveNode(document, 'directions', { x: 640, y: 320 });
    expect(moved.semantic_revision).toBe(document.semantic_revision);
    expect(moved.ui.positions.directions).toEqual({ x: 640, y: 320 });
  });

  it('deleting a node also deletes incident edges', () => {
    const edited = deleteNode(document, 'directions');
    expect(edited.edges.some((edge) => edge.source === 'directions' || edge.target === 'directions')).toBe(false);
  });
});
```

- [ ] **Step 2: Implement projection conversion without claiming official equivalence**

Create one custom draft node per projection node. Convert projection `task`,
`orchestrator`, and `delivery` nodes to custom task nodes; bind a suggested
Skill only when exactly one suggestion is installed and unambiguous, otherwise
use `skill_ref: null`. Give cloned tasks the single declared outcome
`succeeded`, copy projected logical outputs, and initialize `write_scopes` from
those outputs plus any projected protected scope. Convert projected gates to
validators with `validator_ref: null` unless the projection names exactly one
registered validator. Set each clone's `origin_projection_node_id`, preserve
source projection ID/hash in `derived_from`,
copy acyclic edges, place every projected input without an upstream producer in
workflow `external_inputs`, and surface every projection note in the validation
area.

Do not render or accept an editable control-tag field. Control coverage and
risk warnings come from backend-verified projection provenance and the fixed
validator registry, never from user-authored labels.

The clone is an editable starting point, not an executable copy of official mode. Label it that way in both the UI and tests.

- [ ] **Step 3: Implement immutable graph operations**

Add pure functions for add, duplicate, disable/enable, delete, move, connect,
disconnect, insert-before, insert-after, update properties, and create blank workflow. `insert-before`/`insert-after` are available only for a selected node on a simple one-predecessor/one-successor segment and rewire that segment atomically; general DAG reordering uses explicit disconnect/connect controls. Classify each edit as
`semantic` or `visual`; keep the last server-issued `document_revision` and
`semantic_revision` unchanged in the browser until save. Every operation
increments an in-memory dirty generation. The server assigns the new document
revision and increments semantic revision only when its canonical behavior
payload changed; the client replaces both counters with the saved response.

- [ ] **Step 4: Build the approved desktop layout**

Render:

- top bar: project, mode badge, **Copy as custom workflow**, **New blank workflow**, **Validate**, **Save draft**, and **Validate and activate**;
- left: searchable categories for task, condition, join, validator, and installed Skills;
- center: React Flow canvas with MiniMap, controls, background, connection handles, selection, and disabled styling;
- right: selected-node properties, Skill/validator selection, inputs/outputs, failure policy, condition cases, join mode, and connection form;
- bottom/persistent area: validation errors, risk warnings, projection notes, save state, and recovery guidance.

Expose declared external input IDs as a validated workflow-settings tag control;
the Studio does not browse or register files. After activation, the
Orchestrator uses the manager's project-contained `register-artifact` command
for user-supplied files.

Official mode is visibly read-only. Merely dragging or clicking in official mode must not create a draft or selection file.

Condition editing uses form controls only: fact source, named node/artifact/
decision/fact, allowed comparator, typed value, `all`/`any` group, `not`, named
outcome, and explicit default edge. Do not expose a raw AST, JSON textarea,
JavaScript expression, template, or command field. Inputs, outputs, outcomes,
and write scopes use validated tag controls rather than a JSON editor.

- [ ] **Step 5: Make keyboard editing a complete alternative**

`GraphOutline` exposes every node as a labelled button with status, type, and outgoing targets. The inspector provides labelled source/target/trigger controls and **Add connection**/**Remove connection** actions. Add keyboard-accessible buttons for **Insert before**, **Insert after**, duplicate, disable, delete, and position nudge. Preserve focus after edits and announce validation/save results through `aria-live`.

- [ ] **Step 6: Test the UI at user-visible boundaries**

Use Testing Library roles and visible labels. Cover official read-only mode,
clone confirmation, blank workflow, add/delete/duplicate/disable,
insert-before/after on a linear segment, Skill selection, condition-builder and
join fields, keyboard connection, dirty indicator, textual outline, and reload
prompt. Do not assert React Flow implementation classes.

- [ ] **Step 7: Run type, component, build, and Python regression checks**

Run from `studio/`:

```bash
npm run typecheck
npm test -- --run
npm run build
```

Run from repository root: `python3 -B -m unittest discover -s tests -v`
Expected: all checks pass and the rebuilt bundle contains the editor.

- [ ] **Step 8: Commit the editor**

```bash
git add studio/src assets/workflow-studio
git commit -m "feat: add graphical workflow editor"
```

---

### Task 4: Add validation, draft, activation, and conflict UX

**Files:**
- Modify: `studio/src/App.tsx`
- Modify: `studio/src/App.test.tsx`
- Modify: `studio/src/components/StudioHeader.tsx`
- Modify: `studio/src/components/ValidationPanel.tsx`
- Create: `studio/src/components/RiskAcknowledgementDialog.tsx`
- Create: `studio/src/components/RevisionConflictDialog.tsx`
- Modify: `studio/src/styles.css`

**Interfaces:**
- Consumes: server validation findings, semantic hash, saved revision, activation response, and `409` revision conflicts
- Produces: debounced advisory validation, explicit save, exact high-risk acknowledgement, activation, deactivation, and conflict recovery

- [ ] **Step 1: Write failing activation and conflict interaction tests**

```typescript
it('requires every returned high-risk code before activation', async () => {
  render(<App api={apiWithRiskWarnings} />);
  await user.click(screen.getByRole('button', { name: /validate and activate/i }));
  expect(screen.getByRole('dialog', { name: /risk acknowledgement/i })).toBeVisible();
  expect(screen.getByRole('button', { name: /^activate$/i })).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: /integrity control removed/i }));
  expect(screen.getByRole('button', { name: /^activate$/i })).toBeEnabled();
});

it('never overwrites after a document revision conflict', async () => {
  render(<App api={apiReturningConflict} />);
  await user.click(screen.getByRole('button', { name: /save draft/i }));
  expect(screen.getByRole('dialog', { name: /newer draft exists/i })).toBeVisible();
  expect(apiReturningConflict.saveWorkflow).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Separate advisory validation from authoritative activation**

Run local structural hints after 300 ms of idle editing, but label them advisory.
**Validate** always calls `/api/validate`. **Validate and activate** first saves
the current draft, validates the returned saved revision, then opens
acknowledgement only when no blocking error remains. Activation sends the exact
saved revision, compiled semantic hash (including resolved Skill/validator
identities), and selected warning codes; a changed graph or capability identity
invalidates prior acknowledgement.

- [ ] **Step 3: Implement risk acknowledgement without forced repair**

List each high-risk warning with code, affected node/control, consequence, and recovery. Require one unchecked-by-default checkbox per required code. Offer **Back to editing** and **Activate this custom workflow**. Do not add official stages, rewrite the graph, pre-check boxes, or collapse multiple warnings into one consent.

- [ ] **Step 4: Implement optimistic-concurrency recovery**

On `409`, disable further save/activation attempts until the conflict is
resolved and present exactly three non-destructive choices: load the newer
server draft, download the current browser draft as a local JSON backup, or
keep the browser draft open without saving. v1.1.0 has no background autosave
and no overwrite button. The download is generated entirely from the current
in-memory document and does not call a server filesystem endpoint.

- [ ] **Step 5: Implement activation/deactivation status**

After activation, show workflow name, semantic revision, and shortened hash. Deactivation requires an explicit confirmation and returns to official mode while leaving the saved custom draft available. Activation or deactivation errors keep the prior mode and show whether a file was written.

- [ ] **Step 6: Run focused and complete frontend checks**

Run from `studio/`:

```bash
npm run typecheck
npm test -- --run
npm run build
```

Expected: all tests pass and the committed bundle reflects the activation UX.

- [ ] **Step 7: Commit validation and activation UX**

```bash
git add studio/src assets/workflow-studio
git commit -m "feat: validate and activate custom workflows"
```

---

### Task 5: Prove the browser flow in Chromium and WebKit

**Files:**
- Create: `studio/playwright.config.ts`
- Create: `studio/e2e/workflow-studio.spec.ts`
- Create: `studio/e2e/fixtures.ts`
- Modify: `studio/package.json`
- Modify: `studio/package-lock.json`

**Interfaces:**
- Consumes: production frontend bundle and real Python Studio server
- Produces: isolated test project launcher, Chromium/WebKit projects, screenshots, and offline-network assertions

- [ ] **Step 1: Add a failing real-browser happy-path test**

The fixture creates a temporary project and temporary installed-Skill root with real `SKILL.md` files, launches `python3 scripts/workflow_studio.py --project <temp> --skills-root <temp-skills> --no-browser --idle-timeout 120`, parses its one-line startup JSON, and terminates it during teardown.

The first test performs this exact sequence through visible controls:

1. open the tokenized loopback URL;
2. verify **Official workflow v1.0 — read only**;
3. copy as custom workflow;
4. add one task and choose an installed Skill;
5. add a connection through the keyboard-accessible inspector;
6. validate and save;
7. reload and verify the graph persists;
8. review all returned high-risk warnings;
9. acknowledge each code and activate;
10. verify the header reports custom mode and a semantic hash.

- [ ] **Step 2: Add conflict, keyboard, and offline tests**

Cover two tabs saving the same revision, full edit flow without drag-and-drop, deactivation, and explicit shutdown. Register a browser request handler that aborts any request whose host is not `127.0.0.1`; assert the attempted external-request list remains empty. Verify scripts, styles, fonts, images, and API calls are all loopback resources.

- [ ] **Step 3: Configure two browser projects**

Configure desktop Chromium and desktop WebKit with screenshots retained on failure, trace on first retry, one worker for server isolation, and no remote web server. The Python fixture owns the service lifecycle.

- [ ] **Step 4: Run browser tests**

From `studio/`:

```bash
npx playwright install chromium webkit
npm run e2e -- --project=chromium
npm run e2e -- --project=webkit
```

Expected: both projects pass; no request escapes loopback; shutdown leaves no Python listener.

- [ ] **Step 5: Commit browser coverage**

```bash
git add studio/playwright.config.ts studio/e2e studio/package.json studio/package-lock.json
git commit -m "test: cover workflow studio in browsers"
```

---

### Task 6: Make the prebuilt bundle reproducible and installable

**Files:**
- Create: `studio/scripts/write-license-inventory.mjs`
- Create: `studio/scripts/write-bundle-manifest.mjs`
- Modify: `studio/package.json`
- Modify: `studio/package-lock.json`
- Create: `scripts/verify_workflow_studio_bundle.py`
- Create: `tests/test_workflow_studio_bundle.py`
- Modify: `tests/test_install_workflow.py`
- Modify: `.github/workflows/tests.yml`
- Regenerate: `assets/workflow-studio/`

**Interfaces:**
- Consumes: locked npm tree and Vite output
- Produces: deterministic `THIRD_PARTY_LICENSES.json`, `bundle-manifest.json`, offline verifier, installed bundle in every profile, and CI jobs

- [ ] **Step 1: Write failing bundle and installation tests**

```python
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.verify_workflow_studio_bundle import verify_bundle


ROOT = Path(__file__).resolve().parents[1]


class WorkflowStudioBundleTests(unittest.TestCase):
    def test_committed_bundle_hashes_and_offline_policy_pass(self):
        result = verify_bundle(ROOT / "assets/workflow-studio")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["errors"], [])

    def test_core_install_contains_python_launcher_and_bundle(self):
        from scripts.install_workflow import install, load_manifest
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "skills"
            install(load_manifest(ROOT / "dependencies.lock.json"), "core", target, ROOT)
            installed = target / "paper-workflow-orchestrator"
            self.assertTrue(installed.joinpath("scripts/workflow_studio.py").is_file())
            self.assertTrue(installed.joinpath("assets/workflow-studio/index.html").is_file())
            catalog = self.discover_installed_catalog(target)
            self.assertTrue(catalog.skills["paper-workflow-orchestrator"].locked)
```

- [ ] **Step 2: Generate a deterministic license inventory and bundle manifest**

`write-license-inventory.mjs` reads only `package-lock.json` plus installed package metadata, sorts entries by package name/version, and writes name, version, license expression, homepage, and repository. It fails if a production dependency has no declared license.

`write-bundle-manifest.mjs` hashes every runtime file except `bundle-manifest.json`, uses slash-separated relative paths, sorts keys, and writes schema `workflow-studio-bundle-v1`, release version, and SHA-256 values. Neither generated file contains timestamps or machine paths.

Define `npm run build` as Vite build followed by both deterministic generators.

- [ ] **Step 3: Implement the Python bundle verifier**

`verify_bundle(path)` validates exact manifest keys, contained regular files, no
symlinks, no missing/unlisted runtime files, and every hash. Parse HTML resource
attributes, CSS `url()`/`@import`, SVG resource links, and JSON runtime
configuration; require relative or `data:` resources and reject source-map,
remote font, telemetry, protocol-relative, HTTP(S), and WebSocket targets. In
JavaScript, reject absolute/protocol-relative arguments to `fetch`,
`WebSocket`, `EventSource`, dynamic `import`, and DOM resource assignment rather
than rejecting harmless attribution/help URL strings embedded by a dependency.
Exclude `THIRD_PARTY_LICENSES.json` attribution fields from runtime-resource
checks. The network-blocked browser test remains the authoritative proof for
the exercised runtime. Require CSP-compatible relative asset references.

CLI command:

```bash
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
```

Expected: one JSON object with `status: "pass"`, file count, aggregate hash, and no errors.

- [ ] **Step 4: Prove every installation profile carries the same Orchestrator bundle**

The repository source materializer already copies `assets/`, `references/`, and `scripts/`; keep that allowlist explicit. Extend installer tests to assert the core installed tree contains the launcher, engine, projection, and bundle. Add a manifest-level test proving `paper-workflow-orchestrator` is present in core, standard, and full, so all profiles receive the same Studio runtime without copying `studio/` development sources.

- [ ] **Step 5: Add locked frontend and browser CI jobs**

Keep the existing Python OS/Python matrix. Add:

- `studio-unit`: Ubuntu, Node `22.12.0`, `npm ci --ignore-scripts`, typecheck, Vitest, production build, Python bundle verification, and `git diff --exit-code -- assets/workflow-studio`;
- `studio-browser`: Ubuntu, Python 3.13, Node `22.12.0`, `npm ci --ignore-scripts`, Playwright Chromium install, and the Chromium end-to-end suite.

Pin official GitHub Action major versions consistently with the existing workflow. Do not download browsers or npm packages in end-user install tests.

- [ ] **Step 6: Rebuild and run all bundle/install checks**

```bash
cd studio
npm ci --ignore-scripts
npm run typecheck
npm test -- --run
npm run build
cd ..
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
python3 -B -m unittest tests.test_workflow_studio_bundle tests.test_install_workflow -v
python3 -B -m unittest discover -s tests -v
git diff --check
```

Expected: all checks pass; `git status --short` shows only the intentional Task 6 files and rebuilt bundle.

- [ ] **Step 7: Commit reproducible packaging**

```bash
git add studio/scripts studio/package.json studio/package-lock.json \
  scripts/verify_workflow_studio_bundle.py tests/test_workflow_studio_bundle.py \
  tests/test_install_workflow.py .github/workflows/tests.yml assets/workflow-studio
git commit -m "build: package workflow studio offline"
```

---

### Task 7: Document the advanced entry and release v1.1.0 metadata

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `DEVELOPMENT_GUIDE.md`
- Modify: `CHANGELOG.md`
- Modify: `SKILL.md`
- Modify: `dependencies.lock.json`
- Modify: `scripts/install_workflow.py`
- Modify: `tests/test_progress_version.py`
- Modify: `tests/test_install_workflow.py`
- Create: `assets/workflow-studio.png`

**Interfaces:**
- Consumes: completed UI, installed launch path, release/default-workflow identity rules
- Produces: bilingual advanced section, human developer documentation, real screenshot, release `1.1.0`, and decoupled release/workflow version validation

- [ ] **Step 1: Write failing version and README-positioning tests**

Extend tests to require:

```python
self.assertEqual(skill_metadata["version"], "1.1.0")
self.assertEqual(manifest["release_version"], "1.1.0")
self.assertEqual(manifest["workflow_version"], "paper-workflow-orchestrator-v1.0")
```

For each README, assert exactly one advanced-workflow heading, the launch command `python3 scripts/workflow_studio.py --project .`, an explicit statement that official v1.0 remains default, a statement that JSON editing is unnecessary, and the real screenshot path. Preserve the existing Router Skills framing and subtitle.

- [ ] **Step 2: Decouple project release validation from workflow identity**

Change `_validate_release_metadata` so it validates both fields independently rather than deriving `workflow_version` from the release major/minor. Add a regression test proving `{release_version: "1.1.0", workflow_version: "paper-workflow-orchestrator-v1.0"}` is accepted and malformed values are still rejected. Installation receipts continue to record both fields unchanged.

- [ ] **Step 3: Capture a real implementation screenshot**

Launch the production bundle with the Playwright fixture, load the official projection, copy it to a custom draft, select a representative node, and capture the approved full desktop layout at 1440×900. Store the optimized PNG at `assets/workflow-studio.png`. Inspect the image at original resolution before committing; it must show the top mode bar, node library, graph canvas, inspector, and validation area with no temporary path, token, personal data, or browser debug chrome.

- [ ] **Step 4: Add one advanced section to each README**

Place the section after the default quick start and before deep architecture details. Keep it compact:

1. official v1.0 is the ready-to-use default;
2. advanced users launch Studio with one command;
3. copy or start blank, arrange nodes, select installed Skills, validate, review risks, activate;
4. Studio supports sequence, condition, parallel, and join and requires no JSON editing;
5. Studio never installs Skills and the end-user runtime needs no Node.js.

Use the same screenshot in both languages. Do not repeat the whole custom-engine contract or turn the README into frontend developer documentation.

- [ ] **Step 5: Rewrite the developer section for human contributors**

Document repository components, Python environment, Node `>=22.12.0`, locked install, typecheck/unit/build/E2E commands, bundle verification, server security boundary, how release and workflow versions differ, and how to update the screenshot. Use explanatory prose addressed to a human contributor. Avoid prompt-style language such as “the agent must” and retain the existing tests for developer-facing tone.

- [ ] **Step 6: Update release metadata and changelog**

- set `SKILL.md` package metadata version to `1.1.0`;
- set `dependencies.lock.json.release_version` to `1.1.0`;
- keep `dependencies.lock.json.workflow_version` and official progress schema at v1.0;
- add a `1.1.0` changelog entry for optional Studio, DAG runtime, official-mode compatibility, offline bundle, and security model;
- distinguish README badges as **Release v1.1.0** and **Default workflow v1.0** so the earlier “formal v1.0” status is not misrepresented.

- [ ] **Step 7: Run documentation, version, and full regression checks**

```bash
python3 -B -m unittest tests.test_progress_version tests.test_install_workflow -v
python3 -B -m unittest discover -s tests -v
cd studio
npm run typecheck
npm test -- --run
npm run build
cd ..
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
git diff --check
```

Expected: all checks pass; the workflow identity remains v1.0 while project release metadata is v1.1.0.

- [ ] **Step 8: Commit documentation and release metadata**

```bash
git add README.md README.zh-CN.md DEVELOPMENT_GUIDE.md CHANGELOG.md SKILL.md \
  dependencies.lock.json scripts/install_workflow.py tests/test_progress_version.py \
  tests/test_install_workflow.py assets/workflow-studio.png assets/workflow-studio
git commit -m "docs: release optional workflow studio"
```

---

### Task 8: Run release-candidate review and downloadable-install verification

**Files:**
- Create: `docs/release-verification-v1.1.0.md`
- Modify only if a verified defect requires a fix: files owned by Tasks 1-7 and their tests

**Interfaces:**
- Consumes: clean release-candidate commit
- Produces: reproducible evidence for code quality, security, compatibility, UI/readability, local archive installation, and offline launch

- [ ] **Step 1: Establish a clean release-candidate baseline**

```bash
git status --short
git rev-parse HEAD
python3 -B -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
cd studio
npm ci --ignore-scripts
npm run typecheck
npm test -- --run
npm run build
npm run e2e -- --project=chromium
npm run e2e -- --project=webkit
cd ..
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
git diff --exit-code -- assets/workflow-studio
git diff --check
```

Expected: the initial and final worktree are clean and every command exits `0`.

- [ ] **Step 2: Request independent subagent reviews**

Run three read-only reviews against the same commit:

1. engine/official-v1 compatibility and receipt/state recovery;
2. localhost API, path containment, token/CSRF/CSP, frontend supply chain, and offline policy;
3. UI usability, keyboard access, bilingual README readability, developer-guide audience, installer/archive usability.

Each reviewer reports evidence-backed findings with severity and exact file/line references. Resolve every confirmed high or medium issue with a focused failing test and a separate fix commit, then rerun Step 1. Record rejected findings and technical reasons in the verification document.

- [ ] **Step 3: Verify a local downloadable archive before any push**

Create an isolated task-specific directory, then:

```bash
studio_verify_root="$(mktemp -d)"
studio_python="$(command -v python3)"
mkdir -p "$studio_verify_root/archive" "$studio_verify_root/project"
git archive --format=zip --output "$studio_verify_root/codex-paper-workflow-v1.1.0.zip" HEAD
ditto -x -k "$studio_verify_root/codex-paper-workflow-v1.1.0.zip" "$studio_verify_root/archive"
(cd "$studio_verify_root/archive" && "$studio_python" -B -m unittest discover -s tests -v)
"$studio_python" "$studio_verify_root/archive/scripts/install_workflow.py" \
  --manifest "$studio_verify_root/archive/dependencies.lock.json" \
  --profile core \
  --target "$studio_verify_root/codex-home/skills"
"$studio_python" "$studio_verify_root/archive/scripts/install_workflow.py" \
  --verify \
  --manifest "$studio_verify_root/archive/dependencies.lock.json" \
  --profile core \
  --target "$studio_verify_root/codex-home/skills"
"$studio_python" "$studio_verify_root/codex-home/skills/paper-workflow-orchestrator/scripts/verify_workflow_studio_bundle.py" \
  "$studio_verify_root/codex-home/skills/paper-workflow-orchestrator/assets/workflow-studio"
cp "$studio_verify_root/archive/tests/fixtures/workflow_valid_installed_core.json" \
  "$studio_verify_root/project/workflow.json"
env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  "$studio_python" "$studio_verify_root/codex-home/skills/paper-workflow-orchestrator/scripts/workflow_manager.py" \
  summary --project "$studio_verify_root/project"
env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  "$studio_python" "$studio_verify_root/codex-home/skills/paper-workflow-orchestrator/scripts/workflow_manager.py" \
  validate --project "$studio_verify_root/project" \
  --workflow "$studio_verify_root/project/workflow.json" \
  --skills-root "$studio_verify_root/codex-home/skills"
env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  "$studio_python" "$studio_verify_root/codex-home/skills/paper-workflow-orchestrator/scripts/workflow_studio.py" \
  --project "$studio_verify_root/project" \
  --no-browser \
  --idle-timeout 2
```

The sanitized PATH contains system utilities but not Homebrew/npm locations;
both manager commands must emit one valid JSON object with exit code `0`, and
the launcher must still reach `status: "ready"` and exit cleanly. Record the archive
SHA-256, HEAD commit, interpreter version, and command outcomes. Do not remove
the temporary directory until the verification record has been written and its
paths have been redacted.

- [ ] **Step 4: Verify standard and full profile downloads in isolated targets**

From the extracted archive, install `standard` and `full` into separate temporary targets using the real pinned dependency URLs. For each target run installer `--verify`, confirm the same Orchestrator bundle manifest passes, confirm only locally installed Skills appear in `/api/catalog`, and launch Studio with Node/npm absent from PATH. Network is permitted only during pinned dependency download; Studio runtime verification then runs with browser external requests blocked.

- [ ] **Step 5: Inspect the final UI and documentation**

Render the real production UI at 1440×900 in Chromium and WebKit, then run a
manual localhost smoke in the installed macOS Google Chrome and Safari apps.
Inspect official read-only mode, custom edit mode, validation errors, risk
acknowledgement, revision conflict, and narrow layout. Confirm both READMEs
point to the final screenshot and contain only one advanced section. The user
reviews the implemented UI and both README sections before publishing approval
is requested.

- [ ] **Step 6: Write the verification record**

`docs/release-verification-v1.1.0.md` records:

- exact commit and date;
- Python/Node/npm/browser versions used;
- Python, frontend, E2E, bundle, and archive commands with pass/fail status;
- archive SHA-256 and install-profile outcomes;
- independent review findings and dispositions;
- known limitations that are real and reproducible;
- a clear statement that no GitHub Release or remote push occurred.

Do not include session tokens, temporary absolute paths, credentials, or copied dependency source.

- [ ] **Step 7: Commit only the verification record and verified fixes**

```bash
git add docs/release-verification-v1.1.0.md
git diff --cached --check
git diff --cached --name-only
git commit -m "test: verify workflow studio release candidate"
```

If defect fixes were necessary, commit each fix with its regression test before the verification-record commit. End with a clean worktree.

- [ ] **Step 8: Stop for the separate publishing decision**

Present the final commit, test summary, archive hash, screenshot, README sections, and review dispositions to the user. Do not push. If the user later explicitly approves a push, push that exact reviewed commit without creating a GitHub Release, then download the GitHub archive for that commit into a fresh temporary directory and repeat the core install, bundle verification, and no-Node Studio launch. Report any remote/archive mismatch before claiming publication success.

---

## Studio-plan completion gate

Run from the repository root:

```bash
python3 -B -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
cd studio
npm ci --ignore-scripts
npm run typecheck
npm test -- --run
npm run build
npm run e2e -- --project=chromium
npm run e2e -- --project=webkit
cd ..
python3 scripts/verify_workflow_studio_bundle.py assets/workflow-studio
git diff --exit-code -- assets/workflow-studio
git diff --check
git status --short
```

Expected:

- all legacy, engine, server, frontend, and browser tests pass;
- the production bundle exactly matches a clean locked build and contains no remote runtime dependency;
- a core, standard, and full installation contains the launcher and the same verified bundle;
- Studio launches from an installed archive with Python 3.10+ and no Node.js runtime;
- opening Studio in official mode neither creates custom state nor changes selection;
- project release metadata is `1.1.0`, official workflow identity remains `paper-workflow-orchestrator-v1.0`;
- no GitHub Release exists and no push has occurred; and
- the worktree is clean at the reviewed release-candidate commit.
