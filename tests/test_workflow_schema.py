import copy
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.workflow_engine.schema import (
    WorkflowError,
    document_payload,
    document_sha256,
    load_workflow,
    parse_workflow,
)


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSchemaTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())

    def test_visual_position_does_not_change_semantic_hash(self):
        """Catches accidentally including canvas coordinates in semantic identity."""
        original = parse_workflow(self.value)
        moved = copy.deepcopy(self.value)
        moved["ui"]["positions"]["directions"] = {"x": 900, "y": 700}
        self.assertEqual(document_sha256(original), document_sha256(parse_workflow(moved)))

    def test_duplicate_node_id_is_rejected_with_stable_code(self):
        """Catches accepting ambiguous node references after a duplicate is added."""
        duplicate = copy.deepcopy(self.value)
        duplicate["nodes"].append(copy.deepcopy(duplicate["nodes"][0]))
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(duplicate)
        self.assertEqual(caught.exception.code, "schema.duplicate_node_id")

    def test_unbound_task_is_a_saveable_but_incomplete_draft(self):
        """Catches rejecting the Studio's deliberately incomplete task draft."""
        draft = copy.deepcopy(self.value)
        draft["nodes"][0]["skill_ref"] = None
        document = parse_workflow(draft)
        self.assertIsNone(document.nodes[0].skill_ref)

    def test_projection_id_accepts_locked_version_and_rejects_unsafe_forms(self):
        """Catches general IDs blocking the locked projection or provenance accepting paths."""
        derived = copy.deepcopy(self.value)
        derived["derived_from"] = {
            "projection_id": "official-v1.0",
            "projection_sha256": "0" * 64,
        }
        self.assertEqual(
            parse_workflow(derived).derived_from["projection_id"], "official-v1.0"
        )

        for unsafe in (
            " official-v1.0",
            "official-v1.0 ",
            "../official-v1.0",
            "official/v1.0",
            "official\\v1.0",
            "official..v1",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(WorkflowError) as caught:
                invalid = copy.deepcopy(derived)
                invalid["derived_from"]["projection_id"] = unsafe
                parse_workflow(invalid)
            self.assertIn(
                caught.exception.code,
                {"schema.invalid_scalar", "schema.invalid_projection_id"},
            )

    def test_control_node_cannot_bind_a_skill(self):
        """Catches a control node becoming an executable Skill capability."""
        invalid = copy.deepcopy(self.value)
        invalid["nodes"][0]["type"] = "join"
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(invalid)
        self.assertEqual(caught.exception.code, "schema.control_skill_forbidden")

    def test_condition_outcomes_are_derived_from_cases_and_default(self):
        """Catches condition outcomes drifting from its named branch cases."""
        branch = json.loads(
            (ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text()
        )
        document = parse_workflow(branch)
        condition = next(node for node in document.nodes if node.id == "condition")
        self.assertEqual(condition.outcomes, ("has_sources", "default"))

    def test_document_payload_excludes_display_and_draft_revision(self):
        """Catches presentation-only draft changes changing the semantic payload."""
        changed = copy.deepcopy(self.value)
        changed["document_revision"] = 99
        changed["nodes"][0]["display_name"] = "A renamed visual card"
        document = parse_workflow(self.value)
        changed_document = parse_workflow(changed)
        self.assertEqual(document_payload(document), document_payload(changed_document))

    def test_load_workflow_parses_a_json_document_from_disk(self):
        """Catches the public loader bypassing the same structural parser as Studio input."""
        document = load_workflow(ROOT / "tests/fixtures/workflow_valid_installed_core.json")
        self.assertEqual(document.workflow_id, "installed-core-paper-flow")

    def test_unknown_node_field_is_rejected(self):
        """Catches silently accepting a node-schema change without a version bump."""
        invalid = copy.deepcopy(self.value)
        invalid["nodes"][0]["unreviewed_field"] = True
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(invalid)
        self.assertEqual(caught.exception.code, "schema.unknown_node_field")

    def test_load_workflow_rejects_duplicate_json_member_names(self):
        """Catches a later duplicate JSON key shadowing an earlier workflow value."""
        text = (ROOT / "tests/fixtures/workflow_valid_linear.json").read_text()
        duplicate = text.replace(
            '  "workflow_id": "linear-paper-flow",',
            '  "workflow_id": "linear-paper-flow",\n  "workflow_id": "shadow-flow",',
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate-key.json"
            path.write_text(duplicate, encoding="utf-8")
            with self.assertRaises(WorkflowError) as caught:
                load_workflow(path)
        self.assertEqual(caught.exception.code, "schema.duplicate_json_key")

    def test_large_integer_coordinate_is_rejected_as_a_workflow_error(self):
        """Catches a huge JSON integer leaking OverflowError during coordinate validation."""
        invalid = copy.deepcopy(self.value)
        invalid["ui"]["positions"]["directions"]["x"] = 10 ** 400
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(invalid)
        self.assertEqual(caught.exception.code, "schema.invalid_coordinate")

    def test_binding_matrix_allows_only_valid_node_bindings(self):
        """Catches every executable/control node type accepting the wrong binding kind."""
        def node_for(node_type):
            node = copy.deepcopy(self.value["nodes"][0])
            node.update({
                "type": node_type,
                "skill_ref": None,
                "validator_ref": None,
                "outcomes": [],
                "condition_cases": [],
            })
            if node_type == "task":
                node["outcomes"] = ["succeeded"]
            elif node_type == "validator":
                node["outcomes"] = ["pass", "fail", "blocked"]
                node["outputs"] = []
                node["inputs"] = []
            elif node_type == "condition":
                node["condition_cases"] = [
                    {"outcome": "chosen", "when": {"op": "fact_is", "name": "ready", "value": True}}
                ]
            return node

        valid_bindings = (
            ("task", "skill_ref", None),
            ("task", "skill_ref", "clarify-research-idea"),
            ("validator", "validator_ref", None),
            ("validator", "validator_ref", "paper-section"),
            ("condition", None, None),
            ("join", None, None),
        )
        for node_type, field, binding in valid_bindings:
            with self.subTest(valid_node=node_type, field=field, binding=binding):
                document = copy.deepcopy(self.value)
                node = node_for(node_type)
                if field is not None:
                    node[field] = binding
                if node_type == "validator" and binding == "paper-section":
                    node["inputs"] = ["section"]
                    node["validator_config"] = {"input_roles": {"file": "section"}, "options": {
                        "phase": "body", "paper_type": "empirical", "language": "en",
                        "method_profile": "method-first", "validity_status": "pending",
                        "discussion_integrated": False,
                    }}
                document["nodes"] = [node]
                document["edges"] = []
                parsed = parse_workflow(document).nodes[0]
                self.assertEqual(parsed.skill_ref, node["skill_ref"])
                self.assertEqual(parsed.validator_ref, node["validator_ref"])

        invalid_bindings = (
            ("task", "validator_ref", "paper-section", "schema.task_validator_forbidden"),
            ("validator", "skill_ref", "clarify-research-idea", "schema.validator_skill_forbidden"),
            ("condition", "skill_ref", "clarify-research-idea", "schema.control_skill_forbidden"),
            ("condition", "validator_ref", "paper-section", "schema.control_validator_forbidden"),
            ("join", "skill_ref", "clarify-research-idea", "schema.control_skill_forbidden"),
            ("join", "validator_ref", "paper-section", "schema.control_validator_forbidden"),
        )
        for node_type, field, binding, expected_code in invalid_bindings:
            with self.subTest(node_type=node_type, field=field):
                document = copy.deepcopy(self.value)
                node = node_for(node_type)
                node[field] = binding
                document["nodes"] = [node]
                document["edges"] = []
                with self.assertRaises(WorkflowError) as caught:
                    parse_workflow(document)
                self.assertEqual(caught.exception.code, expected_code)

    def test_parsed_nested_data_resists_mutation_and_source_changes(self):
        """Catches parsed predicates, edge maps, and UI positions sharing mutable input data."""
        source = json.loads((ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text())
        document = parse_workflow(source)
        condition = next(node for node in document.nodes if node.id == "condition")
        mapped_edge = next(edge for edge in document.edges if edge.id == "literature-to-join")

        with self.assertRaises(TypeError):
            condition.condition_cases[0]["when"]["value"] = "tampered"
        with self.assertRaises(TypeError):
            mapped_edge.output_map["sources"] = "tampered"
        with self.assertRaises(TypeError):
            document.ui["positions"]["condition"]["x"] = 999

        source["nodes"][0]["condition_cases"][0]["when"]["value"] = "tampered"
        source["edges"][2]["output_map"]["sources"] = "tampered"
        source["ui"]["positions"]["condition"]["x"] = 999
        self.assertEqual(condition.condition_cases[0]["when"]["value"], "verified")
        self.assertEqual(mapped_edge.output_map["sources"], "evidence_bundle")
        self.assertEqual(document.ui["positions"]["condition"]["x"], 80)

    def test_node_and_edge_input_order_do_not_change_semantic_hash(self):
        """Catches hash identity depending on Studio canvas list insertion order."""
        original = json.loads((ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text())
        reordered = copy.deepcopy(original)
        reordered["nodes"].reverse()
        reordered["edges"].reverse()
        self.assertEqual(
            document_sha256(parse_workflow(original)),
            document_sha256(parse_workflow(reordered)),
        )

    def test_canonical_payload_and_sha256_match_hand_constructed_bytes(self):
        """Catches changes to the exact persisted semantic JSON or its SHA-256 digest."""
        document = load_workflow(ROOT / "tests/fixtures/workflow_valid_installed_core.json")
        expected_json = (
            '{"derived_from":null,"edges":[],"external_inputs":[],"max_parallelism":1,'
            '"nodes":[{"condition_cases":[],"enabled":true,"entry":true,'
            '"failure_policy":"block","id":"orchestrate","inputs":[],'
            '"join_mode":"all_active","origin_projection_node_id":null,'
            '"outcomes":["succeeded"],"outputs":[],"skill_ref":"paper-workflow-orchestrator",'
            '"type":"task","validator_config":null,"validator_ref":null,"write_scopes":[]}],'
            '"schema_version":"paper-workflow-custom-v1","semantic_revision":1,'
            '"workflow_id":"installed-core-paper-flow"}'
        )
        expected_sha256 = "6e95145857c854f7cfc56fe4ae55ee1d4f233c1411fc77610aed69b6c0463eef"
        actual_json = json.dumps(
            document_payload(document), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(actual_json, expected_json)
        self.assertEqual(hashlib.sha256(expected_json.encode("utf-8")).hexdigest(), expected_sha256)
        self.assertEqual(document_sha256(document), expected_sha256)


if __name__ == "__main__":
    unittest.main()
