#!/usr/bin/env python3
"""Manage a durable, auditable paper progress document.

The helper is intentionally dependency-free. It provides exclusive updates,
atomic replacement with a backup, structural validation, compact handoff
summaries, and an append-aware error command. The main model remains the
authority for scientific content; this script protects the state file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SECTIONS = [
    "## Project Metadata",
    "## Current Snapshot",
    "## Core Progress",
    "## Core Experience",
    "## Error Avoidance Rules",
    "## Decisions",
    "## Open Questions and Risks",
    "## Handoff Card",
    "## Append-only Event Log",
]
SECTION_RE = re.compile(r"^## .+$", re.MULTILINE)
ENTRY_ID_RE = re.compile(r"^-\s+((?:P|E|R|D|Q|K)\d+|EVT-\d+):", re.MULTILINE)
RULE_ID_RE = re.compile(r"^-\s+(R\d+):[ \t]*$", re.MULTILINE)
EVENT_ID_RE = re.compile(r"^-\s+(EVT-\d+):[ \t]*", re.MULTILINE)
FIELD_RE = re.compile(r"^-\s+([a-z][a-z0-9_]*)\s*:\s*(.*)$", re.MULTILINE)
ALLOWED_RULE_STATUS = {"active", "superseded", "resolved"}
ALLOWED_RULE_SEVERITY = {"critical", "major", "minor", "unspecified"}
ALLOWED_BLOCKING = {"true", "false"}
ALLOWED_VALIDITY_STATUS = {"pending", "clear", "blocked"}
CURRENT_WORKFLOW_VERSION = "paper-workflow-orchestrator-v0.4"
LEGACY_WORKFLOW_VERSIONS = {"paper-workflow-orchestrator-v0.2", "paper-workflow-orchestrator-v0.3"}
ALLOWED_MODES = {"guided_idea", "draft_audit", "write_or_revise", "autonomous_experiment"}
ALLOWED_STAGES = {
    "intake", "directions", "literature", "topic", "design", "outline",
    "drafting", "integrity", "prose_naturalization", "review", "revision",
    "experiments", "finalize",
}
ALLOWED_HUB_STATUS = {"ready", "degraded", "missing", "not_applicable"}
ALLOWED_EVENT_TYPES = {"milestone", "experience", "error", "decision", "risk", "recovery", "warning"}
MAX_SCALAR = 4000
MAX_ID_DIGITS = 18
REPLACE_RETRIES = 8
EVENT_LINE_RE = re.compile(
    r"^-\s+(EVT-(\d+)):\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(.+)\s+\[([^\]]*)\]\s*$"
)
HANDOFF_COMPACT_RE = re.compile(r"\+(\d+) more in Error Avoidance Rules$")


class ProgressError(RuntimeError):
    """A user-correctable progress state or command error."""


def _replace_with_retry(source: str | Path, destination: str | Path) -> None:
    """Retry transient Windows sharing/AV violations around atomic replace."""
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if os.name != "nt" or winerror not in {5, 32, 33} or attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_text(value: str) -> str:
    if value.startswith("\ufeff"):
        value = value[1:]
    return value.replace("\r\n", "\n").replace("\r", "\n")


def validate_scalar(value: str, label: str, *, required: bool = True) -> str:
    value = value.strip()
    if required and not value:
        raise ProgressError(f"{label} must not be empty")
    if len(value) > MAX_SCALAR:
        raise ProgressError(f"{label} exceeds {MAX_SCALAR} characters")
    if any(ord(char) < 32 and char not in "\t" for char in value):
        raise ProgressError(f"{label} contains a newline or control character")
    return value


def template(project_id: str, target_venue: str = "", document_format: str = "") -> str:
    now = utc_now()
    return f"""# Paper Progress

## Project Metadata
- project_id: {project_id}
- last_updated: {now}
- current_stage: intake
- target_venue: {target_venue}
- document_format: {document_format}
- workflow_version: {CURRENT_WORKFLOW_VERSION}
- mode: guided_idea

## Current Snapshot
- research_question: not yet defined
- selected_direction: not yet selected
- current_status: initialized
- completed_milestones: none
- next_action: diagnose the user's materials and confirm the entry stage
- blockers: none recorded
- validity_status: pending
- hub_status: not_applicable
- last_stage_receipt: none

## Core Progress

## Core Experience

## Error Avoidance Rules

## Decisions

## Open Questions and Risks

## Handoff Card
- next_agent_reads: Project Metadata, Current Snapshot, active Error Avoidance Rules, Decisions, Open Questions and Risks
- must_not_repeat: no active rules recorded yet
- active_constraints: use Codex-internal subagents; verify evidence before formal prose
- resume_instruction: diagnose the user's materials, report the proposed entry stage, and wait for confirmation

## Append-only Event Log
- EVT-001: [{now}] [intake] [milestone] initialized progress document [progress.md]
"""


def read_text(path: Path) -> str:
    try:
        return canonical_text(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProgressError(f"progress file not found: {path}") from exc
    except UnicodeError as exc:
        raise ProgressError(f"progress file is not valid UTF-8: {path}") from exc


def _section_bounds(text: str) -> dict[str, tuple[int, int]]:
    """Return exact section spans, rejecting duplicate headings."""
    matches: list[tuple[int, str]] = []
    offset = 0
    fence: str | None = None
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        stripped = line.lstrip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
        elif stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
        elif re.fullmatch(r"## .+", line):
            matches.append((offset, line))
        offset += len(raw_line)
    seen: set[str] = set()
    bounds: dict[str, tuple[int, int]] = {}
    for index, match in enumerate(matches):
        start, heading = match
        if heading in seen:
            raise ProgressError(f"duplicate section heading: {heading}")
        seen.add(heading)
        end = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        bounds[heading] = (start, end)
    return bounds


def _section(text: str, heading: str, bounds: dict[str, tuple[int, int]] | None = None) -> str:
    bounds = bounds or _section_bounds(text)
    if heading not in bounds:
        raise ProgressError(f"missing section: {heading}")
    start, end = bounds[heading]
    return text[start:end]


def _section_field(section: str, name: str) -> str:
    match = re.search(rf"^[ \t]*-[ \t]+{re.escape(name)}[ \t]*:[ \t]*(.*)$", section, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _top_level_fields(section: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for match in re.finditer(r"^-[ \t]+([a-z][a-z0-9_]*)[ \t]*:[ \t]*(.*)$", section, re.MULTILINE):
        fields.setdefault(match.group(1), []).append(match.group(2).strip())
    return fields


def _nested_fields(section: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for match in re.finditer(r"^[ \t]+-[ \t]+([a-z][a-z0-9_]*)[ \t]*:[ \t]*(.*)$", section, re.MULTILINE):
        fields.setdefault(match.group(1), []).append(match.group(2).strip())
    return fields


def _validate_nested_fields(body: str, entry_id: str, required: Iterable[str]) -> list[str]:
    errors: list[str] = []
    fields = _nested_fields(body)
    for name in required:
        values = fields.get(name, [])
        if len(values) == 0 or not values[0]:
            errors.append(f"{entry_id} lacks {name}")
        elif len(values) > 1:
            errors.append(f"{entry_id} has duplicate {name}")
    for name, values in fields.items():
        if len(values) > 1 and name not in required:
            errors.append(f"{entry_id} has duplicate {name}")
    return errors


def _validate_unique_fields(section: str, label: str, required: Iterable[str]) -> list[str]:
    errors: list[str] = []
    fields = _top_level_fields(section)
    for name in required:
        values = fields.get(name, [])
        if len(values) == 0:
            errors.append(f"missing {label} field: {name}")
        elif len(values) > 1:
            errors.append(f"duplicate {label} field: {name}")
    for name, values in fields.items():
        if len(values) > 1 and name not in required:
            errors.append(f"duplicate {label} field: {name}")
    return errors


def _rule_blocks(section: str) -> list[tuple[str, str]]:
    matches = list(RULE_ID_RE.finditer(section))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        blocks.append((match.group(1), section[match.end():end]))
    return blocks


def _decision_blocks(section: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^-\s+(D\d+):[ \t]*$", section, re.MULTILINE))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        blocks.append((match.group(1), section[match.end():end]))
    return blocks


def _validate_simple_entries(section: str, prefix: str, label: str) -> list[str]:
    errors: list[str] = []
    entry_re = re.compile(rf"^-\s+({re.escape(prefix)}\d+):[ \t]*(.*)$")
    for line in section.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        match = entry_re.match(stripped)
        if not match:
            if stripped.startswith(f"- {prefix}"):
                errors.append(f"malformed {label} entry: {stripped[:120]}")
            continue
        if not match.group(2).strip():
            errors.append(f"empty {label} entry: {match.group(1)}")
    return errors


def _active_rule_summaries(text: str) -> list[str]:
    section = _section(text, "## Error Avoidance Rules")
    active: list[str] = []
    for rule_id, body in _rule_blocks(section):
        status = _section_field(body, "status")
        rule = _section_field(body, "prevention_rule")
        if status == "active":
            active.append(f"{rule_id} — {rule or 'active rule without summary'}")
    return active


def _active_rule_ids(text: str) -> list[str]:
    return [item.split(" — ", 1)[0] for item in _active_rule_summaries(text)]


def _validate_structure(text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not text.startswith("# Paper Progress\n"):
        errors.append("missing exact root heading: # Paper Progress")
    try:
        bounds = _section_bounds(text)
    except ProgressError as exc:
        return [str(exc)], warnings
    missing = [section for section in SECTIONS if section not in bounds]
    errors.extend(f"missing section: {section}" for section in missing)
    unknown = [heading for heading in bounds if heading not in SECTIONS]
    warnings.extend(f"unknown extension section: {heading}" for heading in unknown)
    present = [heading for heading in bounds if heading in SECTIONS]
    expected = [section for section in SECTIONS if section in bounds]
    if present != expected:
        errors.append("sections are out of canonical order")
    if missing:
        return errors, warnings

    metadata = _section(text, "## Project Metadata", bounds)
    snapshot = _section(text, "## Current Snapshot", bounds)
    errors.extend(_validate_unique_fields(
        metadata,
        "Project Metadata",
        ("project_id", "last_updated", "current_stage", "target_venue", "document_format", "workflow_version", "mode"),
    ))
    errors.extend(_validate_unique_fields(
        snapshot,
        "Current Snapshot",
        ("research_question", "selected_direction", "current_status", "completed_milestones", "next_action", "blockers", "validity_status", "hub_status", "last_stage_receipt"),
    ))
    for field in ("project_id", "last_updated", "current_stage", "workflow_version", "mode"):
        if not _section_field(metadata, field):
            errors.append(f"Project Metadata field is empty: {field}")
    for field in ("research_question", "selected_direction", "current_status", "completed_milestones", "next_action", "blockers", "validity_status", "hub_status", "last_stage_receipt"):
        if not _section_field(snapshot, field):
            errors.append(f"Current Snapshot field is empty: {field}")
    version = _section_field(metadata, "workflow_version")
    if version != CURRENT_WORKFLOW_VERSION:
        errors.append(f"unsupported workflow_version: {version or '<missing>'}")
    last_updated = _section_field(metadata, "last_updated")
    try:
        parsed_last_updated = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        if parsed_last_updated.tzinfo is None:
            errors.append("last_updated must include a timezone")
    except ValueError:
        errors.append("last_updated must be ISO-8601")
    mode = _section_field(metadata, "mode")
    if mode and mode not in ALLOWED_MODES:
        errors.append(f"invalid mode: {mode}")
    current_stage = _section_field(metadata, "current_stage")
    if current_stage and current_stage not in ALLOWED_STAGES:
        errors.append(f"invalid current_stage: {current_stage}")
    hub_status = _section_field(snapshot, "hub_status")
    if hub_status and hub_status not in ALLOWED_HUB_STATUS:
        errors.append(f"invalid hub_status: {hub_status}")
    validity_status = _section_field(snapshot, "validity_status")
    if validity_status and validity_status not in ALLOWED_VALIDITY_STATUS:
        errors.append(f"invalid validity_status: {validity_status}")

    all_ids = ENTRY_ID_RE.findall(text)
    duplicates = sorted(item for item, count in Counter(all_ids).items() if count > 1)
    if duplicates:
        errors.append("duplicate entry IDs: " + ", ".join(duplicates))
    for entry_id in all_ids:
        number_text = entry_id.split("-", 1)[1] if entry_id.startswith("EVT-") else entry_id[1:]
        if len(number_text) > MAX_ID_DIGITS:
            errors.append(f"entry ID exceeds {MAX_ID_DIGITS} digits: {entry_id}")
        elif int(number_text) <= 0:
            errors.append(f"entry ID must be positive: {entry_id}")
    for rule_id, body in _rule_blocks(_section(text, "## Error Avoidance Rules", bounds)):
        status = _section_field(body, "status")
        if status not in ALLOWED_RULE_STATUS:
            errors.append(f"{rule_id} has invalid status: {status or '<missing>'}")
        errors.extend(_validate_nested_fields(body, rule_id, ("error", "cause", "impact", "severity", "blocking", "prevention_rule", "required_check", "applicable_stages", "status")))
        severity = _section_field(body, "severity")
        if severity and severity not in ALLOWED_RULE_SEVERITY:
            errors.append(f"{rule_id} has invalid severity: {severity}")
        blocking = _section_field(body, "blocking")
        if blocking and blocking not in ALLOWED_BLOCKING:
            errors.append(f"{rule_id} has invalid blocking value: {blocking}")
        if severity == "critical" and blocking != "true":
            errors.append(f"{rule_id} critical rules must set blocking: true")
    rules_section = _section(text, "## Error Avoidance Rules", bounds)
    for line in rules_section.splitlines()[1:]:
        if line.strip().startswith("- R") and not RULE_ID_RE.match(line.strip()):
            errors.append("malformed error rule entry: " + line.strip()[:120])

    errors.extend(_validate_simple_entries(_section(text, "## Core Progress", bounds), "P", "Core Progress"))
    errors.extend(_validate_simple_entries(_section(text, "## Core Experience", bounds), "E", "Core Experience"))
    risks_section = _section(text, "## Open Questions and Risks", bounds)
    errors.extend(_validate_simple_entries(risks_section, "Q", "open question"))
    errors.extend(_validate_simple_entries(risks_section, "K", "risk"))
    decisions_section = _section(text, "## Decisions", bounds)
    for line in decisions_section.splitlines()[1:]:
        if line.strip().startswith("- D") and not re.match(r"^-\s+D\d+:[ \t]*$", line.strip()):
            errors.append("malformed decision entry: " + line.strip()[:120])
    for decision_id, body in _decision_blocks(decisions_section):
        errors.extend(_validate_nested_fields(body, decision_id, ("question", "chosen_option", "rejected_options", "decision_owner", "evidence")))
        owner = _section_field(body, "decision_owner")
        if owner and owner not in {"user", "main-model", "evidence"}:
            errors.append(f"{decision_id} has invalid decision_owner: {owner}")

    event_section = _section(text, "## Append-only Event Log", bounds)
    event_ids: list[str] = []
    event_numbers: list[int] = []
    for line in event_section.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        match = EVENT_LINE_RE.match(stripped)
        if not match:
            errors.append("malformed event log entry: " + stripped[:120])
            continue
        event_ids.append(match.group(1))
        event_numbers.append(int(match.group(2)))
        timestamp = match.group(3)
        if match.group(4) not in ALLOWED_STAGES:
            errors.append(f"event {match.group(1)} has invalid stage: {match.group(4)}")
        if match.group(5) not in ALLOWED_EVENT_TYPES:
            errors.append(f"event {match.group(1)} has invalid type: {match.group(5)}")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed_timestamp.tzinfo is None:
                errors.append(f"event {match.group(1)} timestamp must include a timezone")
        except ValueError:
            errors.append(f"event {match.group(1)} has invalid ISO-8601 timestamp")
    if event_numbers != sorted(set(event_numbers)):
        errors.append("event IDs must be unique and monotonically increasing")
    handoff = _section(text, "## Handoff Card", bounds)
    errors.extend(_validate_unique_fields(
        handoff,
        "Handoff Card",
        ("next_agent_reads", "must_not_repeat", "active_constraints", "resume_instruction"),
    ))
    for field in ("next_agent_reads", "must_not_repeat", "active_constraints", "resume_instruction"):
        if not _section_field(handoff, field):
            errors.append(f"Handoff Card field is empty: {field}")
    listed = _section_field(handoff, "must_not_repeat")
    active = _active_rule_ids(text)
    # Parse only the machine-readable ID prefix of each handoff item.  Rule
    # prose may itself mention identifiers (for example, "check R999").
    listed_ids = re.findall(r"(?:^|;[ \t]*)(R\d+)(?=[ \t]+—)", listed)
    compact_match = HANDOFF_COMPACT_RE.search(listed)
    if active:
        if compact_match:
            omitted = int(compact_match.group(1))
            if listed_ids != active[:len(listed_ids)] or omitted != len(active) - len(listed_ids):
                errors.append("Handoff Card compact rule list does not match active rules")
        elif listed_ids != active:
            errors.append("Handoff Card must_not_repeat must exactly list active rule IDs")
    elif listed != "no active rules recorded yet":
        errors.append("Handoff Card must_not_repeat is stale while no active rules are present")
    active_blockers = [
        rule_id for rule_id, body in _rule_blocks(_section(text, "## Error Avoidance Rules", bounds))
        if _section_field(body, "status") == "active" and _section_field(body, "blocking") == "true"
    ]
    if active_blockers and validity_status != "blocked":
        errors.append("active blocking rules require Current Snapshot validity_status: blocked")
    if validity_status == "blocked" and not active_blockers:
        errors.append("validity_status: blocked requires an active blocking Error Avoidance Rule")
    return errors, warnings


def validate_text(text: str) -> dict[str, object]:
    errors, warnings = _validate_structure(canonical_text(text))
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _lock_path(path: Path) -> Path:
    return Path(str(path) + ".lock")


@contextmanager
def progress_lock(path: Path, timeout: float = 20.0):
    """Use an OS advisory lock; the lock file is intentionally persistent."""
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    handle = None
    while handle is None:
        try:
            handle = lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            if handle is not None:
                handle.close()
                handle = None
            if time.monotonic() - started >= timeout:
                raise ProgressError(f"timed out waiting for progress lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with source.open("rb") as source_handle, tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
        ) as target_handle:
            temp_name = target_handle.name
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        _replace_with_retry(temp_name, destination)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def atomic_write(path: Path, text: str, *, keep_backup: bool = True) -> None:
    """Write UTF-8 text with a same-directory temp file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = canonical_text(text)
    if keep_backup and path.exists():
        _atomic_copy(path, Path(str(path) + ".bak"))
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_name, path)
        temp_name = None
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _next_entry_id(section: str, prefix: str) -> str:
    pattern = re.compile(rf"^-\s+{re.escape(prefix)}(\d+):", re.MULTILINE)
    values = [int(match.group(1)) for match in pattern.finditer(section)]
    return f"{prefix}{((max(values) + 1) if values else 1):03d}"


def _replace_handoff(text: str) -> str:
    bounds = _section_bounds(text)
    handoff_start, handoff_end = bounds["## Handoff Card"]
    handoff = text[handoff_start:handoff_end]
    active = _active_rule_summaries(text)
    if len(active) > 8:
        value = "; ".join(active[:8]) + f"; +{len(active) - 8} more in Error Avoidance Rules"
    else:
        value = "; ".join(active) or "no active rules recorded yet"
    if not re.search(r"^-\s+must_not_repeat:", handoff, re.MULTILINE):
        raise ProgressError("required handoff field not found: must_not_repeat")
    handoff = re.sub(
        r"^-\s+must_not_repeat:.*$",
        f"- must_not_repeat: {value}",
        handoff,
        count=1,
        flags=re.MULTILINE,
    )
    text = text[:handoff_start] + handoff + text[handoff_end:]
    bounds = _section_bounds(text)
    metadata_start, metadata_end = bounds["## Project Metadata"]
    metadata = text[metadata_start:metadata_end]
    if re.search(r"^-\s+last_updated:", metadata, re.MULTILINE):
        metadata = re.sub(
            r"^-\s+last_updated:.*$",
            f"- last_updated: {utc_now()}",
            metadata,
            count=1,
            flags=re.MULTILINE,
        )
    text = text[:metadata_start] + metadata + text[metadata_end:]
    return text


def _insert_before_section(text: str, heading: str, block: str) -> str:
    bounds = _section_bounds(text)
    if heading not in bounds:
        raise ProgressError(f"required section not found: {heading}")
    index = bounds[heading][0]
    prefix = text[:index].rstrip("\n")
    suffix = text[index:]
    return prefix + "\n\n" + block.rstrip() + "\n\n" + suffix


def _append_event(text: str, stage: str, event_type: str, summary: str, refs: str) -> str:
    bounds = _section_bounds(text)
    start, end = bounds["## Append-only Event Log"]
    section = text[start:end].rstrip("\n")
    event_id = _next_entry_id(section, "EVT-")
    # Event records use square brackets as field delimiters. Percent-encode
    # those characters in references instead of rejecting otherwise valid
    # paths/URLs; the encoded value remains unambiguous to the validator.
    ref_text = (refs or "none").replace("%", "%25").replace("[", "%5B").replace("]", "%5D")
    line = f"- {event_id}: [{utc_now()}] [{stage}] [{event_type}] {summary} [{ref_text}]"
    section = section + "\n" + line + "\n"
    return text[:start] + section + text[end:]


def _upsert_field(text: str, heading: str, name: str, value: str) -> str:
    bounds = _section_bounds(text)
    start, end = bounds[heading]
    section = text[start:end]
    field_pattern = rf"^[ \t]*-[ \t]+{re.escape(name)}[ \t]*:.*$"
    replacement = f"- {name}: {value}"
    if re.search(field_pattern, section, re.MULTILINE):
        section = re.sub(field_pattern, replacement, section, count=1, flags=re.MULTILINE)
    else:
        section = section.rstrip("\n") + "\n" + replacement + "\n"
    return text[:start] + section + text[end:]


def _write_checked(path: Path, text: str) -> None:
    result = validate_text(text)
    if not result["valid"]:
        raise ProgressError("refusing to write invalid progress: " + "; ".join(result["errors"]))
    atomic_write(path, text)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.project_dir).resolve()
    project_id = validate_scalar(args.project_id or root.name or "paper-project", "project_id")
    venue = validate_scalar(args.target_venue, "target_venue", required=False)
    document_format = validate_scalar(args.document_format, "document_format", required=False)
    path = root / ".research" / "progress.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with progress_lock(path):
        if path.exists():
            if not path.is_file():
                raise ProgressError(f"progress path exists but is not a file: {path}")
            existing = validate_text(read_text(path))
            if not existing["valid"]:
                raise ProgressError("progress file exists but is invalid: " + "; ".join(existing["errors"]))
            print(f"exists: {path}")
            return 0
        _write_checked(path, template(project_id, venue, document_format))
    print(f"initialized: {path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    result = validate_text(read_text(path))
    result["file"] = str(path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


def cmd_summary(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    text = read_text(path)
    validation = validate_text(text)
    if not validation["valid"]:
        result = {
            "file": str(path),
            "valid": False,
            "validation_errors": validation["errors"],
            "validation_warnings": validation["warnings"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    bounds = _section_bounds(text)
    metadata = _section(text, "## Project Metadata", bounds)
    snapshot = _section(text, "## Current Snapshot", bounds)
    result = {
        "file": str(path),
        "valid": validation["valid"],
        "validation_errors": validation["errors"],
        "validation_warnings": validation["warnings"],
        "project_id": _section_field(metadata, "project_id"),
        "current_stage": _section_field(metadata, "current_stage"),
        "mode": _section_field(metadata, "mode"),
        "target_venue": _section_field(metadata, "target_venue"),
        "document_format": _section_field(metadata, "document_format"),
        "current_status": _section_field(snapshot, "current_status"),
        "next_action": _section_field(snapshot, "next_action"),
        "blockers": _section_field(snapshot, "blockers"),
        "validity_status": _section_field(snapshot, "validity_status"),
        "active_rule_ids": _active_rule_ids(text),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if validation["valid"] else 1


def cmd_record_error(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    values = {
        "stage": validate_scalar(args.stage, "stage"),
        "error": validate_scalar(args.error, "error"),
        "cause": validate_scalar(args.cause, "cause"),
        "impact": validate_scalar(args.impact, "impact"),
        "severity": validate_scalar(args.severity, "severity"),
        "blocking": validate_scalar(args.blocking, "blocking"),
        "rule": validate_scalar(args.rule, "rule"),
        "check": validate_scalar(args.check, "check"),
        "stages": validate_scalar(args.stages or "all applicable stages", "stages"),
        "refs": validate_scalar(args.refs or "none", "refs"),
    }
    if values["stage"] not in ALLOWED_STAGES:
        raise ProgressError(f"invalid stage: {values['stage']}")
    if values["severity"] not in ALLOWED_RULE_SEVERITY:
        raise ProgressError(f"invalid severity: {values['severity']}")
    if values["blocking"] not in ALLOWED_BLOCKING:
        raise ProgressError(f"invalid blocking value: {values['blocking']}")
    if values["severity"] == "critical" and values["blocking"] != "true":
        raise ProgressError("critical errors must use --blocking true")
    for label, value in values.items():
        if label != "refs" and ("[" in value or "]" in value):
            raise ProgressError(f"{label} cannot contain '[' or ']' because event records use bracket delimiters")
    with progress_lock(path):
        text = read_text(path)
        existing = validate_text(text)
        if not existing["valid"]:
            raise ProgressError("refusing to modify invalid progress: " + "; ".join(existing["errors"]))
        rules_section = _section(text, "## Error Avoidance Rules")
        rule_id = _next_entry_id(rules_section, "R")
        block = f"""- {rule_id}:
  - error: {values['error']}
  - cause: {values['cause']}
  - impact: {values['impact']}
  - severity: {values['severity']}
  - blocking: {values['blocking']}
  - prevention_rule: {values['rule']}
  - required_check: {values['check']}
  - applicable_stages: {values['stages']}
  - status: active"""
        text = _insert_before_section(text, "## Decisions", block)
        text = _replace_handoff(text)
        if values["blocking"] == "true":
            text = _upsert_field(text, "## Current Snapshot", "validity_status", "blocked")
        summary = f"recorded {rule_id}: {values['error']} -> {values['rule']}"
        text = _append_event(text, values["stage"], "error", summary, values["refs"])
        _write_checked(path, text)
    print(f"recorded: {rule_id} in {path}")
    return 0


def _ensure_legacy_rule_fields(text: str) -> str:
    """Add v0.4 rule fields without guessing legacy severity."""
    bounds = _section_bounds(text)
    start, end = bounds["## Error Avoidance Rules"]
    section = text[start:end]
    output: list[str] = []
    current_rule = False
    severity_seen = False
    blocking_seen = False
    for line in section.splitlines():
        if re.match(r"^-\s+R\d+:[ \t]*$", line):
            current_rule = True
            severity_seen = False
            blocking_seen = False
        if current_rule and re.match(r"^\s+-\s+severity\s*:", line):
            severity_seen = True
        if current_rule and re.match(r"^\s+-\s+blocking\s*:", line):
            blocking_seen = True
        if current_rule and re.match(r"^\s+-\s+status\s*:", line):
            if not severity_seen:
                output.append("  - severity: unspecified")
            if not blocking_seen:
                output.append("  - blocking: false")
        output.append(line)
    replacement = "\n".join(output)
    if section.endswith("\n"):
        replacement += "\n"
    return text[:start] + replacement + text[end:]


def _legacy_validity_status(text: str) -> str:
    bounds = _section_bounds(text)
    for _, body in _rule_blocks(_section(text, "## Error Avoidance Rules", bounds)):
        if _section_field(body, "status") == "active" and _section_field(body, "blocking") == "true":
            return "blocked"
    return "pending"


def cmd_migrate(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    mode = validate_scalar(args.mode, "mode")
    if mode not in ALLOWED_MODES:
        raise ProgressError(f"invalid mode: {mode}")
    if not args.confirm:
        raise ProgressError("migration is mutating; pass --confirm after the user confirms the selected mode")
    with progress_lock(path):
        text = read_text(path)
        try:
            bounds = _section_bounds(text)
        except ProgressError as exc:
            raise ProgressError(f"cannot migrate malformed progress: {exc}") from exc
        if any(section not in bounds for section in SECTIONS):
            raise ProgressError("cannot migrate progress with missing canonical sections")
        current_version = _section_field(_section(text, "## Project Metadata", bounds), "workflow_version")
        if current_version == CURRENT_WORKFLOW_VERSION:
            existing = validate_text(text)
            if not existing["valid"]:
                raise ProgressError("progress is already v0.4 but invalid: " + "; ".join(existing["errors"]))
            print(f"already current: {path}")
            return 0
        if current_version and current_version not in LEGACY_WORKFLOW_VERSIONS:
            raise ProgressError(f"unsupported legacy workflow_version: {current_version}")
        legacy_suffix = "v0.3" if current_version.endswith("v0.3") else "v0.2"
        legacy_backup = Path(str(path) + f".legacy-{legacy_suffix}")
        if path.exists():
            _atomic_copy(path, legacy_backup)
        current_stage = _section_field(_section(text, "## Project Metadata", bounds), "current_stage")
        if current_stage not in ALLOWED_STAGES:
            if not args.current_stage:
                raise ProgressError("current_stage is not normalized; supply --current-stage")
            current_stage = validate_scalar(args.current_stage, "current_stage")
        if current_stage not in ALLOWED_STAGES:
            raise ProgressError(f"invalid current_stage: {current_stage}")
        text = _ensure_legacy_rule_fields(text)
        legacy_validity = _legacy_validity_status(text)
        text = _upsert_field(text, "## Project Metadata", "workflow_version", CURRENT_WORKFLOW_VERSION)
        text = _upsert_field(text, "## Project Metadata", "mode", mode)
        text = _upsert_field(text, "## Project Metadata", "current_stage", current_stage)
        text = _upsert_field(text, "## Current Snapshot", "validity_status", legacy_validity)
        text = _upsert_field(text, "## Current Snapshot", "hub_status", "not_applicable")
        text = _upsert_field(text, "## Current Snapshot", "last_stage_receipt", "none")
        text = _replace_handoff(text)
        text = _append_event(text, current_stage, "decision", "migrated progress state to v0.4", "progress.md")
        # Stage the upgraded recovery point before replacing the legacy main
        # file.  A crash before the second replace therefore leaves either
        # the old main plus a valid v0.4 backup, or both at v0.4.
        if not validate_text(text)["valid"]:
            raise ProgressError("refusing to write invalid migrated progress")
        atomic_write(Path(str(path) + ".bak"), text, keep_backup=False)
        atomic_write(path, text, keep_backup=False)
    print(f"migrated: {path}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    backup = Path(str(path) + ".bak")
    with progress_lock(path):
        backup_text = read_text(backup)
        result = validate_text(backup_text)
        if not result["valid"]:
            raise ProgressError("backup is invalid: " + "; ".join(result["errors"]))
        current_stage = _section_field(_section(backup_text, "## Project Metadata"), "current_stage")
        restored = _replace_handoff(backup_text)
        restored = _append_event(restored, current_stage, "recovery", "restored from validated backup generation", "progress.md.bak")
        _write_checked(Path(str(path) + ".restore-candidate"), restored)
        candidate = Path(str(path) + ".restore-candidate")
        if path.exists():
            corrupt = Path(str(path) + f".corrupt-{time.time_ns()}-{uuid.uuid4().hex[:8]}")
            _atomic_copy(path, corrupt)
        _replace_with_retry(candidate, path)
    print(f"restored: {path} from {backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a durable paper progress document")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create .research/progress.md if absent")
    init.add_argument("--project-dir", required=True)
    init.add_argument("--project-id", default="")
    init.add_argument("--target-venue", default="")
    init.add_argument("--document-format", default="")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="validate required sections, IDs, and handoff state")
    validate.add_argument("--file", required=True)
    validate.set_defaults(func=cmd_validate)

    summary = sub.add_parser("summary", help="print a validated machine-readable snapshot")
    summary.add_argument("--file", required=True)
    summary.set_defaults(func=cmd_summary)

    error = sub.add_parser("record-error", help="append an error rule and event transactionally")
    error.add_argument("--file", required=True)
    error.add_argument("--stage", required=True)
    error.add_argument("--error", required=True)
    error.add_argument("--cause", required=True)
    error.add_argument("--impact", required=True)
    error.add_argument("--severity", choices=sorted(ALLOWED_RULE_SEVERITY), default="unspecified")
    error.add_argument("--blocking", choices=sorted(ALLOWED_BLOCKING), default="false")
    error.add_argument("--rule", required=True)
    error.add_argument("--check", required=True)
    error.add_argument("--stages", default="")
    error.add_argument("--refs", default="")
    error.set_defaults(func=cmd_record_error)

    migrate = sub.add_parser("migrate", help="migrate a legacy progress file to v0.4")
    migrate.add_argument("--file", required=True)
    migrate.add_argument("--mode", required=True)
    migrate.add_argument("--current-stage", default="")
    migrate.add_argument("--confirm", action="store_true", help="confirm the user-selected migration mode")
    migrate.set_defaults(func=cmd_migrate)

    restore = sub.add_parser("restore", help="restore a validated .bak generation")
    restore.add_argument("--file", required=True)
    restore.set_defaults(func=cmd_restore)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (OSError, ProgressError, UnicodeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
