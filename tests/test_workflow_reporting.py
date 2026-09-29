"""Read-only progress reporting must not become a second scheduler."""

import unittest
from dataclasses import replace

from scripts.workflow_engine.reporting import summarize_run_progress
from scripts.workflow_engine.scheduler import NodeRuntime, NodeStatus, RunState


def state_with(*statuses):
    return RunState(
        run_id="run-reporting", workflow_id="reporting-flow",
        semantic_sha256="a" * 64,
        nodes={f"step-{index}": NodeRuntime(status) for index, status in enumerate(statuses)},
        edges={}, artifacts={}, decisions={}, project_booleans={},
    )


class WorkflowReportingTests(unittest.TestCase):
    def test_every_recorded_status_is_counted_separately(self):
        state = state_with(*NodeStatus)
        result = summarize_run_progress(state)
        self.assertEqual(result["total_nodes"], 8)
        self.assertEqual(result["counts"], {status.value: 1 for status in NodeStatus})
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["node_ids_by_status"]["ready"], ["step-1"])

    def test_phase_precedence_does_not_hide_failed_or_stale_states(self):
        cases = [
            ((NodeStatus.RUNNING, NodeStatus.BLOCKED), "running"),
            ((NodeStatus.READY, NodeStatus.FAILED), "needs_attention"),
            ((NodeStatus.STALE, NodeStatus.PENDING), "needs_attention"),
            ((NodeStatus.BLOCKED,), "needs_attention"),
            ((NodeStatus.READY, NodeStatus.PENDING), "ready"),
            ((NodeStatus.PENDING,), "waiting"),
            ((NodeStatus.SUCCEEDED, NodeStatus.SKIPPED), "finished"),
            ((NodeStatus.SKIPPED,), "finished"),
            ((), "empty"),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                self.assertEqual(summarize_run_progress(state_with(*statuses))["phase"], expected)

    def test_skipped_stages_are_not_counted_as_successes(self):
        result = summarize_run_progress(state_with(NodeStatus.SUCCEEDED, NodeStatus.SKIPPED))
        self.assertEqual(result["counts"]["succeeded"], 1)
        self.assertEqual(result["counts"]["skipped"], 1)

    def test_stopped_run_reports_historical_states_without_inviting_execution(self):
        result = summarize_run_progress(state_with(NodeStatus.RUNNING, NodeStatus.READY), run_status="stopped")
        self.assertEqual(result["phase"], "stopped")
        self.assertEqual(result["counts"]["running"], 1)

    def test_result_is_deterministic_detached_and_does_not_mutate_state(self):
        state = state_with(NodeStatus.READY, NodeStatus.READY)
        reverse = replace(state, nodes=dict(reversed(list(state.nodes.items()))))
        result = summarize_run_progress(state)
        self.assertEqual(result, summarize_run_progress(reverse))
        result["node_ids_by_status"]["ready"].clear()
        result["counts"]["ready"] = 0
        self.assertEqual(summarize_run_progress(state)["counts"]["ready"], 2)
        self.assertEqual(state.nodes["step-0"].status, NodeStatus.READY)

    def test_unknown_lifecycle_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize_run_progress(state_with(NodeStatus.READY), run_status="assumed-complete")


if __name__ == "__main__":
    unittest.main()
