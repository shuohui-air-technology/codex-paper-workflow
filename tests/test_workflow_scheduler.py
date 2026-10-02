import hashlib
import json
import math
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from scripts.workflow_engine.catalog import CatalogResult, SkillIdentity, ValidatorIdentity
from scripts.workflow_engine.compiler import compile_workflow
from scripts.workflow_engine.conditions import evaluate_predicate
from scripts.workflow_engine.schema import WorkflowError, parse_workflow
from scripts.workflow_engine.scheduler import (
    ArtifactRuntime,
    EdgeRuntime,
    EdgeStatus,
    NodeStatus,
    claim_transition,
    condition_facts,
    initial_run,
    mark_descendants_stale,
    approval_decision_name,
    approval_fingerprint,
    approval_state,
    record_condition_fact_transition,
    ready_node_ids,
    refresh_ready,
    result_transition,
    retry_transition,
    rerun_stale_transition,
    validator_claim_transition,
    validator_result_transition,
    validator_retry_transition,
    stabilize_control_nodes,
)


ROOT = Path(__file__).resolve().parents[1]


def task(node_id, *, entry=False, inputs=(), outputs=(), failure_policy="block"):
    return {
        "id": node_id,
        "type": "task",
        "display_name": node_id,
        "entry": entry,
        "enabled": True,
        "skill_ref": f"skill-{node_id}",
        "validator_ref": None, "validator_config": None,
        "origin_projection_node_id": None,
        "inputs": list(inputs),
        "outputs": list(outputs),
        "outcomes": ["succeeded"],
        "write_scopes": [],
        "failure_policy": failure_policy,
        "condition_cases": [],
        "join_mode": "all_active",
    }


def condition(node_id, *, entry=False, cases=(), approval_source=None):
    return {
        "id": node_id,
        "type": "condition",
        "display_name": node_id,
        "entry": entry,
        "enabled": True,
        "skill_ref": None,
        "validator_ref": None, "validator_config": None,
        "origin_projection_node_id": None,
        "inputs": [],
        "outputs": [],
        "outcomes": [],
        "write_scopes": [],
        "failure_policy": "block",
        "condition_cases": list(cases),
        "join_mode": "all_active",
        "approval_source": approval_source,
    }


def join(node_id, *, outputs=(), mode="all_active"):
    return {
        "id": node_id,
        "type": "join",
        "display_name": node_id,
        "entry": False,
        "enabled": True,
        "skill_ref": None,
        "validator_ref": None, "validator_config": None,
        "origin_projection_node_id": None,
        "inputs": [],
        "outputs": list(outputs),
        "outcomes": [],
        "write_scopes": [],
        "failure_policy": "block",
        "condition_cases": [],
        "join_mode": mode,
    }


def validator(node_id, *, entry=False):
    return {
        "id": node_id,
        "type": "validator",
        "display_name": node_id,
        "entry": entry,
        "enabled": True,
        "skill_ref": None,
        "validator_ref": "paper-section",
        "validator_config": {"input_roles": {"file": "section"}, "options": {
            "phase": "body", "paper_type": "empirical", "language": "en",
            "method_profile": "method-first", "validity_status": "pending",
            "discussion_integrated": False,
        }},
        "origin_projection_node_id": None,
        "inputs": ["section"],
        "outputs": [],
        "outcomes": ["pass", "fail", "blocked"],
        "write_scopes": [],
        "failure_policy": "block",
        "condition_cases": [],
        "join_mode": "all_active",
    }


def edge(edge_id, source, target, *, trigger="succeeded", output_map=None):
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "trigger": trigger,
        "output_map": {} if output_map is None else dict(output_map),
    }


class WorkflowSchedulerTests(unittest.TestCase):
    def compile_result(self, nodes, edges, *, external_inputs=(), max_parallelism=4):
        value = {
            "schema_version": "paper-workflow-custom-v1",
            "workflow_id": "scheduler-test-flow",
            "document_revision": 1,
            "semantic_revision": 1,
            "derived_from": None,
            "max_parallelism": max_parallelism,
            "external_inputs": list(external_inputs),
            "nodes": nodes,
            "edges": edges,
            "ui": {"positions": {node["id"]: {"x": 0, "y": 0} for node in nodes}},
        }
        skills = {}
        for node in nodes:
            skill_id = node["skill_ref"]
            if skill_id is not None:
                skills[skill_id] = SkillIdentity(
                    catalog_id=skill_id,
                    root=ROOT / "test-skills",
                    relative_path=skill_id,
                    skill_sha256=hashlib.sha256((skill_id + "/SKILL.md").encode()).hexdigest(),
                    tree_sha256="sha256:" + hashlib.sha256(skill_id.encode()).hexdigest(),
                    locked=True,
                )
        validators = {
            "paper-section": ValidatorIdentity(
                validator_id="paper-section",
                script=ROOT / "scripts/validate_paper_sections.py",
                sha256="0" * 64,
                adapter="paper_section_v1",
                input_schema="paper_section_v1",
                control_tags=(),
                outcomes=("pass", "fail", "blocked"),
            )
        }
        return compile_workflow(
            parse_workflow(value),
            CatalogResult(skills, (), ()),
            validators,
            json.loads(
                (ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text()
            ),
        )

    def compile(self, nodes, edges, *, external_inputs=(), max_parallelism=4):
        compiled = self.compile_result(
            nodes,
            edges,
            external_inputs=external_inputs,
            max_parallelism=max_parallelism,
        )
        self.assertEqual(compiled.errors, ())
        self.assertIsNotNone(compiled.plan)
        return compiled.plan

    def artifact(self, artifact_id, path=None, *, state="verified", node="external", attempt=0):
        return ArtifactRuntime(
            artifact_id=artifact_id,
            path=path or f"{artifact_id}.md",
            sha256=hashlib.sha256(artifact_id.encode()).hexdigest(),
            state=state,
            producer_node_id=node,
            producer_attempt=attempt,
        )

    def register(self, state, *artifacts):
        updated = dict(state.artifacts)
        updated.update({artifact.artifact_id: artifact for artifact in artifacts})
        return replace(state, artifacts=MappingProxyType(updated))

    def test_approval_gate_waits_then_releases_only_after_bound_source_approval(self):
        nodes = [
            task("source", entry=True, outputs=("draft",)),
            condition("gate", approval_source="source"),
            task("target", inputs=("draft",)),
        ]
        edges = [
            edge("source-target", "source", "target", output_map={"draft": "draft"}),
            edge("source-gate", "source", "gate"),
            edge("gate-target", "gate", "target", trigger="approved"),
        ]
        plan = self.compile(nodes, edges)
        state = initial_run(plan, "approval-run")
        self.assertEqual(ready_node_ids(plan, state), ("source",))
        state = self.complete(
            plan,
            state,
            "source",
            outputs={"draft": "draft.md"},
            artifacts=(self.artifact("draft", "draft.md", node="source", attempt=1),),
        )
        self.assertEqual(state.nodes["gate"].status, NodeStatus.PENDING)
        self.assertEqual(state.nodes["target"].status, NodeStatus.PENDING)
        self.assertEqual(approval_state(plan, state, "gate"), "awaiting_confirmation")

        fingerprint = approval_fingerprint(plan, state, "gate")
        self.assertIsNotNone(fingerprint)
        approved = record_condition_fact_transition(
            plan,
            state,
            approval_decision_name("gate"),
            "approved:" + fingerprint,
            decision=True,
        )
        approved, transitions = stabilize_control_nodes(plan, approved)
        self.assertEqual([item.node_id for item in transitions], ["gate"])
        self.assertEqual(approved.nodes["gate"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(ready_node_ids(plan, approved), ("target",))

    def test_revision_invalidates_source_and_old_approval_cannot_release_new_attempt(self):
        nodes = [
            task("source", entry=True, outputs=("draft",)),
            condition("gate", approval_source="source"),
            task("target", inputs=("draft",)),
        ]
        edges = [
            edge("source-target", "source", "target", output_map={"draft": "draft"}),
            edge("source-gate", "source", "gate"),
            edge("gate-target", "gate", "target", trigger="approved"),
        ]
        plan = self.compile(nodes, edges)
        state = self.complete(
            plan,
            initial_run(plan, "revision-run"),
            "source",
            outputs={"draft": "draft.md"},
            artifacts=(self.artifact("draft", "draft.md", node="source", attempt=1),),
        )
        old = approval_fingerprint(plan, state, "gate")
        state = record_condition_fact_transition(
            plan,
            state,
            approval_decision_name("gate"),
            "revise:" + old,
            decision=True,
        )
        self.assertEqual(state.nodes["source"].status, NodeStatus.STALE)
        self.assertEqual(approval_state(plan, state, "gate"), "revision_requested")
        state, affected = rerun_stale_transition(plan, state, "source")
        self.assertIn("source", affected)
        self.assertEqual(ready_node_ids(plan, state), ("source",))
        state = self.complete(
            plan,
            state,
            "source",
            outputs={"draft": "draft-v2.md"},
            artifacts=(self.artifact("draft", "draft-v2.md", node="source", attempt=2),),
        )
        self.assertNotEqual(old, approval_fingerprint(plan, state, "gate"))
        self.assertEqual(approval_state(plan, state, "gate"), "awaiting_confirmation")

    def complete(self, plan, state, node_id, *, outputs=None, artifacts=()):
        claimed = claim_transition(plan, refresh_ready(plan, state), node_id, f"token-{node_id}")
        return result_transition(
            plan,
            claimed,
            {
                "node_id": node_id,
                "attempt": claimed.nodes[node_id].attempt,
                "status": "succeeded",
                "outcome": "succeeded",
                "outputs": {} if outputs is None else outputs,
                "artifacts": tuple(artifacts),
            },
        )

    def test_external_inputs_stay_pending_until_verified_registration(self):
        plan = self.compile(
            [task("source", entry=True, inputs=("request",))], [],
            external_inputs=("request",),
        )
        initial = initial_run(plan, "run-external")
        self.assertEqual(initial.nodes["source"].status, NodeStatus.PENDING)
        stale = self.register(initial, self.artifact("request", state="stale"))
        self.assertEqual(refresh_ready(plan, stale).nodes["source"].status, NodeStatus.PENDING)
        verified = self.register(initial, self.artifact("request"))
        refreshed = refresh_ready(plan, verified)
        self.assertEqual(refreshed.nodes["source"].status, NodeStatus.READY)
        self.assertEqual(refreshed.nodes["source"].selected_inputs["request"], "request.md")

    def test_incoming_producer_takes_node_local_authority_over_external_membership(self):
        plan = self.compile(
            [
                task("source", entry=True, inputs=("draft",), outputs=("draft",)),
                task("sink", inputs=("draft",)),
            ],
            [edge("source-to-sink", "source", "sink", output_map={"draft": "draft"})],
            external_inputs=("draft",),
        )
        old = self.artifact("draft", "old.md")
        refined = replace(
            self.artifact("draft", "new.md", node="source", attempt=1),
            sha256=hashlib.sha256(b"refined-draft").hexdigest(),
        )
        state = self.register(initial_run(plan, "run-lineage"), old)
        state = self.complete(
            plan,
            state,
            "source",
            outputs={"draft": "new.md"},
            artifacts=(refined,),
        )
        self.assertEqual(state.artifacts["draft"], refined)
        self.assertEqual(condition_facts(state).artifact_states["draft"], "verified")
        self.assertEqual(state.nodes["sink"].status, NodeStatus.READY)
        self.assertEqual(state.nodes["sink"].selected_inputs["draft"], "new.md")

    def test_condition_and_all_active_join_advance_in_topological_order(self):
        plan = self.compile(
            [
                condition("choose", entry=True, cases=({"outcome": "left", "when": {"op": "fact_is", "name": "take_left", "value": True}},)),
                task("left", outputs=("draft",)),
                task("right", outputs=("draft",)),
                join("merge", outputs=("draft",)),
            ],
            [
                edge("choose-left", "choose", "left", trigger="left"),
                edge("choose-right", "choose", "right", trigger="default"),
                edge("left-merge", "left", "merge", output_map={"draft": "draft"}),
                edge("right-merge", "right", "merge", output_map={"draft": "draft"}),
            ],
        )
        state = initial_run(plan, "run-branch")
        state = replace(state, project_booleans=MappingProxyType({"take_left": True}))
        state, controls = stabilize_control_nodes(plan, refresh_ready(plan, state))
        self.assertEqual(tuple(item.node_id for item in controls), ("choose",))
        self.assertEqual(state.edges["choose-right"].status, EdgeStatus.INACTIVE)
        self.assertEqual(state.nodes["right"].status, NodeStatus.SKIPPED)
        state = self.complete(plan, state, "left", outputs={"draft": "left.md"})
        state, controls = stabilize_control_nodes(plan, state)
        self.assertEqual(tuple(item.node_id for item in controls), ("merge",))
        self.assertEqual(state.nodes["merge"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(state.nodes["merge"].selected_inputs["draft"], "left.md")

    def test_all_active_join_with_conflicting_outputs_becomes_durably_blocked(self):
        plan = self.compile(
            [
                task("left", entry=True, outputs=("draft",)),
                task("right", entry=True, outputs=("draft",)),
                join("merge", outputs=("draft",)),
                task("sink"),
            ],
            [
                edge("left-merge", "left", "merge", output_map={"draft": "draft"}),
                edge("right-merge", "right", "merge", output_map={"draft": "draft"}),
                edge("merge-sink", "merge", "sink"),
            ],
            max_parallelism=2,
        )
        state = initial_run(plan, "run-ambiguous-all-join")
        state = self.complete(plan, state, "left", outputs={"draft": "left.md"})
        state = self.complete(plan, state, "right", outputs={"draft": "right.md"})
        self.assertEqual(state.nodes["merge"].status, NodeStatus.BLOCKED)
        self.assertEqual(state.edges["merge-sink"].status, EdgeStatus.FAILED)
        self.assertEqual(state.nodes["sink"].status, NodeStatus.BLOCKED)
        self.assertEqual(refresh_ready(plan, state), state)

    def test_all_active_join_with_missing_declared_output_becomes_blocked(self):
        plan = self.compile(
            [task("source", entry=True, outputs=("other",)), join("merge", outputs=("draft",))],
            [edge("source-merge", "source", "merge")],
        )
        state = self.complete(plan, initial_run(plan, "run-incomplete-all-join"), "source", outputs={"other": "other.md"})
        self.assertEqual(state.nodes["merge"].status, NodeStatus.BLOCKED)
        self.assertEqual(refresh_ready(plan, state), state)

    def test_all_inactive_path_skips_recursively_instead_of_becoming_ready(self):
        plan = self.compile(
            [
                condition("choose", entry=True, cases=({"outcome": "selected", "when": {"op": "fact_is", "name": "selected", "value": True}},)),
                task("selected-terminal"),
                task("excluded", outputs=("draft",)),
                join("excluded-join", outputs=("draft",)),
            ],
            [
                edge("choose-selected", "choose", "selected-terminal", trigger="selected"),
                edge("choose-excluded", "choose", "excluded", trigger="default"),
                edge("excluded-join-edge", "excluded", "excluded-join", output_map={"draft": "draft"}),
            ],
        )
        state = initial_run(plan, "run-inactive")
        state = replace(state, project_booleans=MappingProxyType({"selected": True}))
        state, _ = stabilize_control_nodes(plan, state)
        self.assertEqual(state.nodes["excluded"].status, NodeStatus.SKIPPED)
        self.assertEqual(state.nodes["excluded-join"].status, NodeStatus.SKIPPED)
        self.assertEqual(state.edges["excluded-join-edge"].status, EdgeStatus.INACTIVE)

    def test_any_success_freezes_winner_and_records_late_auxiliary_output(self):
        plan = self.compile(
            [task("branch-a", entry=True, outputs=("draft",)), task("branch-b", entry=True, outputs=("draft",)), join("join", outputs=("draft",), mode="any_success")],
            [edge("z-first-arrival", "branch-a", "join", output_map={"draft": "draft"}), edge("a-late-arrival", "branch-b", "join", output_map={"draft": "draft"})],
            max_parallelism=2,
        )
        state = initial_run(plan, "run-any")
        state = self.complete(plan, state, "branch-a", outputs={"draft": "a.md"})
        self.assertEqual(state.nodes["join"].selected_inputs["draft"], "a.md")
        self.assertEqual(state.nodes["join"].winner_edge_id, "z-first-arrival")
        state = self.complete(plan, state, "branch-b", outputs={"draft": "b.md"})
        self.assertEqual(state.nodes["join"].selected_inputs["draft"], "a.md")
        self.assertEqual(state.nodes["join"].winner_edge_id, "z-first-arrival")
        state, _ = stabilize_control_nodes(plan, state)
        self.assertEqual(state.nodes["join"].selected_inputs["draft"], "a.md")
        self.assertEqual(state.nodes["join"].auxiliary_outputs["draft"], ("b.md",))

    def test_any_success_winner_identity_survives_same_path_auxiliary_and_cuts_invalidated_route(self):
        plan = self.compile(
            [task("a", entry=True, outputs=("draft",)), task("b", entry=True, outputs=("draft",)),
             join("joined", outputs=("draft",), mode="any_success"),
             task("consumer", inputs=("draft",))],
            [edge("a-join", "a", "joined", output_map={"draft": "draft"}),
             edge("b-join", "b", "joined", output_map={"draft": "draft"}),
             edge("join-consumer", "joined", "consumer")],
            max_parallelism=2,
        )
        state = initial_run(plan, "run-same-path")
        state = self.complete(plan, state, "b", outputs={"draft": "shared.md"})
        self.assertEqual(state.nodes["joined"].winner_edge_id, "b-join")
        state = self.complete(plan, state, "a", outputs={"draft": "shared.md"})
        self.assertEqual(state.nodes["joined"].winner_edge_id, "b-join")
        self.assertEqual(state.edges["a-join"].status, EdgeStatus.SATISFIED)
        self.assertEqual(state.nodes["joined"].auxiliary_outputs, {})
        state, _ = stabilize_control_nodes(plan, state)
        unaffected = mark_descendants_stale(plan, state, ("a",))
        self.assertEqual(unaffected.nodes["joined"].status, NodeStatus.SUCCEEDED)
        invalidated = mark_descendants_stale(plan, state, ("b",))
        self.assertEqual(invalidated.nodes["joined"].status, NodeStatus.STALE)
        self.assertEqual(invalidated.edges["join-consumer"].status, EdgeStatus.WAITING)
        self.assertEqual(invalidated.edges["join-consumer"].selected_output_map, {})
        self.assertEqual(invalidated.nodes["joined"].winner_edge_id, "b-join")

    def test_simultaneous_first_satisfactions_have_no_implicit_plan_order_winner(self):
        plan = self.compile(
            [task("a", entry=True, outputs=("draft",)), task("b", entry=True, outputs=("draft",)),
             join("joined", outputs=("draft",), mode="any_success")],
            [edge("a-join", "a", "joined", output_map={"draft": "draft"}),
             edge("b-join", "b", "joined", output_map={"draft": "draft"})],
        )
        state = initial_run(plan, "run-simultaneous")
        nodes = dict(state.nodes)
        nodes["a"] = replace(nodes["a"], status=NodeStatus.SUCCEEDED,
                             outcome="succeeded", outputs={"draft": "a.md"})
        nodes["b"] = replace(nodes["b"], status=NodeStatus.SUCCEEDED,
                             outcome="succeeded", outputs={"draft": "b.md"})
        candidate = replace(state, nodes=MappingProxyType(nodes), edges=MappingProxyType({
            "a-join": EdgeRuntime(EdgeStatus.SATISFIED, {"draft": "a.md"}),
            "b-join": EdgeRuntime(EdgeStatus.SATISFIED, {"draft": "b.md"}),
        }))
        with self.assertRaises(WorkflowError) as caught:
            refresh_ready(plan, candidate)
        self.assertEqual(caught.exception.code, "runtime.ambiguous_winner")

    def test_any_success_blocks_only_after_every_active_edge_is_terminal(self):
        plan = self.compile(
            [task("a", entry=True, outputs=("draft",)), task("b", entry=True, outputs=("draft",)), join("join", outputs=("draft",), mode="any_success")],
            [edge("a-join", "a", "join", output_map={"draft": "draft"}), edge("b-join", "b", "join", output_map={"draft": "draft"})],
            max_parallelism=2,
        )
        state = initial_run(plan, "run-any-fail")
        first = claim_transition(plan, state, "a", "a-token")
        first = result_transition(plan, first, {"node_id": "a", "attempt": 1, "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
        self.assertEqual(first.nodes["join"].status, NodeStatus.PENDING)
        second = claim_transition(plan, first, "b", "b-token")
        second = result_transition(plan, second, {"node_id": "b", "attempt": 1, "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
        self.assertEqual(second.nodes["join"].status, NodeStatus.BLOCKED)

    def test_partial_any_success_winner_is_rejected_before_runtime(self):
        compiled = self.compile_result(
            [
                task("partial", entry=True, outputs=("draft",)),
                task("complete", entry=True, outputs=("draft", "notes")),
                join("join", outputs=("draft", "notes"), mode="any_success"),
            ],
            [
                edge("partial-join", "partial", "join", output_map={"draft": "draft"}),
                edge(
                    "complete-join",
                    "complete",
                    "join",
                    output_map={"draft": "draft", "notes": "notes"},
                ),
            ],
            max_parallelism=2,
        )
        self.assertIsNone(compiled.plan)
        self.assertIn("join.output_map_contract", {issue.code for issue in compiled.errors})

    def test_condition_facts_use_only_the_immutable_recorded_snapshot(self):
        plan = self.compile([task("entry", entry=True)], [])
        state = initial_run(plan, "run-facts")
        state = replace(
            self.register(state, self.artifact("sources")),
            decisions=MappingProxyType({"route": "review"}),
            project_booleans=MappingProxyType({"has_manifest": True}),
        )
        facts = condition_facts(state)
        changed = self.register(state, self.artifact("sources", state="stale"))
        self.assertEqual(facts.artifact_states["sources"], "verified")
        self.assertEqual(condition_facts(changed).artifact_states["sources"], "stale")
        self.assertEqual(facts.decisions["route"], "review")
        self.assertIs(facts.project_booleans["has_manifest"], True)

    def test_verified_artifact_fact_selects_condition_branch_without_filesystem_access(self):
        plan = self.compile(
            [
                condition("has-sources", entry=True, cases=({"outcome": "verified", "when": {"op": "artifact_state_is", "artifact": "sources", "value": "verified"}},)),
                task("use-sources"),
                task("no-sources"),
            ],
            [
                edge("has-sources-verified", "has-sources", "use-sources", trigger="verified"),
                edge("has-sources-default", "has-sources", "no-sources", trigger="default"),
            ],
        )
        state = self.register(initial_run(plan, "run-artifact-condition"), self.artifact("sources", path="does-not-need-to-exist.md"))
        stabilized, transitions = stabilize_control_nodes(plan, state)
        self.assertEqual(stabilized.nodes["has-sources"].outcome, "verified")
        self.assertEqual(tuple(item.node_id for item in transitions), ("has-sources",))
        self.assertEqual(stabilized.edges["has-sources-default"].status, EdgeStatus.INACTIVE)

    def test_stabilization_orders_multiple_ready_controls_by_compiled_topology(self):
        nodes = []
        edges = []
        for prefix in ("b", "a"):
            nodes.extend([
                condition(prefix, entry=True, cases=({"outcome": "yes", "when": {"op": "fact_is", "name": prefix, "value": True}},)),
                task(f"{prefix}-yes"),
                task(f"{prefix}-default"),
            ])
            edges.extend([
                edge(f"{prefix}-yes-edge", prefix, f"{prefix}-yes", trigger="yes"),
                edge(f"{prefix}-default-edge", prefix, f"{prefix}-default", trigger="default"),
            ])
        plan = self.compile(nodes, edges)
        state = initial_run(plan, "run-controls")
        state = replace(state, project_booleans=MappingProxyType({"a": True, "b": True}))
        _, controls = stabilize_control_nodes(plan, state)
        self.assertEqual(tuple(item.node_id for item in controls), ("a", "b"))

    def test_claim_is_task_only_attempt_bound_hashed_and_parallel_capped(self):
        plan = self.compile(
            [task("a", entry=True), task("b", entry=True), validator("check", entry=True)],
            [],
            external_inputs=("section",),
            max_parallelism=1,
        )
        state = refresh_ready(plan, self.register(initial_run(plan, "run-claim"), self.artifact("section")))
        self.assertEqual(ready_node_ids(plan, state), ("a", "b", "check"))
        claimed = claim_transition(plan, state, "a", "secret-token")
        self.assertEqual(claimed.nodes["a"].status, NodeStatus.RUNNING)
        self.assertEqual(claimed.nodes["a"].attempt, 1)
        self.assertEqual(claimed.nodes["a"].claim_token_hash, hashlib.sha256(b"secret-token").hexdigest())
        with self.assertRaises(WorkflowError) as capped:
            claim_transition(plan, claimed, "b", "other-token")
        self.assertEqual(capped.exception.code, "runtime.parallelism_exceeded")
        with self.assertRaises(WorkflowError) as wrong_type:
            claim_transition(plan, state, "check", "validator-token")
        self.assertEqual(wrong_type.exception.code, "runtime.node_type")

    def test_result_verifies_attempt_outcome_outputs_and_records_artifacts(self):
        plan = self.compile([task("produce", entry=True, outputs=("draft",))], [])
        state = claim_transition(plan, initial_run(plan, "run-result"), "produce", "token")
        artifact = self.artifact("draft", "draft.md", node="produce", attempt=1)
        completed = result_transition(
            plan,
            state,
            {"node_id": "produce", "attempt": 1, "status": "succeeded", "outcome": "succeeded", "outputs": {"draft": "draft.md"}, "artifacts": (artifact,)},
        )
        self.assertEqual(completed.nodes["produce"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(completed.artifacts["draft"], artifact)
        with self.assertRaises(WorkflowError) as stale_attempt:
            result_transition(plan, state, {"node_id": "produce", "attempt": 2, "status": "succeeded", "outcome": "succeeded", "outputs": {}, "artifacts": ()})
        self.assertEqual(stale_attempt.exception.code, "runtime.stale_attempt")
        with self.assertRaises(WorkflowError) as bad_outcome:
            result_transition(plan, state, {"node_id": "produce", "attempt": 1, "status": "succeeded", "outcome": "invented", "outputs": {}, "artifacts": ()})
        self.assertEqual(bad_outcome.exception.code, "runtime.invalid_outcome")

    def test_result_fails_closed_for_invalid_state_status_and_output_contract(self):
        plan = self.compile([task("produce", entry=True, outputs=("draft",))], [])
        ready = initial_run(plan, "run-invalid-result")
        with self.assertRaises(WorkflowError) as not_running:
            result_transition(plan, ready, {"node_id": "produce", "attempt": 0, "status": "succeeded", "outcome": "succeeded", "outputs": {"draft": "draft.md"}, "artifacts": ()})
        self.assertEqual(not_running.exception.code, "runtime.node_not_running")
        running = claim_transition(plan, ready, "produce", "token")
        with self.assertRaises(WorkflowError) as invalid_status:
            result_transition(plan, running, {"node_id": "produce", "attempt": 1, "status": "skipped", "outcome": "succeeded", "outputs": {"draft": "draft.md"}, "artifacts": ()})
        self.assertEqual(invalid_status.exception.code, "runtime.invalid_result_status")
        with self.assertRaises(WorkflowError) as undeclared_output:
            result_transition(plan, running, {"node_id": "produce", "attempt": 1, "status": "succeeded", "outcome": "succeeded", "outputs": {"other": "other.md"}, "artifacts": ()})
        self.assertEqual(undeclared_output.exception.code, "runtime.invalid_outputs")

    def test_result_rejects_wrong_artifact_receipts_and_copies_mutable_payloads(self):
        plan = self.compile([task("produce", entry=True, outputs=("draft",))], [])
        running = claim_transition(plan, initial_run(plan, "run-artifact-receipt"), "produce", "token")
        wrong = self.artifact("draft", "draft.md", node="other", attempt=1)
        with self.assertRaises(WorkflowError) as wrong_producer:
            result_transition(plan, running, {"node_id": "produce", "attempt": 1, "status": "succeeded", "outcome": "succeeded", "outputs": {"draft": "draft.md"}, "artifacts": (wrong,)})
        self.assertEqual(wrong_producer.exception.code, "runtime.invalid_artifact")
        outputs = {"draft": "draft.md"}
        artifact = self.artifact("draft", "draft.md", node="produce", attempt=1)
        completed = result_transition(plan, running, {"node_id": "produce", "attempt": 1, "status": "succeeded", "outcome": "succeeded", "outputs": outputs, "artifacts": (artifact,)})
        outputs["draft"] = "mutated.md"
        self.assertEqual(completed.nodes["produce"].outputs["draft"], "draft.md")

    def test_failed_result_rejects_falsey_values_with_wrong_types(self):
        plan = self.compile([task("source", entry=True)], [])
        running = claim_transition(plan, initial_run(plan, "run-invalid-failure"), "source", "token")
        invalid_fields = (
            {"outcome": None, "outputs": {}, "artifacts": ()},
            {"outcome": "", "outputs": None, "artifacts": ()},
            {"outcome": "", "outputs": [], "artifacts": ()},
            {"outcome": "", "outputs": {}, "artifacts": None},
            {"outcome": "", "outputs": {}, "artifacts": {}},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with self.assertRaises(WorkflowError) as invalid:
                    result_transition(
                        plan,
                        running,
                        {
                            "node_id": "source",
                            "attempt": 1,
                            "status": "failed",
                            **fields,
                        },
                    )
                self.assertEqual(invalid.exception.code, "runtime.invalid_failure")
        self.assertEqual(running.nodes["source"].status, NodeStatus.RUNNING)
        accepted = result_transition(
            plan,
            running,
            {
                "node_id": "source",
                "attempt": 1,
                "status": "failed",
                "outcome": "",
                "outputs": {},
                "artifacts": [],
            },
        )
        self.assertEqual(accepted.nodes["source"].status, NodeStatus.FAILED)

    def test_block_failure_propagates_and_retry_rechecks_dependencies(self):
        plan = self.compile(
            [task("source", entry=True), task("sink")],
            [edge("source-sink", "source", "sink")],
        )
        running = claim_transition(plan, initial_run(plan, "run-retry"), "source", "token")
        failed = result_transition(plan, running, {"node_id": "source", "attempt": 1, "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
        self.assertEqual(failed.nodes["source"].status, NodeStatus.FAILED)
        self.assertEqual(failed.edges["source-sink"].status, EdgeStatus.FAILED)
        self.assertEqual(failed.nodes["sink"].status, NodeStatus.BLOCKED)
        retried = retry_transition(plan, failed, "source")
        self.assertEqual(retried.nodes["source"].status, NodeStatus.READY)
        self.assertEqual(retried.nodes["source"].attempt, 1)
        self.assertEqual(retried.nodes["sink"].status, NodeStatus.PENDING)

        with self.assertRaises(WorkflowError) as invalid_retry:
            retry_transition(plan, retried, "source")
        self.assertEqual(invalid_retry.exception.code, "runtime.invalid_retry")

    def test_skip_branch_failure_deactivates_and_recursively_skips(self):
        plan = self.compile(
            [task("source", entry=True, failure_policy="skip_branch"), task("sink")],
            [edge("source-sink", "source", "sink")],
        )
        running = claim_transition(plan, initial_run(plan, "run-skip"), "source", "token")
        skipped = result_transition(plan, running, {"node_id": "source", "attempt": 1, "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
        self.assertEqual(skipped.nodes["source"].status, NodeStatus.SKIPPED)
        self.assertEqual(skipped.edges["source-sink"].status, EdgeStatus.INACTIVE)
        self.assertEqual(skipped.nodes["sink"].status, NodeStatus.SKIPPED)
        retried = retry_transition(plan, skipped, "source")
        self.assertEqual(retried.nodes["source"].status, NodeStatus.READY)
        self.assertEqual(retried.nodes["source"].attempt, 1)
        self.assertEqual(retried.nodes["sink"].status, NodeStatus.PENDING)
        self.assertEqual(retried.edges["source-sink"].status, EdgeStatus.WAITING)
        with self.assertRaises(WorkflowError) as repeated:
            retry_transition(plan, retried, "source")
        self.assertEqual(repeated.exception.code, "runtime.invalid_retry")
        running_again = claim_transition(plan, retried, "source", "retry-token")
        succeeded = result_transition(plan, running_again, {
            "node_id": "source", "attempt": 2, "status": "succeeded",
            "outcome": "succeeded", "outputs": {}, "artifacts": (),
        })
        self.assertEqual(succeeded.nodes["sink"].status, NodeStatus.READY)

    def test_skip_branch_retry_is_rejected_after_a_descendant_has_completed(self):
        plan = self.compile(
            [
                task("source", entry=True, failure_policy="skip_branch"),
                task("other", entry=True),
                task("sink"),
            ],
            [edge("source-sink", "source", "sink"), edge("other-sink", "other", "sink")],
            max_parallelism=2,
        )
        state = initial_run(plan, "run-retry-after-descendant")
        failed = claim_transition(plan, state, "source", "source-first-token")
        failed = result_transition(plan, failed, {
            "node_id": "source", "attempt": 1, "status": "failed",
            "outcome": "", "outputs": {}, "artifacts": (),
        })
        state = self.complete(plan, failed, "other")
        state = self.complete(plan, state, "sink")
        with self.assertRaises(WorkflowError) as caught:
            retry_transition(plan, state, "source")
        self.assertEqual(caught.exception.code, "runtime.retry_descendant_started")
        self.assertEqual(state.nodes["source"].status, NodeStatus.SKIPPED)
        self.assertEqual(state.nodes["sink"].status, NodeStatus.SUCCEEDED)

    def test_skip_branch_retry_does_not_reopen_a_frozen_any_success_winner(self):
        plan = self.compile(
            [
                task("source", entry=True, outputs=("draft",), failure_policy="skip_branch"),
                task("other", entry=True, outputs=("draft",)),
                join("race", outputs=("draft",), mode="any_success"),
                task("sink"),
            ],
            [
                edge("source-race", "source", "race", output_map={"draft": "draft"}),
                edge("other-race", "other", "race", output_map={"draft": "draft"}),
                edge("race-sink", "race", "sink"),
            ],
            max_parallelism=2,
        )
        state = claim_transition(plan, initial_run(plan, "run-retry-frozen-winner"), "source", "source-token")
        state = result_transition(plan, state, {
            "node_id": "source", "attempt": 1, "status": "failed",
            "outcome": "", "outputs": {}, "artifacts": (),
        })
        state = self.complete(plan, state, "other", outputs={"draft": "other.md"})
        state, _ = stabilize_control_nodes(plan, state)
        state = self.complete(plan, state, "sink")
        winner = state.nodes["race"].winner_edge_id
        self.assertEqual(winner, "other-race")

        retried = retry_transition(plan, state, "source")
        self.assertEqual(retried.nodes["race"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(retried.nodes["race"].winner_edge_id, winner)
        self.assertEqual(retried.nodes["sink"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(retried.edges["source-race"].status, EdgeStatus.WAITING)

        running = claim_transition(plan, retried, "source", "source-retry-token")
        completed = result_transition(plan, running, {
            "node_id": "source", "attempt": 2, "status": "succeeded",
            "outcome": "succeeded", "outputs": {"draft": "source.md"}, "artifacts": (),
        })
        self.assertEqual(completed.nodes["race"].winner_edge_id, winner)
        self.assertEqual(completed.nodes["race"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(completed.nodes["sink"].status, NodeStatus.SUCCEEDED)

    def test_mark_descendants_stale_uses_compiled_adjacency_without_mutating_old_state(self):
        plan = self.compile(
            [task("a", entry=True), task("b"), task("c")],
            [edge("a-b", "a", "b"), edge("b-c", "b", "c")],
        )
        state = initial_run(plan, "run-stale")
        state = self.complete(plan, state, "a")
        state = self.complete(plan, state, "b")
        state = self.complete(plan, state, "c")
        stale = mark_descendants_stale(plan, state, ("a",))
        self.assertEqual(state.nodes["b"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(stale.nodes["a"].status, NodeStatus.SUCCEEDED)
        self.assertEqual(stale.nodes["b"].status, NodeStatus.STALE)
        self.assertEqual(stale.nodes["c"].status, NodeStatus.STALE)

    def test_rerun_stale_requeues_only_the_stale_closure_without_consuming_attempts(self):
        plan = self.compile(
            [
                task("a", entry=True, outputs=("draft",)),
                task("b", inputs=("draft",)),
                task("c"),
                task("independent", entry=True),
            ],
            [edge("a-b", "a", "b", output_map={"draft": "draft"}), edge("b-c", "b", "c")],
        )
        state = initial_run(plan, "run-stale-rerun")
        state = self.complete(
            plan,
            state,
            "a",
            outputs={"draft": "draft.md"},
            artifacts=(self.artifact("draft", "draft.md", node="a", attempt=1),),
        )
        state = self.complete(plan, state, "b")
        state = self.complete(plan, state, "c")
        state = self.complete(plan, state, "independent")

        nodes = dict(state.nodes)
        for node_id in ("a", "b", "c"):
            nodes[node_id] = replace(
                nodes[node_id], status=NodeStatus.STALE, outcome="",
                selected_inputs=MappingProxyType({}),
                auxiliary_outputs=MappingProxyType({}), claim_token_hash="",
            )
        edges = {edge_id: EdgeRuntime(EdgeStatus.WAITING) for edge_id in plan.edges}
        stale = replace(
            state,
            nodes=MappingProxyType(nodes),
            edges=MappingProxyType(edges),
            artifacts=MappingProxyType({
                "draft": replace(state.artifacts["draft"], state="stale"),
            }),
        )
        rerun, affected = rerun_stale_transition(plan, stale, "a")

        self.assertEqual(affected, ("a", "b", "c"))
        self.assertEqual(rerun.nodes["a"].status, NodeStatus.READY)
        self.assertEqual(rerun.nodes["b"].status, NodeStatus.PENDING)
        self.assertEqual(rerun.nodes["c"].status, NodeStatus.PENDING)
        self.assertEqual(rerun.nodes["independent"], stale.nodes["independent"])
        self.assertEqual(rerun.nodes["a"].attempt, stale.nodes["a"].attempt)
        self.assertEqual(rerun.nodes["a"].outputs, stale.nodes["a"].outputs)
        self.assertEqual(rerun.artifacts["draft"].state, "stale")
        self.assertTrue(all(
            rerun.edges[edge_id] == EdgeRuntime(EdgeStatus.WAITING)
            for node_id in affected for edge_id in plan.outgoing[node_id]
        ))
        self.assertEqual(stale.nodes["a"].status, NodeStatus.STALE)

        for invalid_id in ("missing", "independent"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(WorkflowError) as caught:
                rerun_stale_transition(plan, stale, invalid_id)
            self.assertEqual(caught.exception.code, "runtime.invalid_stale_rerun")
        with self.assertRaises(WorkflowError) as duplicate:
            rerun_stale_transition(plan, rerun, "a")
        self.assertEqual(duplicate.exception.code, "runtime.invalid_stale_rerun")

    def test_rerun_stale_refuses_to_reset_a_frozen_any_success_winner(self):
        plan = self.compile(
            [
                task("a", entry=True, outputs=("draft",)),
                task("b", entry=True, outputs=("draft",)),
                join("joined", outputs=("draft",), mode="any_success"),
                task("consumer", inputs=("draft",)),
            ],
            [
                edge("a-joined", "a", "joined", output_map={"draft": "draft"}),
                edge("b-joined", "b", "joined", output_map={"draft": "draft"}),
                edge("joined-consumer", "joined", "consumer", output_map={"draft": "draft"}),
            ],
        )
        state = initial_run(plan, "run-stale-frozen-winner")
        state = self.complete(
            plan, state, "b", outputs={"draft": "draft.md"},
            artifacts=(self.artifact("draft", "draft.md", node="b", attempt=1),),
        )
        state, _ = stabilize_control_nodes(plan, state)
        self.assertEqual(state.nodes["joined"].status, NodeStatus.SUCCEEDED)
        invalidated = mark_descendants_stale(plan, state, ("b",))
        nodes = dict(invalidated.nodes)
        nodes["b"] = replace(
            nodes["b"], status=NodeStatus.STALE, outcome="",
            selected_inputs=MappingProxyType({}),
            auxiliary_outputs=MappingProxyType({}), claim_token_hash="",
        )
        invalidated = replace(
            invalidated,
            nodes=MappingProxyType(nodes),
            artifacts=MappingProxyType({
                "draft": replace(invalidated.artifacts["draft"], state="stale"),
            }),
        )
        before = invalidated
        with self.assertRaises(WorkflowError) as caught:
            rerun_stale_transition(plan, invalidated, "b")
        self.assertEqual(caught.exception.code, "runtime.stale_join_requires_new_run")
        self.assertIs(invalidated, before)

    def test_rerun_stale_closure_includes_artifact_state_condition_and_both_branches(self):
        plan = self.compile(
            [
                task("a", entry=True, outputs=("doc",)),
                condition("choose", entry=True, cases=(
                    {"outcome": "drifted", "when": {
                        "op": "artifact_state_is", "artifact": "doc", "value": "stale",
                    }},
                )),
                task("on-stale"),
                task("default"),
            ],
            [
                edge("choose-on-stale", "choose", "on-stale", trigger="drifted"),
                edge("choose-default", "choose", "default", trigger="default"),
            ],
        )
        state = initial_run(plan, "run-artifact-state-condition-rerun")
        state = self.complete(
            plan, state, "a", outputs={"doc": "doc.md"},
            artifacts=(self.artifact("doc", "doc.md", node="a", attempt=1),),
        )
        nodes = {
            node_id: replace(
                runtime, status=NodeStatus.STALE, outcome="",
                selected_inputs=MappingProxyType({}),
                auxiliary_outputs=MappingProxyType({}), claim_token_hash="",
            )
            for node_id, runtime in state.nodes.items()
        }
        stale = replace(
            state,
            nodes=MappingProxyType(nodes),
            edges=MappingProxyType({
                edge_id: EdgeRuntime(EdgeStatus.WAITING) for edge_id in plan.edges
            }),
            artifacts=MappingProxyType({
                "doc": replace(state.artifacts["doc"], state="stale"),
            }),
        )

        rerun, affected = rerun_stale_transition(plan, stale, "a")
        self.assertEqual(affected, ("a", "choose", "default", "on-stale"))
        self.assertEqual(rerun.nodes["choose"].status, NodeStatus.READY)
        self.assertEqual(rerun.nodes["default"].status, NodeStatus.PENDING)
        self.assertEqual(rerun.nodes["on-stale"].status, NodeStatus.PENDING)
        stabilized, controls = stabilize_control_nodes(plan, rerun)
        self.assertEqual(stabilized.nodes["choose"].outcome, "drifted")
        self.assertEqual(stabilized.nodes["on-stale"].status, NodeStatus.READY)
        self.assertEqual(stabilized.nodes["default"].status, NodeStatus.SKIPPED)
        self.assertEqual(tuple(item.node_id for item in controls), ("choose",))

    def test_rerun_stale_waits_for_a_reverified_external_input(self):
        plan = self.compile(
            [task("source", entry=True, inputs=("request",))],
            [],
            external_inputs=("request",),
        )
        initial = self.register(
            initial_run(plan, "run-stale-external-input"),
            self.artifact("request", "old-request.md"),
        )
        completed = self.complete(plan, initial, "source")
        stale = replace(
            completed,
            nodes=MappingProxyType({
                "source": replace(
                    completed.nodes["source"], status=NodeStatus.STALE, outcome="",
                    selected_inputs=MappingProxyType({}),
                    auxiliary_outputs=MappingProxyType({}), claim_token_hash="",
                ),
            }),
            artifacts=MappingProxyType({
                "request": replace(completed.artifacts["request"], state="stale"),
            }),
        )

        rerun, affected = rerun_stale_transition(plan, stale, "source")
        self.assertEqual(affected, ("source",))
        self.assertEqual(rerun.nodes["source"].status, NodeStatus.PENDING)
        self.assertEqual(rerun.nodes["source"].attempt, 1)

        fresh = self.artifact("request", "new-request.md")
        refreshed = refresh_ready(
            plan,
            replace(rerun, artifacts=MappingProxyType({"request": fresh})),
        )
        self.assertEqual(refreshed.nodes["source"].status, NodeStatus.READY)
        self.assertEqual(refreshed.nodes["source"].selected_inputs["request"], "new-request.md")

    def test_runtime_snapshots_and_nested_maps_are_immutable(self):
        plan = self.compile([task("entry", entry=True)], [])
        initial = initial_run(plan, "run-immutable")
        with self.assertRaises(TypeError):
            initial.nodes["entry"] = replace(initial.nodes["entry"], status=NodeStatus.FAILED)
        with self.assertRaises(TypeError):
            initial.nodes["entry"].outputs["draft"] = "changed.md"
        claimed = claim_transition(plan, initial, "entry", "token")
        self.assertEqual(initial.nodes["entry"].status, NodeStatus.READY)
        self.assertEqual(claimed.nodes["entry"].status, NodeStatus.RUNNING)

    def test_decisions_and_project_facts_are_validated_scalar_snapshots(self):
        plan = self.compile([task("entry", entry=True)], [])
        initial = initial_run(plan, "run-fact-values")
        invalid_decisions = (
            {"nested": {"owned": []}},
            {"nested": ["owned"]},
            {"not-finite": math.inf},
            {"not-a-number": math.nan},
        )
        for decisions in invalid_decisions:
            with self.subTest(decisions=decisions):
                with self.assertRaises(WorkflowError) as invalid:
                    replace(initial, decisions=decisions)
                self.assertEqual(invalid.exception.code, "runtime.invalid_decision")
        with self.assertRaises(WorkflowError) as invalid_fact:
            replace(initial, project_booleans={"has_manifest": 1})
        self.assertEqual(invalid_fact.exception.code, "runtime.invalid_project_fact")

        decisions = {"boolean": True, "number": 1, "label": "review", "empty": None}
        project_facts = {"has_manifest": True}
        state = replace(initial, decisions=decisions, project_booleans=project_facts)
        decisions["boolean"] = False
        project_facts["has_manifest"] = False
        facts = condition_facts(state)
        self.assertIs(facts.decisions["boolean"], True)
        self.assertEqual(type(facts.decisions["number"]), int)
        self.assertIs(facts.project_booleans["has_manifest"], True)
        self.assertTrue(evaluate_predicate({"op": "decision_is", "name": "boolean", "value": True}, facts))
        self.assertFalse(evaluate_predicate({"op": "decision_is", "name": "boolean", "value": 1}, facts))
        self.assertTrue(evaluate_predicate({"op": "decision_is", "name": "number", "value": 1}, facts))


    def test_validator_domain_outcomes_complete_only_matching_gate(self):
        nodes = [validator("check", entry=True), *(task(f"{name}-task") for name in ("pass", "fail", "blocked"))]
        routes = [edge(f"to-{name}", "check", f"{name}-task", trigger=name)
                  for name in ("pass", "fail", "blocked")]
        plan = self.compile(nodes, routes, external_inputs=("section",))
        initial = self.register(initial_run(plan, "run-validator"), self.artifact("section"))
        for outcome in ("pass", "fail", "blocked"):
            with self.subTest(outcome=outcome):
                running = validator_claim_transition(plan, refresh_ready(plan, initial), "check", "token")
                completed = validator_result_transition(plan, running, {
                    "node_id": "check", "attempt": 1, "status": "succeeded",
                    "outcome": outcome, "outputs": {}, "artifacts": (),
                })
                self.assertEqual(completed.nodes["check"].status, NodeStatus.SUCCEEDED)
                self.assertEqual(completed.nodes["check"].outcome, outcome)
                for name in ("pass", "fail", "blocked"):
                    self.assertEqual(completed.edges[f"to-{name}"].status,
                                     EdgeStatus.SATISFIED if name == outcome else EdgeStatus.INACTIVE)
                self.assertEqual(completed.nodes[f"{outcome}-task"].status, NodeStatus.READY)
                with self.assertRaises(WorkflowError):
                    validator_retry_transition(plan, completed, "check")

    def test_validator_execution_failure_retry_and_type_isolation(self):
        plan = self.compile([validator("check", entry=True), task("sink")],
                            [edge("gate", "check", "sink", trigger="pass")],
                            external_inputs=("section",))
        ready = refresh_ready(plan, self.register(initial_run(plan, "run-validator-failure"),
                                                  self.artifact("section")))
        running = validator_claim_transition(plan, ready, "check", "first-token")
        failed = validator_result_transition(plan, running, {
            "node_id": "check", "attempt": 1, "status": "failed",
            "outcome": "", "outputs": {}, "artifacts": (),
        })
        self.assertEqual(failed.nodes["check"].status, NodeStatus.FAILED)
        self.assertEqual(failed.edges["gate"].status, EdgeStatus.FAILED)
        self.assertEqual(failed.nodes["sink"].status, NodeStatus.BLOCKED)
        retried = validator_retry_transition(plan, failed, "check")
        self.assertEqual(retried.nodes["check"].status, NodeStatus.READY)
        self.assertEqual(retried.nodes["check"].attempt, 1)
        self.assertEqual(retried.nodes["check"].claim_token_hash, "")
        self.assertEqual(retried.edges["gate"].status, EdgeStatus.WAITING)
        self.assertEqual(validator_claim_transition(plan, retried, "check", "next-token").nodes["check"].attempt, 2)
        interrupted = replace(running, nodes={
            **running.nodes, "check": replace(running.nodes["check"], status=NodeStatus.BLOCKED),
        })
        resumed = validator_retry_transition(plan, interrupted, "check")
        self.assertEqual((resumed.nodes["check"].status, resumed.nodes["check"].attempt,
                          resumed.nodes["check"].claim_token_hash), (NodeStatus.READY, 1, ""))
        tokenless_blocked = replace(interrupted, nodes={
            **interrupted.nodes, "check": replace(interrupted.nodes["check"], claim_token_hash=""),
        })
        with self.assertRaises(WorkflowError):
            validator_retry_transition(plan, tokenless_blocked, "check")
        for invalid in ({"node_id": "check", "attempt": 1, "status": "succeeded", "outcome": "pass",
                         "outputs": {"invented": "file"}, "artifacts": ()},
                        {"node_id": "check", "attempt": 1, "status": "failed", "outcome": "fail",
                         "outputs": {}, "artifacts": ()}):
            with self.assertRaises(WorkflowError):
                validator_result_transition(plan, running, invalid)
        with self.assertRaises(WorkflowError):
            claim_transition(plan, ready, "check", "token")
        with self.assertRaises(WorkflowError):
            result_transition(plan, running, {"node_id": "check", "attempt": 1,
                "status": "succeeded", "outcome": "pass", "outputs": {}, "artifacts": ()})
        with self.assertRaises(WorkflowError):
            retry_transition(plan, failed, "check")

    def test_validator_claim_respects_shared_parallel_limit(self):
        plan = self.compile([validator("check", entry=True), task("work", entry=True)], [],
                            external_inputs=("section",), max_parallelism=1)
        ready = refresh_ready(plan, self.register(initial_run(plan, "run-types"), self.artifact("section")))
        running = claim_transition(plan, ready, "work", "task-token")
        with self.assertRaises(WorkflowError) as full:
            validator_claim_transition(plan, running, "check", "validator-token")
        self.assertEqual(full.exception.code, "runtime.parallelism_exceeded")
        with self.assertRaises(WorkflowError):
            validator_claim_transition(plan, ready, "work", "token")
        with self.assertRaises(WorkflowError):
            validator_result_transition(plan, running, {"node_id": "work", "attempt": 1,
                "status": "succeeded", "outcome": "pass", "outputs": {}, "artifacts": ()})

    def test_validator_execution_failure_obeys_skip_branch_policy(self):
        check = validator("check", entry=True)
        check["failure_policy"] = "skip_branch"
        plan = self.compile([check, task("sink")],
                            [edge("gate", "check", "sink", trigger="pass")],
                            external_inputs=("section",))
        ready = refresh_ready(plan, self.register(initial_run(plan, "run-validator-skip"), self.artifact("section")))
        running = validator_claim_transition(plan, ready, "check", "token")
        failed = validator_result_transition(plan, running, {"node_id": "check", "attempt": 1,
            "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
        self.assertEqual(failed.nodes["check"].status, NodeStatus.FAILED)
        self.assertEqual(failed.nodes["check"].outcome, "")
        self.assertEqual(failed.edges["gate"].status, EdgeStatus.INACTIVE)
        self.assertEqual(failed.nodes["sink"].status, NodeStatus.SKIPPED)
        retried = validator_retry_transition(plan, failed, "check")
        self.assertEqual(retried.nodes["check"].status, NodeStatus.READY)
        self.assertEqual(retried.edges["gate"].status, EdgeStatus.WAITING)
        self.assertEqual(retried.nodes["sink"].status, NodeStatus.PENDING)
        running_again = validator_claim_transition(plan, retried, "check", "second-token")
        passed = validator_result_transition(plan, running_again, {"node_id": "check", "attempt": 2,
            "status": "succeeded", "outcome": "pass", "outputs": {}, "artifacts": ()})
        self.assertEqual(passed.edges["gate"].status, EdgeStatus.SATISFIED)
        self.assertEqual(passed.nodes["sink"].status, NodeStatus.READY)

    def test_unclaimed_dependency_blocked_validator_cannot_retry(self):
        plan = self.compile([task("source", entry=True), validator("check")],
                            [edge("source-check", "source", "check")],
                            external_inputs=("section",))
        ready = refresh_ready(plan, self.register(initial_run(plan, "run-never-claimed"),
                                                  self.artifact("section")))
        running = claim_transition(plan, ready, "source", "source-token")
        failed = result_transition(plan, running, {"node_id": "source", "attempt": 1,
            "status": "failed", "outcome": "", "outputs": {}, "artifacts": ()})
        self.assertEqual((failed.nodes["check"].status, failed.nodes["check"].attempt),
                         (NodeStatus.BLOCKED, 0))
        with self.assertRaises(WorkflowError) as rejected:
            validator_retry_transition(plan, failed, "check")
        self.assertEqual(rejected.exception.code, "runtime.invalid_retry")


if __name__ == "__main__":
    unittest.main()
