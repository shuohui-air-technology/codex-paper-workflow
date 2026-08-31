import copy
from dataclasses import replace
import hashlib
import json
import unittest
from pathlib import Path

from scripts.workflow_engine.catalog import CatalogResult, SkillIdentity, load_validator_registry
from scripts.workflow_engine.conditions import ConditionFacts, evaluate_condition
from scripts.workflow_engine.compiler import compile_workflow
from scripts.workflow_engine.schema import WorkflowError, document_sha256, parse_workflow


ROOT = Path(__file__).resolve().parents[1]
PROJECTION_SHA256 = "f196ac418405f2b3b3bd1fadbb25dc7915344d10b6316238565cc95cd29ae318"


def identity(skill_id, *, tree_suffix=""):
    return SkillIdentity(
        catalog_id=skill_id,
        root=ROOT / "test-skills",
        relative_path=skill_id,
        skill_sha256=hashlib.sha256((skill_id + "/SKILL.md").encode()).hexdigest(),
        tree_sha256="sha256:" + hashlib.sha256((skill_id + tree_suffix).encode()).hexdigest(),
        locked=True,
    )


class WorkflowCompilerTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads(
            (ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text()
        )
        skill_ids = {
            node["skill_ref"]
            for node in self.value["nodes"]
            if node["skill_ref"] is not None
        }
        skills = {skill_id: identity(skill_id) for skill_id in skill_ids}
        self.catalog = CatalogResult(skills=skills, errors=(), warnings=())
        self.validators = load_validator_registry(
            ROOT / "references/workflows/validator-registry.v1.json", ROOT
        )
        self.projection = json.loads(
            (ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text()
        )

    def compile(self, value=None):
        return compile_workflow(
            parse_workflow(self.value if value is None else value),
            self.catalog,
            self.validators,
            self.projection,
        )

    def derive_from_projection(self, value):
        value["derived_from"] = {
            "projection_id": self.projection["projection_id"],
            "projection_sha256": PROJECTION_SHA256,
        }

    def issue_codes(self, result):
        return {issue.code for issue in result.errors}

    def projected_literature_chain(self):
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        projected = {
            node["id"]: node
            for node in self.projection["nodes"]
            if node["id"] in {"directions", "literature", "topic"}
        }
        bindings = {
            "directions": "clarify-research-idea",
            "literature": "research-hub",
            "topic": "gap-to-topic",
        }
        value["external_inputs"] = sorted(
            {artifact for node in projected.values() for artifact in node["inputs"]}
        )
        value["nodes"] = [
            {
                "id": node_id, "type": "task", "display_name": node_id,
                "entry": node_id == "directions", "enabled": True,
                "skill_ref": bindings[node_id], "validator_ref": None,
                "origin_projection_node_id": node_id,
                "inputs": list(projected[node_id]["inputs"]),
                "outputs": list(projected[node_id]["outputs"]),
                "outcomes": ["succeeded"],
                "write_scopes": list(projected[node_id]["outputs"]) + [
                    scope for scope in projected[node_id]["write_scopes"]
                    if scope == "canonical_manuscript"
                ],
                "failure_policy": "block", "condition_cases": [], "join_mode": "all_active",
            }
            for node_id in ("directions", "literature", "topic")
        ]
        value["edges"] = [
            {"id": "directions-to-literature", "source": "directions",
             "target": "literature", "trigger": "succeeded", "output_map": {}},
            {"id": "literature-to-topic", "source": "literature",
             "target": "topic", "trigger": "succeeded", "output_map": {}},
        ]
        catalog = CatalogResult(
            {skill_id: identity(skill_id) for skill_id in bindings.values()}, (), ()
        )
        return value, catalog

    def projected_final_audit_chain(self):
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        projected = {
            node["id"]: node
            for node in self.projection["nodes"]
            if node["id"] in {
                "prose-naturalization", "final-editorial-audit", "finalize"
            }
        }
        value["external_inputs"] = sorted(
            {artifact for node in projected.values() for artifact in node["inputs"]}
        )
        value["nodes"] = []
        for node_id in (
            "prose-naturalization", "final-editorial-audit", "finalize"
        ):
            is_validator = node_id == "final-editorial-audit"
            outputs = list(projected[node_id]["outputs"])
            value["nodes"].append({
                "id": node_id,
                "type": "validator" if is_validator else "task",
                "display_name": node_id,
                "entry": node_id == "prose-naturalization",
                "enabled": True,
                "skill_ref": None if is_validator else (
                    "humanizer" if node_id == "prose-naturalization"
                    else "paper-memory-builder"
                ),
                "validator_ref": "final-edit-receipt" if is_validator else None,
                "origin_projection_node_id": node_id,
                "inputs": list(projected[node_id]["inputs"]),
                "outputs": outputs,
                "outcomes": ["pass", "fail", "blocked"] if is_validator else ["succeeded"],
                "write_scopes": outputs + [
                    scope for scope in projected[node_id]["write_scopes"]
                    if scope == "canonical_manuscript" and scope not in outputs
                ],
                "failure_policy": "block",
                "condition_cases": [],
                "join_mode": "all_active",
            })
        value["edges"] = [
            {
                "id": "prose-naturalization-to-final-editorial-audit",
                "source": "prose-naturalization", "target": "final-editorial-audit",
                "trigger": "succeeded", "output_map": {},
            },
            {
                "id": "final-editorial-audit-to-finalize",
                "source": "final-editorial-audit", "target": "finalize",
                "trigger": "pass", "output_map": {},
            },
        ]
        catalog = CatalogResult(
            {
                "humanizer": identity("humanizer"),
                "paper-memory-builder": identity("paper-memory-builder"),
            },
            (),
            (),
        )
        return value, catalog

    def test_cycle_is_activation_blocking(self):
        """Catches Kahn compilation returning a partial plan after a back edge is added."""
        value = copy.deepcopy(self.value)
        value["edges"].append({
            "id": "back-edge",
            "source": "join",
            "target": "condition",
            "trigger": "succeeded",
            "output_map": {},
        })
        result = self.compile(value)
        self.assertIsNone(result.plan)
        self.assertIn("graph.cycle", {issue.code for issue in result.errors})

    def test_removed_integrity_control_warns_but_compiles(self):
        """Catches risk loss being silently ignored or incorrectly promoted to an error."""
        result = self.compile()
        self.assertIsNotNone(result.plan)
        self.assertIn(
            "risk.control_removed.integrity", {issue.code for issue in result.warnings}
        )

    def test_unbound_task_is_activation_blocking(self):
        """Catches saveable incomplete task drafts crossing the activation boundary."""
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["type"] == "task")[
            "skill_ref"
        ] = None
        result = self.compile(value)
        self.assertIsNone(result.plan)
        self.assertIn("catalog.skill_required", {issue.code for issue in result.errors})

    def test_condition_evaluator_uses_first_matching_case_and_default(self):
        """Catches evaluating every case or returning a false named branch."""
        facts = ConditionFacts(
            node_statuses={},
            node_outcomes={},
            decisions={"route": "review"},
            artifact_states={},
            project_booleans={"ready": True},
        )
        cases = (
            {"outcome": "ready", "when": {"op": "fact_is", "name": "ready", "value": True}},
            {"outcome": "review", "when": {"op": "decision_is", "name": "route", "value": "review"}},
        )
        self.assertEqual(evaluate_condition(cases, facts), "ready")
        self.assertEqual(
            evaluate_condition(
                ({"outcome": "blocked", "when": {"op": "fact_is", "name": "ready", "value": False}},),
                facts,
            ),
            "default",
        )

    def test_condition_evaluator_supports_only_composed_recorded_facts(self):
        """Catches status, outcome, artifact, any, all, or not dispatching to wrong operands."""
        facts = ConditionFacts(
            node_statuses={"review": "succeeded"},
            node_outcomes={"review": "pass"},
            decisions={"route": "revise"},
            artifact_states={"manuscript": "verified"},
            project_booleans={"ready": False},
        )
        cases = ({
            "outcome": "revise",
            "when": {
                "op": "all",
                "args": [
                    {"op": "status_is", "node": "review", "value": "succeeded"},
                    {"op": "outcome_is", "node": "review", "value": "pass"},
                    {"op": "artifact_state_is", "artifact": "manuscript", "value": "verified"},
                    {"op": "any", "args": [
                        {"op": "fact_is", "name": "ready", "value": True},
                        {"op": "decision_is", "name": "route", "value": "revise"},
                    ]},
                    {"op": "not", "arg": {"op": "fact_is", "name": "ready", "value": True}},
                ],
            },
        },)
        self.assertEqual(evaluate_condition(cases, facts), "revise")

    def test_decision_equality_separates_json_booleans_from_numbers(self):
        """Catches Python equality selecting boolean branches for numeric decisions and vice versa."""
        for recorded, branch_value in (
            (1, True),
            (True, 1),
            (0, False),
            (False, 0),
        ):
            with self.subTest(recorded=recorded, branch_value=branch_value):
                facts = ConditionFacts({}, {}, {"choice": recorded}, {}, {})
                cases = ({
                    "outcome": "matched",
                    "when": {
                        "op": "decision_is", "name": "choice", "value": branch_value,
                    },
                },)
                self.assertEqual(evaluate_condition(cases, facts), "default")

        null_case = ({
            "outcome": "matched",
            "when": {"op": "decision_is", "name": "choice", "value": None},
        },)
        self.assertEqual(
            evaluate_condition(null_case, ConditionFacts({}, {}, {}, {}, {})),
            "default",
        )
        self.assertEqual(
            evaluate_condition(null_case, ConditionFacts({}, {}, {"choice": None}, {}, {})),
            "matched",
        )

    def test_condition_evaluator_rejects_unknown_keys_and_wrong_types(self):
        """Catches executable-looking or truthy malformed predicates escaping the AST boundary."""
        facts = ConditionFacts({}, {}, {}, {}, {})
        invalid = (
            {"op": "fact_is", "name": "ready", "value": True, "code": "run()"},
            {"op": "fact_is", "name": "ready", "value": 1},
            {"op": "all", "args": []},
            {"op": "any", "args": []},
            {"op": "not", "arg": {"op": "all", "args": "not-a-list"}},
            {"op": "status_is", "node": "review", "value": "complete"},
        )
        for expression in invalid:
            with self.subTest(expression=expression), self.assertRaises(WorkflowError) as caught:
                evaluate_condition(({"outcome": "chosen", "when": expression},), facts)
            self.assertEqual(caught.exception.code, "condition.invalid_ast")

    def test_compiler_rejects_condition_reference_outside_predecessors(self):
        """Catches conditions reading arbitrary node state rather than predecessor facts."""
        value = copy.deepcopy(self.value)
        condition = next(node for node in value["nodes"] if node["id"] == "condition")
        condition["condition_cases"][0]["when"] = {
            "op": "status_is", "node": "literature", "value": "succeeded"
        }
        result = self.compile(value)
        self.assertIn("condition.invalid_reference", self.issue_codes(result))

    def test_compiler_rejects_unsupported_condition_operation(self):
        """Catches accepting arbitrary expression operation names during activation."""
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["id"] == "condition")[
            "condition_cases"
        ][0]["when"] = {"op": "python", "code": "True"}
        result = self.compile(value)
        self.assertIn("condition.unsupported_op", self.issue_codes(result))

    def test_condition_requires_exactly_one_default_edge(self):
        """Catches ambiguous or absent fallback routing."""
        missing = copy.deepcopy(self.value)
        missing["edges"] = [edge for edge in missing["edges"] if edge["trigger"] != "default"]
        self.assertIn("condition.default_edge", self.issue_codes(self.compile(missing)))

        duplicate = copy.deepcopy(self.value)
        duplicate["edges"].append({
            "id": "second-default", "source": "condition", "target": "literature",
            "trigger": "default", "output_map": {},
        })
        self.assertIn("condition.default_edge", self.issue_codes(self.compile(duplicate)))

    def test_disabled_nodes_and_incident_edges_are_removed_before_validation(self):
        """Catches an intentionally disabled draft node blocking activation through its binding or edge."""
        value = copy.deepcopy(self.value)
        literature = next(node for node in value["nodes"] if node["id"] == "literature")
        literature["enabled"] = False
        literature["skill_ref"] = None
        edge = next(edge for edge in value["edges"] if edge["source"] == "literature")
        edge["target"] = "missing-node"
        result = self.compile(value)
        self.assertIsNotNone(result.plan)
        self.assertNotIn("literature", result.plan.nodes)
        self.assertNotIn(edge["id"], result.plan.edges)

    def test_disabling_a_predecessor_does_not_invent_a_bypass_edge(self):
        """Catches disabled-node pruning silently reconnecting its downstream task."""
        value = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())
        value["nodes"][0]["enabled"] = False
        skills = {node["skill_ref"]: identity(node["skill_ref"]) for node in value["nodes"]}
        result = compile_workflow(
            parse_workflow(value), CatalogResult(skills, (), ()), self.validators, self.projection
        )
        self.assertIn("graph.entry_required", self.issue_codes(result))

    def test_every_enabled_zero_incoming_node_must_be_an_explicit_entry(self):
        """Catches silently treating a disconnected card as runnable."""
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["id"] == "condition")["entry"] = False
        result = self.compile(value)
        self.assertIn("graph.entry_required", self.issue_codes(result))

    def test_unknown_edge_endpoint_and_illegal_source_trigger_block_activation(self):
        """Catches scheduler guesswork for unresolved endpoints or unsupported task outcomes."""
        unknown = copy.deepcopy(self.value)
        unknown["edges"][0]["target"] = "ghost"
        self.assertIn("graph.unknown_target", self.issue_codes(self.compile(unknown)))

        trigger = copy.deepcopy(self.value)
        next(edge for edge in trigger["edges"] if edge["source"] == "literature")["trigger"] = "fail"
        self.assertIn("graph.invalid_trigger", self.issue_codes(self.compile(trigger)))

    def test_task_edges_allow_only_succeeded_even_if_an_outcome_is_declared(self):
        """Catches treating task scientific labels as scheduler completion signals."""
        value = copy.deepcopy(self.value)
        literature = next(node for node in value["nodes"] if node["id"] == "literature")
        literature["outcomes"].append("reviewed")
        next(edge for edge in value["edges"] if edge["source"] == "literature")[
            "trigger"
        ] = "reviewed"
        result = self.compile(value)
        self.assertIn("graph.invalid_trigger", self.issue_codes(result))

    def test_edge_with_two_unknown_endpoints_is_not_silently_dropped(self):
        """Catches pruning malformed edges merely because neither endpoint is enabled."""
        value = copy.deepcopy(self.value)
        value["edges"].append({
            "id": "ghost-edge", "source": "ghost-a", "target": "ghost-b",
            "trigger": "succeeded", "output_map": {},
        })
        result = self.compile(value)
        self.assertIn("graph.unknown_source", self.issue_codes(result))
        self.assertIn("graph.unknown_target", self.issue_codes(result))

    def test_all_disabled_workflow_cannot_compile_an_empty_plan(self):
        """Catches activation succeeding without an entry, runnable node, or terminal."""
        value = copy.deepcopy(self.value)
        for node in value["nodes"]:
            node["enabled"] = False
        result = self.compile(value)
        self.assertIsNone(result.plan)
        self.assertIn("graph.no_enabled_nodes", self.issue_codes(result))

    def test_inputs_require_external_or_incoming_artifact_producers(self):
        """Catches a matching artifact elsewhere in the graph satisfying an unrelated node input."""
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["id"] == "draft-audit")["inputs"] = ["sources"]
        result = self.compile(value)
        self.assertIn("artifact.input_unbound", self.issue_codes(result))

    def test_input_rejects_producers_from_two_incoming_edges(self):
        """Catches first-match selection hiding cross-edge artifact ambiguity."""
        value = copy.deepcopy(self.value)
        value["external_inputs"] = []
        value["nodes"] = [
            {
                "id": "source-a", "type": "task", "display_name": "Source A",
                "entry": True, "enabled": True, "skill_ref": "research-hub",
                "validator_ref": None, "origin_projection_node_id": None,
                "inputs": [], "outputs": ["shared"], "outcomes": ["succeeded"],
                "write_scopes": [], "failure_policy": "block", "condition_cases": [],
                "join_mode": "all_active",
            },
            {
                "id": "source-b", "type": "task", "display_name": "Source B",
                "entry": True, "enabled": True, "skill_ref": "paper-memory-builder",
                "validator_ref": None, "origin_projection_node_id": None,
                "inputs": [], "outputs": ["shared"], "outcomes": ["succeeded"],
                "write_scopes": [], "failure_policy": "block", "condition_cases": [],
                "join_mode": "all_active",
            },
            {
                "id": "sink", "type": "task", "display_name": "Sink",
                "entry": False, "enabled": True, "skill_ref": "research-hub",
                "validator_ref": None, "origin_projection_node_id": None,
                "inputs": ["shared"], "outputs": [], "outcomes": ["succeeded"],
                "write_scopes": [], "failure_policy": "block", "condition_cases": [],
                "join_mode": "all_active",
            },
        ]
        value["edges"] = [
            {"id": "a-to-sink", "source": "source-a", "target": "sink",
             "trigger": "succeeded", "output_map": {}},
            {"id": "b-to-sink", "source": "source-b", "target": "sink",
             "trigger": "succeeded", "output_map": {}},
        ]
        result = self.compile(value)
        self.assertIsNone(result.plan)
        self.assertIn("artifact.input_ambiguous", self.issue_codes(result))

    def test_input_rejects_implicit_and_explicit_aliases_on_one_edge(self):
        """Catches two source outputs colliding on one target input after mapping."""
        value = copy.deepcopy(self.value)
        value["external_inputs"] = []
        value["nodes"] = [
            {
                "id": "source", "type": "task", "display_name": "Source",
                "entry": True, "enabled": True, "skill_ref": "research-hub",
                "validator_ref": None, "origin_projection_node_id": None,
                "inputs": [], "outputs": ["left", "right"], "outcomes": ["succeeded"],
                "write_scopes": [], "failure_policy": "block", "condition_cases": [],
                "join_mode": "all_active",
            },
            {
                "id": "sink", "type": "task", "display_name": "Sink",
                "entry": False, "enabled": True, "skill_ref": "paper-memory-builder",
                "validator_ref": None, "origin_projection_node_id": None,
                "inputs": ["right"], "outputs": [], "outcomes": ["succeeded"],
                "write_scopes": [], "failure_policy": "block", "condition_cases": [],
                "join_mode": "all_active",
            },
        ]
        value["edges"] = [{
            "id": "source-to-sink", "source": "source", "target": "sink",
            "trigger": "succeeded", "output_map": {"left": "right"},
        }]
        result = self.compile(value)
        self.assertIsNone(result.plan)
        self.assertIn("artifact.input_ambiguous", self.issue_codes(result))

    def test_any_success_join_requires_declared_winner_output_maps(self):
        """Catches a race join whose winning branch cannot expose deterministic output."""
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["id"] == "join")["join_mode"] = "any_success"
        next(edge for edge in value["edges"] if edge["source"] == "literature")[
            "output_map"
        ] = {}
        result = self.compile(value)
        self.assertIn("join.output_map_required", self.issue_codes(result))

    def test_output_maps_must_use_declared_artifacts(self):
        """Catches misspelled producer outputs and target inputs entering the compiled plan."""
        bad_source = copy.deepcopy(self.value)
        next(edge for edge in bad_source["edges"] if edge["source"] == "literature")[
            "output_map"
        ] = {"unknown": "evidence_bundle"}
        self.assertIn("artifact.output_map_source", self.issue_codes(self.compile(bad_source)))

        bad_target = copy.deepcopy(self.value)
        next(edge for edge in bad_target["edges"] if edge["source"] == "literature")[
            "output_map"
        ] = {"sources": "unknown"}
        self.assertIn("artifact.output_map_target", self.issue_codes(self.compile(bad_target)))

    def test_protected_canonical_scope_cannot_be_renamed_by_output_map(self):
        """Catches laundering a protected write scope through a logical artifact alias."""
        value = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())
        value["nodes"][0]["outputs"] = ["canonical_manuscript"]
        value["nodes"][0]["write_scopes"] = ["canonical_manuscript"]
        value["nodes"][1]["inputs"] = ["renamed_manuscript"]
        value["edges"][0]["output_map"] = {"canonical_manuscript": "renamed_manuscript"}
        skills = {node["skill_ref"]: identity(node["skill_ref"]) for node in value["nodes"]}
        result = compile_workflow(
            parse_workflow(value), CatalogResult(skills, (), ()), self.validators, self.projection
        )
        self.assertIn("artifact.protected_scope_renamed", self.issue_codes(result))

    def test_unordered_nodes_with_a_shared_write_scope_conflict(self):
        """Catches parallel conflicts hidden by adjacent positions in one topological ordering."""
        value = copy.deepcopy(self.value)
        for node_id in ("literature", "draft-audit"):
            next(node for node in value["nodes"] if node["id"] == node_id)["write_scopes"] = ["shared"]
        result = self.compile(value)
        self.assertIn("graph.parallel_write_conflict", self.issue_codes(result))

    def test_ordered_nodes_may_share_a_write_scope(self):
        """Catches treating transitive topological distance as parallelism."""
        value = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())
        last = value["nodes"][1]
        middle = copy.deepcopy(last)
        middle.update({
            "id": "middle", "display_name": "Middle", "skill_ref": "research-design-helper",
            "inputs": ["research_idea_brief"], "outputs": ["design_brief"],
            "write_scopes": ["middle_scope"],
        })
        last["inputs"] = ["design_brief"]
        value["nodes"].insert(1, middle)
        value["nodes"][0]["write_scopes"] = ["canonical_manuscript"]
        last["write_scopes"] = ["canonical_manuscript"]
        value["edges"] = [
            {"id": "directions-to-middle", "source": "directions", "target": "middle",
             "trigger": "succeeded", "output_map": {"research_idea_brief": "research_idea_brief"}},
            {"id": "middle-to-design", "source": "middle", "target": "design",
             "trigger": "succeeded", "output_map": {"design_brief": "design_brief"}},
        ]
        skills = {node["skill_ref"]: identity(node["skill_ref"]) for node in value["nodes"]}
        result = compile_workflow(
            parse_workflow(value), CatalogResult(skills, (), ()), self.validators, self.projection
        )
        self.assertIsNotNone(result.plan)

    def test_topological_order_and_edge_indexes_are_deterministic(self):
        """Catches source JSON order leaking into dispatch or adjacency order."""
        first = self.compile()
        reordered = copy.deepcopy(self.value)
        reordered["nodes"].reverse()
        reordered["edges"].reverse()
        second = self.compile(reordered)
        self.assertEqual(
            first.plan.topological_order,
            ("condition", "draft-audit", "literature", "join"),
        )
        self.assertEqual(first.plan.topological_order, second.plan.topological_order)
        self.assertEqual(dict(first.plan.incoming), dict(second.plan.incoming))
        self.assertEqual(dict(first.plan.outgoing), dict(second.plan.outgoing))
        self.assertEqual(tuple(first.plan.incoming), tuple(second.plan.incoming))
        self.assertEqual(tuple(first.plan.outgoing), tuple(second.plan.outgoing))

    def test_projection_id_hash_and_origin_must_resolve(self):
        """Catches forged projection provenance being accepted as reviewed control coverage."""
        bad_hash = copy.deepcopy(self.value)
        self.derive_from_projection(bad_hash)
        bad_hash["derived_from"]["projection_sha256"] = "0" * 64
        self.assertIn("projection.hash_mismatch", self.issue_codes(self.compile(bad_hash)))

        bad_id = copy.deepcopy(self.value)
        self.derive_from_projection(bad_id)
        bad_id["derived_from"]["projection_id"] = "other-v1.0"
        self.assertIn("projection.id_mismatch", self.issue_codes(self.compile(bad_id)))

        unknown = copy.deepcopy(self.value)
        self.derive_from_projection(unknown)
        unknown["nodes"][0]["origin_projection_node_id"] = "unknown-origin"
        self.assertIn("projection.unknown_node", self.issue_codes(self.compile(unknown)))

    def test_duplicate_enabled_projection_origin_is_activation_blocking(self):
        """Catches two custom nodes claiming one reviewed provenance identity."""
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        value["nodes"][1]["origin_projection_node_id"] = "literature"
        value["nodes"][2]["origin_projection_node_id"] = "literature"
        result = self.compile(value)
        self.assertIn("projection.duplicate_origin", self.issue_codes(result))

    def test_disabled_clone_cannot_duplicate_projection_provenance(self):
        """Catches retaining ambiguous provenance on a disabled duplicate card."""
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        value["nodes"][1]["origin_projection_node_id"] = "literature"
        value["nodes"][2]["origin_projection_node_id"] = "literature"
        value["nodes"][2]["enabled"] = False
        result = self.compile(value)
        self.assertIn("projection.duplicate_origin", self.issue_codes(result))

    def test_changed_cloned_control_binding_emits_replaced_warning(self):
        """Catches a provenance label preserving coverage after its reviewed Skill binding changed."""
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        literature = next(node for node in value["nodes"] if node["id"] == "literature")
        literature["origin_projection_node_id"] = "literature"
        literature["skill_ref"] = "paper-memory-builder"
        result = self.compile(value)
        self.assertIn(
            "risk.control_replaced.citation", {issue.code for issue in result.warnings}
        )

    def test_disabled_cloned_control_emits_removed_not_replaced_warning(self):
        """Catches disabled provenance continuing to count as a changed active control."""
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        literature = next(node for node in value["nodes"] if node["id"] == "literature")
        literature["origin_projection_node_id"] = "literature"
        literature["enabled"] = False
        result = self.compile(value)
        codes = {issue.code for issue in result.warnings}
        self.assertIn("risk.control_removed.citation", codes)
        self.assertNotIn("risk.control_replaced.citation", codes)

    def test_arbitrary_task_cannot_self_assert_projected_control_coverage(self):
        """Catches a matching artifact shape without verified provenance suppressing a control warning."""
        result = self.compile()
        self.assertIn(
            "risk.control_removed.citation", {issue.code for issue in result.warnings}
        )

    def test_registered_validator_control_tags_supply_server_derived_coverage(self):
        """Catches ignoring fixed registry control tags while deriving warnings."""
        value = copy.deepcopy(self.value)
        value["nodes"] = [{
            "id": "figure-check", "type": "validator", "display_name": "Figure check",
            "entry": True, "enabled": True, "skill_ref": None,
            "validator_ref": "figure-contract", "origin_projection_node_id": None,
            "inputs": [], "outputs": [], "outcomes": ["pass", "fail", "blocked"],
            "write_scopes": [], "failure_policy": "block", "condition_cases": [],
            "join_mode": "all_active",
        }]
        value["edges"] = []
        result = self.compile(value)
        self.assertIsNotNone(result.plan)
        self.assertNotIn("risk.control_removed.figure", {issue.code for issue in result.warnings})

    def test_compatible_projected_clone_retains_server_derived_coverage(self):
        """Catches warning on a clone whose provenance, binding, and artifact shape still match."""
        value, catalog = self.projected_literature_chain()
        result = compile_workflow(
            parse_workflow(value), catalog, self.validators, self.projection
        )
        self.assertIsNotNone(result.plan)
        self.assertNotIn("risk.control_removed.citation", {i.code for i in result.warnings})
        self.assertNotIn("risk.control_replaced.citation", {i.code for i in result.warnings})

    def test_duplicate_projected_edge_is_material_rewiring(self):
        """Catches duplicate same-endpoint edges masquerading as projected adjacency."""
        value, catalog = self.projected_literature_chain()
        value["edges"].append({
            "id": "directions-to-literature-again", "source": "directions",
            "target": "literature", "trigger": "succeeded", "output_map": {},
        })
        result = compile_workflow(
            parse_workflow(value), catalog, self.validators, self.projection
        )
        self.assertIsNotNone(result.plan)
        self.assertIn("risk.control_replaced.citation", {i.code for i in result.warnings})

    def test_projected_validator_fail_edge_replaces_control_coverage(self):
        """Catches delivery following a failed gate while retaining final-audit coverage."""
        value, catalog = self.projected_final_audit_chain()
        safe = compile_workflow(
            parse_workflow(value), catalog, self.validators, self.projection
        )
        self.assertIsNotNone(safe.plan)
        self.assertNotIn(
            "risk.control_replaced.final_audit", {i.code for i in safe.warnings}
        )

        next(
            edge for edge in value["edges"]
            if edge["id"] == "final-editorial-audit-to-finalize"
        )["trigger"] = "fail"
        unsafe = compile_workflow(
            parse_workflow(value), catalog, self.validators, self.projection
        )
        self.assertIsNotNone(unsafe.plan)
        self.assertIn(
            "risk.control_replaced.final_audit", {i.code for i in unsafe.warnings}
        )

    def test_projected_edge_mapping_mismatch_replaces_control_coverage(self):
        """Catches changed adjacent artifact routing retaining final-audit coverage."""
        value, catalog = self.projected_final_audit_chain()
        next(
            edge for edge in value["edges"]
            if edge["id"] == "prose-naturalization-to-final-editorial-audit"
        )["output_map"] = {"protected_manifest": "final_edit_receipt"}
        result = compile_workflow(
            parse_workflow(value), catalog, self.validators, self.projection
        )
        self.assertIsNotNone(result.plan)
        self.assertIn(
            "risk.control_replaced.final_audit", {i.code for i in result.warnings}
        )

    def test_missing_projected_adjacent_edges_replaces_control_coverage(self):
        """Catches isolated provenance suppressing risk after projected neighbors are removed."""
        projected = next(node for node in self.projection["nodes"] if node["id"] == "literature")
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        value["external_inputs"] = list(projected["inputs"])
        value["nodes"] = [{
            "id": "literature", "type": "task", "display_name": "Literature",
            "entry": True, "enabled": True, "skill_ref": "research-hub",
            "validator_ref": None, "origin_projection_node_id": "literature",
            "inputs": list(projected["inputs"]), "outputs": list(projected["outputs"]),
            "outcomes": ["succeeded"], "write_scopes": list(projected["write_scopes"]),
            "failure_policy": "block", "condition_cases": [], "join_mode": "all_active",
        }]
        value["edges"] = []
        result = self.compile(value)
        self.assertIsNotNone(result.plan)
        self.assertIn("risk.control_replaced.citation", {i.code for i in result.warnings})

    def test_material_rewiring_replaces_projected_control_coverage(self):
        """Catches preserving provenance coverage after an unprojected adjacent edge is added."""
        projected = next(node for node in self.projection["nodes"] if node["id"] == "literature")
        value = copy.deepcopy(self.value)
        self.derive_from_projection(value)
        value["external_inputs"] = list(projected["inputs"])
        value["nodes"] = [
            {
                "id": "literature", "type": "task", "display_name": "Literature",
                "entry": True, "enabled": True, "skill_ref": "research-hub",
                "validator_ref": None, "origin_projection_node_id": "literature",
                "inputs": list(projected["inputs"]), "outputs": list(projected["outputs"]),
                "outcomes": ["succeeded"], "write_scopes": list(projected["write_scopes"]),
                "failure_policy": "block", "condition_cases": [], "join_mode": "all_active",
            },
            {
                "id": "sink", "type": "task", "display_name": "Sink", "entry": False,
                "enabled": True, "skill_ref": "paper-memory-builder", "validator_ref": None,
                "origin_projection_node_id": None, "inputs": ["sources"], "outputs": [],
                "outcomes": ["succeeded"], "write_scopes": [], "failure_policy": "block",
                "condition_cases": [], "join_mode": "all_active",
            },
        ]
        value["edges"] = [{
            "id": "literature-to-sink", "source": "literature", "target": "sink",
            "trigger": "succeeded", "output_map": {"sources": "sources"},
        }]
        result = self.compile(value)
        self.assertIsNotNone(result.plan)
        self.assertIn("risk.control_replaced.citation", {i.code for i in result.warnings})

    def test_validator_binding_and_registry_outcomes_are_required(self):
        """Catches incomplete or identity-incompatible validator drafts activating."""
        value = copy.deepcopy(self.value)
        value["nodes"] = [{
            "id": "check", "type": "validator", "display_name": "Check", "entry": True,
            "enabled": True, "skill_ref": None, "validator_ref": None,
            "origin_projection_node_id": None, "inputs": [], "outputs": [],
            "outcomes": ["pass", "fail", "blocked"], "write_scopes": [],
            "failure_policy": "block", "condition_cases": [], "join_mode": "all_active",
        }]
        value["edges"] = []
        self.assertIn("validator.identity_required", self.issue_codes(self.compile(value)))

        value["nodes"][0]["validator_ref"] = "missing-validator"
        self.assertIn("validator.identity_not_found", self.issue_codes(self.compile(value)))

        value["nodes"][0]["validator_ref"] = "paper-section"
        mismatched = dict(self.validators)
        mismatched["paper-section"] = replace(
            mismatched["paper-section"], outcomes=("pass", "blocked")
        )
        result = compile_workflow(
            parse_workflow(value), self.catalog, mismatched, self.projection
        )
        self.assertIn("validator.outcomes_mismatch", self.issue_codes(result))

        wrong_id = dict(self.validators)
        wrong_id["paper-section"] = replace(
            self.validators["paper-section"], validator_id="different-validator"
        )
        result = compile_workflow(
            parse_workflow(value), self.catalog, wrong_id, self.projection
        )
        self.assertIn("validator.identity_mismatch", self.issue_codes(result))

    def test_catalog_errors_block_compilation_and_warnings_are_preserved(self):
        """Catches activation discarding discovery ambiguity or unlocked-Skill warnings."""
        from scripts.workflow_engine.schema import WorkflowIssue

        error_catalog = CatalogResult(
            self.catalog.skills,
            (WorkflowIssue("error", "catalog.ambiguous_skill", "ambiguous"),),
            (WorkflowIssue("warning", "catalog.unlocked_skill", "unlocked"),),
        )
        result = compile_workflow(
            parse_workflow(self.value), error_catalog, self.validators, self.projection
        )
        self.assertIsNone(result.plan)
        self.assertIn("catalog.ambiguous_skill", self.issue_codes(result))
        self.assertIn("catalog.unlocked_skill", {issue.code for issue in result.warnings})

    def test_catalog_mapping_key_must_match_resolved_identity(self):
        """Catches compiling a binding under a different capability's identity payload."""
        changed = dict(self.catalog.skills)
        skill_id = next(iter(changed))
        changed[skill_id] = replace(changed[skill_id], catalog_id="different-skill")
        result = compile_workflow(
            parse_workflow(self.value), CatalogResult(changed, (), ()),
            self.validators, self.projection,
        )
        self.assertIn("catalog.skill_identity_mismatch", self.issue_codes(result))

    def test_compiled_hash_changes_with_skill_identity_not_document_hash(self):
        """Catches stale compiled plans surviving a bound local Skill tree change."""
        document = parse_workflow(self.value)
        first = compile_workflow(document, self.catalog, self.validators, self.projection)
        self.assertEqual(first.plan.document_sha256, document_sha256(document))
        skill_id = next(iter(self.catalog.skills))
        mutations = (
            {"relative_path": "alternate-relative-path"},
            {"skill_sha256": "2" * 64},
            {"tree_sha256": "sha256:" + "3" * 64},
            {"locked": False},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed_skills = dict(self.catalog.skills)
                changed_skills[skill_id] = replace(changed_skills[skill_id], **mutation)
                second = compile_workflow(
                    document, CatalogResult(changed_skills, (), ()),
                    self.validators, self.projection,
                )
                self.assertEqual(first.plan.document_sha256, second.plan.document_sha256)
                self.assertNotEqual(first.plan.semantic_sha256, second.plan.semantic_sha256)

    def test_compiled_hash_changes_with_validator_identity(self):
        """Catches omitting fixed validator implementation hashes from compiled identity."""
        value = copy.deepcopy(self.value)
        value["nodes"] = [{
            "id": "check", "type": "validator", "display_name": "Check", "entry": True,
            "enabled": True, "skill_ref": None, "validator_ref": "paper-section",
            "origin_projection_node_id": None, "inputs": [], "outputs": [],
            "outcomes": ["pass", "fail", "blocked"], "write_scopes": [],
            "failure_policy": "block", "condition_cases": [], "join_mode": "all_active",
        }]
        value["edges"] = []
        document = parse_workflow(value)
        first = compile_workflow(document, self.catalog, self.validators, self.projection)
        mutations = (
            {"adapter": "figure_contract_v1"},
            {"sha256": "1" * 64},
            {"input_schema": "changed_schema"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = dict(self.validators)
                changed["paper-section"] = replace(changed["paper-section"], **mutation)
                second = compile_workflow(document, self.catalog, changed, self.projection)
                self.assertEqual(first.plan.document_sha256, second.plan.document_sha256)
                self.assertNotEqual(first.plan.semantic_sha256, second.plan.semantic_sha256)

    def test_missing_bound_skill_identity_blocks_activation(self):
        """Catches a stale catalog reference compiling as an unverified capability."""
        value = copy.deepcopy(self.value)
        next(node for node in value["nodes"] if node["type"] == "task")[
            "skill_ref"
        ] = "not-installed"
        result = self.compile(value)
        self.assertIn("catalog.skill_not_found", self.issue_codes(result))


if __name__ == "__main__":
    unittest.main()
