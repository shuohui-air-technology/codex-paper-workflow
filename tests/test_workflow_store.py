import copy
import concurrent.futures
import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

from scripts.workflow_engine.catalog import SkillIdentity
from scripts.workflow_engine.compiler import CompiledEdge, CompiledNode, CompiledPlan
from scripts.workflow_engine.fs import PathSafetyError, atomic_write_json, resolve_project_path
from scripts.workflow_engine.scheduler import (
    ArtifactRuntime,
    NodeStatus,
    initial_run,
    stabilize_control_nodes,
)
from scripts.workflow_engine.schema import document_sha256, parse_workflow
from scripts.workflow_engine.store import StoreError, WorkflowEvent, WorkflowStore


ZERO_HASH = "0" * 64


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


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
    return CompiledNode(
        id=node_id,
        type=node_type,
        entry=entry,
        skill=skill,
        validator=None,
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
            plan = task_plan()
            with self.assertRaises(StoreError) as missing:
                store.activate_custom(
                    plan,
                    high_risk_warning_codes=("risk.a", "risk.b"),
                    acknowledged_warning_codes=("risk.a",),
                )
            self.assertEqual(missing.exception.code, "activation.acknowledgement_mismatch")

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
            self.assertFalse(restarted.acknowledges(task_plan(semantic_revision=2), ("risk.a", "risk.b")))
            audit_selection = store.read_audit_events()[-1].payload["selection"]
            self.assertEqual(audit_selection["semantic_sha256"], selection.semantic_sha256)
            self.assertEqual(
                tuple(audit_selection["acknowledged_warning_codes"]),
                selection.acknowledged_warning_codes,
            )

            official = WorkflowStore(root).deactivate_custom()
            self.assertEqual(official.mode, "official")
            self.assertEqual(official.selection_revision, selection.selection_revision + 1)

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

    def test_recovery_blocks_uncertain_running_work(self):
        with TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary))
            plan = task_plan()
            store.start_run(plan, "run-running")
            with store.locked_run() as transaction:
                _, state = transaction.load_active_run()
                runtime = replace(state.nodes["produce"], status=NodeStatus.RUNNING, attempt=1)
                running = replace(state, nodes=MappingProxyType({"produce": runtime}))
                transaction.commit_transition("node_claimed", running)
            result = store.recover()
            self.assertEqual((result.status, result.code), ("blocked", "recovery.running_work_uncertain"))
            self.assertEqual(result.state.nodes["produce"].status, NodeStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
