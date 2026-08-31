import copy
import json
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
