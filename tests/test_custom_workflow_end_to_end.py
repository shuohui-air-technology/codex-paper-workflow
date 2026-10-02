import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts.workflow_manager import WorkflowService


ROOT = Path(__file__).resolve().parents[1]


class CustomWorkflowEndToEndTests(unittest.TestCase):
    def make_service(self, project):
        skills_root = project / "skills"
        skill = skills_root / "test-workflow-task"
        skill.mkdir(parents=True, exist_ok=True)
        skill.joinpath("SKILL.md").write_text(
            "---\nname: test-workflow-task\ndescription: End-to-end test skill\n---\n"
            "Create the requested test artifact.\n",
            encoding="utf-8",
        )
        return WorkflowService(project, skill_roots=(skills_root,))

    def branch_document(self):
        value = json.loads(
            (ROOT / "tests" / "fixtures" / "workflow_valid_branch_join.json")
            .read_text(encoding="utf-8")
        )
        value.update(
            workflow_id="parallel-branch-join",
            external_inputs=[],
            max_parallelism=2,
        )

        def task(node_id, display_name, *, entry, inputs=(), outputs=(), write_scopes=()):
            return {
                "id": node_id,
                "type": "task",
                "display_name": display_name,
                "entry": entry,
                "enabled": True,
                "skill_ref": "test-workflow-task",
                "validator_ref": None,
                "validator_config": None,
                "origin_projection_node_id": None,
                "inputs": list(inputs),
                "outputs": list(outputs),
                "outcomes": ["succeeded"],
                "write_scopes": list(write_scopes),
                "failure_policy": "block",
                "condition_cases": [],
                "join_mode": "all_active",
            }

        join = {
            "id": "join",
            "type": "join",
            "display_name": "Wait for both branches",
            "entry": False,
            "enabled": True,
            "skill_ref": None,
            "validator_ref": None,
            "validator_config": None,
            "origin_projection_node_id": None,
            "inputs": [],
            "outputs": ["sources", "design_notes"],
            "outcomes": [],
            "write_scopes": [],
            "failure_policy": "block",
            "condition_cases": [],
            "join_mode": "all_active",
        }
        value["nodes"] = [
            task("literature", "Discover literature", entry=True, outputs=("sources",), write_scopes=("sources",)),
            task("design", "Prepare study design", entry=True, outputs=("design_notes",), write_scopes=("design_notes",)),
            join,
            task(
                "drafting",
                "Draft from both branches",
                entry=False,
                inputs=("sources", "design_notes"),
                outputs=("draft",),
                write_scopes=("draft",),
            ),
        ]
        value["edges"] = [
            {
                "id": "literature-to-join",
                "source": "literature",
                "target": "join",
                "trigger": "succeeded",
                "output_map": {"sources": "sources"},
            },
            {
                "id": "design-to-join",
                "source": "design",
                "target": "join",
                "trigger": "succeeded",
                "output_map": {"design_notes": "design_notes"},
            },
            {
                "id": "join-to-drafting",
                "source": "join",
                "target": "drafting",
                "trigger": "succeeded",
                "output_map": {
                    "sources": "sources",
                    "design_notes": "design_notes",
                },
            },
        ]
        value["ui"] = {"positions": {
            "literature": {"x": 80, "y": 100},
            "design": {"x": 80, "y": 280},
            "join": {"x": 360, "y": 190},
            "drafting": {"x": 620, "y": 190},
        }}
        return value

    def submit_text_result(self, service, invocation, node_id, artifact_id, path, text):
        (service.project_root / path).write_text(text, encoding="utf-8")
        return service.submit_result({
            "schema_version": "node-result-v2",
            "run_id": invocation["run_id"],
            "node_id": node_id,
            "attempt": invocation["attempt"],
            "idempotency_token": invocation["idempotency_token"],
            "status": "succeeded",
            "outcome": "succeeded",
            "summary": f"Created {artifact_id} test evidence.",
            "artifacts": [{"id": artifact_id, "path": path}],
            "consumed_sources": [],
            "uncertainties": [],
        })

    def test_parallel_branch_join_survives_service_restart(self):
        with TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"CODEX_HOME": str(Path(temporary) / "codex-home")}
        ):
            project = Path(temporary)
            service = self.make_service(project)
            document = self.branch_document()
            validation = service.validate_document(document)
            self.assertEqual(validation["status"], "pass", validation["errors"])
            service.activate(
                document,
                acknowledged_warning_codes=validation["required_warning_codes"],
            )

            self.assertEqual(
                {item["node_id"] for item in service.ready()["ready"]},
                {"literature", "design"},
            )
            literature = service.claim("literature")
            self.submit_text_result(
                service, literature, "literature", "sources", "sources.md",
                "Verified source list for this test run.",
            )

            restarted = self.make_service(project)
            self.assertEqual(
                [item["node_id"] for item in restarted.ready()["ready"]],
                ["design"],
            )
            design = restarted.claim("design")
            self.submit_text_result(
                restarted, design, "design", "design_notes", "design.md",
                "Study design evidence for this test run.",
            )

            resumed_again = self.make_service(project)
            self.assertEqual(
                [item["node_id"] for item in resumed_again.ready()["ready"]],
                ["drafting"],
            )

    def test_official_selection_does_not_create_custom_state(self):
        with TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"CODEX_HOME": str(Path(temporary) / "codex-home")}
        ):
            project = Path(temporary)
            service = self.make_service(project)

            summary = service.summary()

            self.assertEqual(summary["mode"], "official")
            self.assertFalse(project.joinpath(".research", "custom-workflow").exists())


if __name__ == "__main__":
    unittest.main()
