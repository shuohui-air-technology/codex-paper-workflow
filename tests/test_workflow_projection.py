import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NORMALIZED_STAGES = {
    "intake", "directions", "literature", "topic", "design", "draft_audit",
    "venue_outline", "outline", "drafting", "scientific_figures",
    "abstract_title_keywords", "integrity", "review", "revision",
    "author_guided_final_editing", "prose_naturalization",
    "final_editorial_audit", "experiments", "finalize",
}


class WorkflowProjectionTests(unittest.TestCase):
    def test_projection_is_bound_to_the_reviewed_v10_sources(self):
        data = json.loads((ROOT / "references/workflows/official-v1.0-studio-projection.json").read_text())
        self.assertEqual(data["schema_version"], "paper-workflow-studio-projection-v1")
        self.assertEqual(data["source_commit"], "e24c34255e3c72a329614e07c431bfa51a778c40")
        self.assertEqual(data["source_files"], {
            "SKILL.md": "d9d95b736f6450421294954e871179c209f2a5ac015f2f057e147a5bd22b4240",
            "references/stage-contracts.md": "e6780f2f00a81a695ba7b6c8b44d3ee3adced6eef7ea795cdf391113b19deff9",
            "references/progress-schema.md": "44c868ca0160906e74a8e61ee413f27bd4b23e151b7d486bc25b5516d45a3c7f",
        })
        self.assertEqual(data["official_contract_sections"], {
            "SKILL.md#body": "4387f4201b8b3e5ff38c49dc555f30397d164b60ce393cdb66b8d223f9cf9786",
            "references/stage-contracts.md#body": "e6780f2f00a81a695ba7b6c8b44d3ee3adced6eef7ea795cdf391113b19deff9",
            "references/progress-schema.md#body": "44c868ca0160906e74a8e61ee413f27bd4b23e151b7d486bc25b5516d45a3c7f",
        })
        self.assertEqual(set(data["stage_map"]), NORMALIZED_STAGES)
        self.assertTrue(any(note["kind"] == "feedback_as_new_revision" for note in data["projection_notes"]))

    def test_validator_registry_contains_only_repository_scripts(self):
        data = json.loads((ROOT / "references/workflows/validator-registry.v1.json").read_text())
        self.assertEqual(data["schema_version"], "paper-workflow-validator-registry-v1")
        expected = {
            "experiment-contract", "figure-contract", "final-edit-receipt",
            "humanizer-preflight", "paper-section",
        }
        self.assertEqual(set(data["validators"]), expected)
        for entry in data["validators"].values():
            path = ROOT / entry["script"]
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
