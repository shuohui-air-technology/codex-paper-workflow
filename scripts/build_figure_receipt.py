"""Build and check a figure receipt from supplied facts; never perform review.

The specification has the figure-receipt-v1 shape, with each file's sha256
omitted (or supplied as an expected digest). The command prints a candidate
receipt and does not write project files or advance workflow state.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.figure_contract_validator import REQUIRED, validate_receipt
from scripts.workflow_engine.fs import (
    PathSafetyError, hash_project_file, read_project_json_object,
)


class FigureReceiptError(ValueError):
    pass


def build_receipt(spec: object, project_root: Path) -> dict:
    """Preserve supplied semantics, bind existing files, and apply the validator."""
    if not isinstance(spec, dict) or set(spec) != set(REQUIRED):
        raise FigureReceiptError("specification must contain exactly the figure-receipt-v1 fields")
    try:
        receipt = copy.deepcopy(spec)
    except RecursionError as exc:
        raise FigureReceiptError("specification nesting exceeds the supported depth") from exc
    file_records = []
    for field in ("figure_plan", "claim_evidence_matrix", "code", "preview"):
        record = receipt[field]
        if not isinstance(record, dict):
            raise FigureReceiptError(f"{field} must be a file record")
        file_records.append(record)
    for field in ("source_files", "outputs"):
        if not isinstance(receipt[field], list) or not receipt[field]:
            raise FigureReceiptError(f"{field} must be a non-empty file list")
        file_records.extend(receipt[field])

    observed = {}
    for record in file_records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise FigureReceiptError("each file record must have a project-relative path")
        path = record["path"]
        digest = "sha256:" + hash_project_file(project_root, path)
        expected = record.get("sha256")
        if expected is not None and expected != digest:
            raise FigureReceiptError(f"expected file hash changed: {path}")
        if path in observed and observed[path] != digest:
            raise FigureReceiptError(f"file changed while binding: {path}")
        observed[path] = digest
        record["sha256"] = digest

    figure_id = receipt.get("figure_id")
    if not isinstance(figure_id, str):
        raise FigureReceiptError("figure_id must be text")
    destination = project_root / ".research" / "figures" / figure_id / "figure_receipt.json"
    try:
        errors = validate_receipt(receipt, project_root, destination)
    except (OverflowError, RecursionError) as exc:
        raise FigureReceiptError("specification contains an unsupported numeric value or nesting depth") from exc
    if errors:
        raise FigureReceiptError("; ".join(errors))
    # Metadata decoding and relationship validation read files too. Recheck
    # all digests so those checks cannot silently bless a different version.
    for path, digest in observed.items():
        if "sha256:" + hash_project_file(project_root, path) != digest:
            raise FigureReceiptError(f"file changed during validation: {path}")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--spec", required=True, help="project-relative JSON specification")
    args = parser.parse_args(argv)
    try:
        root = args.project_root.expanduser().resolve(strict=True)
        spec = read_project_json_object(root, args.spec)
        receipt = build_receipt(spec, root)
        result = {"status": "pass", "errors": [],
                  "receipt_path": f".research/figures/{receipt['figure_id']}/figure_receipt.json",
                  "receipt": receipt}
    except (FigureReceiptError, PathSafetyError, OSError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError) as exc:
        result = {"status": "blocked", "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
