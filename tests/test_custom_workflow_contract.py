import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CustomWorkflowContractTests(unittest.TestCase):
    def test_orchestrator_checks_selection_before_progress(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("workflow_manager.py summary", skill)
        self.assertIn("`mode: official`", skill)
        self.assertIn("Official v1.0", skill)
        self.assertIn("no selection has been recorded", skill)
        self.assertIn("custom state is the only runtime authority", skill)
        self.assertIn("Do not update", skill)
        self.assertIn("`.research/progress.md`", skill)

    def test_custom_contract_names_every_manager_transition(self):
        contract = (ROOT / "references" / "custom-workflow-contract.md").read_text(
            encoding="utf-8"
        )
        for command in (
            "ready",
            "claim",
            "submit-result",
            "run-validator",
            "register-artifact",
            "record-decision",
            "record-fact",
            "retry",
            "rerun-stale",
            "deactivate",
        ):
            with self.subTest(command=command):
                self.assertIn(f"workflow_manager.py {command}", contract)
        for receipt in ("node-invocation-v1", "node-result-v1", "stage-receipt-v2"):
            with self.subTest(receipt=receipt):
                self.assertIn(receipt, contract)

    def test_custom_mode_fails_closed_and_preserves_official_default(self):
        contract = (ROOT / "references" / "custom-workflow-contract.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("No selector or activation journal has been recorded", contract)
        self.assertIn("Selector projection is missing but a valid activation journal exists", contract)
        self.assertIn("existing Official v1.0 path", contract)
        self.assertIn("Do not inspect saved custom drafts or custom run files", contract)
        self.assertIn("Selection is custom and summary returns `mode: custom`", contract)
        for required in (
            "activated plan",
            "event history",
            "state snapshot",
            "artifact hashes against project files",
            "receipt",
        ):
            self.assertIn(required, contract)
        self.assertIn("corrupt/conflicting", contract)
        self.assertIn("required plan/run/evidence is missing or inconsistent", contract)
        self.assertIn("Never fall back silently", contract)
        self.assertIn("a disabled instance remains only", contract)


if __name__ == "__main__":
    unittest.main()
