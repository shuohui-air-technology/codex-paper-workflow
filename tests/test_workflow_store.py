import copy
import concurrent.futures
import errno
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest import mock

from scripts.workflow_engine.catalog import CatalogResult, SkillIdentity, ValidatorIdentity
from scripts.workflow_engine.compiler import CompiledEdge, CompiledNode, CompiledPlan, compile_workflow
from scripts.workflow_engine import fs as workflow_fs
from scripts.workflow_engine import store as workflow_store
from scripts.workflow_engine.receipts import build_claim_evidence, build_stage_receipt
from scripts.workflow_engine.fs import (
    PathSafetyError,
    append_event,
    atomic_write_json,
    ensure_project_directory,
    resolve_project_path,
)
from scripts.workflow_engine.scheduler import (
    ArtifactRuntime,
    ControlTransition,
    EdgeRuntime,
    EdgeStatus,
    NodeStatus,
    claim_transition,
    initial_run,
    result_transition,
    refresh_ready,
    rerun_stale_transition,
    stabilize_control_nodes,
)
from scripts.workflow_engine.schema import document_sha256, parse_workflow
from scripts.workflow_engine.store import (
    Selection,
    StoreError,
    WorkflowEvent,
    WorkflowStore,
    WorkflowTransaction,
)
from tests.test_workflow_scheduler import (
    edge as workflow_edge,
    join as workflow_join,
    task as workflow_task,
)


ZERO_HASH = "0" * 64


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def stale_runtime(runtime):
    return replace(
        runtime,
        status=NodeStatus.STALE,
        outcome="",
        selected_inputs=MappingProxyType({}),
        auxiliary_outputs=MappingProxyType({}),
        claim_token_hash="",
    )


def commit_claim(transaction, running):
    plan, before = transaction._require_loaded()
    node_id, = [key for key in plan.nodes if running.nodes[key].status is NodeStatus.RUNNING
                and before.nodes[key].status is not NodeStatus.RUNNING]
    claim = build_claim_evidence(plan, running, node_id, transaction.input_witnesses(), "2026-09-23T00:00:00Z")
    return transaction.commit_transition("node_claimed", running, {"claim_evidence": claim})


def commit_result(transaction, completed):
    plan, before = transaction._require_loaded()
    node_id, = [key for key in plan.nodes if before.nodes[key].status is NodeStatus.RUNNING
                and completed.nodes[key].status is not NodeStatus.RUNNING]
    claim_event = next(event for event in reversed(transaction.events("node_claimed"))
                       if event.payload["claim_evidence"]["node_id"] == node_id)
    error = None if completed.nodes[node_id].status is NodeStatus.SUCCEEDED else {"code": "test.failed", "message": "Fixture task failed."}
    receipt = build_stage_receipt(plan, completed, node_id, claim_event.payload["claim_evidence"],
        summary="Fixture completion.", uncertainties=[], completed_at="2026-09-23T00:00:01Z", error=error)
    return transaction.commit_receipted_transition("node_result_recorded", completed, receipt,
        result_sha256=sha256_bytes(b"fixture-result"), claim_event_seq=claim_event.event_seq)


def compiled_node(
    node_id,
    *,
    node_type="task",
    entry=False,
    inputs=(),
    outputs=(),
    outcomes=("succeeded",),
    cases=(),
    skill=None,
):
    if node_type == "task" and skill is None:
        skill = SkillIdentity("test-skill", Path("/installed/skills"), "test-skill",
                              sha256_bytes(b"skill"), "sha256:" + sha256_bytes(b"tree"), True)
    return CompiledNode(
        id=node_id,
        type=node_type,
        entry=entry,
        skill=skill,
        validator=None,
        validator_config=None,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        outcomes=tuple(outcomes),
        write_scopes=tuple(outputs),
        failure_policy="block",
        condition_cases=tuple(MappingProxyType(dict(item)) for item in cases),
        join_mode="all_active",
    )


def task_plan(*, workflow_id="stored-flow", semantic_revision=1):
    skill = SkillIdentity(
        catalog_id="test-skill",
        root=Path("/installed/skills"),
        relative_path="test-skill",
        skill_sha256=sha256_bytes(b"skill"),
        tree_sha256="sha256:" + sha256_bytes(b"tree"),
        locked=True,
    )
    node = compiled_node("produce", entry=True, outputs=("draft",), skill=skill)
    semantic = sha256_bytes(f"{workflow_id}:{semantic_revision}".encode())
    return CompiledPlan(
        workflow_id=workflow_id,
        semantic_revision=semantic_revision,
        document_sha256=sha256_bytes(f"document:{semantic_revision}".encode()),
        semantic_sha256=semantic,
        external_inputs=(),
        nodes=MappingProxyType({"produce": node}),
        edges=MappingProxyType({}),
        incoming=MappingProxyType({"produce": ()}),
        outgoing=MappingProxyType({"produce": ()}),
        topological_order=("produce",),
        max_parallelism=1,
    )


def plan_for_document(document):
    plan = task_plan(
        workflow_id=document.workflow_id,
        semantic_revision=document.semantic_revision,
    )
    return replace(plan, document_sha256=document_sha256(document))


def produced_artifact_plan():
    producer = compiled_node("producer", entry=True, outputs=("first", "second"))
    consumer = compiled_node("consumer", inputs=("first",))
    edge = CompiledEdge(
        "producer-consumer",
        "producer",
        "consumer",
        "succeeded",
        MappingProxyType({"first": "first"}),
    )
    semantic = sha256_bytes(b"produced-artifact-plan")
    return CompiledPlan(
        workflow_id="produced-artifact-flow",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"produced-document"),
        semantic_sha256=semantic,
        external_inputs=(),
        nodes=MappingProxyType({"consumer": consumer, "producer": producer}),
        edges=MappingProxyType({edge.id: edge}),
        incoming=MappingProxyType(
            {"consumer": (edge.id,), "producer": ()}
        ),
        outgoing=MappingProxyType(
            {"consumer": (), "producer": (edge.id,)}
        ),
        topological_order=("producer", "consumer"),
        max_parallelism=1,
    )


def external_consumer_plan():
    consumer = compiled_node("consumer", entry=True, inputs=("source",), outputs=("draft",))
    return CompiledPlan(
        workflow_id="external-consumer-flow",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"external-consumer-document"),
        semantic_sha256=sha256_bytes(b"external-consumer-plan"),
        external_inputs=("source",),
        nodes=MappingProxyType({"consumer": consumer}),
        edges=MappingProxyType({}),
        incoming=MappingProxyType({"consumer": ()}),
        outgoing=MappingProxyType({"consumer": ()}),
        topological_order=("consumer",),
        max_parallelism=1,
    )


def any_success_refinement_plan():
    first = compiled_node("first-task", entry=True, outputs=("first",))
    other = compiled_node("other-task", entry=True, outputs=("other",))
    join = replace(
        compiled_node("join", node_type="join", outputs=("joined",)),
        join_mode="any_success",
    )
    edges = {
        "first-join": CompiledEdge(
            "first-join", "first-task", "join", "succeeded",
            MappingProxyType({"first": "joined"}),
        ),
        "other-join": CompiledEdge(
            "other-join", "other-task", "join", "succeeded",
            MappingProxyType({"other": "joined"}),
        ),
    }
    return CompiledPlan(
        workflow_id="any-success-refinement",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"any-success-document"),
        semantic_sha256=sha256_bytes(b"any-success-plan"),
        external_inputs=("first",),
        nodes=MappingProxyType({
            "first-task": first, "other-task": other, "join": join,
        }),
        edges=MappingProxyType(edges),
        incoming=MappingProxyType({
            "first-task": (), "other-task": (),
            "join": ("first-join", "other-join"),
        }),
        outgoing=MappingProxyType({
            "first-task": ("first-join",), "other-task": ("other-join",),
            "join": (),
        }),
        topological_order=("first-task", "other-task", "join"),
        max_parallelism=2,
    )


def compiled_winner_plan(*, b_has_notes=False):
    """Exercise the actual compiler, including repeated logical output IDs."""
    nodes = [
            workflow_task("a", entry=True, outputs=("draft",)),
            workflow_task("b", entry=True, outputs=("draft", "notes") if b_has_notes else ("draft",)),
            workflow_task("c", entry=True, inputs=("source",), outputs=("other",)),
            workflow_join("joined", outputs=("joined",), mode="any_success"),
            workflow_task("consumer", inputs=("joined",), outputs=("final",)),
    ]
    edges = [
            workflow_edge("a-joined", "a", "joined", output_map={"draft": "joined"}),
            workflow_edge("b-joined", "b", "joined", output_map={"draft": "joined"}),
            workflow_edge("c-joined", "c", "joined", output_map={"other": "joined"}),
            workflow_edge("joined-consumer", "joined", "consumer", output_map={"joined": "joined"}),
    ]
    raw = {
        "schema_version": "paper-workflow-custom-v1",
        "workflow_id": "compiled-winner-flow",
        "document_revision": 1,
        "semantic_revision": 1,
        "derived_from": None,
        "max_parallelism": 4,
        "external_inputs": ["source"],
        "nodes": nodes,
        "edges": edges,
        "ui": {"positions": {node["id"]: {"x": 0, "y": 0} for node in nodes}},
    }
    skills = {
        node["skill_ref"]: SkillIdentity(
            catalog_id=node["skill_ref"],
            root=Path(__file__).resolve().parents[1] / "test-skills",
            relative_path=node["skill_ref"],
            skill_sha256=sha256_bytes((node["skill_ref"] + "/SKILL.md").encode()),
            tree_sha256="sha256:" + sha256_bytes(node["skill_ref"].encode()),
            locked=True,
        )
        for node in nodes if node["skill_ref"] is not None
    }
    compiled = compile_workflow(
        parse_workflow(raw),
        CatalogResult(skills, (), ()),
        {},
        json.loads((Path(__file__).resolve().parents[1] / "references/workflows/official-v1.0-studio-projection.json").read_text()),
    )
    if compiled.errors or compiled.plan is None:
        raise AssertionError(f"compiled winner fixture is invalid: {compiled.errors}")
    return compiled.plan


def complete_compiled_winner(
    store, plan, root, *, b_path="b.txt", a_path="a.txt",
    downstream=True, complete_join=True,
):
    """B wins, then A completes late; all file receipts use actual bytes."""
    (root / "source-old.txt").write_bytes(b"source-old")
    (root / b_path).write_bytes(b"winner")
    if "notes" in plan.nodes["b"].outputs:
        (root / "notes.txt").write_bytes(b"notes")
    (root / "final.txt").write_bytes(b"final")
    with store.locked_run() as transaction:
        _, state = transaction.load_active_run()
        source = ArtifactRuntime("source", "source-old.txt", sha256_bytes(b"source-old"), "verified", "external", 0)
        state = replace(state, artifacts=MappingProxyType({"source": source}))
        state = refresh_ready(plan, state)
        transaction.commit_transition("artifact_registered", state)
        running = claim_transition(plan, state, "b", "b-token")
        commit_claim(transaction, running)
        state = result_transition(plan, running, {
            "node_id": "b", "attempt": 1, "status": "succeeded", "outcome": "succeeded",
            "outputs": {"draft": b_path, **({"notes": "notes.txt"} if "notes" in plan.nodes["b"].outputs else {})},
            "artifacts": [
                ArtifactRuntime("draft", b_path, sha256_bytes(b"winner"), "verified", "b", 1),
                *([ArtifactRuntime("notes", "notes.txt", sha256_bytes(b"notes"), "verified", "b", 1)]
                  if "notes" in plan.nodes["b"].outputs else []),
            ],
        })
        commit_result(transaction, state)
        self_winner = state.nodes["joined"]
        assert self_winner.selected_inputs == {"joined": b_path}
        if complete_join:
            stable, controls = stabilize_control_nodes(plan, state)
            transaction.commit_control_transitions(controls, stable)
            state = stable
        if downstream:
            running = claim_transition(plan, state, "consumer", "consumer-token")
            commit_claim(transaction, running)
            state = result_transition(plan, running, {
                "node_id": "consumer", "attempt": 1, "status": "succeeded", "outcome": "succeeded",
                "outputs": {"final": "final.txt"},
                "artifacts": [ArtifactRuntime("final", "final.txt", sha256_bytes(b"final"), "verified", "consumer", 1)],
            })
            commit_result(transaction, state)
        (root / a_path).write_bytes(b"winner" if a_path == b_path else b"late")
        running = claim_transition(plan, state, "a", "a-token")
        commit_claim(transaction, running)
        state = result_transition(plan, running, {
            "node_id": "a", "attempt": 1, "status": "succeeded", "outcome": "succeeded",
            "outputs": {"draft": a_path},
            "artifacts": [ArtifactRuntime("draft", a_path, sha256_bytes(b"winner" if a_path == b_path else b"late"), "verified", "a", 1)],
        })
        commit_result(transaction, state)
    return state


def forged_unrelated_winner_invalidation(store, plan, state, root, *, change_source):
    """A C-only registration (or no change) carrying a forged B invalidation."""
    forged = store._mark_drift(plan, state, [], ["b"])
    if not change_source:
        return forged
    (root / "source-new.txt").write_bytes(b"new source")
    nodes = dict(forged.nodes)
    nodes["c"] = replace(nodes["c"], selected_inputs=MappingProxyType({}))
    return refresh_ready(plan, replace(
        forged,
        nodes=MappingProxyType(nodes),
        artifacts=MappingProxyType({
            **forged.artifacts,
            "source": ArtifactRuntime(
                "source", "source-new.txt", sha256_bytes(b"new source"),
                "verified", "external", 0,
            ),
        }),
    ))


def compiled_reconvergence_plan(*, distinct_sources):
    """Compile file-carrying branches that meet at an all-active join."""
    from tests.test_workflow_scheduler import WorkflowSchedulerTests

    if distinct_sources:
        nodes = [
            workflow_task("source-a", entry=True, outputs=("draft",)),
            workflow_task("source-b", entry=True, outputs=("draft",)),
            workflow_join("merged", outputs=("draft",)),
            workflow_task("consumer", inputs=("draft",)),
        ]
        pairs = [("source-a", "merged"), ("source-b", "merged"), ("merged", "consumer")]
    else:
        nodes = [
            workflow_task("source", entry=True, outputs=("draft",)),
            workflow_join("left", outputs=("draft",)),
            workflow_join("right", outputs=("draft",)),
            workflow_join("merged", outputs=("draft",)),
            workflow_task("consumer", inputs=("draft",)),
        ]
        pairs = [
            ("source", "left"), ("source", "right"),
            ("left", "merged"), ("right", "merged"), ("merged", "consumer"),
        ]
    return WorkflowSchedulerTests().compile(
        nodes,
        [workflow_edge(f"{source}-{target}", source, target,
                       output_map={"draft": "draft"}) for source, target in pairs],
    )


def complete_any_success_refinement(store, plan):
    external = ArtifactRuntime(
        "first", "external.txt", sha256_bytes(b"external"), "verified", "external", 0
    )
    with store.locked_run() as transaction:
        _, state = transaction.load_active_run()
        registered = replace(state, artifacts=MappingProxyType({"first": external}))
        transaction.commit_transition("artifact_registered", registered)
        first_running = claim_transition(plan, registered, "first-task", "first-token")
        commit_claim(transaction, first_running)
        first_done = result_transition(plan, first_running, {
            "node_id": "first-task", "attempt": 1, "status": "succeeded",
            "outcome": "succeeded", "outputs": {"first": "first.txt"},
            "artifacts": [ArtifactRuntime(
                "first", "first.txt", sha256_bytes(b"first"),
                "verified", "first-task", 1,
            )],
        })
        commit_result(transaction, first_done)
        self_selected = first_done.nodes["join"].selected_inputs
        assert self_selected == {"joined": "first.txt"}
        other_running = claim_transition(plan, first_done, "other-task", "other-token")
        commit_claim(transaction, other_running)
        both_done = result_transition(plan, other_running, {
            "node_id": "other-task", "attempt": 1, "status": "succeeded",
            "outcome": "succeeded", "outputs": {"other": "other.txt"},
            "artifacts": [ArtifactRuntime(
                "other", "other.txt", sha256_bytes(b"other"),
                "verified", "other-task", 1,
            )],
        })
        commit_result(transaction, both_done)
    return both_done, external


def excluded_branch_plan(*, artifact_condition=False):
    predicate = (
        {"op": "artifact_state_is", "artifact": "source", "value": "verified"}
        if artifact_condition else {"op": "fact_is", "name": "flag", "value": True}
    )


    condition = compiled_node(
        "choose", node_type="condition", entry=True,
        outcomes=("go", "default"), cases=({"outcome": "go", "when": predicate},),
    )
    main = compiled_node("main")
    fallback = compiled_node("fallback", inputs=("source",))
    sink = compiled_node("sink")
    edges = {
        "choose-main": CompiledEdge(
            "choose-main", "choose", "main", "go", MappingProxyType({})
        ),
        "choose-fallback": CompiledEdge(
            "choose-fallback", "choose", "fallback", "default", MappingProxyType({})
        ),
        "fallback-sink": CompiledEdge(
            "fallback-sink", "fallback", "sink", "succeeded", MappingProxyType({})
        ),
    }
    return CompiledPlan(
        workflow_id="excluded-branch-artifact" if artifact_condition else "excluded-branch-fact",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"excluded-branch-document"),
        semantic_sha256=sha256_bytes(b"excluded-branch-plan"),
        external_inputs=("source",),
        nodes=MappingProxyType({
            "choose": condition, "main": main, "fallback": fallback, "sink": sink,
        }),
        edges=MappingProxyType(edges),
        incoming=MappingProxyType({
            "choose": (), "main": ("choose-main",),
            "fallback": ("choose-fallback",), "sink": ("fallback-sink",),
        }),
        outgoing=MappingProxyType({
            "choose": ("choose-main", "choose-fallback"), "main": (),
            "fallback": ("fallback-sink",), "sink": (),
        }),
        topological_order=("choose", "main", "fallback", "sink"),
        max_parallelism=1,
    )


def complete_excluded_branch(store, plan, *, artifact_condition=False):
    source = ArtifactRuntime(
        "source", "old.txt", sha256_bytes(b"old"), "verified", "external", 0
    )
    with store.locked_run() as transaction:
        _, state = transaction.load_active_run()
        registered = replace(state, artifacts=MappingProxyType({"source": source}))
        transaction.commit_transition("artifact_registered", registered)
        if not artifact_condition:
            registered = replace(
                registered, project_booleans=MappingProxyType({"flag": True})
            )
            transaction.commit_transition("fact_recorded", registered)
        selected, transitions = stabilize_control_nodes(plan, registered)
        transaction.commit_control_transitions(transitions, selected)
    assert selected.nodes["choose"].status is NodeStatus.SUCCEEDED
    assert selected.nodes["fallback"].status is NodeStatus.SKIPPED
    assert selected.nodes["sink"].status is NodeStatus.SKIPPED
    return selected


def complete_external_consumer(store, plan):
    source = ArtifactRuntime(
        "source", "old.txt", sha256_bytes(b"old"), "verified", "external", 0
    )
    with store.locked_run() as transaction:
        _, state = transaction.load_active_run()
        registered = refresh_ready(
            plan, replace(state, artifacts=MappingProxyType({"source": source}))
        )
        transaction.commit_transition("artifact_registered", registered)
        running = claim_transition(plan, registered, "consumer", "consumer-token")
        commit_claim(transaction, running)
        complete = result_transition(plan, running, {
            "node_id": "consumer", "attempt": 1, "status": "succeeded",
            "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
            "artifacts": [ArtifactRuntime(
                "draft", "draft.txt", sha256_bytes(b"draft"),
                "verified", "consumer", 1,
            )],
        })
        commit_result(transaction, complete)
        return complete


def append_rehashed_state_event(store, state, event_type="artifact_registered", extra_payload=None):
    prior = json.loads(store.paths.events.read_text(encoding="utf-8").splitlines()[-1])
    forged = WorkflowEvent.create(
        event_seq=prior["event_seq"] + 1,
        run_id=prior["run_id"],
        semantic_sha256=prior["semantic_sha256"],
        event_type=event_type,
        payload={**(extra_payload or {}), "state": state, "run_status": "active"},
        previous_event_hash=prior["event_hash"],
    )
    with store.paths.events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged.to_payload(), sort_keys=True, separators=(",", ":")) + "\n")
    return forged


def validator_plan():
    validator = ValidatorIdentity(
        validator_id="experiment-contract",
        script=Path("/installed/validators/validator.py"),
        sha256=sha256_bytes(b"validator"),
        adapter="json",
        input_schema="paper-section",
        control_tags=("validate",),
        outcomes=("pass", "fail", "blocked"),
    )
    node = replace(
        compiled_node("validate", node_type="validator", entry=True, inputs=("contract",)),
        validator=validator,
        validator_config=MappingProxyType({"input_roles": MappingProxyType({"contract": "contract"}),
                                           "options": MappingProxyType({})}),
        outcomes=("pass", "fail", "blocked"),
    )
    return CompiledPlan(
        workflow_id="validator-flow",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"validator-document"),
        semantic_sha256=sha256_bytes(b"validator-semantic"),
        external_inputs=("contract",),
        nodes=MappingProxyType({"validate": node}),
        edges=MappingProxyType({}),
        incoming=MappingProxyType({"validate": ()}),
        outgoing=MappingProxyType({"validate": ()}),
        topological_order=("validate",),
        max_parallelism=1,
    )


def artifact_condition_plan():
    condition = compiled_node(
        "choose",
        node_type="condition",
        entry=True,
        outcomes=("verified", "default"),
        cases=(
            {
                "outcome": "verified",
                "when": {
                    "op": "artifact_state_is",
                    "artifact": "source",
                    "value": "verified",
                },
            },
        ),
    )
    use = compiled_node("use-source")
    fallback = compiled_node("fallback")
    edges = {
        "choose-use": CompiledEdge("choose-use", "choose", "use-source", "verified", MappingProxyType({})),
        "choose-fallback": CompiledEdge("choose-fallback", "choose", "fallback", "default", MappingProxyType({})),
    }
    return CompiledPlan(
        workflow_id="artifact-flow",
        semantic_revision=3,
        document_sha256=sha256_bytes(b"artifact-document"),
        semantic_sha256=sha256_bytes(b"artifact-plan"),
        external_inputs=("source",),
        nodes=MappingProxyType(
            {"choose": condition, "fallback": fallback, "use-source": use}
        ),
        edges=MappingProxyType(edges),
        incoming=MappingProxyType(
            {"choose": (), "fallback": ("choose-fallback",), "use-source": ("choose-use",)}
        ),
        outgoing=MappingProxyType(
            {"choose": ("choose-fallback", "choose-use"), "fallback": (), "use-source": ()}
        ),
        topological_order=("choose", "fallback", "use-source"),
        max_parallelism=1,
    )


def independent_condition_plan(large_outcome):
    first = compiled_node(
        "first-condition",
        node_type="condition",
        entry=True,
        outcomes=("short",),
        cases=({"outcome": "short", "when": {"op": "not", "arg": {"op": "fact_is", "name": "unrecorded", "value": True}}},),
    )
    second = compiled_node(
        "second-condition",
        node_type="condition",
        entry=True,
        outcomes=(large_outcome,),
        cases=({"outcome": large_outcome, "when": {"op": "not", "arg": {"op": "fact_is", "name": "unrecorded", "value": True}}},),
    )
    nodes = MappingProxyType(
        {"first-condition": first, "second-condition": second}
    )
    return CompiledPlan(
        workflow_id="control-preflight-flow",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"control-preflight-document"),
        semantic_sha256=sha256_bytes(b"control-preflight-plan"),
        external_inputs=(),
        nodes=nodes,
        edges=MappingProxyType({}),
        incoming=MappingProxyType(
            {"first-condition": (), "second-condition": ()}
        ),
        outgoing=MappingProxyType(
            {"first-condition": (), "second-condition": ()}
        ),
        topological_order=("first-condition", "second-condition"),
        max_parallelism=1,
    )


def oversized_initial_event_plan(node_count=16_000):
    nodes = {
        f"n{index:05d}": compiled_node(
            f"n{index:05d}", node_type="condition", entry=True, outcomes=("default",)
        )
        for index in range(node_count)
    }
    empty_adjacency = {node_id: () for node_id in nodes}
    return CompiledPlan(
        workflow_id="oversized-initial-event-flow",
        semantic_revision=1,
        document_sha256=sha256_bytes(b"oversized-initial-event-document"),
        semantic_sha256=sha256_bytes(b"oversized-initial-event-plan"),
        external_inputs=(),
        nodes=MappingProxyType(nodes),
        edges=MappingProxyType({}),
        incoming=MappingProxyType(dict(empty_adjacency)),
        outgoing=MappingProxyType(dict(empty_adjacency)),
        topological_order=tuple(nodes),
        max_parallelism=1,
    )


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / "fixtures/workflow_valid_linear.json"
        self.document_value = json.loads(fixture.read_text(encoding="utf-8"))
        self.document = parse_workflow(self.document_value)

    def test_missing_selection_is_official_without_custom_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            self.assertEqual(store.read_selection().mode, "official")
            self.assertFalse(root.joinpath(".research/custom-workflow").exists())

    def test_resolve_project_path_rejects_absolute_traversal_and_symlink_parents(self):
        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside:
            root = Path(temporary)
            root.joinpath("safe").mkdir()
            self.assertEqual(
                resolve_project_path(root, "safe/result.json"),
                root.resolve() / "safe/result.json",
            )
            invalid = ("/tmp/escape", "../escape", "safe/../../escape", "C:\\escape", "safe\\..\\escape")
            for relative in invalid:
                with self.subTest(relative=relative), self.assertRaises(PathSafetyError):
                    resolve_project_path(root, relative)
            try:
                root.joinpath("linked").symlink_to(Path(outside), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable on this platform")
            with self.assertRaises(PathSafetyError):
                resolve_project_path(root, "linked/evidence.json")

    def test_store_rejects_symlinked_research_parent_before_writing(self):
        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside:
            root = Path(temporary)
            try:
                root.joinpath(".research").symlink_to(Path(outside), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable on this platform")
            with self.assertRaises(StoreError) as caught:
                WorkflowStore(root).save_draft(self.document, expected_document_revision=0)
            self.assertEqual(caught.exception.code, "path.unsafe")
            with self.assertRaises(StoreError) as read_caught:
                WorkflowStore(root).read_selection()
            self.assertEqual(read_caught.exception.code, "path.unsafe")
            self.assertEqual(tuple(Path(outside).iterdir()), ())

    def test_atomic_write_retains_valid_backup_and_refuses_ambiguous_old_json(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"generation": 1})
            atomic_write_json(path, {"generation": 2})
            self.assertEqual(json.loads(path.read_text()), {"generation": 2})
            backups = sorted(path.parent.glob("state.json.bak*"))
            self.assertTrue(backups)
            self.assertIn({"generation": 1}, [json.loads(item.read_text()) for item in backups])

            path.write_text("{corrupt", encoding="utf-8")
            with self.assertRaises(PathSafetyError):
                atomic_write_json(path, {"generation": 3})
            self.assertEqual(path.read_text(encoding="utf-8"), "{corrupt")

    def test_atomic_write_keeps_one_bounded_backup_generation(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"generation": 1})
            atomic_write_json(path, {"generation": 2})
            atomic_write_json(path, {"generation": 3})
            backups = list(path.parent.glob("state.json.bak*"))
            self.assertEqual([item.name for item in backups], ["state.json.bak"])
            self.assertEqual(json.loads(backups[0].read_text()), {"generation": 2})

    def test_directory_fsync_errors_propagate_and_new_entries_sync_their_parent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                workflow_fs.os,
                "fsync",
                side_effect=OSError(errno.EIO, "injected directory sync failure"),
            ):
                with self.assertRaises(OSError):
                    workflow_fs._fsync_directory(root)

            with mock.patch.object(
                workflow_fs,
                "_fsync_directory",
                side_effect=OSError(errno.EIO, "injected atomic directory sync failure"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_json(root / "durable.json", {"durable": True})

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(workflow_fs, "_fsync_directory") as sync:
                ensure_project_directory(root, "one/two")
            self.assertGreaterEqual(sync.call_count, 2)
            self.assertEqual(sync.call_args_list[0].args[0], root.resolve())

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            event = WorkflowEvent.create(
                event_seq=1,
                run_id="run",
                semantic_sha256=sha256_bytes(b"semantic"),
                event_type="created",
                payload={},
                previous_event_hash=ZERO_HASH,
            )
            with mock.patch.object(workflow_fs, "_fsync_directory") as sync:
                append_event(path, event)
            sync.assert_called_once_with(path.parent)

    def test_event_and_lock_hard_links_are_rejected_without_touching_outside_victim(self):
        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside:
            root = Path(temporary)
            event_dir = root / "events"
            event_dir.mkdir()
            victim = Path(outside) / "victim.jsonl"
            victim.write_bytes(b"outside evidence\n")
            linked = event_dir / "events.jsonl"
            try:
                os.link(victim, linked)
            except OSError:
                self.skipTest("hard links are unavailable on this filesystem")
            before = victim.read_bytes()
            event = WorkflowEvent.create(
                event_seq=1,
                run_id="run",
                semantic_sha256=sha256_bytes(b"semantic"),
                event_type="created",
                payload={},
                previous_event_hash=ZERO_HASH,
            )
            with self.assertRaises(PathSafetyError):
                append_event(linked, event)
            self.assertEqual(victim.read_bytes(), before)

        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside:
            root = Path(temporary)
            store = WorkflowStore(root)
            store.paths.base.mkdir(parents=True)
            victim = Path(outside) / "lock-victim"
            victim.write_bytes(b"0")
            try:
                os.link(victim, store.paths.lock)
            except OSError:
                self.skipTest("hard links are unavailable on this filesystem")
            before = victim.read_bytes()
            with self.assertRaises(StoreError) as caught:
                store.save_draft(self.document, expected_document_revision=0)
            self.assertEqual(caught.exception.code, "path.unsafe")
            self.assertEqual(victim.read_bytes(), before)

    def test_save_draft_assigns_revisions_and_ui_only_save_keeps_semantic_identity(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            proposed = parse_workflow(
                {**self.document_value, "document_revision": 99, "semantic_revision": 99}
            )
            first = store.save_draft(proposed, expected_document_revision=0)
            self.assertEqual((first.document_revision, first.semantic_revision), (1, 1))

            ui_only = copy.deepcopy(self.document_value)
            ui_only["document_revision"] = 500
            ui_only["semantic_revision"] = 700
            ui_only["ui"]["positions"]["directions"]["x"] = 999
            second = store.save_draft(parse_workflow(ui_only), expected_document_revision=1)
            self.assertEqual((second.document_revision, second.semantic_revision), (2, 1))
            self.assertEqual(document_sha256(second), document_sha256(first))

            changed = copy.deepcopy(ui_only)
            changed["max_parallelism"] = 2
            third = store.save_draft(parse_workflow(changed), expected_document_revision=2)
            self.assertEqual((third.document_revision, third.semantic_revision), (3, 2))
            self.assertNotEqual(document_sha256(third), document_sha256(second))
            self.assertEqual(len(list(store.paths.revisions.glob("*.json"))), 2)

    def test_branch_join_draft_preserves_authored_control_outcomes_across_revisions(self):
        fixture = Path(__file__).parent / "fixtures/workflow_valid_branch_join.json"
        authored = json.loads(fixture.read_text(encoding="utf-8"))
        branch = parse_workflow(authored)
        self.assertEqual(branch.nodes[0].outcomes, ("has_sources", "default"))
        self.assertEqual(branch.nodes[-1].outcomes, ("succeeded",))
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            first = store.save_draft(branch, expected_document_revision=0)
            raw = json.loads(store.paths.workflow.read_text(encoding="utf-8"))
            control = {node["type"]: node["outcomes"] for node in raw["nodes"] if node["type"] in {"condition", "join"}}
            self.assertEqual(control, {"condition": [], "join": []})
            self.assertEqual(store.load_draft(), first)

            ui = copy.deepcopy(authored)
            ui["ui"]["positions"]["condition"]["x"] += 1
            second = store.save_draft(parse_workflow(ui), expected_document_revision=1)
            self.assertEqual(second.semantic_revision, first.semantic_revision)

            semantic = copy.deepcopy(ui)
            semantic["max_parallelism"] = 1
            third = store.save_draft(parse_workflow(semantic), expected_document_revision=2)
            self.assertEqual(third.semantic_revision, second.semantic_revision + 1)
            self.assertEqual(store.load_draft(), third)

        validator = copy.deepcopy(self.document_value)
        validator_node = validator["nodes"][0]
        validator_node.update(
            {
                "type": "validator",
                "skill_ref": None,
                "validator_ref": "paper-section",
                "validator_config": {"input_roles": {"file": "section"}, "options": {
                    "phase": "body", "paper_type": "empirical", "language": "en",
                    "method_profile": "method-first", "validity_status": "pending",
                    "discussion_integrated": False,
                }},
                "inputs": ["section"],
                "outputs": [],
                "outcomes": ["pass", "fail", "blocked"],
            }
        )
        validator["nodes"] = [validator_node]
        validator["external_inputs"] = ["section"]
        validator["edges"] = []
        validator["ui"]["positions"] = {validator_node["id"]: {"x": 0, "y": 0}}
        with TemporaryDirectory() as temporary:
            saved = WorkflowStore(Path(temporary)).save_draft(
                parse_workflow(validator), expected_document_revision=0
            )
            self.assertEqual(saved.nodes[0].type, "validator")

    def test_scalar_kind_behavior_changes_are_semantic_and_revision_tokens_are_exact_bounded_ints(self):
        fixture = Path(__file__).parent / "fixtures/workflow_valid_branch_join.json"
        authored = json.loads(fixture.read_text(encoding="utf-8"))
        authored["nodes"][0]["condition_cases"][0]["when"] = {
            "op": "fact_is",
            "name": "ready",
            "value": False,
        }
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            first = store.save_draft(parse_workflow(authored), expected_document_revision=0)
            changed = copy.deepcopy(authored)
            changed["nodes"][0]["condition_cases"][0]["when"]["value"] = 0
            second = store.save_draft(parse_workflow(changed), expected_document_revision=1)
            self.assertEqual(second.semantic_revision, first.semantic_revision + 1)

        for invalid in (False, 0.0, "0", -1, 10**20):
            with self.subTest(invalid=invalid), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                with self.assertRaises(StoreError) as caught:
                    store.save_draft(self.document, expected_document_revision=invalid)
                self.assertEqual(caught.exception.code, "store.invalid_revision")
                self.assertFalse(store.paths.workflow.exists())

    def test_stale_revision_and_immutable_snapshot_collision_fail_closed(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            saved = store.save_draft(self.document, expected_document_revision=0)
            with self.assertRaises(StoreError) as stale:
                store.save_draft(self.document, expected_document_revision=0)
            self.assertEqual(stale.exception.code, "store.revision_conflict")

            revision = store.paths.revisions / f"{saved.semantic_revision}-{document_sha256(saved)}.json"
            revision.write_text("{}\n", encoding="utf-8")
            changed = copy.deepcopy(self.document_value)
            changed["nodes"][0]["display_name"] = "Changed"
            with self.assertRaises(StoreError) as collision:
                store.save_draft(parse_workflow(changed), expected_document_revision=1)
            self.assertEqual(collision.exception.code, "store.revision_snapshot_collision")
            self.assertEqual(store.load_draft(), saved)

    def test_concurrent_same_revision_saves_have_one_winner(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)

            def save_once():
                try:
                    saved = WorkflowStore(root).save_draft(
                        self.document, expected_document_revision=0
                    )
                    return ("saved", saved.document_revision)
                except StoreError as exc:
                    return (exc.code, None)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: save_once(), range(2)))
            self.assertEqual(results.count(("saved", 1)), 1)
            self.assertEqual(results.count(("store.revision_conflict", None)), 1)

    def test_corrupt_selection_never_falls_back_to_official(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.paths.base.mkdir(parents=True)
            store.paths.selection.write_text("{not-json\n", encoding="utf-8")
            with self.assertRaises(StoreError) as caught:
                store.read_selection()
            self.assertEqual(caught.exception.code, "selection.invalid")
            store.paths.selection.write_text(
                '{"mode":"official","mode":"custom"}\n', encoding="utf-8"
            )
            with self.assertRaises(StoreError) as duplicate:
                store.read_selection()
            self.assertEqual(duplicate.exception.code, "selection.invalid")

    def test_activation_binds_exact_warning_set_and_semantic_hash_across_restart(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            saved = store.save_draft(self.document, expected_document_revision=0)
            plan = plan_for_document(saved)
            with self.assertRaises(StoreError) as missing:
                store.activate_custom(
                    plan,
                    high_risk_warning_codes=("risk.a", "risk.b"),
                    acknowledged_warning_codes=("risk.a",),
                )
            self.assertEqual(missing.exception.code, "activation.acknowledgement_mismatch")
            for required, acknowledged in (
                ("risk.a", ("risk.a",)),
                (("risk.a\n",), ("risk.a\n",)),
                (("risk.a",), ("risk.a", "risk.a")),
            ):
                with self.subTest(required=required, acknowledged=acknowledged):
                    with self.assertRaises(StoreError) as invalid_codes:
                        store.activate_custom(
                            plan,
                            high_risk_warning_codes=required,
                            acknowledged_warning_codes=acknowledged,
                        )
                    self.assertEqual(
                        invalid_codes.exception.code,
                        "activation.acknowledgement_mismatch",
                    )

            selection = store.activate_custom(
                plan,
                high_risk_warning_codes=("risk.b", "risk.a"),
                acknowledged_warning_codes=("risk.a", "risk.b"),
            )
            self.assertEqual(selection.acknowledged_warning_codes, ("risk.a", "risk.b"))
            restarted = WorkflowStore(root).read_selection()
            self.assertTrue(restarted.acknowledges(plan, ("risk.a", "risk.b")))
            self.assertFalse(restarted.acknowledges(plan, ("risk.a",)))
            self.assertFalse(restarted.acknowledges(plan, ("risk.a", "risk.b", "risk.c")))
            self.assertFalse(
                restarted.acknowledges(
                    replace(plan, semantic_revision=2), ("risk.a", "risk.b")
                )
            )
            audit_selection = store.read_audit_events()[-1].payload["selection"]
            self.assertEqual(audit_selection["semantic_sha256"], selection.semantic_sha256)
            self.assertEqual(
                tuple(audit_selection["acknowledged_warning_codes"]),
                selection.acknowledged_warning_codes,
            )

            official = WorkflowStore(root).deactivate_custom()
            self.assertEqual(official.mode, "official")
            self.assertEqual(official.selection_revision, selection.selection_revision + 1)

    def test_activation_requires_exact_latest_saved_revision_and_reconciles_journal_commit(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            with self.assertRaises(StoreError) as missing:
                store.activate_custom(
                    task_plan(),
                    high_risk_warning_codes=(),
                    acknowledged_warning_codes=(),
                )
            self.assertEqual(missing.exception.code, "activation.draft_mismatch")

            saved = store.save_draft(self.document, expected_document_revision=0)
            exact = plan_for_document(saved)
            for stale in (
                replace(exact, workflow_id="another-flow"),
                replace(exact, semantic_revision=exact.semantic_revision + 1),
                replace(exact, document_sha256=sha256_bytes(b"other-document")),
            ):
                with self.subTest(stale=stale), self.assertRaises(StoreError) as caught:
                    store.activate_custom(
                        stale,
                        high_risk_warning_codes=(),
                        acknowledged_warning_codes=(),
                    )
                self.assertEqual(caught.exception.code, "activation.draft_mismatch")

            original_atomic = store._atomic_json

            def fail_selection(path, value):
                if path == store.paths.selection:
                    raise StoreError("test.selection_write_failed", "injected crash")
                return original_atomic(path, value)

            with mock.patch.object(store, "_atomic_json", side_effect=fail_selection):
                with self.assertRaises(StoreError) as crashed:
                    store.activate_custom(
                        exact,
                        high_risk_warning_codes=("risk.one",),
                        acknowledged_warning_codes=("risk.one",),
                    )
            self.assertEqual(crashed.exception.code, "test.selection_write_failed")
            self.assertFalse(store.paths.selection.exists())

            restarted = WorkflowStore(root)
            reconciled = restarted.read_selection()
            self.assertEqual((reconciled.mode, reconciled.selection_revision), ("custom", 1))
            self.assertEqual(len(restarted.read_audit_events()), 1)
            official = restarted.deactivate_custom()
            self.assertEqual(official.selection_revision, 2)
            self.assertEqual(
                [event.payload["selection"]["selection_revision"] for event in restarted.read_audit_events()],
                [1, 2],
            )

    def test_corrupt_activation_journal_blocks_selection_reads_and_mutations(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            saved = store.save_draft(self.document, expected_document_revision=0)
            plan = plan_for_document(saved)
            store.activate_custom(
                plan,
                high_risk_warning_codes=(),
                acknowledged_warning_codes=(),
            )
            with store.paths.audit_events.open("ab") as handle:
                handle.write(b"{corrupt}\n")
            for operation in (
                lambda: WorkflowStore(root).read_selection(),
                lambda: WorkflowStore(root).deactivate_custom(),
            ):
                with self.assertRaises(StoreError) as caught:
                    operation()
                self.assertIn(caught.exception.code, {"events.invalid_json", "selection.journal_invalid"})

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            saved = store.save_draft(self.document, expected_document_revision=0)
            plan = plan_for_document(saved)
            store.activate_custom(
                plan,
                high_risk_warning_codes=(),
                acknowledged_warning_codes=(),
            )
            event = json.loads(store.paths.audit_events.read_text(encoding="utf-8"))
            event["semantic_sha256"] = sha256_bytes(b"conflicting-semantic")
            unsigned = dict(event)
            unsigned.pop("event_hash")
            event["event_hash"] = sha256_bytes(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            store.paths.audit_events.write_text(
                json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(StoreError) as caught:
                WorkflowStore(root).read_selection()
            self.assertEqual(caught.exception.code, "selection.journal_invalid")

    def test_behind_or_orphaned_selection_projection_must_match_its_journal_boundary(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            saved = store.save_draft(self.document, expected_document_revision=0)
            plan = plan_for_document(saved)
            store.activate_custom(
                plan,
                high_risk_warning_codes=(),
                acknowledged_warning_codes=(),
            )
            first = json.loads(store.paths.selection.read_text(encoding="utf-8"))
            store.deactivate_custom()
            store.paths.selection.write_text(
                json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            reconciled = WorkflowStore(root).read_selection()
            self.assertEqual((reconciled.mode, reconciled.selection_revision), ("official", 2))
            first["acknowledged_at"] = "2000-01-01T00:00:00Z"
            store.paths.selection.write_text(
                json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(StoreError) as conflicting:
                WorkflowStore(root).read_selection()
            self.assertEqual(conflicting.exception.code, "selection.journal_conflict")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            store.paths.base.mkdir(parents=True)
            orphan = {
                "mode": "official",
                "selection_revision": 1,
                "workflow_id": "",
                "semantic_revision": 0,
                "semantic_sha256": "",
                "acknowledged_warning_codes": [],
                "acknowledged_semantic_sha256": "",
                "acknowledged_at": "",
            }
            store.paths.selection.write_text(json.dumps(orphan) + "\n", encoding="utf-8")
            with self.assertRaises(StoreError) as orphaned:
                store.read_selection()
            self.assertEqual(orphaned.exception.code, "selection.journal_conflict")

    def test_plan_and_run_state_round_trip_preserves_immutable_types_and_json_scalars(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = task_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-001")
            with store.locked_run() as transaction:
                loaded_plan, state = transaction.load_active_run()
                runtime = replace(
                    state.nodes["produce"],
                    auxiliary_outputs=MappingProxyType({"draft": ("first.md", "later.md")}),
                )
                state = replace(
                    state,
                    nodes=MappingProxyType({"produce": runtime}),
                    decisions=MappingProxyType(
                        {"none": None, "false": False, "zero": 0, "one": 1, "float": 1.5, "text": "1"}
                    ),
                    project_booleans=MappingProxyType({"ready": True}),
                )
                transaction.commit_transition(
                    "facts_recorded",
                    state,
                    {
                        "receipt": {
                            "attempt": 1,
                            "accepted": False,
                            "hashes": [sha256_bytes(b"first"), sha256_bytes(b"later")],
                        }
                    },
                )

            with WorkflowStore(root).locked_run() as transaction:
                restarted_plan, restarted = transaction.load_active_run()
                self.assertEqual(len(transaction.events("facts_recorded")), 1)
            self.assertEqual(restarted_plan, loaded_plan)
            self.assertIsInstance(restarted_plan.nodes, MappingProxyType)
            self.assertIsInstance(restarted.nodes, MappingProxyType)
            self.assertIs(restarted.decisions["false"], False)
            self.assertEqual(type(restarted.decisions["zero"]), int)
            self.assertEqual(type(restarted.decisions["one"]), int)
            self.assertEqual(type(restarted.decisions["float"]), float)
            self.assertEqual(restarted.nodes["produce"].status, NodeStatus.READY)
            self.assertEqual(
                restarted.nodes["produce"].auxiliary_outputs["draft"],
                ("first.md", "later.md"),
            )
            self.assertEqual(restarted_plan.nodes["produce"].skill, plan.nodes["produce"].skill)
            receipt = WorkflowStore(root).read_run_events()[-1].payload["receipt"]
            self.assertEqual(type(receipt["attempt"]), int)
            self.assertIs(receipt["accepted"], False)
            self.assertEqual(len(receipt["hashes"]), 2)
            with self.assertRaises(TypeError):
                receipt["attempt"] = 2

    def test_plan_digest_binds_canonical_plan_and_plan_must_round_trip_before_persistence(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            with self.assertRaises(StoreError) as invalid:
                store.start_run(replace(task_plan(), max_parallelism=True), "bad-plan")
            self.assertEqual(invalid.exception.code, "plan.invalid")
            self.assertFalse(store.paths.plan.exists())

        for field, mutation in (
            ("max_parallelism", lambda raw: raw.__setitem__("max_parallelism", 2)),
            (
                "write_scopes",
                lambda raw: raw["nodes"]["produce"].__setitem__("write_scopes", ["other"]),
            ),
            (
                "skill_path",
                lambda raw: raw["nodes"]["produce"]["skill"].__setitem__(
                    "relative_path", "changed-skill"
                ),
            ),
        ):
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(task_plan(), "run-plan-digest")
                snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
                started = json.loads(store.paths.events.read_text(encoding="utf-8"))
                raw_plan = json.loads(store.paths.plan.read_text(encoding="utf-8"))
                canonical = json.dumps(
                    raw_plan,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                asserted = sha256_bytes(canonical)
                self.assertEqual(snapshot["plan_sha256"], asserted)
                self.assertEqual(started["payload"]["plan_sha256"], asserted)
                mutation(raw_plan)
                store.paths.plan.write_text(
                    json.dumps(raw_plan, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                evidence = store.paths.plan.read_bytes()
                result = store.recover()
                self.assertEqual((result.status, result.code), ("blocked", "plan.digest_mismatch"))
                self.assertEqual(store.paths.plan.read_bytes(), evidence)

        for field, plan, mutation in (
            (
                "skill_hash",
                task_plan(),
                lambda raw: raw["nodes"]["produce"]["skill"].__setitem__(
                    "skill_sha256", sha256_bytes(b"changed-skill")
                ),
            ),
            (
                "topology",
                produced_artifact_plan(),
                lambda raw: raw["edges"]["producer-consumer"].__setitem__("trigger", "failed"),
            ),
            (
                "condition",
                artifact_condition_plan(),
                lambda raw: raw["nodes"]["choose"]["condition_cases"][0]["when"].__setitem__(
                    "value", "stale"
                ),
            ),
            (
                "validator_path",
                validator_plan(),
                lambda raw: raw["nodes"]["validate"]["validator"].__setitem__(
                    "script", "/changed/validator.py"
                ),
            ),
            (
                "validator_hash",
                validator_plan(),
                lambda raw: raw["nodes"]["validate"]["validator"].__setitem__(
                    "sha256", sha256_bytes(b"changed-validator")
                ),
            ),
        ):
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(plan, "run-plan-field-digest")
                raw_plan = json.loads(store.paths.plan.read_text(encoding="utf-8"))
                mutation(raw_plan)
                store.paths.plan.write_text(
                    json.dumps(raw_plan, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(store.recover().code, "plan.digest_mismatch")

    def test_non_roundtrippable_state_is_rejected_before_event_append(self):
        mutations = {
            "bool_attempt": lambda state: replace(
                state,
                nodes=MappingProxyType(
                    {"produce": replace(state.nodes["produce"], attempt=True)}
                ),
            ),
            "bad_artifact_path": lambda state: replace(
                state,
                artifacts=MappingProxyType(
                    {
                        "draft": ArtifactRuntime(
                            "draft",
                            "bad\npath",
                            sha256_bytes(b"draft"),
                            "stale",
                            "produce",
                            1,
                        )
                    }
                ),
            ),
            "bad_artifact_hash": lambda state: replace(
                state,
                artifacts=MappingProxyType(
                    {"draft": ArtifactRuntime("draft", "draft", "BAD", "stale", "produce", 1)}
                ),
            ),
            "bad_producer": lambda state: replace(
                state,
                artifacts=MappingProxyType(
                    {
                        "draft": ArtifactRuntime(
                            "draft", "draft", sha256_bytes(b"draft"), "stale", "produce\tother", 1
                        )
                    }
                ),
            ),
            "stale_artifact_escape": lambda state: replace(
                state,
                artifacts=MappingProxyType(
                    {
                        "draft": ArtifactRuntime(
                            "draft", "../outside", sha256_bytes(b"draft"), "stale", "produce", 1
                        )
                    }
                ),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(task_plan(), "run-state-validation")
                before = store.paths.events.read_bytes()
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    with self.assertRaises(StoreError) as caught:
                        transaction.commit_transition("invalid_state", mutate(state))
                self.assertIn(caught.exception.code, {"snapshot.invalid", "path.unsafe"})
                self.assertEqual(store.paths.events.read_bytes(), before)

    def test_transaction_is_confined_to_one_owner_lease_and_nested_locks_fail_immediately(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-lease")
            with self.assertRaises(StoreError) as direct:
                WorkflowTransaction(store)
            self.assertEqual(direct.exception.code, "store.transaction_invalid")

            with store.locked_run() as transaction:
                transaction.load_active_run()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    code = executor.submit(
                        lambda: self._transaction_error_code(transaction)
                    ).result()
                self.assertEqual(code, "store.transaction_wrong_owner")
                started = time.monotonic()
                with self.assertRaises(StoreError) as nested:
                    with store._lock(timeout=0.01):
                        pass
                self.assertEqual(nested.exception.code, "store.lock_reentrant")
                self.assertLess(time.monotonic() - started, 0.5)
                with self.assertRaises(StoreError) as second_instance:
                    with WorkflowStore(Path(temporary))._lock(timeout=0.01):
                        pass
                self.assertEqual(second_instance.exception.code, "store.lock_reentrant")

            with self.assertRaises(StoreError) as stale:
                transaction.events()
            self.assertEqual(stale.exception.code, "store.transaction_inactive")

    @staticmethod
    def _transaction_error_code(transaction):
        try:
            transaction.load_active_run()
        except StoreError as exc:
            return exc.code
        return "no-error"

    def test_control_final_state_comparison_preserves_json_scalar_kinds(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-control-kind")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                current = replace(state, decisions=MappingProxyType({"kind": False}))
                transaction.commit_transition("fact", current)
            with store.locked_run() as transaction:
                _, current = transaction.load_active_run()
                changed = replace(current, decisions=MappingProxyType({"kind": 0}))
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_control_transitions((), changed)
                self.assertEqual(caught.exception.code, "control.final_state_mismatch")

    def test_control_batch_is_fully_simulated_before_first_event_append(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("source.txt").write_bytes(b"evidence")
            store = WorkflowStore(root)
            plan = artifact_condition_plan()
            store.start_run(plan, "run-control-preflight")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                artifact = ArtifactRuntime(
                    "source", "source.txt", sha256_bytes(b"evidence"), "verified", "external", 0
                )
                transaction.commit_transition(
                    "artifact_registered",
                    replace(state, artifacts=MappingProxyType({"source": artifact})),
                )
            with store.locked_run() as transaction:
                _, ready = transaction.load_active_run()
                final, transitions = stabilize_control_nodes(plan, ready)
                invalid_final = replace(final, decisions=MappingProxyType({"kind": False}))
                before = store.paths.events.read_bytes()
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_control_transitions(transitions, invalid_final)
                self.assertEqual(caught.exception.code, "control.final_state_mismatch")
                self.assertEqual(store.paths.events.read_bytes(), before)

    def test_only_one_active_run_is_allowed_and_activation_cannot_replace_it(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-one")
            with self.assertRaises(StoreError) as second:
                store.start_run(plan, "run-two")
            self.assertEqual(second.exception.code, "run.already_active")
            with self.assertRaises(StoreError) as activation:
                store.activate_custom(plan, high_risk_warning_codes=(), acknowledged_warning_codes=())
            self.assertEqual(activation.exception.code, "run.already_active")
            with self.assertRaises(StoreError) as unsafe_id:
                WorkflowStore(Path(temporary)).start_run(plan, "../run-escape")
            self.assertEqual(unsafe_id.exception.code, "run.invalid_id")

    def test_terminal_or_recovery_blocked_active_run_still_requires_explicit_stop(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-terminal-active")
            Path(temporary, "draft.txt").write_bytes(b"draft")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "terminal-token")
                commit_claim(transaction, running)
                terminal = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime("draft", "draft.txt", sha256_bytes(b"draft"), "verified", "produce", 1)],
                })
                commit_result(transaction, terminal)
            with self.assertRaises(StoreError) as next_run:
                store.start_run(plan, "run-must-not-replace")
            self.assertEqual(next_run.exception.code, "run.already_active")

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-recovery-blocked")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "interrupted-token")
                commit_claim(transaction, running)
            self.assertEqual(store.recover().code, "recovery.running_work_uncertain")
            with self.assertRaises(StoreError) as next_run:
                store.start_run(plan, "run-after-block")
            self.assertEqual(next_run.exception.code, "run.already_active")

    def test_stopped_run_is_archived_with_evidence_before_a_new_run_starts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, "run-old")
            store.deactivate_custom()
            stopped = json.loads(store.paths.state.read_text(encoding="utf-8"))
            self.assertEqual(stopped["run_status"], "stopped")

            store.start_run(plan, "run-new")
            archives = list(store.paths.archived_runs.glob("run-old-*"))
            self.assertEqual(len(archives), 1)
            archived_events = [
                json.loads(line)
                for line in archives[0].joinpath("events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [event["event_type"] for event in archived_events],
                ["run_started", "run_stopped"],
            )
            with store.locked_run() as transaction:
                _, active = transaction.load_active_run()
            self.assertEqual(active.run_id, "run-new")

    def test_archive_rename_attempts_both_parent_syncs_and_reports_first_failure(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-archive-sync")
            store.deactivate_custom()
            first_error = OSError(errno.EIO, "first parent sync failed")
            with mock.patch.object(
                workflow_store,
                "_fsync_directory",
                side_effect=(first_error, None),
            ) as sync:
                with self.assertRaises(StoreError) as caught:
                    store.start_run(plan, "run-after-sync-failure")
            self.assertEqual(caught.exception.code, "run.archive_failed")
            self.assertIs(caught.exception.__cause__, first_error)
            self.assertEqual(sync.call_count, 2)

    def test_start_run_never_overwrites_partial_active_run_material(self):
        for remnant in ("plan_only", "append_before_snapshot", "projection_only", "truncated_event"):
            with self.subTest(remnant=remnant), TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = WorkflowStore(root)
                if remnant == "append_before_snapshot":
                    store.start_run(task_plan(), "crashed-run")
                    store.paths.state.unlink()
                else:
                    store.paths.run_dir.mkdir(parents=True)
                if remnant == "plan_only":
                    store.paths.plan.write_text('{"partial":"plan"}\n', encoding="utf-8")
                elif remnant == "projection_only":
                    store.paths.artifacts.write_text('{"projection":true}\n', encoding="utf-8")
                elif remnant == "truncated_event":
                    store.paths.events.write_bytes(b'{"event_seq":1')
                before = {
                    path.name: path.read_bytes()
                    for path in store.paths.run_dir.iterdir()
                    if path.is_file()
                }
                with self.assertRaises(StoreError) as caught:
                    store.start_run(task_plan(), "replacement")
                self.assertIn(
                    caught.exception.code,
                    {"run.recovery_required", "recovery.incomplete_run", "plan.invalid"},
                )
                after = {
                    path.name: path.read_bytes()
                    for path in store.paths.run_dir.iterdir()
                    if path.is_file()
                }
                self.assertEqual(after, before)

    def test_verified_artifact_update_requires_contained_matching_bytes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            plan = artifact_condition_plan()
            store.start_run(plan, "run-artifact-boundary")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                unsafe = ArtifactRuntime(
                    "source", "/tmp/outside", sha256_bytes(b"missing"), "verified", "external", 0
                )
                with self.assertRaises(StoreError) as outside:
                    transaction.commit_transition(
                        "artifact_registered",
                        replace(state, artifacts=MappingProxyType({"source": unsafe})),
                    )
                self.assertEqual(outside.exception.code, "artifact.verification_failed")

            root.joinpath("source.txt").write_bytes(b"actual")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                wrong_hash = ArtifactRuntime(
                    "source", "source.txt", sha256_bytes(b"other"), "verified", "external", 0
                )
                with self.assertRaises(StoreError) as mismatch:
                    transaction.commit_transition(
                        "artifact_registered",
                        replace(state, artifacts=MappingProxyType({"source": wrong_hash})),
                    )
                self.assertEqual(mismatch.exception.code, "artifact.verification_failed")

    def test_events_are_canonical_hash_linked_and_snapshot_boundary_matches(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-chain")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                transaction.commit_transition("state_observed", state, {"note": "same"})

            raw_events = [json.loads(line) for line in store.paths.events.read_text().splitlines()]
            self.assertEqual([item["event_seq"] for item in raw_events], [1, 2])
            self.assertEqual(raw_events[0]["previous_event_hash"], ZERO_HASH)
            self.assertEqual(raw_events[1]["previous_event_hash"], raw_events[0]["event_hash"])
            for raw in raw_events:
                asserted = raw.pop("event_hash")
                canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                self.assertEqual(asserted, sha256_bytes(canonical.encode("utf-8")))
                raw["event_hash"] = asserted
            snapshot = json.loads(store.paths.state.read_text())
            self.assertEqual(snapshot["last_applied_event_seq"], 2)
            self.assertEqual(snapshot["last_applied_event_hash"], raw_events[-1]["event_hash"])

    def test_event_hash_gap_blocks_recovery_without_rewriting_evidence(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-gap")
            before = store.paths.events.read_bytes()
            invalid = WorkflowEvent.create(
                event_seq=99,
                run_id="run-gap",
                semantic_sha256=plan.semantic_sha256,
                event_type="invented_gap",
                payload={"state": {}},
                previous_event_hash="bad",
            )
            with store.paths.events.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(invalid.to_payload(), sort_keys=True, separators=(",", ":")) + "\n")
            corrupted = store.paths.events.read_bytes()
            result = store.recover()
            self.assertEqual((result.status, result.code), ("blocked", "events.sequence_gap"))
            self.assertEqual(store.paths.events.read_bytes(), corrupted)
            self.assertTrue(store.paths.events.read_bytes().startswith(before))

    def test_minimal_sequence_gap_and_terminated_corrupt_json_are_never_rewritten(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-minimal-gap")
            with store.paths.events.open("a", encoding="utf-8") as handle:
                handle.write('{"event_seq":99,"previous_event_hash":"bad"}\n')
            evidence = store.paths.events.read_bytes()
            self.assertEqual(store.recover().code, "events.sequence_gap")
            self.assertEqual(store.paths.events.read_bytes(), evidence)

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-corrupt-json")
            with store.paths.events.open("ab") as handle:
                handle.write(b'{not-json}\n')
            evidence = store.paths.events.read_bytes()
            self.assertEqual(store.recover().code, "events.invalid_json")
            self.assertEqual(store.paths.events.read_bytes(), evidence)

    def test_hash_and_snapshot_boundary_tampering_block_without_evidence_rewrite(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-hash-tamper")
            raw = json.loads(store.paths.events.read_text(encoding="utf-8"))
            raw["event_type"] = "changed_without_rehash"
            store.paths.events.write_text(
                json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            evidence = store.paths.events.read_bytes()
            self.assertEqual(store.recover().code, "events.hash_mismatch")
            self.assertEqual(store.paths.events.read_bytes(), evidence)

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-boundary-tamper")
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            snapshot["last_applied_event_hash"] = ZERO_HASH
            store.paths.state.write_text(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            evidence = store.paths.state.read_bytes()
            self.assertEqual(store.recover().code, "snapshot.boundary_mismatch")
            self.assertEqual(store.paths.state.read_bytes(), evidence)

    def test_exact_duplicate_event_is_idempotent_but_conflicting_duplicate_blocks(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-duplicate")
            first_line = store.paths.events.read_text(encoding="utf-8").splitlines()[0]
            with store.paths.events.open("a", encoding="utf-8") as handle:
                handle.write(first_line + "\n")
            self.assertNotEqual(store.recover().status, "blocked")

            raw = json.loads(first_line)
            raw["event_type"] = "conflict"
            with store.paths.events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n")
            self.assertEqual(store.recover().code, "events.hash_mismatch")

    def test_duplicate_hash_is_always_validated_and_scalar_kinds_conflict(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-duplicate-hash")
            raw = json.loads(store.paths.events.read_text(encoding="utf-8"))
            raw["event_hash"] = ZERO_HASH
            with store.paths.events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n")
            self.assertEqual(store.recover().code, "events.hash_mismatch")

        for original, changed in ((False, 0), (True, 1), (1, 1.0)):
            with self.subTest(original=original, changed=changed), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(task_plan(), "run-duplicate-kind")
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    transaction.commit_transition("kind", state, {"value": original})
                lines = store.paths.events.read_text(encoding="utf-8").splitlines()
                duplicate = json.loads(lines[-1])
                duplicate["payload"]["value"] = changed
                unsigned = dict(duplicate)
                unsigned.pop("event_hash")
                duplicate["event_hash"] = sha256_bytes(
                    json.dumps(
                        unsigned,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                with store.paths.events.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(duplicate, sort_keys=True, separators=(",", ":")) + "\n")
                self.assertEqual(store.recover().code, "events.duplicate_conflict")

    def test_truncated_final_line_is_archived_and_continuous_suffix_is_replayed(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-truncated")
            old_snapshot = store.paths.state.read_bytes()
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                changed = replace(state, project_booleans=MappingProxyType({"ready": True}))
                transaction.commit_transition("fact_recorded", changed)
            store.paths.state.write_bytes(old_snapshot)
            with store.paths.events.open("ab") as handle:
                handle.write(b'{"event_seq":3,"partial"')

            result = store.recover()
            self.assertEqual(result.status, "recovered")
            self.assertTrue(result.state.project_booleans["ready"])
            archived = list(store.paths.recovery.glob("events-truncated-*.jsonl"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), b'{"event_seq":3,"partial"')
            self.assertTrue(store.paths.events.read_bytes().endswith(b"\n"))

    def test_invalid_suffix_is_fully_validated_before_tail_or_snapshot_writes(self):
        for with_tail in (False, True):
            with self.subTest(with_tail=with_tail), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(task_plan(), "run-invalid-suffix")
                snapshot_before = store.paths.state.read_bytes()
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    transaction.commit_transition("suffix", state)
                lines = store.paths.events.read_text(encoding="utf-8").splitlines()
                suffix = json.loads(lines[-1])
                suffix["payload"]["state"]["nodes"].pop("produce")
                unsigned = dict(suffix)
                unsigned.pop("event_hash")
                suffix["event_hash"] = sha256_bytes(
                    json.dumps(
                        unsigned,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                raw_events = (
                    (lines[0] + "\n").encode("utf-8")
                    + (json.dumps(suffix, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                )
                if with_tail:
                    raw_events += b'{"event_seq":3,"partial"'
                store.paths.events.write_bytes(raw_events)
                store.paths.state.write_bytes(snapshot_before)
                events_before = store.paths.events.read_bytes()
                result = store.recover()
                self.assertEqual((result.status, result.code), ("blocked", "events.unreplayable"))
                self.assertEqual(store.paths.events.read_bytes(), events_before)
                self.assertEqual(store.paths.state.read_bytes(), snapshot_before)
                self.assertFalse(store.paths.recovery.exists())

    def test_missing_snapshot_is_rebuilt_from_durable_events_and_projections_are_derived(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            expected = store.start_run(plan, "run-no-snapshot")
            store.paths.state.unlink()
            store.paths.artifacts.write_text("{manual-corruption", encoding="utf-8")
            store.paths.summary.write_text("manual edit\n", encoding="utf-8")

            recovered = store.recover()
            self.assertEqual((recovered.status, recovered.code), ("recovered", "recovery.replayed"))
            self.assertEqual(recovered.state, expected)
            self.assertEqual(
                json.loads(store.paths.state.read_text())["last_applied_event_seq"], 1
            )
            self.assertEqual(
                json.loads(store.paths.artifacts.read_text())["schema_version"],
                "artifact-projection-v1",
            )
            self.assertIn("run-no-snapshot", store.paths.summary.read_text(encoding="utf-8"))
            self.assertNotIn("manual edit", store.paths.summary.read_text(encoding="utf-8"))

    def test_control_transition_events_replay_to_identical_state(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("source.txt").write_bytes(b"evidence")
            plan = artifact_condition_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-control")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                artifact = ArtifactRuntime(
                    "source", "source.txt", sha256_bytes(b"evidence"), "verified", "external", 0
                )
                registered = replace(state, artifacts=MappingProxyType({"source": artifact}))
                transaction.commit_transition("artifact_registered", registered)
            with store.locked_run() as transaction:
                _, registered = transaction.load_active_run()
                old_snapshot = store.paths.state.read_bytes()
                stabilized, transitions = stabilize_control_nodes(plan, registered)
                transaction.commit_control_transitions(transitions, stabilized)
            store.paths.state.write_bytes(old_snapshot)

            result = store.recover()
            self.assertEqual(result.state, stabilized)
            self.assertIn("condition_selected", [item.event_type for item in store.read_run_events()])
            self.assertEqual(json.loads(store.paths.artifacts.read_text())["artifacts"][0]["state"], "verified")
            self.assertIn("use-source", store.paths.summary.read_text(encoding="utf-8"))

    def test_artifact_restart_condition_and_byte_drift_mark_affected_lineage_stale(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_bytes(b"verified bytes")
            plan = artifact_condition_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-artifact")
            artifact = ArtifactRuntime(
                "source", "source.txt", sha256_bytes(b"verified bytes"), "verified", "external", 0
            )
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = replace(state, artifacts=MappingProxyType({"source": artifact}))
                transaction.commit_transition("artifact_registered", registered)

            with WorkflowStore(root).locked_run() as transaction:
                restarted_plan, restarted = transaction.load_active_run()
                with self.assertRaises(TypeError):
                    restarted_plan.nodes["choose"].condition_cases[0]["when"]["artifact"] = "changed"
                stabilized, transitions = stabilize_control_nodes(restarted_plan, restarted)
                self.assertEqual(stabilized.nodes["choose"].outcome, "verified")
                transaction.commit_control_transitions(transitions, stabilized)

            source.write_bytes(b"changed bytes")
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.code, "recovery.artifact_drift")
            self.assertEqual(recovered.state.artifacts["source"].state, "stale")
            self.assertEqual(recovered.state.nodes["choose"].status, NodeStatus.STALE)
            self.assertEqual(recovered.state.nodes["use-source"].status, NodeStatus.STALE)
            self.assertEqual(json.loads(store.paths.artifacts.read_text())["artifacts"][0]["state"], "stale")

    def test_input_drift_blocks_running_attempt_before_stale_invalidation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "old.txt"
            source_path.write_bytes(b"verified input")
            plan = external_consumer_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-running-input-drift")
            source = ArtifactRuntime(
                "source", "old.txt", sha256_bytes(b"verified input"), "verified", "external", 0
            )
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                ready = refresh_ready(plan, replace(state, artifacts=MappingProxyType({"source": source})))
                transaction.commit_transition("artifact_registered", ready)
                running = claim_transition(plan, ready, "consumer", "running-consumer-token")
                claim_event = commit_claim(transaction, running)
                self.assertEqual(running.nodes["consumer"].status, NodeStatus.RUNNING)

            source_path.write_bytes(b"changed while running")
            with mock.patch.object(
                store, "_mark_drift", side_effect=StoreError("test.interrupted", "stop after uncertainty event")
            ):
                with self.assertRaises(StoreError):
                    store.recover()
            first_recovery_events = store.read_run_events()
            self.assertEqual(first_recovery_events[-1].event_type, "recovery_running_blocked")
            self.assertEqual(first_recovery_events[-1].payload["node_ids"], ("consumer",))

            # A restart after the uncertainty event must still detect the
            # unchanged byte drift and append the lineage invalidation.
            recovered = WorkflowStore(root).recover()
            events = store.read_run_events()
            event_types = [event.event_type for event in events]
            blocked_index = event_types.index("recovery_running_blocked")
            stale_index = event_types.index("artifacts_marked_stale")

            self.assertLess(blocked_index, stale_index)
            self.assertEqual(recovered.state.nodes["consumer"].status, NodeStatus.STALE)
            stale_runtime = recovered.state.nodes["consumer"]
            self.assertEqual(stale_runtime.attempt, 1)
            self.assertEqual(stale_runtime.claim_token_hash, "")
            self.assertEqual(stale_runtime.outcome, "")
            self.assertEqual(stale_runtime.selected_inputs, {})
            self.assertEqual(stale_runtime.auxiliary_outputs, {})
            self.assertEqual(stale_runtime.outputs, {})
            self.assertEqual(events[claim_event.event_seq - 1].event_type, "node_claimed")
            self.assertEqual(recovered.state.artifacts["source"].state, "stale")
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_stale_event_cannot_skip_running_block_on_commit_or_replay(self):
        payload = {
            "artifact_ids": ["source"],
            "witness_producer_ids": [],
            "receipt_node_ids": [],
        }
        for boundary in ("direct", "replay"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_path = root / "old.txt"
                source_path.write_bytes(b"verified input")
                plan = external_consumer_plan()
                store = WorkflowStore(root)
                store.start_run(plan, f"run-forged-running-stale-{boundary}")
                source = ArtifactRuntime(
                    "source", "old.txt", sha256_bytes(b"verified input"), "verified", "external", 0
                )
                replay_before = None
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    ready = refresh_ready(plan, replace(state, artifacts=MappingProxyType({"source": source})))
                    transaction.commit_transition("artifact_registered", ready)
                    running = claim_transition(plan, ready, "consumer", "running-token")
                    commit_claim(transaction, running)
                    source_path.write_bytes(b"drifted input")
                    stale = store._mark_drift(plan, running, ("source",))
                    if boundary == "direct":
                        before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                        with self.assertRaises(StoreError) as caught:
                            transaction.commit_transition("artifacts_marked_stale", stale, payload)
                        self.assertEqual(caught.exception.code, "events.invalid_invalidation")
                        self.assertEqual(
                            (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                        )
                    else:
                        append_rehashed_state_event(
                            store, workflow_store._state_data(stale),
                            "artifacts_marked_stale", payload,
                        )
                        replay_before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                if boundary == "replay":
                    recovered = store.recover()
                    self.assertEqual(
                        (recovered.status, recovered.code),
                        ("blocked", "events.invalid_invalidation"),
                    )
                    self.assertEqual(
                        (store.paths.events.read_bytes(), store.paths.state.read_bytes()), replay_before
                    )

    def test_produced_artifact_drift_stales_producer_attempt_siblings_and_descendants(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.txt"
            second_path = root / "second.txt"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            plan = produced_artifact_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-produced-drift")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "producer", "producer-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "producer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"first": "first.txt", "second": "second.txt"},
                    "artifacts": [
                        ArtifactRuntime("first", "first.txt", sha256_bytes(b"first"), "verified", "producer", 1),
                        ArtifactRuntime("second", "second.txt", sha256_bytes(b"second"), "verified", "producer", 1),
                    ],
                })
                commit_result(transaction, completed)
                running = claim_transition(plan, completed, "consumer", "consumer-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "consumer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {}, "artifacts": [],
                })
                commit_result(transaction, completed)

            first_path.write_bytes(b"changed")
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.code, "recovery.artifact_drift")
            self.assertEqual(recovered.state.nodes["producer"].status, NodeStatus.STALE)
            self.assertEqual(recovered.state.nodes["consumer"].status, NodeStatus.STALE)
            self.assertEqual(recovered.state.nodes["producer"].outcome, "")
            self.assertEqual(recovered.state.nodes["producer"].claim_token_hash, "")
            self.assertEqual(recovered.state.nodes["producer"].outputs, {
                "first": "first.txt", "second": "second.txt",
            })
            self.assertEqual(recovered.state.nodes["consumer"].selected_inputs, {})
            self.assertEqual(recovered.state.nodes["consumer"].claim_token_hash, "")
            self.assertEqual(recovered.state.artifacts["first"].state, "stale")
            self.assertEqual(recovered.state.artifacts["second"].state, "stale")

    def test_authoritative_inputs_are_bounded_and_excessive_nesting_fails_closed(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.paths.base.mkdir(parents=True)
            store.paths.selection.write_bytes(b" " * (9 * 1024 * 1024))
            with self.assertRaises(StoreError) as caught:
                store.read_selection()
            self.assertEqual(caught.exception.code, "store.input_too_large")

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-large-event")
            before = store.paths.events.read_bytes()
            with store.paths.events.open("ab") as handle:
                handle.write(b"{" + b'\"padding\":\"' + b"x" * (3 * 1024 * 1024) + b'\"}\n')
            evidence = store.paths.events.read_bytes()
            self.assertEqual(store.recover().code, "events.input_too_large")
            self.assertEqual(store.paths.events.read_bytes(), evidence)
            self.assertTrue(evidence.startswith(before))

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-deep-event")
            deep = b'{"payload":' + b'{"x":' * 1500 + b"0" + b"}" * 1500 + b"}\n"
            with store.paths.events.open("ab") as handle:
                handle.write(deep)
            evidence = store.paths.events.read_bytes()
            self.assertIn(store.recover().code, {"events.invalid_json", "events.nesting_too_deep"})
            self.assertEqual(store.paths.events.read_bytes(), evidence)

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-deep-append")
            nested = 0
            for _index in range(200):
                nested = {"nested": nested}
            before = store.paths.events.read_bytes()
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("too_deep", state, {"nested": nested})
            self.assertEqual(caught.exception.code, "events.invalid_event")
            self.assertEqual(store.paths.events.read_bytes(), before)

    def test_named_lock_replacement_invalidates_old_lease_before_another_write(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            store.start_run(task_plan(), "run-lock-name")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                events_before = store.paths.events.read_bytes()
                state_before = store.paths.state.read_bytes()
                displaced = store.paths.lock.with_name(".lock-displaced")
                os.replace(store.paths.lock, displaced)
                store.paths.lock.write_bytes(b"0")
                repository = Path(__file__).resolve().parents[1]
                environment = dict(os.environ)
                environment["PYTHONPATH"] = os.pathsep.join(
                    filter(
                        None,
                        (str(repository), environment.get("PYTHONPATH", "")),
                    )
                )
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib,sys; "
                            "from scripts.workflow_engine.store import WorkflowStore; "
                            "store=WorkflowStore(pathlib.Path(sys.argv[1])); "
                            "context=store.locked_run(); transaction=context.__enter__(); "
                            "transaction.load_active_run(); print('replacement-lock-acquired'); "
                            "context.__exit__(None,None,None)"
                        ),
                        str(root),
                    ],
                    cwd=repository,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertIn("replacement-lock-acquired", child.stdout)
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("must-not-append", state)
                self.assertEqual(caught.exception.code, "store.lock_invalidated")
                self.assertEqual(store.paths.events.read_bytes(), events_before)
                self.assertEqual(store.paths.state.read_bytes(), state_before)

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-post-append-lock")
            original_append = workflow_store.append_event
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                events_before = store.paths.events.read_bytes()
                state_before = store.paths.state.read_bytes()

                def append_then_replace_lock(path, event):
                    original_append(path, event)
                    os.replace(
                        store.paths.lock,
                        store.paths.lock.with_name(".lock-after-append"),
                    )
                    store.paths.lock.write_bytes(b"0")

                with mock.patch.object(
                    workflow_store, "append_event", side_effect=append_then_replace_lock
                ):
                    with self.assertRaises(StoreError) as caught:
                        transaction.commit_transition("durable-suffix-only", state)
                self.assertEqual(caught.exception.code, "store.lock_invalidated")
                self.assertNotEqual(store.paths.events.read_bytes(), events_before)
                self.assertEqual(store.paths.state.read_bytes(), state_before)

    def test_control_batch_preflights_every_event_before_first_append(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            large_outcome = "x" * workflow_fs.MAX_EVENT_BYTES
            plan = independent_condition_plan(large_outcome)
            store.start_run(plan, "run-whole-control-preflight")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                transitions = (
                    ControlTransition(
                        "first-condition",
                        "condition_selected",
                        "short",
                        MappingProxyType({}),
                    ),
                    ControlTransition(
                        "second-condition",
                        "condition_selected",
                        large_outcome,
                        MappingProxyType({}),
                    ),
                )
                intermediate = WorkflowTransaction._apply_control(
                    plan, state, transitions[0]
                )
                final_state = WorkflowTransaction._apply_control(
                    plan, intermediate, transitions[1]
                )
                events_before = store.paths.events.read_bytes()
                state_before = store.paths.state.read_bytes()
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_control_transitions(transitions, final_state)
                self.assertEqual(caught.exception.code, "events.input_too_large")
                self.assertEqual(store.paths.events.read_bytes(), events_before)
                self.assertEqual(store.paths.state.read_bytes(), state_before)

    def test_start_run_preflights_oversized_initial_event_before_run_material(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            with self.assertRaises(StoreError) as caught:
                store.start_run(oversized_initial_event_plan(), "run-too-large-to-start")
            self.assertEqual(caught.exception.code, "events.input_too_large")
            self.assertFalse(store.paths.run_dir.exists())
            retried = store.start_run(task_plan(), "run-valid-retry")
            self.assertEqual(retried.run_id, "run-valid-retry")

    def test_event_type_is_validated_before_append(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-event-type-codec")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                events_before = store.paths.events.read_bytes()
                state_before = store.paths.state.read_bytes()
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("bad\nevent", state)
                self.assertEqual(caught.exception.code, "events.invalid_event")
                self.assertEqual(store.paths.events.read_bytes(), events_before)
                self.assertEqual(store.paths.state.read_bytes(), state_before)
            with WorkflowStore(Path(temporary)).locked_run() as transaction:
                transaction.load_active_run()

    def test_complete_event_envelope_limit_is_checked_before_append(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-complete-event-bound")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                state_data = workflow_store._state_data(state)
                base_payload = {
                    "note": "",
                    "state": state_data,
                    "run_status": "active",
                }
                base_size = len(
                    json.dumps(
                        base_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                padding = "x" * (workflow_fs.MAX_EVENT_BYTES - base_size - 1)
                complete_payload = dict(base_payload, note=padding)
                event = WorkflowEvent.create(
                    event_seq=2,
                    run_id=state.run_id,
                    semantic_sha256=plan.semantic_sha256,
                    event_type="envelope_bound",
                    payload=complete_payload,
                    previous_event_hash=transaction.events()[-1].event_hash,
                )
                encoded_payload = json.dumps(
                    complete_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                encoded_line = (
                    json.dumps(
                        event.to_payload(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                self.assertLessEqual(len(encoded_payload), workflow_fs.MAX_EVENT_BYTES)
                self.assertGreater(len(encoded_line), workflow_fs.MAX_EVENT_BYTES)
                events_before = store.paths.events.read_bytes()
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition(
                        "envelope_bound", state, {"note": padding}
                    )
                self.assertEqual(caught.exception.code, "events.input_too_large")
                self.assertEqual(store.paths.events.read_bytes(), events_before)

    def test_claim_hash_codec_rejects_malformed_values_before_write_and_on_restart(self):
        malformed_hashes = ("A" * 64, "a" * 63, "a" * 65, "g" * 64)
        for malformed_hash in malformed_hashes:
            with self.subTest(commit=malformed_hash[:8]), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(task_plan(), "run-claim-hash-commit")
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    malformed = replace(
                        state,
                        nodes=MappingProxyType(
                            {
                                "produce": replace(
                                    state.nodes["produce"],
                                    status=NodeStatus.RUNNING,
                                    attempt=1,
                                    claim_token_hash=malformed_hash,
                                )
                            }
                        ),
                    )
                    events_before = store.paths.events.read_bytes()
                    with self.assertRaises(StoreError) as caught:
                        transaction.commit_transition("node_claimed", malformed)
                    self.assertEqual(caught.exception.code, "snapshot.invalid")
                    self.assertEqual(store.paths.events.read_bytes(), events_before)

        for malformed_hash in malformed_hashes:
            with self.subTest(restart=malformed_hash[:8]), TemporaryDirectory() as temporary:
                store = WorkflowStore(Path(temporary))
                store.start_run(task_plan(), "run-claim-hash-restart")
                first = json.loads(store.paths.events.read_text(encoding="utf-8"))
                forged_state = copy.deepcopy(first["payload"]["state"])
                forged_state["nodes"]["produce"].update(
                    {
                        "status": "running",
                        "attempt": 1,
                        "claim_token_hash": malformed_hash,
                    }
                )
                forged = WorkflowEvent.create(
                    event_seq=2,
                    run_id=first["run_id"],
                    semantic_sha256=first["semantic_sha256"],
                    event_type="node_claimed",
                    payload={"state": forged_state, "run_status": "active"},
                    previous_event_hash=first["event_hash"],
                )
                with store.paths.events.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            forged.to_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                events_before = store.paths.events.read_bytes()
                state_before = store.paths.state.read_bytes()
                recovered = WorkflowStore(Path(temporary)).recover()
                self.assertEqual(
                    (recovered.status, recovered.code),
                    ("blocked", "events.unreplayable"),
                )
                self.assertEqual(store.paths.events.read_bytes(), events_before)
                self.assertEqual(store.paths.state.read_bytes(), state_before)

    def test_warning_code_codec_is_identical_for_activation_and_persisted_selection(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            saved = store.save_draft(self.document, expected_document_revision=0)
            plan = plan_for_document(saved)
            valid_code = "risk.compiler.missing-integrity"
            selected = store.activate_custom(
                plan,
                high_risk_warning_codes=(valid_code,),
                acknowledged_warning_codes=(valid_code,),
            )
            self.assertEqual(
                Selection.from_payload(selected.to_payload()).acknowledged_warning_codes,
                (valid_code,),
            )
            invalid_code_sets = (
                ["risk\ncode"],
                ["risk\tcode"],
                [" risk.code "],
                ["x" * 257],
                [f"risk.{index:03d}" for index in range(257)],
            )
            for codes in invalid_code_sets:
                with self.subTest(codes=codes[:2]):
                    payload = selected.to_payload()
                    payload["acknowledged_warning_codes"] = codes
                    with self.assertRaises(StoreError) as decoded:
                        Selection.from_payload(payload)
                    self.assertEqual(decoded.exception.code, "selection.invalid")
                    journal_before = store.paths.audit_events.read_bytes()
                    selection_before = store.paths.selection.read_bytes()
                    with self.assertRaises(StoreError) as activated:
                        store.activate_custom(
                            plan,
                            high_risk_warning_codes=tuple(codes),
                            acknowledged_warning_codes=tuple(codes),
                        )
                    self.assertEqual(
                        activated.exception.code,
                        "activation.acknowledgement_mismatch",
                    )
                    self.assertEqual(store.paths.audit_events.read_bytes(), journal_before)
                    self.assertEqual(store.paths.selection.read_bytes(), selection_before)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WorkflowStore(root)
            saved = store.save_draft(self.document, expected_document_revision=0)
            plan = plan_for_document(saved)
            store.activate_custom(
                plan,
                high_risk_warning_codes=("risk.valid",),
                acknowledged_warning_codes=("risk.valid",),
            )
            journal = json.loads(store.paths.audit_events.read_text(encoding="utf-8"))
            journal["payload"]["selection"]["acknowledged_warning_codes"] = [
                "risk\nforged"
            ]
            unsigned = dict(journal)
            unsigned.pop("event_hash")
            journal["event_hash"] = sha256_bytes(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            store.paths.audit_events.write_text(
                json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            store.paths.selection.unlink()
            evidence = store.paths.audit_events.read_bytes()
            with self.assertRaises(StoreError) as caught:
                WorkflowStore(root).read_selection()
            self.assertEqual(caught.exception.code, "selection.journal_invalid")
            self.assertEqual(store.paths.audit_events.read_bytes(), evidence)
            self.assertFalse(store.paths.selection.exists())

    def test_run_status_lifecycle_is_validated_for_full_chain_boundary_and_suffix(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            store.start_run(task_plan(), "run-illegal-resume")
            first = json.loads(store.paths.events.read_text(encoding="utf-8"))
            stopped = WorkflowEvent.create(
                event_seq=2,
                run_id=first["run_id"],
                semantic_sha256=first["semantic_sha256"],
                event_type="run_stopped",
                payload={"state": first["payload"]["state"], "run_status": "stopped"},
                previous_event_hash=first["event_hash"],
            )
            resumed = WorkflowEvent.create(
                event_seq=3,
                run_id=first["run_id"],
                semantic_sha256=first["semantic_sha256"],
                event_type="fact_recorded",
                payload={"state": first["payload"]["state"], "run_status": "active"},
                previous_event_hash=stopped.event_hash,
            )
            with store.paths.events.open("a", encoding="utf-8") as handle:
                for event in (stopped, resumed):
                    handle.write(
                        json.dumps(
                            event.to_payload(), sort_keys=True, separators=(",", ":")
                        )
                        + "\n"
                    )
            with store.paths.events.open("ab") as handle:
                handle.write(b'{"event_seq":4,"partial"')
            events_before = store.paths.events.read_bytes()
            state_before = store.paths.state.read_bytes()
            recovered = store.recover()
            self.assertEqual(
                (recovered.status, recovered.code),
                ("blocked", "events.lifecycle_invalid"),
            )
            self.assertEqual(store.paths.events.read_bytes(), events_before)
            self.assertEqual(store.paths.state.read_bytes(), state_before)
            self.assertFalse(store.paths.recovery.exists())

        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-illegal-boundary")
            started = json.loads(store.paths.events.read_text(encoding="utf-8"))
            started["payload"]["run_status"] = "archived"
            unsigned = dict(started)
            unsigned.pop("event_hash")
            started["event_hash"] = sha256_bytes(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            store.paths.events.write_text(
                json.dumps(started, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            snapshot["run_status"] = "archived"
            snapshot["last_applied_event_hash"] = started["event_hash"]
            store.paths.state.write_text(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            events_before = store.paths.events.read_bytes()
            state_before = store.paths.state.read_bytes()
            recovered = store.recover()
            self.assertEqual(
                (recovered.status, recovered.code),
                ("blocked", "events.lifecycle_invalid"),
            )
            with self.assertRaises(StoreError) as caught:
                store.start_run(plan, "run-must-not-replace-illegal-boundary")
            self.assertEqual(caught.exception.code, "events.lifecycle_invalid")
            self.assertEqual(store.paths.events.read_bytes(), events_before)
            self.assertEqual(store.paths.state.read_bytes(), state_before)

    def test_artifact_receipts_are_bound_to_compiled_producer_authority(self):
        cases = {
            "unknown_producer": ArtifactRuntime(
                "draft", "artifact.txt", sha256_bytes(b"artifact"), "verified", "unknown", 1
            ),
            "external_swap": ArtifactRuntime(
                "draft", "artifact.txt", sha256_bytes(b"artifact"), "verified", "external", 0
            ),
            "undeclared_output": ArtifactRuntime(
                "ghost", "artifact.txt", sha256_bytes(b"artifact"), "verified", "produce", 1
            ),
            "impossible_attempt": ArtifactRuntime(
                "draft", "artifact.txt", sha256_bytes(b"artifact"), "verified", "produce", 2
            ),
        }
        for name, artifact in cases.items():
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                root.joinpath("artifact.txt").write_bytes(b"artifact")
                store = WorkflowStore(root)
                store.start_run(task_plan(), "run-artifact-authority")
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    nodes = MappingProxyType(
                        {"produce": replace(state.nodes["produce"], attempt=1)}
                    )
                    invalid = replace(
                        state,
                        nodes=nodes,
                        artifacts=MappingProxyType({artifact.artifact_id: artifact}),
                    )
                    events_before = store.paths.events.read_bytes()
                    state_before = store.paths.state.read_bytes()
                    with self.assertRaises(StoreError) as caught:
                        transaction.commit_transition("artifact_forged", invalid)
                    self.assertEqual(
                        caught.exception.code, "artifact.authority_invalid"
                    )
                    self.assertEqual(store.paths.events.read_bytes(), events_before)
                    self.assertEqual(store.paths.state.read_bytes(), state_before)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("artifact.txt").write_bytes(b"artifact")
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, "run-artifact-authority-suffix")
            first = json.loads(store.paths.events.read_text(encoding="utf-8"))
            forged_state = copy.deepcopy(first["payload"]["state"])
            forged_state["artifacts"]["draft"] = {
                "artifact_id": "draft",
                "path": "artifact.txt",
                "sha256": sha256_bytes(b"artifact"),
                "state": "verified",
                "producer_node_id": "external",
                "producer_attempt": 0,
            }
            forged = WorkflowEvent.create(
                event_seq=2,
                run_id=first["run_id"],
                semantic_sha256=first["semantic_sha256"],
                event_type="artifact_registered",
                payload={"state": forged_state, "run_status": "active"},
                previous_event_hash=first["event_hash"],
            )
            with store.paths.events.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        forged.to_payload(), sort_keys=True, separators=(",", ":")
                    )
                    + "\n"
                )
            events_before = store.paths.events.read_bytes()
            state_before = store.paths.state.read_bytes()
            recovered = store.recover()
            self.assertEqual(
                (recovered.status, recovered.code),
                ("blocked", "artifact.authority_invalid"),
            )
            self.assertEqual(store.paths.events.read_bytes(), events_before)
            self.assertEqual(store.paths.state.read_bytes(), state_before)

    def test_prior_attempt_stale_artifact_requires_exact_historical_success_receipt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "artifact.txt"
            artifact_path.write_bytes(b"artifact")
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, "run-prior-attempt-stale-proof")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "attempt-one")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "artifact.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "artifact.txt", sha256_bytes(b"artifact"),
                        "verified", "produce", 1,
                    )],
                })
                commit_result(transaction, completed)

            with store.locked_run() as transaction:
                plan, completed = transaction.load_active_run()
                history = transaction.events()
            stale_artifact = replace(completed.artifacts["draft"], state="stale")
            # Model a later attempt whose mutable runtime path no longer points at
            # the stale artifact; only its earlier success receipt may authorize it.
            later_runtime = replace(
                completed.nodes["produce"], status=NodeStatus.RUNNING, attempt=2,
                outputs=MappingProxyType({"draft": "later-attempt.txt"}),
                claim_token_hash=sha256_bytes(b"attempt-two"),
            )
            later_state = replace(
                completed,
                nodes=MappingProxyType({"produce": later_runtime}),
                artifacts=MappingProxyType({"draft": stale_artifact}),
            )
            workflow_store._validate_artifact_authority(plan, later_state, history)

            invalid_cases = {
                "missing_history": (later_state, ()),
                "wrong_path": (
                    replace(later_state, artifacts=MappingProxyType({
                        "draft": replace(stale_artifact, path="other.txt"),
                    })), history,
                ),
                "wrong_hash": (
                    replace(later_state, artifacts=MappingProxyType({
                        "draft": replace(stale_artifact, sha256=sha256_bytes(b"other")),
                    })), history,
                ),
                "mutable_output_without_receipt": (
                    replace(later_state, nodes=MappingProxyType({
                        "produce": replace(later_runtime, outputs=MappingProxyType({"draft": "artifact.txt"})),
                    })), (),
                ),
                "undeclared_historical_producer": (
                    replace(later_state, artifacts=MappingProxyType({
                        "draft": replace(stale_artifact, producer_node_id="other-producer"),
                    })), history,
                ),
                "invalid_prior_attempt": (
                    replace(later_state, artifacts=MappingProxyType({
                        "draft": replace(stale_artifact, producer_attempt=0),
                    })), history,
                ),
            }
            for name, (candidate, events) in invalid_cases.items():
                with self.subTest(name=name), self.assertRaises(StoreError) as caught:
                    workflow_store._validate_artifact_authority(plan, candidate, events)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")

            for field, value in (("node_id", "other-producer"), ("attempt", 2)):
                forged_history = list(history)
                result_index = next(
                    index for index, event in enumerate(forged_history)
                    if event.event_type == "node_result_recorded"
                )
                original = forged_history[result_index]
                forged_payload = json.loads(workflow_store._canonical_bytes(original.payload))
                forged_payload["receipt"][field] = value
                forged_history[result_index] = replace(original, payload=forged_payload)
                with self.subTest(receipt_field=field), self.assertRaises(StoreError) as caught:
                    workflow_store._validate_artifact_authority(plan, later_state, forged_history)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")

            for field, value in (("id", "other"), ("path", "other.txt"),
                                 ("sha256", sha256_bytes(b"other"))):
                forged_history = list(history)
                result_index = next(
                    index for index, event in enumerate(forged_history)
                    if event.event_type == "node_result_recorded"
                )
                original = forged_history[result_index]
                forged_payload = json.loads(workflow_store._canonical_bytes(original.payload))
                forged_payload["receipt"]["output_artifacts"][0][field] = value
                forged_history[result_index] = replace(original, payload=forged_payload)
                with self.subTest(receipt_output_field=field), self.assertRaises(StoreError) as caught:
                    workflow_store._validate_artifact_authority(plan, later_state, forged_history)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")

    def test_stale_rerun_event_is_exact_idempotent_and_claim_advances_attempt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft.txt"
            draft.write_bytes(b"original")
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, "run-stale-rerun-event")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "original-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "draft.txt", sha256_bytes(b"original"),
                        "verified", "produce", 1,
                    )],
                })
                commit_result(transaction, completed)

            draft.write_bytes(b"changed")
            stale = store.recover()
            self.assertIn(stale.status, {"clean", "recovered"})
            self.assertEqual(stale.state.nodes["produce"].status.value, "stale")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                expected, affected = rerun_stale_transition(plan, state, "produce")
                payload = workflow_store._stale_rerun_event_fields(
                    plan, state, expected, "produce", affected
                )
                before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition(
                        "stale_rerun_requested", expected,
                        {**payload, "affected_node_ids": []},
                    )
                self.assertEqual(caught.exception.code, "events.invalid_stale_rerun")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

                event = transaction.request_stale_rerun("produce")
                self.assertEqual(event.event_type, "stale_rerun_requested")
                _, rerun_state = transaction._require_loaded()
                self.assertEqual(rerun_state.nodes["produce"].status, NodeStatus.READY)
                self.assertEqual(rerun_state.nodes["produce"].attempt, 1)
                self.assertEqual(rerun_state.nodes["produce"].outputs, {"draft": "draft.txt"})
                self.assertEqual(rerun_state.artifacts["draft"].state, "stale")
                self.assertEqual(event.payload["attempts_before"], {"produce": 1})
                self.assertEqual(event.payload["attempts_after"], {"produce": 1})

                before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
                with self.assertRaises(StoreError) as duplicate:
                    transaction.request_stale_rerun("produce")
                self.assertEqual(duplicate.exception.code, "runtime.invalid_stale_rerun")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

            self.assertEqual(WorkflowStore(root).recover().status, "clean")
            with store.locked_run() as transaction:
                plan, rerun_state = transaction.load_active_run()
                second_attempt = claim_transition(plan, rerun_state, "produce", "new-token")
                commit_claim(transaction, second_attempt)
                self.assertEqual(second_attempt.nodes["produce"].attempt, 2)
                self.assertEqual(second_attempt.artifacts["draft"].state, "stale")

    def test_rehashed_stale_rerun_forgery_blocks_full_chain_and_suffix_without_writes(self):
        def stale_store(root, run_id):
            draft = root / "draft.txt"
            draft.write_bytes(b"original")
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, run_id)
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", f"{run_id}-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "draft.txt", sha256_bytes(b"original"),
                        "verified", "produce", 1,
                    )],
                })
                commit_result(transaction, completed)
            draft.write_bytes(b"changed")
            store.recover()
            return store, plan

        for boundary in ("suffix", "full_chain"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_id = f"run-stale-rerun-forged-{boundary}"
                store, plan = stale_store(root, run_id)
                with store.locked_run() as transaction:
                    plan, state = transaction.load_active_run()
                    expected, affected = rerun_stale_transition(plan, state, "produce")
                    payload = workflow_store._stale_rerun_event_fields(
                        plan, state, expected, "produce", affected
                    )
                forged_payload = {**payload, "affected_node_ids": []}
                event = append_rehashed_state_event(
                    store,
                    workflow_store._state_data(expected),
                    "stale_rerun_requested",
                    forged_payload,
                )
                if boundary == "full_chain":
                    snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
                    snapshot["state"] = workflow_store._state_data(expected)
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    store.paths.state.write_text(
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
                recovered = WorkflowStore(root).recover()
                self.assertEqual((recovered.status, recovered.code),
                                 ("blocked", "events.invalid_stale_rerun"))
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_stale_rerun_rejects_attempt_output_and_route_tampering_without_writes(self):
        from tests.test_workflow_scheduler import WorkflowSchedulerTests

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft.txt"
            draft.write_bytes(b"original")
            plan = WorkflowSchedulerTests().compile(
                [
                    workflow_task("source", entry=True, outputs=("draft",)),
                    workflow_task("sink", inputs=("draft",)),
                ],
                [workflow_edge(
                    "source-sink", "source", "sink", output_map={"draft": "draft"}
                )],
            )
            store = WorkflowStore(root)
            store.start_run(plan, "run-stale-rerun-forgery")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                running = claim_transition(plan, state, "source", "source-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "source", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "draft.txt", sha256_bytes(b"original"),
                        "verified", "source", 1,
                    )],
                })
                commit_result(transaction, completed)

            draft.write_bytes(b"changed")
            store.recover()
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                expected, affected = rerun_stale_transition(plan, state, "source")
                payload = workflow_store._stale_rerun_event_fields(
                    plan, state, expected, "source", affected
                )
                self.assertEqual(payload["reset_edge_ids"], ["source-sink"])
                mutations = (
                    ("edge_list", expected, {**payload, "reset_edge_ids": []}),
                    ("attempt_map", expected, {
                        **payload, "attempts_after": {"sink": 0, "source": 2},
                    }),
                    ("attempt_state", replace(expected, nodes=MappingProxyType({
                        **expected.nodes,
                        "source": replace(expected.nodes["source"], attempt=2),
                    })), payload),
                    ("output_resurrection", replace(expected, nodes=MappingProxyType({
                        **expected.nodes,
                        "source": replace(expected.nodes["source"], outcome="succeeded"),
                    })), payload),
                    ("satisfied_route", replace(expected, edges=MappingProxyType({
                        **expected.edges,
                        "source-sink": EdgeRuntime(EdgeStatus.SATISFIED, {"draft": "draft.txt"}),
                    })), payload),
                )
                for name, forged, forged_payload in mutations:
                    before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
                    with self.subTest(name=name), self.assertRaises(StoreError):
                        transaction.commit_transition(
                            "stale_rerun_requested", forged, forged_payload
                        )
                    self.assertEqual(
                        (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                    )

    def test_stale_rerun_suffix_recovery_applies_the_committed_transition(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft.txt"
            draft.write_bytes(b"original")
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, "run-stale-rerun-suffix")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "original-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "draft.txt", sha256_bytes(b"original"),
                        "verified", "produce", 1,
                    )],
                })
                commit_result(transaction, completed)
            draft.write_bytes(b"changed")
            store.recover()

            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                updated, affected = rerun_stale_transition(plan, state, "produce")
                payload = workflow_store._stale_rerun_event_fields(
                    plan, state, updated, "produce", affected
                )
                transaction._commit_event(
                    "stale_rerun_requested", updated, payload, replace_snapshot=False
                )

            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code),
                             ("recovered", "recovery.replayed"))
            self.assertEqual(recovered.state.nodes["produce"].status, NodeStatus.READY)
            self.assertEqual(recovered.state.nodes["produce"].attempt, 1)
            self.assertEqual(recovered.state.artifacts["draft"].state, "stale")
            self.assertEqual(store.read_run_events()[-1].event_type, "stale_rerun_requested")

    def test_stale_rerun_suffix_after_run_stop_is_rejected_without_writes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft.txt"
            draft.write_bytes(b"original")
            store = WorkflowStore(root)
            plan = task_plan()
            store.start_run(plan, "run-stale-rerun-stopped")
            with store.locked_run() as transaction:
                plan, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "original-token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "draft.txt", sha256_bytes(b"original"),
                        "verified", "produce", 1,
                    )],
                })
                commit_result(transaction, completed)

            draft.write_bytes(b"changed")
            store.recover()
            store.deactivate_custom()
            with store._lock():
                material = store._validated_run_material(
                    allow_truncated=False, require_no_suffix=True,
                )
            plan, state, sequence, event_hash, run_status, _events, _tail, _prefix, _witnesses = material
            self.assertEqual(run_status, "stopped")
            updated, affected = rerun_stale_transition(plan, state, "produce")
            payload = workflow_store._stale_rerun_event_fields(
                plan, state, updated, "produce", affected,
            )
            event = WorkflowEvent.create(
                event_seq=sequence + 1,
                run_id=state.run_id,
                semantic_sha256=plan.semantic_sha256,
                event_type="stale_rerun_requested",
                payload={**payload, "state": workflow_store._state_data(updated),
                         "run_status": "stopped"},
                previous_event_hash=event_hash,
            )
            with store.paths.events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    event.to_payload(), sort_keys=True, separators=(",", ":")
                ) + "\n")

            before = store.paths.events.read_bytes(), store.paths.state.read_bytes()
            recovered = WorkflowStore(root).recover()
            self.assertEqual(
                (recovered.status, recovered.code),
                ("blocked", "events.invalid_stale_rerun"),
            )
            self.assertEqual(
                (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before,
            )

    def test_verified_produced_artifact_requires_completed_matching_attempt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("draft.txt").write_bytes(b"draft")
            store = WorkflowStore(root)
            store.start_run(task_plan(), "run-unfinished-producer")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = replace(
                    state.nodes["produce"],
                    status=NodeStatus.RUNNING,
                    attempt=1,
                    claim_token_hash=sha256_bytes(b"claim"),
                )
                forged = replace(
                    state,
                    nodes=MappingProxyType({"produce": running}),
                    artifacts=MappingProxyType({
                        "draft": ArtifactRuntime(
                            "draft", "draft.txt", sha256_bytes(b"draft"),
                            "verified", "produce", 1,
                        )
                    }),
                )
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", forged)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual(
                    (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                )

            append_rehashed_state_event(store, workflow_store._state_data(forged))
            with store.paths.events.open("ab") as handle:
                handle.write(b'{"event_seq":3,"partial"')
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code), ("blocked", "artifact.authority_invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
            self.assertFalse(store.paths.recovery.exists())

    def test_produced_artifact_path_must_equal_completed_output_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("declared.txt").write_bytes(b"declared")
            root.joinpath("other.txt").write_bytes(b"other")
            store = WorkflowStore(root)
            store.start_run(task_plan(), "run-output-path-mismatch")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                completed = replace(
                    state.nodes["produce"], status=NodeStatus.SUCCEEDED,
                    attempt=1, outputs=MappingProxyType({"draft": "declared.txt"}),
                )
                forged = replace(
                    state,
                    nodes=MappingProxyType({"produce": completed}),
                    artifacts=MappingProxyType({
                        "draft": ArtifactRuntime(
                            "draft", "other.txt", sha256_bytes(b"other"),
                            "verified", "produce", 1,
                        )
                    }),
                )
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("result_forged", forged)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual(
                    (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                )

            event = append_rehashed_state_event(
                store, workflow_store._state_data(forged), "result_forged"
            )
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            snapshot["state"] = workflow_store._state_data(forged)
            snapshot["last_applied_event_seq"] = event.event_seq
            snapshot["last_applied_event_hash"] = event.event_hash
            store.paths.state.write_text(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code), ("blocked", "artifact.authority_invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_historical_produced_receipt_forgery_blocks_valid_later_boundary(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("declared.txt").write_bytes(b"declared")
            root.joinpath("other.txt").write_bytes(b"other")
            store = WorkflowStore(root)
            store.start_run(task_plan(), "run-historical-forgery")
            first = json.loads(store.paths.events.read_text(encoding="utf-8"))
            forged = copy.deepcopy(first["payload"]["state"])
            forged["nodes"]["produce"].update({
                "status": "succeeded", "attempt": 1,
                "outputs": {"draft": "declared.txt"},
            })
            forged["artifacts"]["draft"] = {
                "artifact_id": "draft", "path": "other.txt",
                "sha256": sha256_bytes(b"other"), "state": "verified",
                "producer_node_id": "produce", "producer_attempt": 1,
            }
            append_rehashed_state_event(store, forged, "result_forged")
            last = append_rehashed_state_event(
                store, first["payload"]["state"], "fact_recorded"
            )
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            snapshot["last_applied_event_seq"] = last.event_seq
            snapshot["last_applied_event_hash"] = last.event_hash
            store.paths.state.write_text(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            result = WorkflowStore(root).recover()
            self.assertEqual((result.status, result.code), ("blocked", "artifact.authority_invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_produced_to_external_reset_preserves_unfinished_consumer(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("first.txt").write_bytes(b"first")
            root.joinpath("second.txt").write_bytes(b"second")
            plan = replace(produced_artifact_plan(), external_inputs=("first",))
            store = WorkflowStore(root)
            store.start_run(plan, "run-reset-unfinished")
            external = ArtifactRuntime(
                "first", "old.txt", sha256_bytes(b"old"), "verified", "external", 0
            )
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = replace(state, artifacts=MappingProxyType({"first": external}))
                transaction.commit_transition("artifact_registered", registered)
                running = claim_transition(plan, registered, "producer", "producer-token")
                commit_claim(transaction, running)
                produced = result_transition(plan, running, {
                    "node_id": "producer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded",
                    "outputs": {"first": "first.txt", "second": "second.txt"},
                    "artifacts": [
                        ArtifactRuntime("first", "first.txt", sha256_bytes(b"first"), "verified", "producer", 1),
                        ArtifactRuntime("second", "second.txt", sha256_bytes(b"second"), "verified", "producer", 1),
                    ],
                })
                commit_result(transaction, produced)
                self.assertEqual(produced.nodes["consumer"].status, NodeStatus.READY)
                self.assertEqual(produced.nodes["consumer"].selected_inputs["first"], "first.txt")
                reset_nodes = dict(produced.nodes)
                reset_nodes["producer"] = stale_runtime(reset_nodes["producer"])
                reset_nodes["consumer"] = replace(
                    reset_nodes["consumer"], status=NodeStatus.PENDING,
                    selected_inputs=MappingProxyType({}),
                )
                reset = replace(
                    produced,
                    nodes=MappingProxyType(reset_nodes),
                    edges=MappingProxyType({
                        "producer-consumer": EdgeRuntime(EdgeStatus.WAITING),
                    }),
                    artifacts=MappingProxyType({
                        "first": external,
                        "second": replace(produced.artifacts["second"], state="stale"),
                    }),
                )
                transaction.commit_transition("artifact_registered", reset)
                self.assertEqual(refresh_ready(plan, reset).nodes["consumer"].status, NodeStatus.PENDING)
                with self.assertRaises(Exception):
                    claim_transition(plan, reset, "consumer", "must-wait-for-new-producer")
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_source_replacement_rejects_old_satisfied_edge_and_selected_input(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("first.txt").write_bytes(b"first")
            root.joinpath("second.txt").write_bytes(b"second")
            plan = replace(produced_artifact_plan(), external_inputs=("first",))
            store = WorkflowStore(root)
            store.start_run(plan, "run-old-edge")
            external = ArtifactRuntime("first", "old.txt", sha256_bytes(b"old"), "verified", "external", 0)
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = replace(state, artifacts=MappingProxyType({"first": external}))
                transaction.commit_transition("artifact_registered", registered)
                running = claim_transition(plan, registered, "producer", "token")
                commit_claim(transaction, running)
                produced = result_transition(plan, running, {
                    "node_id": "producer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"first": "first.txt", "second": "second.txt"},
                    "artifacts": [
                        ArtifactRuntime("first", "first.txt", sha256_bytes(b"first"), "verified", "producer", 1),
                        ArtifactRuntime("second", "second.txt", sha256_bytes(b"second"), "verified", "producer", 1),
                    ],
                })
                commit_result(transaction, produced)
                nodes = dict(produced.nodes)
                nodes["producer"] = replace(nodes["producer"], status=NodeStatus.STALE)
                nodes["consumer"] = replace(nodes["consumer"], status=NodeStatus.STALE)
                stale = replace(
                    produced, nodes=MappingProxyType(nodes),
                    artifacts=MappingProxyType({
                        "first": external,
                        "second": replace(produced.artifacts["second"], state="stale"),
                    }),
                )
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as edge:
                    transaction.commit_transition("artifact_registered", stale)
                self.assertEqual(edge.exception.code, "edge.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                nodes["consumer"] = replace(
                    nodes["consumer"], status=NodeStatus.READY,
                    selected_inputs=MappingProxyType({"first": "first.txt"}),
                )
                stale_route = replace(
                    stale, nodes=MappingProxyType(nodes),
                    edges=MappingProxyType({"producer-consumer": EdgeRuntime(EdgeStatus.WAITING)}),
                )
                with self.assertRaises(StoreError) as selected:
                    transaction.commit_transition("artifact_registered", stale_route)
                self.assertEqual(selected.exception.code, "artifact.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_any_success_replacement_rejects_superseded_frozen_winner_at_every_boundary(self):
        for boundary in ("direct", "suffix", "full_chain"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name, value in (
                    ("external.txt", b"external"), ("first.txt", b"first"),
                    ("other.txt", b"other"),
                ):
                    root.joinpath(name).write_bytes(value)
                plan = any_success_refinement_plan()
                store = WorkflowStore(root)
                store.start_run(plan, f"run-any-success-{boundary}")
                complete, external = complete_any_success_refinement(store, plan)
                self.assertEqual(complete.nodes["join"].status, NodeStatus.READY)
                self.assertEqual(complete.nodes["join"].selected_inputs, {"joined": "first.txt"})
                self.assertEqual(complete.edges["other-join"].status, EdgeStatus.SATISFIED)
                nodes = dict(complete.nodes)
                nodes["first-task"] = replace(nodes["first-task"], status=NodeStatus.STALE)
                invalid = replace(
                    complete,
                    nodes=MappingProxyType(nodes),
                    edges=MappingProxyType({
                        **complete.edges,
                        "first-join": EdgeRuntime(EdgeStatus.WAITING),
                    }),
                    artifacts=MappingProxyType({**complete.artifacts, "first": external}),
                )
                if boundary == "direct":
                    with store.locked_run() as transaction:
                        transaction.load_active_run()
                        before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                        with self.assertRaises(StoreError) as caught:
                            transaction.commit_transition("artifact_registered", invalid)
                        self.assertEqual(caught.exception.code, "join.winner_invalid")
                        self.assertEqual(
                            (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                        )
                    continue
                event = append_rehashed_state_event(store, workflow_store._state_data(invalid))
                if boundary == "full_chain":
                    snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
                    snapshot["state"] = workflow_store._state_data(invalid)
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    store.paths.state.write_text(
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                else:
                    with store.paths.events.open("ab") as handle:
                        handle.write(b'{"event_seq":99,"partial"')
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                recovered = WorkflowStore(root).recover()
                self.assertEqual((recovered.status, recovered.code), ("blocked", "join.winner_invalid"))
                self.assertEqual(
                    (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                )
                if boundary == "suffix":
                    self.assertFalse(store.paths.recovery.exists())

    def test_any_success_replacement_stales_winner_without_promoting_auxiliary(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (
                ("external.txt", b"external"), ("first.txt", b"first"),
                ("other.txt", b"other"),
            ):
                root.joinpath(name).write_bytes(value)
            plan = any_success_refinement_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-any-success-valid")
            complete, external = complete_any_success_refinement(store, plan)
            nodes = dict(complete.nodes)
            nodes["first-task"] = stale_runtime(nodes["first-task"])
            nodes["join"] = stale_runtime(nodes["join"])
            replacement = replace(
                complete,
                nodes=MappingProxyType(nodes),
                edges=MappingProxyType({
                    **complete.edges, "first-join": EdgeRuntime(EdgeStatus.WAITING),
                }),
                artifacts=MappingProxyType({**complete.artifacts, "first": external}),
            )
            with store.locked_run() as transaction:
                transaction.load_active_run()
                transaction.commit_transition("artifact_registered", replacement)
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.status, "clean")
            self.assertEqual(recovered.state.nodes["join"].status, NodeStatus.STALE)
            self.assertEqual(recovered.state.nodes["join"].winner_edge_id, "first-join")
            with store.locked_run() as transaction:
                _, current = transaction.load_active_run()
                stabilized, transitions = stabilize_control_nodes(plan, current)
                transaction.commit_control_transitions(transitions, stabilized)
            self.assertEqual(transitions, ())
            self.assertEqual(stabilized.nodes["join"].status, NodeStatus.STALE)

    def test_stale_frozen_join_cannot_retain_a_selectable_old_input_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("external.txt", b"external"), ("first.txt", b"first"), ("other.txt", b"other")):
                (root / name).write_bytes(value)
            plan = any_success_refinement_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-stale-selected")
            complete, external = complete_any_success_refinement(store, plan)
            nodes = dict(complete.nodes)
            nodes["first-task"] = replace(nodes["first-task"], status=NodeStatus.STALE)
            nodes["join"] = replace(nodes["join"], status=NodeStatus.STALE)
            candidate = replace(
                complete,
                nodes=MappingProxyType(nodes),
                edges=MappingProxyType({**complete.edges, "first-join": EdgeRuntime(EdgeStatus.WAITING)}),
                artifacts=MappingProxyType({**complete.artifacts, "first": external}),
            )
            with store.locked_run() as transaction:
                transaction.load_active_run()
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", candidate)
                self.assertEqual(caught.exception.code, "join.winner_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_compiled_frozen_winner_survives_unrelated_registration_with_downstream(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-winner-unrelated")
            state = complete_compiled_winner(store, plan, root)
            self.assertEqual(state.nodes["joined"].winner_edge_id, "b-joined")
            self.assertEqual(state.nodes["consumer"].status, NodeStatus.SUCCEEDED)
            (root / "source-new.txt").write_bytes(b"source-new")
            source = ArtifactRuntime("source", "source-new.txt", sha256_bytes(b"source-new"), "verified", "external", 0)
            nodes = dict(state.nodes)
            nodes["c"] = replace(nodes["c"], selected_inputs=MappingProxyType({}))
            changed = replace(state, nodes=MappingProxyType(nodes), artifacts=MappingProxyType({**state.artifacts, "source": source}))
            changed = refresh_ready(plan, changed)
            with store.locked_run() as transaction:
                transaction.load_active_run()
                transaction.commit_transition("artifact_registered", changed)
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.status, "clean")
            self.assertEqual(recovered.state.nodes["joined"].winner_edge_id, "b-joined")
            self.assertEqual(recovered.state.nodes["joined"].outputs, {"joined": "b.txt"})
            self.assertEqual(recovered.state.nodes["consumer"].status, NodeStatus.SUCCEEDED)

    def test_ready_frozen_winner_survives_unrelated_registration_after_late_auxiliary(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-ready-winner-unrelated")
            state = complete_compiled_winner(store, plan, root, downstream=False, complete_join=False)
            self.assertEqual(state.nodes["joined"].status, NodeStatus.READY)
            self.assertEqual(state.nodes["joined"].winner_edge_id, "b-joined")
            self.assertEqual(state.nodes["joined"].auxiliary_outputs["joined"], ("a.txt",))
            (root / "source-new.txt").write_bytes(b"source-new")
            source = ArtifactRuntime("source", "source-new.txt", sha256_bytes(b"source-new"), "verified", "external", 0)
            nodes = dict(state.nodes)
            nodes["c"] = replace(nodes["c"], selected_inputs=MappingProxyType({}))
            changed = refresh_ready(plan, replace(
                state, nodes=MappingProxyType(nodes),
                artifacts=MappingProxyType({**state.artifacts, "source": source}),
            ))
            with store.locked_run() as transaction:
                transaction.load_active_run()
                transaction.commit_transition("artifact_registered", changed)
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.status, "clean")
            self.assertEqual(recovered.state.nodes["joined"].winner_edge_id, "b-joined")
            self.assertEqual(recovered.state.nodes["joined"].selected_inputs, {"joined": "b.txt"})

    def test_historical_winner_hash_survives_same_id_registry_overwrite_and_detects_drift(self):
        for changed in ("a.txt", "b.txt"):
            with self.subTest(changed=changed), TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan = compiled_winner_plan()
                store = WorkflowStore(root)
                store.start_run(plan, f"run-witness-{changed[0]}")
                state = complete_compiled_winner(store, plan, root)
                self.assertEqual(state.artifacts["draft"].producer_node_id, "a")
                self.assertEqual(state.nodes["joined"].winner_edge_id, "b-joined")
                (root / changed).write_bytes(b"changed")
                recovered = WorkflowStore(root).recover()
                self.assertEqual(recovered.code, "recovery.artifact_drift")
                if changed == "b.txt":
                    self.assertEqual(recovered.state.nodes["joined"].status, NodeStatus.STALE)
                    self.assertEqual(recovered.state.nodes["joined"].winner_edge_id, "b-joined")
                    self.assertEqual(recovered.state.nodes["consumer"].status, NodeStatus.STALE)
                    self.assertEqual(recovered.state.edges["joined-consumer"].status, EdgeStatus.WAITING)
                else:
                    self.assertEqual(recovered.state.nodes["joined"].status, NodeStatus.SUCCEEDED)
                    self.assertEqual(recovered.state.nodes["consumer"].status, NodeStatus.SUCCEEDED)

    def test_snapshotless_recovery_reconstructs_the_frozen_winner_and_its_old_hash(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-snapshotless-witness")
            complete_compiled_winner(store, plan, root)
            store.paths.state.unlink()
            replayed = WorkflowStore(root).recover()
            self.assertEqual((replayed.status, replayed.code), ("recovered", "recovery.replayed"))
            self.assertEqual(replayed.state.nodes["joined"].winner_edge_id, "b-joined")
            (root / "b.txt").write_bytes(b"drifted old winner")
            drift = WorkflowStore(root).recover()
            self.assertEqual(drift.code, "recovery.artifact_drift")
            self.assertEqual(drift.state.nodes["joined"].status, NodeStatus.STALE)

    def test_historical_winner_drift_stales_its_registry_visible_sibling_receipt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan(b_has_notes=True)
            store = WorkflowStore(root)
            store.start_run(plan, "run-winner-sibling")
            state = complete_compiled_winner(store, plan, root)
            self.assertEqual(state.artifacts["draft"].producer_node_id, "a")
            self.assertEqual(state.artifacts["notes"].producer_node_id, "b")
            (root / "b.txt").write_bytes(b"winner changed")
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.code, "recovery.artifact_drift")
            self.assertEqual(recovered.state.nodes["joined"].status, NodeStatus.STALE)
            self.assertEqual(recovered.state.artifacts["notes"].state, "stale")

    def test_same_path_auxiliary_identity_and_changed_winner_bytes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-same-path-winner")
            state = complete_compiled_winner(store, plan, root, b_path="shared.txt", a_path="shared.txt")
            self.assertEqual(state.nodes["joined"].winner_edge_id, "b-joined")
            self.assertEqual(state.nodes["joined"].auxiliary_outputs, {})
            self.assertEqual(state.edges["a-joined"].status, EdgeStatus.SATISFIED)
            self.assertEqual(state.edges["b-joined"].status, EdgeStatus.SATISFIED)
            self.assertEqual(WorkflowStore(root).recover().status, "clean")
            (root / "shared.txt").write_bytes(b"changed")
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.code, "recovery.artifact_drift")
            self.assertEqual(recovered.state.nodes["joined"].status, NodeStatus.STALE)

    def test_forged_unaffected_edge_and_false_completion_are_zero_write_rejections(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-forged-edge")
            state = complete_compiled_winner(store, plan, root)
            forged_edges = dict(state.edges)
            forged_edges["a-joined"] = EdgeRuntime(EdgeStatus.SATISFIED, {"joined": "b.txt"})
            forged = replace(state, edges=MappingProxyType(forged_edges))
            with store.locked_run() as transaction:
                transaction.load_active_run()
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError):
                    transaction.commit_transition("artifact_registered", forged)
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

        for event_type in ("artifact_registered", "node_result"):
            with self.subTest(event_type=event_type), TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "b.txt").write_bytes(b"winner")
                plan = compiled_winner_plan()
                store = WorkflowStore(root)
                store.start_run(plan, f"run-false-{event_type}")
                with store.locked_run() as transaction:
                    _, state = transaction.load_active_run()
                    nodes = dict(state.nodes)
                    nodes["b"] = replace(nodes["b"], status=NodeStatus.SUCCEEDED, attempt=1,
                                         outcome="succeeded", outputs=MappingProxyType({"draft": "b.txt"}))
                    forged = replace(state, nodes=MappingProxyType(nodes),
                                     edges=MappingProxyType({**state.edges, "b-joined": EdgeRuntime(EdgeStatus.SATISFIED, {"joined": "b.txt"})}),
                                     artifacts=MappingProxyType({"draft": ArtifactRuntime("draft", "b.txt", sha256_bytes(b"winner"), "verified", "b", 1)}))
                    forged = refresh_ready(plan, forged)
                    before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                    with self.assertRaises(StoreError):
                        transaction.commit_transition(event_type, forged)
                    self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_registration_cannot_cut_a_satisfied_edge_from_an_unaffected_succeeded_source(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-forged-route-cut")
            state = complete_compiled_winner(store, plan, root)
            nodes = dict(state.nodes)
            nodes["joined"] = replace(nodes["joined"], status=NodeStatus.STALE,
                                      selected_inputs=MappingProxyType({}))
            nodes["consumer"] = replace(nodes["consumer"], status=NodeStatus.STALE,
                                        selected_inputs=MappingProxyType({}))
            candidate = replace(state, nodes=MappingProxyType(nodes), edges=MappingProxyType({
                **state.edges,
                "b-joined": EdgeRuntime(EdgeStatus.WAITING),
                "joined-consumer": EdgeRuntime(EdgeStatus.WAITING),
            }), artifacts=MappingProxyType({
                **state.artifacts,
                "final": replace(state.artifacts["final"], state="stale"),
            }))
            with store.locked_run() as transaction:
                transaction.load_active_run()
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", candidate)
                self.assertEqual(caught.exception.code, "edge.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_registration_cannot_stale_an_unaffected_winning_source_or_route(self):
        for change_source in (False, True):
            with self.subTest(change_source=change_source), TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan = compiled_winner_plan()
                store = WorkflowStore(root)
                store.start_run(plan, "run-unrelated-winner-stale")
                state = complete_compiled_winner(store, plan, root)
                forged = forged_unrelated_winner_invalidation(
                    store, plan, state, root, change_source=change_source
                )
                with store.locked_run() as transaction:
                    transaction.load_active_run()
                    before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                    with self.assertRaises(StoreError) as caught:
                        transaction.commit_transition("artifact_registered", forged)
                    self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                    self.assertEqual(
                        (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                    )

    def test_rehashed_unrelated_winner_invalidation_rejects_full_and_suffix_zero_write(self):
        for boundary in ("full_chain", "suffix"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan = compiled_winner_plan()
                store = WorkflowStore(root)
                store.start_run(plan, f"run-forged-stale-{boundary}")
                state = complete_compiled_winner(store, plan, root)
                forged = forged_unrelated_winner_invalidation(
                    store, plan, state, root, change_source=True
                )
                event = append_rehashed_state_event(
                    store, workflow_store._state_data(forged), "artifact_registered"
                )
                if boundary == "full_chain":
                    snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
                    snapshot["state"] = workflow_store._state_data(forged)
                    snapshot["last_applied_event_seq"] = event.event_seq
                    snapshot["last_applied_event_hash"] = event.event_hash
                    store.paths.state.write_text(
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                recovered = WorkflowStore(root).recover()
                self.assertEqual((recovered.status, recovered.code),
                                 ("blocked", "artifact.authority_invalid"))
                self.assertEqual(
                    (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                )

    def test_recovery_uses_snapshot_boundary_witnesses_for_valid_multi_event_suffix(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-boundary-witness")
            complete_compiled_winner(store, plan, root)
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                transaction._commit_event("fact_recorded", state, {}, replace_snapshot=False)
                (root / "b.txt").write_bytes(b"changed")
                stale = store._mark_drift(plan, state, [], ["b"])
                transaction._commit_event("artifacts_marked_stale", stale,
                                          {"artifact_ids": [], "witness_producer_ids": ["b"]},
                                          replace_snapshot=False)
            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code),
                             ("recovered", "recovery.replayed"))
            self.assertEqual(recovered.state.nodes["joined"].status, NodeStatus.STALE)
            self.assertEqual(recovered.state.nodes["joined"].winner_edge_id, "b-joined")
            self.assertEqual(recovered.state.edges["b-joined"].status, EdgeStatus.WAITING)

    def test_all_active_reconvergence_accepts_identical_historical_proof(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "draft.txt").write_bytes(b"draft")
            plan = compiled_reconvergence_plan(distinct_sources=False)
            store = WorkflowStore(root)
            store.start_run(plan, "run-identical-reconvergence")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "source", "token")
                commit_claim(transaction, running)
                state = result_transition(plan, running, {
                    "node_id": "source", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "draft.txt", sha256_bytes(b"draft"),
                        "verified", "source", 1,
                    )],
                })
                commit_result(transaction, state)
                stable, controls = stabilize_control_nodes(plan, state)
                self.assertEqual((stable.nodes["merged"].status, len(controls)),
                                 (NodeStatus.SUCCEEDED, 3))
                transaction.commit_control_transitions(controls, stable)
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_all_active_reconvergence_rejects_distinct_historical_proofs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "draft.txt").write_bytes(b"draft")
            plan = compiled_reconvergence_plan(distinct_sources=True)
            store = WorkflowStore(root)
            store.start_run(plan, "run-distinct-reconvergence")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                for source in ("source-a", "source-b"):
                    running = claim_transition(plan, state, source, f"token-{source}")
                    commit_claim(transaction, running)
                    state = result_transition(plan, running, {
                        "node_id": source, "attempt": 1, "status": "succeeded",
                        "outcome": "succeeded", "outputs": {"draft": "draft.txt"},
                        "artifacts": [ArtifactRuntime(
                            "draft", "draft.txt", sha256_bytes(b"draft"),
                            "verified", source, 1,
                        )],
                    })
                    commit_result(transaction, state)
                stable, controls = stabilize_control_nodes(plan, state)
                self.assertEqual(stable.nodes["merged"].status, NodeStatus.SUCCEEDED)
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_control_transitions(controls, stable)
                self.assertEqual(caught.exception.code, "edge.witness_missing")
                self.assertEqual(
                    (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                )

    def test_rehashed_forgery_is_rejected_on_full_chain_and_suffix_before_writes(self):
        for forgery in ("edge_map", "winner_rebind", "false_registration", "false_result"):
            for boundary in ("suffix", "full_chain"):
                with self.subTest(forgery=forgery, boundary=boundary), TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    plan = compiled_winner_plan()
                    store = WorkflowStore(root)
                    store.start_run(plan, f"run-forgery-{forgery}-{boundary}")
                    if forgery in {"edge_map", "winner_rebind"}:
                        state = complete_compiled_winner(store, plan, root)
                        nodes = dict(state.nodes)
                        edges = dict(state.edges)
                        if forgery == "edge_map":
                            edges["a-joined"] = EdgeRuntime(EdgeStatus.SATISFIED, {"joined": "b.txt"})
                        else:
                            nodes["joined"] = replace(nodes["joined"], winner_edge_id="a-joined",
                                                       selected_inputs=MappingProxyType({"joined": "a.txt"}),
                                                       outputs=MappingProxyType({"joined": "a.txt"}))
                            edges["joined-consumer"] = EdgeRuntime(EdgeStatus.SATISFIED, {"joined": "a.txt"})
                        forged = replace(state, nodes=MappingProxyType(nodes), edges=MappingProxyType(edges))
                        event_type = "artifact_registered"
                    else:
                        (root / "b.txt").write_bytes(b"winner")
                        with store.locked_run() as transaction:
                            _, state = transaction.load_active_run()
                        nodes = dict(state.nodes)
                        nodes["b"] = replace(nodes["b"], status=NodeStatus.SUCCEEDED, attempt=1,
                                             outcome="succeeded", outputs=MappingProxyType({"draft": "b.txt"}))
                        forged = replace(state, nodes=MappingProxyType(nodes),
                                         edges=MappingProxyType({**state.edges, "b-joined": EdgeRuntime(EdgeStatus.SATISFIED, {"joined": "b.txt"})}),
                                         artifacts=MappingProxyType({"draft": ArtifactRuntime("draft", "b.txt", sha256_bytes(b"winner"), "verified", "b", 1)}))
                        forged = refresh_ready(plan, forged)
                        event_type = "node_result" if forgery == "false_result" else "artifact_registered"
                    event = append_rehashed_state_event(store, workflow_store._state_data(forged), event_type)
                    if boundary == "full_chain":
                        snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
                        snapshot["state"] = workflow_store._state_data(forged)
                        snapshot["last_applied_event_seq"] = event.event_seq
                        snapshot["last_applied_event_hash"] = event.event_hash
                        store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                    else:
                        with store.paths.events.open("ab") as handle:
                            handle.write(b'{"partial":')
                    before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                    recovered = WorkflowStore(root).recover()
                    self.assertEqual(recovered.status, "blocked")
                    self.assertIn(recovered.code, {"edge.authority_invalid", "join.winner_invalid", "events.invalid_completion", "events.invalid_claim"})
                    self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                    if boundary == "suffix":
                        self.assertFalse(store.paths.recovery.exists())

    def test_file_carrying_task_edge_requires_a_real_completion_hash_receipt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b.txt").write_bytes(b"winner")
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-no-edge-witness")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "b", "b-token")
                commit_claim(transaction, running)
                no_receipt = result_transition(plan, running, {
                    "node_id": "b", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "b.txt"}, "artifacts": [],
                })
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("node_result", no_receipt)
                self.assertEqual(caught.exception.code, "events.invalid_completion")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_late_same_path_rewrite_cannot_keep_old_winner_bytes_authoritative(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared.txt"
            shared.write_bytes(b"winner")
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-late-rewrite")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "b", "b-token")
                commit_claim(transaction, running)
                state = result_transition(plan, running, {
                    "node_id": "b", "attempt": 1, "status": "succeeded", "outcome": "succeeded",
                    "outputs": {"draft": "shared.txt"},
                    "artifacts": [ArtifactRuntime("draft", "shared.txt", sha256_bytes(b"winner"), "verified", "b", 1)],
                })
                commit_result(transaction, state)
                running = claim_transition(plan, state, "a", "a-token")
                commit_claim(transaction, running)
                shared.write_bytes(b"overwritten")
                late = result_transition(plan, running, {
                    "node_id": "a", "attempt": 1, "status": "succeeded", "outcome": "succeeded",
                    "outputs": {"draft": "shared.txt"},
                    "artifacts": [ArtifactRuntime("draft", "shared.txt", sha256_bytes(b"overwritten"), "verified", "a", 1)],
                })
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    commit_result(transaction, late)
                self.assertEqual(caught.exception.code, "artifact.verification_failed")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.code, "recovery.running_work_uncertain")
            self.assertEqual(recovered.state.nodes["joined"].status, NodeStatus.STALE)

    def test_legacy_custom_state_without_winner_field_fails_closed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = compiled_winner_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-old-custom-codec")
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            del snapshot["state"]["nodes"]["joined"]["winner_edge_id"]
            store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            result = WorkflowStore(root).recover()
            self.assertEqual((result.status, result.code), ("blocked", "snapshot.invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_rehashed_run_start_cannot_begin_with_an_already_completed_task(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "draft.txt").write_bytes(b"draft")
            plan = task_plan()
            store = WorkflowStore(root)
            state = store.start_run(plan, "run-forged-start")
            raw_first = json.loads(store.paths.events.read_text(encoding="utf-8").splitlines()[0])
            nodes = dict(state.nodes)
            nodes["produce"] = replace(nodes["produce"], status=NodeStatus.SUCCEEDED, attempt=1,
                                       outcome="succeeded", outputs=MappingProxyType({"draft": "draft.txt"}))
            forged = replace(state, nodes=MappingProxyType(nodes), artifacts=MappingProxyType({
                "draft": ArtifactRuntime("draft", "draft.txt", sha256_bytes(b"draft"), "verified", "produce", 1)
            }))
            first = WorkflowEvent.create(
                event_seq=1,
                run_id=state.run_id,
                semantic_sha256=plan.semantic_sha256,
                event_type="run_started",
                payload={**raw_first["payload"], "state": workflow_store._state_data(forged)},
                previous_event_hash=ZERO_HASH,
            )
            store.paths.events.write_text(json.dumps(first.to_payload(), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            snapshot["state"] = workflow_store._state_data(forged)
            snapshot["last_applied_event_hash"] = first.event_hash
            store.paths.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            result = WorkflowStore(root).recover()
            self.assertEqual((result.status, result.code), ("blocked", "events.invalid_completion"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_source_independent_zero_attempt_exclusion_survives_reregistration(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("new.txt").write_bytes(b"new")
            plan = excluded_branch_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-independent-exclusion")
            excluded = complete_excluded_branch(store, plan)
            self.assertEqual(excluded.nodes["fallback"].attempt, 0)
            self.assertEqual(excluded.edges["choose-fallback"].status, EdgeStatus.INACTIVE)
            self.assertEqual(excluded.edges["fallback-sink"].status, EdgeStatus.INACTIVE)
            changed = ArtifactRuntime(
                "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
            )
            candidate = replace(
                excluded, artifacts=MappingProxyType({"source": changed})
            )
            with store.locked_run() as transaction:
                transaction.load_active_run()
                transaction.commit_transition("artifact_registered", candidate)
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.status, "clean")
            for node_id in ("fallback", "sink"):
                self.assertEqual(recovered.state.nodes[node_id].status, NodeStatus.SKIPPED)
            self.assertEqual(recovered.state.edges["fallback-sink"].status, EdgeStatus.INACTIVE)

    def test_changed_controlling_lineage_reopens_unattempted_excluded_branch(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("new.txt").write_bytes(b"new")
            plan = excluded_branch_plan(artifact_condition=True)
            store = WorkflowStore(root)
            store.start_run(plan, "run-changed-exclusion")
            selected = complete_excluded_branch(store, plan, artifact_condition=True)
            changed = ArtifactRuntime(
                "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
            )
            nodes = dict(selected.nodes)
            nodes["choose"] = stale_runtime(nodes["choose"])
            for node_id in ("main", "fallback", "sink"):
                nodes[node_id] = replace(
                    nodes[node_id], status=NodeStatus.PENDING,
                    selected_inputs=MappingProxyType({}),
                )
            candidate = replace(
                selected,
                nodes=MappingProxyType(nodes),
                edges=MappingProxyType({
                    edge_id: EdgeRuntime(EdgeStatus.WAITING) for edge_id in plan.edges
                }),
                artifacts=MappingProxyType({"source": changed}),
            )
            with store.locked_run() as transaction:
                transaction.load_active_run()
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                unsafe = replace(candidate, nodes=MappingProxyType({
                    **candidate.nodes, "fallback": selected.nodes["fallback"],
                }))
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", unsafe)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                transaction.commit_transition("artifact_registered", candidate)
            recovered = WorkflowStore(root).recover()
            self.assertEqual(recovered.status, "clean")
            for node_id in ("fallback", "sink"):
                self.assertEqual(recovered.state.nodes[node_id].status, NodeStatus.PENDING)
                self.assertEqual(recovered.state.nodes[node_id].attempt, 0)

    def test_external_replacement_cannot_retain_stale_condition_outcome_on_commit_or_replay(self):
        for boundary in ("direct", "replay"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as temporary:
                root = Path(temporary)
                root.joinpath("old.txt").write_bytes(b"old")
                root.joinpath("new.txt").write_bytes(b"new")
                plan = excluded_branch_plan(artifact_condition=True)
                store = WorkflowStore(root)
                store.start_run(plan, f"run-stale-condition-{boundary}")
                selected = complete_excluded_branch(store, plan, artifact_condition=True)
                changed = ArtifactRuntime(
                    "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
                )
                nodes = dict(selected.nodes)
                nodes["choose"] = replace(
                    stale_runtime(selected.nodes["choose"]),
                    outcome=selected.nodes["choose"].outcome,
                )
                for node_id in ("main", "fallback", "sink"):
                    nodes[node_id] = replace(
                        nodes[node_id], status=NodeStatus.PENDING,
                        selected_inputs=MappingProxyType({}),
                    )
                unsafe = replace(
                    selected,
                    nodes=MappingProxyType(nodes),
                    edges=MappingProxyType({
                        edge_id: EdgeRuntime(EdgeStatus.WAITING) for edge_id in plan.edges
                    }),
                    artifacts=MappingProxyType({"source": changed}),
                )
                if boundary == "direct":
                    with store.locked_run() as transaction:
                        transaction.load_active_run()
                        before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                        with self.assertRaises(StoreError) as caught:
                            transaction.commit_transition("artifact_registered", unsafe)
                        self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                        self.assertEqual(
                            (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                        )
                else:
                    append_rehashed_state_event(
                        store, workflow_store._state_data(unsafe), "artifact_registered"
                    )
                    before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                    recovered = store.recover()
                    self.assertEqual(
                        (recovered.status, recovered.code),
                        ("blocked", "artifact.authority_invalid"),
                    )
                    self.assertEqual(
                        (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                    )

    def test_attempted_skip_still_requires_stale_evidence_on_source_change(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("new.txt").write_bytes(b"new")
            base = external_consumer_plan()
            consumer = replace(base.nodes["consumer"], failure_policy="skip_branch")
            plan = replace(base, nodes=MappingProxyType({"consumer": consumer}))
            store = WorkflowStore(root)
            store.start_run(plan, "run-attempted-skip")
            old = ArtifactRuntime(
                "source", "old.txt", sha256_bytes(b"old"), "verified", "external", 0
            )
            new = ArtifactRuntime(
                "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
            )
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                ready = refresh_ready(
                    plan, replace(state, artifacts=MappingProxyType({"source": old}))
                )
                transaction.commit_transition("artifact_registered", ready)
                running = claim_transition(plan, ready, "consumer", "skip-token")
                commit_claim(transaction, running)
                skipped = result_transition(plan, running, {
                    "node_id": "consumer", "attempt": 1, "status": "failed",
                    "outcome": "", "outputs": {}, "artifacts": [],
                })
                commit_result(transaction, skipped)
                self.assertEqual(skipped.nodes["consumer"].status, NodeStatus.SKIPPED)
                self.assertEqual(skipped.nodes["consumer"].attempt, 1)
                candidate = replace(
                    skipped, artifacts=MappingProxyType({"source": new})
                )
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", candidate)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                stale = replace(candidate, nodes=MappingProxyType({
                    "consumer": stale_runtime(skipped.nodes["consumer"]),
                }))
                transaction.commit_transition("artifact_registered", stale)
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_changed_external_registration_stales_completed_consumer_but_identical_is_idempotent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("old.txt", b"old"), ("new.txt", b"new"), ("draft.txt", b"draft")):
                root.joinpath(name).write_bytes(value)
            plan = external_consumer_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-external-change")
            complete = complete_external_consumer(store, plan)
            with store.locked_run() as transaction:
                _, current = transaction.load_active_run()
                transaction.commit_transition("artifact_registered", current)
                self.assertEqual(transaction.load_active_run()[1].nodes["consumer"].status, NodeStatus.SUCCEEDED)
                changed = ArtifactRuntime(
                    "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
                )
                unsafe = replace(current, artifacts=MappingProxyType({**current.artifacts, "source": changed}))
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", unsafe)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
                valid = replace(
                    complete,
                    nodes=MappingProxyType({
                        "consumer": stale_runtime(complete.nodes["consumer"]),
                    }),
                    artifacts=MappingProxyType({
                        "source": changed,
                        "draft": replace(complete.artifacts["draft"], state="stale"),
                    }),
                )
                transaction.commit_transition("artifact_registered", valid)
                current_stale = transaction.load_active_run()[1].nodes["consumer"]
                self.assertEqual(current_stale.outcome, "")
                self.assertEqual(current_stale.claim_token_hash, "")
                self.assertEqual(current_stale.outputs, {"draft": "draft.txt"})
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_changed_external_registration_cannot_silently_continue_running_work(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("new.txt").write_bytes(b"new")
            plan = external_consumer_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-external-running")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = refresh_ready(plan, replace(state, artifacts=MappingProxyType({
                    "source": ArtifactRuntime(
                        "source", "old.txt", sha256_bytes(b"old"), "verified", "external", 0
                    ),
                })))
                transaction.commit_transition("artifact_registered", registered)
                running = claim_transition(plan, registered, "consumer", "token")
                commit_claim(transaction, running)
                changed = ArtifactRuntime(
                    "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
                )
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", replace(
                        running, artifacts=MappingProxyType({"source": changed})
                    ))
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_rehashed_changed_external_registration_blocks_before_tail_rewrite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("old.txt", b"old"), ("new.txt", b"new"), ("draft.txt", b"draft")):
                root.joinpath(name).write_bytes(value)
            plan = external_consumer_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-external-forgery")
            complete = complete_external_consumer(store, plan)
            forged = replace(complete, artifacts=MappingProxyType({
                **complete.artifacts,
                "source": ArtifactRuntime(
                    "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
                ),
            }))
            append_rehashed_state_event(store, workflow_store._state_data(forged))
            with store.paths.events.open("ab") as handle:
                handle.write(b'{"event_seq":5,"partial"')
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code), ("blocked", "artifact.authority_invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
            self.assertFalse(store.paths.recovery.exists())

    def test_rehashed_changed_external_registration_blocks_full_chain(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("old.txt", b"old"), ("new.txt", b"new"), ("draft.txt", b"draft")):
                root.joinpath(name).write_bytes(value)
            plan = external_consumer_plan()
            store = WorkflowStore(root)
            store.start_run(plan, "run-external-full-chain")
            complete = complete_external_consumer(store, plan)
            forged = replace(complete, artifacts=MappingProxyType({
                **complete.artifacts,
                "source": ArtifactRuntime(
                    "source", "new.txt", sha256_bytes(b"new"), "verified", "external", 0
                ),
            }))
            event = append_rehashed_state_event(store, workflow_store._state_data(forged))
            snapshot = json.loads(store.paths.state.read_text(encoding="utf-8"))
            snapshot["state"] = workflow_store._state_data(forged)
            snapshot["last_applied_event_seq"] = event.event_seq
            snapshot["last_applied_event_hash"] = event.event_hash
            store.paths.state.write_text(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code), ("blocked", "artifact.authority_invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

    def test_ordered_producers_may_refine_one_logical_artifact_id(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"external")
            root.joinpath("first.txt").write_bytes(b"first")
            root.joinpath("second.txt").write_bytes(b"second")
            root.joinpath("refined.txt").write_bytes(b"refined")
            base = produced_artifact_plan()
            consumer = compiled_node(
                "consumer", inputs=("first",), outputs=("first",)
            )
            plan = replace(
                base, external_inputs=("first",),
                nodes=MappingProxyType({**base.nodes, "consumer": consumer}),
            )
            store = WorkflowStore(root)
            store.start_run(plan, "run-ordered-refinement")
            external = ArtifactRuntime(
                "first", "old.txt", sha256_bytes(b"external"), "verified", "external", 0
            )
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = replace(state, artifacts=MappingProxyType({"first": external}))
                transaction.commit_transition("artifact_registered", registered)
                first_running = claim_transition(plan, registered, "producer", "first-token")
                commit_claim(transaction, first_running)
                first_result = result_transition(plan, first_running, {
                    "node_id": "producer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded",
                    "outputs": {"first": "first.txt", "second": "second.txt"},
                    "artifacts": [
                        ArtifactRuntime("first", "first.txt", sha256_bytes(b"first"), "verified", "producer", 1),
                        ArtifactRuntime("second", "second.txt", sha256_bytes(b"second"), "verified", "producer", 1),
                    ],
                })
                commit_result(transaction, first_result)
                second_running = claim_transition(plan, first_result, "consumer", "second-token")
                commit_claim(transaction, second_running)
                second_result = result_transition(plan, second_running, {
                    "node_id": "consumer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"first": "refined.txt"},
                    "artifacts": [ArtifactRuntime(
                        "first", "refined.txt", sha256_bytes(b"refined"),
                        "verified", "consumer", 1,
                    )],
                })
                commit_result(transaction, second_result)
            self.assertEqual(second_result.artifacts["first"].producer_node_id, "consumer")
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_same_id_external_refinement_requires_stale_lineage_on_reregistration(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"external")
            root.joinpath("first.txt").write_bytes(b"refined")
            root.joinpath("second.txt").write_bytes(b"sibling")
            plan = replace(produced_artifact_plan(), external_inputs=("first",))
            store = WorkflowStore(root)
            store.start_run(plan, "run-refine-external")
            external = ArtifactRuntime(
                "first", "old.txt", sha256_bytes(b"external"), "verified", "external", 0
            )
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = replace(state, artifacts=MappingProxyType({"first": external}))
                transaction.commit_transition("artifact_registered", registered)
                running = claim_transition(plan, registered, "producer", "producer-token")
                commit_claim(transaction, running)
                produced = result_transition(plan, running, {
                    "node_id": "producer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded",
                    "outputs": {"first": "first.txt", "second": "second.txt"},
                    "artifacts": [
                        ArtifactRuntime("first", "first.txt", sha256_bytes(b"refined"), "verified", "producer", 1),
                        ArtifactRuntime("second", "second.txt", sha256_bytes(b"sibling"), "verified", "producer", 1),
                    ],
                })
                commit_result(transaction, produced)
                consumer_running = claim_transition(plan, produced, "consumer", "consumer-token")
                commit_claim(transaction, consumer_running)
                complete = result_transition(plan, consumer_running, {
                    "node_id": "consumer", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {}, "artifacts": [],
                })
                commit_result(transaction, complete)
                self.assertEqual(complete.artifacts["first"].producer_node_id, "producer")
                unsafe = replace(complete, artifacts=MappingProxyType({
                    **complete.artifacts, "first": external,
                }))
                before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                with self.assertRaises(StoreError) as caught:
                    transaction.commit_transition("artifact_registered", unsafe)
                self.assertEqual(caught.exception.code, "artifact.authority_invalid")
                self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)

                stale_nodes = {
                    node_id: stale_runtime(runtime)
                    for node_id, runtime in complete.nodes.items()
                }
                legitimate = replace(
                    complete,
                    nodes=MappingProxyType(stale_nodes),
                    edges=MappingProxyType({
                        "producer-consumer": EdgeRuntime(EdgeStatus.WAITING),
                    }),
                    artifacts=MappingProxyType({
                        "first": external,
                        "second": replace(complete.artifacts["second"], state="stale"),
                    }),
                )
                incomplete_consumer = replace(
                    legitimate,
                    nodes=MappingProxyType({
                        **legitimate.nodes,
                        "consumer": complete.nodes["consumer"],
                    }),
                )
                incomplete_sibling = replace(
                    legitimate,
                    artifacts=MappingProxyType({
                        **legitimate.artifacts,
                        "second": complete.artifacts["second"],
                    }),
                )
                missing_prior_receipt = replace(
                    complete,
                    artifacts=MappingProxyType({"second": complete.artifacts["second"]}),
                )
                for name, event_type, candidate in (
                    ("dependent", "artifact_registered", incomplete_consumer),
                    ("sibling", "artifact_registered", incomplete_sibling),
                    ("explicit_event", "fact_recorded", legitimate),
                    ("deletion", "artifact_registered", missing_prior_receipt),
                ):
                    with self.subTest(name=name):
                        before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
                        with self.assertRaises(StoreError) as caught:
                            transaction.commit_transition(event_type, candidate)
                        expected_code = "events.invalid_evidence" if name == "explicit_event" else "artifact.authority_invalid"
                        self.assertEqual(caught.exception.code, expected_code)
                        self.assertEqual(
                            (store.paths.events.read_bytes(), store.paths.state.read_bytes()), before
                        )
                transaction.commit_transition("artifact_registered", legitimate)
            self.assertEqual(WorkflowStore(root).recover().status, "clean")

    def test_rehashed_produced_to_external_flip_blocks_before_tail_rewrite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("old.txt").write_bytes(b"old")
            root.joinpath("new.txt").write_bytes(b"new")
            plan = replace(task_plan(), external_inputs=("draft",))
            store = WorkflowStore(root)
            store.start_run(plan, "run-flip-suffix")
            old = ArtifactRuntime("draft", "old.txt", sha256_bytes(b"old"), "verified", "external", 0)
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                registered = replace(state, artifacts=MappingProxyType({"draft": old}))
                transaction.commit_transition("artifact_registered", registered)
                running = claim_transition(plan, registered, "produce", "token")
                commit_claim(transaction, running)
                completed = result_transition(plan, running, {
                    "node_id": "produce", "attempt": 1, "status": "succeeded",
                    "outcome": "succeeded", "outputs": {"draft": "new.txt"},
                    "artifacts": [ArtifactRuntime(
                        "draft", "new.txt", sha256_bytes(b"new"), "verified", "produce", 1
                    )],
                })
                commit_result(transaction, completed)
            forged = replace(completed, artifacts=MappingProxyType({"draft": old}))
            append_rehashed_state_event(store, workflow_store._state_data(forged))
            with store.paths.events.open("ab") as handle:
                handle.write(b'{"event_seq":6,"partial"')
            before = (store.paths.events.read_bytes(), store.paths.state.read_bytes())
            recovered = WorkflowStore(root).recover()
            self.assertEqual((recovered.status, recovered.code), ("blocked", "artifact.authority_invalid"))
            self.assertEqual((store.paths.events.read_bytes(), store.paths.state.read_bytes()), before)
            self.assertFalse(store.paths.recovery.exists())

    def test_recovery_blocks_uncertain_running_work(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-running")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                running = claim_transition(plan, state, "produce", "interrupted-token")
                commit_claim(transaction, running)
            result = store.recover()
            self.assertEqual((result.status, result.code), ("blocked", "recovery.running_work_uncertain"))
            self.assertEqual(result.state.nodes["produce"].status, NodeStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
