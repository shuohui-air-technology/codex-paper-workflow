# v1.1.0 Local Release-Candidate Verification

Date: 2026-09-27

Verified code commit: `97b127f68728ab549699e69dc41ac42ca328e5cc`

Platform: macOS 26.5.2, arm64; Python 3.14.6

Frontend build tools: Node.js 26.0.0, npm 12.0.2

## Archive and integrity

A clean ZIP archive was created with `git archive` from the verified code commit,
extracted to a new temporary directory, and tested from that extracted copy.

- Local archive: `codex-paper-workflow-97b127f.zip`
- SHA-256: `6fec2ba6dc6e7e55a58b1410ef30b2a1bccb40147f6e7be9345467824f13a1e8`
- Python suite from the extracted archive: 458 tests passed.
- Python bytecode compilation: passed.
- Offline Studio bundle verifier: passed; 4 runtime files, aggregate SHA-256
  `3aa1e15f58bd67fff5d4ba0b0e729253f2777726c247f3de6d432f1e02acc8d3`.

## Installation and runtime

The clean archive installed and verified all three installer profiles in
isolated temporary skill roots:

- `core`: 3 Skills; install and verification passed.
- `standard`: 14 Skills; install and verification passed.
- `full`: 16 Skills; install and verification passed.

External dependencies in the standard and full profiles were fetched from the
full commit SHAs recorded in `dependencies.lock.json`. The installer completed
its archive checks and installation verification successfully.

Workflow Studio was launched from the extracted archive with a restricted
runtime `PATH` containing no Node.js or npm. The static index, production
JavaScript bundle, and authenticated catalog endpoint each returned HTTP 200.
The standard and full catalogs exposed 14 and 16 Skills respectively, with no
catalog errors. This confirms that Node.js is not required to run the installed
Studio.

Frontend verification from the source checkout also passed: TypeScript
type-check, 34 unit tests, 4 browser end-to-end tests across Chromium and
WebKit, and one screenshot-capture test. Two consecutive production builds
produced the same bundle names and passed the offline bundle verifier.

## Scope and limitations

This is local verification of a source archive and installer, not proof that a
GitHub-hosted archive is downloadable: this commit has not been pushed and no
GitHub Release or hosted artifact was created. The installer reports three
upstream license warnings in the standard and full profiles: `clarify-research-idea`
has no declared upstream license, while `academic-paper` and
`academic-paper-reviewer` are marked CC BY-NC 4.0. No independent legal license
review, malware scan, or third-party security audit was performed. The checks
above establish the tested archive's integrity, profile installation, and
Studio startup behavior; they do not certify third-party Skill behavior or
research outputs.

This record is added in a documentation-only commit after the verified code
commit. The recorded ZIP hash binds to the code commit above, not to this
verification-record commit.
