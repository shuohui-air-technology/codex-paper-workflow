"""Pure views of recorded workflow state, shared by CLI and disk projections.

Reporting never advances controls, verifies external files, or writes state.
The caller supplies an already validated snapshot. Execution progress and the
selected run's lifecycle remain separate: an active run can be finished.
"""

from __future__ import annotations

from .scheduler import NodeStatus, RunState


def summarize_run_progress(state: RunState, *, run_status: str = "active") -> dict[str, object]:
    """Group all recorded stages without equating skipped work with success.

    ``finished`` means every node is succeeded or skipped. It describes stage
    execution, not manuscript acceptance or the outcome of individual checks.
    A stopped run retains its historical node states, including old claims.
    """
    if run_status not in {"active", "stopped"}:
        raise ValueError("run_status must be active or stopped")
    grouped: dict[str, list[str]] = {status.value: [] for status in NodeStatus}
    for node_id, runtime in sorted(state.nodes.items()):
        grouped[runtime.status.value].append(node_id)
    counts = {status: len(node_ids) for status, node_ids in grouped.items()}
    total = len(state.nodes)
    if run_status == "stopped":
        phase = "stopped"
    elif counts["running"]:
        phase = "running"
    elif any(counts[status] for status in ("failed", "blocked", "stale")):
        phase = "needs_attention"
    elif counts["ready"]:
        phase = "ready"
    elif total and counts["succeeded"] + counts["skipped"] == total:
        phase = "finished"
    elif total:
        phase = "waiting"
    else:
        phase = "empty"
    return {
        "phase": phase,
        "total_nodes": total,
        "counts": counts,
        "node_ids_by_status": grouped,
    }
