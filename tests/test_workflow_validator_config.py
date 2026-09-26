"""Task 7B1: code-owned validator forms are activation identity, not CLI text."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest import mock

from scripts.workflow_engine.catalog import CatalogResult, load_validator_registry
from scripts.workflow_engine.compiler import compile_workflow
from scripts.workflow_engine.schema import WorkflowError, document_sha256, parse_workflow
from scripts.workflow_engine.store import StoreError, WorkflowStore, _document_data, _plan_data, _plan_from_data
from scripts.workflow_manager import WorkflowService


ROOT = Path(__file__).resolve().parents[1]
PAPER_OPTIONS = {
    "phase": "body", "paper_type": "empirical", "language": "en",
    "method_profile": "method-first", "validity_status": "pending",
    "discussion_integrated": False,
}


def document_for(validator_id, roles, options):
    value = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())
    value["nodes"] = [{
        "id": "check", "type": "validator", "display_name": "Check", "entry": True,
        "enabled": True, "skill_ref": None, "validator_ref": validator_id,
        "validator_config": {"input_roles": roles, "options": options},
        "origin_projection_node_id": None, "inputs": list(roles.values()),
        "outputs": [], "outcomes": ["pass", "fail", "blocked"],
        "write_scopes": [], "failure_policy": "block", "condition_cases": [],
        "join_mode": "all_active",
    }]
    value["external_inputs"] = list(roles.values())
    value["edges"] = []
    value["ui"]["positions"] = {"check": {"x": 0, "y": 0}}
    return value


class ValidatorFormTests(unittest.TestCase):
    def setUp(self):
        self.validators = load_validator_registry(
            ROOT / "references/workflows/validator-registry.v1.json", ROOT,
        )
        self.projection = json.loads((ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text())
        self.catalog = CatalogResult({}, (), ())

    def compile(self, value):
        return compile_workflow(parse_workflow(value), self.catalog, self.validators, self.projection)

    def assert_invalid(self, value, code):
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(value)
        self.assertEqual(caught.exception.code, code)

    def test_four_runnable_forms_compile_with_frozen_config(self):
        forms = (
            ("experiment-contract", {"contract": "contract_file"}, {}),
            ("figure-contract", {"receipt": "figure_receipt"}, {}),
            ("final-edit-receipt", {"receipt": "edit_receipt"}, {}),
            ("paper-section", {"file": "section_file"}, PAPER_OPTIONS),
        )
        for validator_id, roles, options in forms:
            with self.subTest(validator_id=validator_id):
                value = document_for(validator_id, roles, options)
                result = self.compile(value)
                self.assertIsNotNone(result.plan, result.errors)
                config = result.plan.nodes["check"].validator_config
                self.assertEqual(dict(config["input_roles"]), roles)
                self.assertEqual(dict(config["options"]), options)
                with self.assertRaises(TypeError):
                    config["input_roles"][next(iter(roles))] = "changed"

    def test_paper_final_requires_receipt_and_other_phases_reject_it(self):
        final = document_for("paper-section", {"file": "section", "semantic_receipt": "receipt"},
                             {**PAPER_OPTIONS, "phase": "final"})
        self.assertIsNotNone(self.compile(final).plan)
        missing = copy.deepcopy(final)
        del missing["nodes"][0]["validator_config"]["input_roles"]["semantic_receipt"]
        self.assert_invalid(missing, "schema.validator_roles_invalid")
        body = copy.deepcopy(final)
        body["nodes"][0]["validator_config"]["options"]["phase"] = "body"
        self.assert_invalid(body, "schema.validator_roles_invalid")
        abstract = document_for("paper-section", {"file": "section"},
                                {**PAPER_OPTIONS, "phase": "abstract"})
        self.assertIsNotNone(self.compile(abstract).plan)

    def test_each_single_role_validator_rejects_extra_roles_and_options(self):
        for validator_id, role in (("experiment-contract", "contract"),
                                   ("figure-contract", "receipt"),
                                   ("final-edit-receipt", "receipt")):
            with self.subTest(validator_id=validator_id):
                value = document_for(validator_id, {role: "evidence"}, {})
                extra_role = copy.deepcopy(value)
                extra_role["nodes"][0]["validator_config"]["input_roles"]["file"] = "other"
                self.assert_invalid(extra_role, "schema.validator_roles_invalid")
                extra_option = copy.deepcopy(value)
                extra_option["nodes"][0]["validator_config"]["options"]["phase"] = "body"
                self.assert_invalid(extra_option, "schema.validator_options_invalid")

    def test_rejects_missing_extra_duplicate_and_nonlocal_roles(self):
        base = document_for("paper-section", {"file": "section"}, PAPER_OPTIONS)
        mutations = (
            (lambda v: v["nodes"][0].pop("validator_config"), "schema.missing_field"),
            (lambda v: v["nodes"][0]["validator_config"].pop("options"), "schema.missing_field"),
            (lambda v: v["nodes"][0]["validator_config"].update({"command": "python x.py"}), "schema.unknown_validator_config_field"),
            (lambda v: v["nodes"][0]["validator_config"]["input_roles"].update({"extra": "other"}), "schema.validator_roles_invalid"),
            (lambda v: v["nodes"][0]["inputs"].append("other"), "schema.validator_inputs_mismatch"),
            (lambda v: v["nodes"][0]["validator_config"]["input_roles"].update({"file": "../section.py"}), "schema.invalid_identifier"),
            (lambda v: v["nodes"][0]["validator_config"]["options"].update({"script": "x.py"}), "schema.validator_options_invalid"),
            (lambda v: v["nodes"][0]["validator_config"]["options"].pop("language"), "schema.validator_options_invalid"),
            (lambda v: v["nodes"][0]["validator_config"]["options"].update({"phase": "publish"}), "schema.validator_option_value_invalid"),
            (lambda v: v["nodes"][0]["validator_config"]["options"].update({"discussion_integrated": 1}), "schema.validator_option_value_invalid"),
        )
        for mutate, code in mutations:
            with self.subTest(code=code, mutation=mutate.__code__.co_firstlineno):
                value = copy.deepcopy(base)
                mutate(value)
                self.assert_invalid(value, code)
        duplicate = document_for("paper-section", {"file": "same", "semantic_receipt": "same"},
                                 {**PAPER_OPTIONS, "phase": "final"})
        duplicate["nodes"][0]["inputs"] = ["same", "other"]
        duplicate["external_inputs"] = ["same", "other"]
        self.assert_invalid(duplicate, "schema.validator_role_duplicate")

    def test_gate_outputs_and_nonvalidator_config_are_forbidden(self):
        value = document_for("experiment-contract", {"contract": "contract"}, {})
        value["nodes"][0]["outputs"] = ["claimed_result"]
        self.assert_invalid(value, "schema.validator_outputs_forbidden")
        task = json.loads((ROOT / "tests/fixtures/workflow_valid_linear.json").read_text())
        for node in task["nodes"]:
            node["validator_config"] = None
        task["nodes"][0]["validator_config"] = {"input_roles": {}, "options": {}}
        self.assert_invalid(task, "schema.nonvalidator_config_forbidden")

    def test_humanizer_disabled_identity_is_visible_but_enabled_is_rejected(self):
        value = document_for("humanizer-preflight", {}, {})
        value["nodes"][0]["validator_config"] = None
        self.assert_invalid(value, "validator.humanizer_unavailable")
        value["nodes"][0]["enabled"] = False
        self.assertIsNone(parse_workflow(value).nodes[0].validator_config)
        from scripts.workflow_engine.schema import validator_form_metadata
        metadata = validator_form_metadata()
        self.assertFalse(metadata["humanizer-preflight"]["available"])
        self.assertIn("humanizer", metadata["humanizer-preflight"]["unavailable_reason"].lower())
        self.assertEqual(set(metadata["paper-section"]["options"]), set(PAPER_OPTIONS))
        self.assertNotIn("script", json.dumps(metadata))
        for option, choices in (("phase", ["body", "abstract", "final"]),
                                ("paper_type", ["empirical", "theoretical", "review", "protocol"]),
                                ("language", ["en", "zh"]),
                                ("method_profile", ["method-first", "data-first"]),
                                ("validity_status", ["pending", "clear", "blocked"]),
                                ("discussion_integrated", [False, True])):
            self.assertEqual(metadata["paper-section"]["options"][option]["choices"], choices)
        with TemporaryDirectory() as temporary:
            service = WorkflowService(Path(temporary), skill_roots=(Path(temporary),))
            for operation in (service.validate_document, service.activate):
                with self.assertRaises(WorkflowError) as caught:
                    operation(document_for("humanizer-preflight", {}, {}))
                self.assertEqual(caught.exception.code, "validator.humanizer_unavailable")
            self.assertFalse(service.store.paths.selection.exists())

    def test_config_changes_identity_but_json_key_order_does_not(self):
        first = document_for("paper-section", {"file": "one", "semantic_receipt": "two"},
                             {**PAPER_OPTIONS, "phase": "final"})
        swapped = copy.deepcopy(first)
        swapped["nodes"][0]["validator_config"]["input_roles"] = {"file": "two", "semantic_receipt": "one"}
        reordered = copy.deepcopy(first)
        reordered["nodes"][0]["validator_config"] = {
            "options": dict(reversed(list(first["nodes"][0]["validator_config"]["options"].items()))),
            "input_roles": dict(reversed(list(first["nodes"][0]["validator_config"]["input_roles"].items()))),
        }
        self.assertNotEqual(document_sha256(parse_workflow(first)), document_sha256(parse_workflow(swapped)))
        self.assertEqual(document_sha256(parse_workflow(first)), document_sha256(parse_workflow(reordered)))
        self.assertNotEqual(self.compile(first).plan.semantic_sha256, self.compile(swapped).plan.semantic_sha256)
        self.assertEqual(self.compile(first).plan.semantic_sha256, self.compile(reordered).plan.semantic_sha256)

    def test_document_and_plan_codec_roundtrip_and_missing_field_fail_closed(self):
        value = document_for("paper-section", {"file": "section"}, PAPER_OPTIONS)
        document = parse_workflow(value)
        self.assertEqual(document_sha256(parse_workflow(_document_data(document))), document_sha256(document))
        plan = self.compile(value).plan
        self.assertEqual(_plan_from_data(_plan_data(plan)), plan)
        with TemporaryDirectory() as temporary:
            with mock.patch("scripts.workflow_manager.discover_skills", return_value=self.catalog):
                validation = WorkflowService(Path(temporary), skill_roots=(Path(temporary),)).validate_document(document)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(validation["semantic_sha256"], plan.semantic_sha256)
        old_document = copy.deepcopy(value)
        old_document["nodes"][0].pop("validator_config")
        self.assert_invalid(old_document, "schema.missing_field")
        old_plan = _plan_data(plan)
        old_plan["nodes"]["check"].pop("validator_config")
        with self.assertRaises(StoreError) as caught:
            _plan_from_data(old_plan)
        self.assertEqual(caught.exception.code, "plan.invalid")

    def test_forged_documents_cannot_bypass_parse_at_compiler_or_service(self):
        base = document_for("experiment-contract", {"contract": "contract_file"}, {})
        cases = (
            ("extra_option", {"input_roles": {"contract": "contract_file"},
                              "options": {"script": "evil.py"}}, "schema.validator_options_invalid"),
            ("missing_role", {"input_roles": {}, "options": {}}, "schema.validator_roles_invalid"),
            ("role_input_mismatch", {"input_roles": {"contract": "other_file"},
                                     "options": {}}, "schema.validator_inputs_mismatch"),
            ("extra_config_field", {"input_roles": {"contract": "contract_file"},
                                    "options": {}, "command": "python evil.py"},
             "schema.unknown_validator_config_field"),
            ("missing_options_member", {"input_roles": {"contract": "contract_file"}},
             "schema.missing_field"),
        )
        with TemporaryDirectory() as temporary:
            service = WorkflowService(Path(temporary), skill_roots=(Path(temporary),))
            for name, config, code in cases:
                with self.subTest(name=name):
                    raw = copy.deepcopy(base)
                    raw["nodes"][0]["validator_config"] = config
                    forged = replace(
                        parse_workflow(base),
                        nodes=(replace(parse_workflow(base).nodes[0],
                                       validator_config=MappingProxyType(config)),),
                    )
                    with self.assertRaises(WorkflowError) as raw_error:
                        parse_workflow(raw)
                    self.assertEqual(raw_error.exception.code, code)
                    result = compile_workflow(forged, self.catalog, self.validators, self.projection)
                    self.assertIsNone(result.plan)
                    self.assertEqual(result.errors[0].code, code)
                    self.assertEqual(result.errors[0].node_id, "check")
                    for candidate in (raw, forged):
                        with self.assertRaises(WorkflowError) as service_error:
                            service.validate_document(candidate)
                        self.assertEqual(service_error.exception.code, code)

            raw = copy.deepcopy(base)
            raw["nodes"][0]["validator_ref"] = "humanizer-preflight"
            raw["nodes"][0]["validator_config"] = None
            forged = replace(parse_workflow(base), nodes=(replace(
                parse_workflow(base).nodes[0], validator_ref="humanizer-preflight",
                validator_config=None,
            ),))
            for candidate in (raw, forged):
                with self.assertRaises(WorkflowError) as error:
                    service.validate_document(candidate)
                self.assertEqual(error.exception.code, "validator.humanizer_unavailable")
            result = compile_workflow(forged, self.catalog, self.validators, self.projection)
            self.assertIsNone(result.plan)
            self.assertEqual(result.errors[0].code, "validator.humanizer_unavailable")
            self.assertFalse(service.store.paths.workflow.exists())
            self.assertFalse(service.store.paths.selection.exists())

    def test_forged_paper_option_and_store_still_fail_closed(self):
        raw = document_for("paper-section", {"file": "section"}, PAPER_OPTIONS)
        parsed = parse_workflow(raw)
        config = copy.deepcopy(raw["nodes"][0]["validator_config"])
        del config["options"]["language"]
        raw["nodes"][0]["validator_config"] = config
        forged = replace(parsed, nodes=(replace(
            parsed.nodes[0], validator_config=MappingProxyType(config),
        ),))
        with self.assertRaises(WorkflowError) as caught:
            parse_workflow(raw)
        self.assertEqual(caught.exception.code, "schema.validator_options_invalid")
        result = compile_workflow(forged, self.catalog, self.validators, self.projection)
        self.assertIsNone(result.plan)
        self.assertEqual(result.errors[0].code, "schema.validator_options_invalid")
        with TemporaryDirectory() as temporary:
            service = WorkflowService(Path(temporary), skill_roots=(Path(temporary),))
            for candidate in (raw, forged):
                with self.assertRaises(WorkflowError) as service_error:
                    service.validate_document(candidate)
                self.assertEqual(service_error.exception.code, "schema.validator_options_invalid")
            store = WorkflowStore(Path(temporary))
            with self.assertRaises(StoreError) as store_error:
                store.save_draft(forged, expected_document_revision=0)
            self.assertEqual(store_error.exception.code, "store.draft_invalid")
            self.assertFalse(store.paths.workflow.exists())
            self.assertFalse(store.paths.selection.exists())

    def test_constructed_control_outcomes_are_not_silently_normalized(self):
        value = json.loads((ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text())
        parsed = parse_workflow(value)
        nodes = tuple(replace(node, outcomes=("forged",)) if node.type == "condition" else node
                      for node in parsed.nodes)
        forged = replace(parsed, nodes=nodes)
        result = compile_workflow(forged, self.catalog, self.validators, self.projection)
        self.assertIsNone(result.plan)
        self.assertEqual(result.errors[0].code, "schema.document_noncanonical")

    def test_constructed_document_with_invalid_collection_type_has_stable_error(self):
        parsed = parse_workflow(document_for("experiment-contract", {"contract": "contract_file"}, {}))
        forged = replace(parsed, nodes=None)
        result = compile_workflow(forged, self.catalog, self.validators, self.projection)
        self.assertIsNone(result.plan)
        self.assertEqual(result.errors[0].code, "schema.invalid_constructed_document")

    def test_save_draft_rejects_forged_control_outcomes_before_any_file_write(self):
        raw = json.loads((ROOT / "tests/fixtures/workflow_valid_branch_join.json").read_text())
        parsed = parse_workflow(raw)
        for node_type in ("condition", "join"):
            forged = replace(parsed, nodes=tuple(
                replace(node, outcomes=("forged",)) if node.type == node_type else node
                for node in parsed.nodes
            ))
            with self.subTest(node_type=node_type), TemporaryDirectory() as temporary:
                root = Path(temporary)
                service = WorkflowService(root, skill_roots=(root,))
                with self.assertRaises(WorkflowError) as service_error:
                    service.save_draft(forged, expected_document_revision=0)
                self.assertEqual(service_error.exception.code, "schema.document_noncanonical")
                with self.assertRaises(StoreError) as store_error:
                    WorkflowStore(root).save_draft(forged, expected_document_revision=0)
                self.assertEqual(store_error.exception.code, "store.draft_invalid")
                self.assertEqual(store_error.exception.__cause__.code, "schema.document_noncanonical")
                self.assertFalse(service.store.paths.workflow.exists())
                self.assertEqual(list(service.store.paths.revisions.glob("*.json")), [])

                saved = service.save_draft(parsed, expected_document_revision=0)
                self.assertEqual(saved["document_revision"], 1)
                self.assertEqual(service.load_draft(), saved)
                workflow_before = service.store.paths.workflow.read_bytes()
                revisions_before = {
                    path.name: path.read_bytes()
                    for path in service.store.paths.revisions.glob("*.json")
                }
                with self.assertRaises(WorkflowError):
                    service.save_draft(forged, expected_document_revision=1)
                with self.assertRaises(StoreError):
                    service.store.save_draft(forged, expected_document_revision=1)
                self.assertEqual(service.store.paths.workflow.read_bytes(), workflow_before)
                self.assertEqual(
                    {path.name: path.read_bytes()
                     for path in service.store.paths.revisions.glob("*.json")},
                    revisions_before,
                )


if __name__ == "__main__":
    unittest.main()
