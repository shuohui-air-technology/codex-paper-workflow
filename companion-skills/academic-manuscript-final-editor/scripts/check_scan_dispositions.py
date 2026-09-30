#!/usr/bin/env python3
"""Prepare and self-check the itemized dispositions for scanner findings.

The orchestrator's ``final_edit_receipt_validator.py`` stays the gate. This
helper applies the same rules before the stage returns, so one forgotten finding
or an empty evidence list never costs a whole manuscript re-run.

    # 1. skeleton covering every finding ID, in report order
    python3 check_scan_dispositions.py --scan editorial_scan.json \
        --template-out editorial_scan_dispositions.json

    # 2. after the editor records one decision and its evidence per finding
    python3 check_scan_dispositions.py --scan editorial_scan.json \
        --dispositions editorial_scan_dispositions.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


DECISIONS = ("accept", "reject", "defer", "not_applicable")
ALLOWED_KEYS = {"finding_id", "decision", "evidence_refs", "reason"}


def finding_id(finding: dict) -> str:
    """Mirror scan_manuscript_style.py and final_edit_receipt_validator.py.

    The ID scheme is one contract implemented in three places; change all three
    together, and keep the agreement test in tests/test_final_editor_scan.py.
    """
    payload = {key: finding.get(key) for key in ("path", "line", "rule_id", "evidence")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "fnd-" + hashlib.sha256(raw).hexdigest()[:20]


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def scan_tier(report: dict) -> tuple[str, list[str], list[str]]:
    """Return (tier, finding_ids, problems) for one scanner report.

    ``itemized`` means every ID matches the finding content, ``legacy`` means the
    report predates itemization, ``invalid`` means it cannot be disposed of.
    """
    findings = report.get("findings")
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        return "invalid", [], ["scanner report must contain a findings array"]
    problems: list[str] = []
    if report.get("finding_count") != len(findings):
        problems.append("scanner report finding_count does not match its findings array")
    declared = [item.get("finding_id") for item in findings]
    if not any(declared):
        return ("invalid" if problems else "legacy"), [], problems
    ids: list[str] = []
    counts: dict[str, int] = {}
    for index, item in enumerate(findings):
        base = finding_id(item)
        counts[base] = counts.get(base, 0) + 1
        expected = base if counts[base] == 1 else f"{base}-{counts[base]}"
        if item.get("finding_id") != expected:
            problems.append(f"findings[{index}].finding_id does not match its content")
        ids.append(expected)
    return ("invalid" if problems else "itemized"), ids, problems


def disposition_problems(dispositions: dict, ids: list[str]) -> list[str]:
    """List every rule the orchestrator validator would reject this file for."""
    problems: list[str] = []
    items = dispositions.get("dispositions")
    if not isinstance(items, list):
        return ["dispositions must be a list"]
    if dispositions.get("finding_count") != len(ids):
        problems.append(f"finding_count must be {len(ids)}")
    if dispositions.get("disposed_count") != len(items):
        problems.append("disposed_count must equal the disposition list")
    seen: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"dispositions[{index}] must be an object")
            continue
        if not {"finding_id", "decision", "evidence_refs"} <= set(item):
            problems.append(f"dispositions[{index}] must contain finding_id, decision, and evidence_refs")
            continue
        extra = set(item) - ALLOWED_KEYS
        if extra:
            problems.append(f"dispositions[{index}] has unsupported keys: {', '.join(sorted(extra))}")
        if item.get("decision") not in DECISIONS:
            problems.append(f"dispositions[{index}].decision must be one of {', '.join(DECISIONS)}")
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
            problems.append(f"dispositions[{index}].evidence_refs must be non-empty")
        seen.append(item.get("finding_id"))
    if len(set(seen)) != len(seen):
        problems.append("finding_id values must be unique")
    if set(seen) != set(ids):
        missing = len(set(ids) - set(seen))
        unknown = len({value for value in seen if isinstance(value, str)} - set(ids))
        problems.append(f"finding_id set must match the scanner report exactly (missing {missing}, unknown {unknown})")
    return problems


def skeleton(report: Path, report_json: dict, ids: list[str]) -> dict:
    """A dispositions file with the bindings filled in and decisions left open."""
    return {
        "status": "pass",
        "scanner_report_sha256": digest(report),
        "finding_count": len(ids),
        "disposed_count": len(ids),
        "dispositions": [{"finding_id": value, "decision": "", "evidence_refs": []} for value in ids],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan", type=Path, required=True, help="scanner report from scan_manuscript_style.py --json")
    parser.add_argument("--dispositions", type=Path, help="itemized dispositions file to self-check")
    parser.add_argument("--template-out", type=Path, help="write a skeleton covering every finding ID")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report_json = json.loads(args.scan.read_text(encoding="utf-8-sig"))
        if not isinstance(report_json, dict):
            raise ValueError(f"JSON root must be an object: {args.scan}")
        tier, ids, problems = scan_tier(report_json)
        if args.template_out:
            if problems:
                raise ValueError("; ".join(problems))
            if tier != "itemized":
                raise ValueError("scanner report predates itemized findings; rescan with the current scanner")
            args.template_out.parent.mkdir(parents=True, exist_ok=True)
            args.template_out.write_text(
                json.dumps(skeleton(args.scan, report_json, ids), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result = {"status": "pass", "tier": tier, "finding_count": len(ids), "finding_ids": ids,
                      "template": str(args.template_out), "errors": []}
            message = f"wrote {args.template_out} covering {len(ids)} findings"
        elif args.dispositions:
            dispositions = json.loads(args.dispositions.read_text(encoding="utf-8-sig"))
            if not isinstance(dispositions, dict):
                raise ValueError(f"JSON root must be an object: {args.dispositions}")
            errors = list(problems)
            if not errors and tier == "legacy":
                errors.append("scanner report predates itemized findings; rescan before disposing of it")
            if not errors:
                if dispositions.get("status") != "pass":
                    errors.append("status must be pass")
                if dispositions.get("scanner_report_sha256") != digest(args.scan):
                    errors.append("scanner_report_sha256 must bind this scanner report")
                else:
                    errors.extend(disposition_problems(dispositions, ids))
            result = {"status": "blocked" if errors else "pass", "tier": tier, "finding_count": len(ids),
                      "finding_ids": ids, "dispositions": str(args.dispositions), "errors": errors}
            message = ("dispositions: blocked" if errors else "dispositions: pass") + f" ({len(ids)} findings, {tier})"
        else:
            result = {"status": "pass", "tier": tier, "finding_count": len(ids), "finding_ids": ids, "errors": problems}
            message = f"scan report: {tier} ({len(ids)} findings)"
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"check error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(message)
        for error in result["errors"]:
            print(f"  - {error}")
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
