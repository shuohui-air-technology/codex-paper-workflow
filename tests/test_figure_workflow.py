import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "figure_contract_validator.py"


def _png_bytes(width: int, height: int, dpi: float) -> bytes:
    ppm = round(dpi / 0.0254)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)

    pixels = b"".join(b"\x00" + (b"\x80" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


class FigureContractTests(unittest.TestCase):
    def _fixture(self, root: Path, *, figure_kind: str = "data_plot") -> tuple[dict, dict[str, bytes]]:
        values = {
            "data.csv": b"x,y\n1,2\n",
            "figure.py": b"print('figure')\n",
            "figure.pdf": b"%PDF-1.4",
            "figure.png": _png_bytes(100, 100, 300),
            "figure-preview.png": _png_bytes(1051, 700, 300),
            ".research/figure_plan.yml": b"figures:\n  - id: F001\n    claims: [C001]\n    sources: [S001]\n",
            ".research/claim_evidence_matrix.yml": b"claims:\n  - claim_id: C001\n    source_refs: [S001]\n",
        }
        digests = {name: "sha256:" + hashlib.sha256(value).hexdigest() for name, value in values.items()}
        receipt = {
            "schema_version": "figure-receipt-v1",
            "figure_id": "F001",
            "figure_kind": figure_kind,
            "claim_refs": ["C001"],
            "source_refs": ["S001"],
            "source_files": [{"path": "data.csv", "sha256": digests["data.csv"]}],
            "figure_plan": {"path": ".research/figure_plan.yml", "sha256": digests[".research/figure_plan.yml"]},
            "claim_evidence_matrix": {"path": ".research/claim_evidence_matrix.yml", "sha256": digests[".research/claim_evidence_matrix.yml"]},
            "claim_bindings": [{"claim_ref": "C001", "source_refs": ["S001"]}],
            "code": {"path": "figure.py", "sha256": digests["figure.py"]},
            "transformations": ["group mean; no smoothing"],
            "uncertainty": "95% bootstrap CI; n=3 per condition",
            "missing_data_policy": "missing values retained as gaps",
            "target": {"venue": "generic", "phase": "draft", "width_mm": 89},
            "outputs": [
                {"path": "figure.pdf", "format": "pdf", "sha256": digests["figure.pdf"]},
                {"path": "figure.png", "format": "png", "sha256": digests["figure.png"]},
            ],
            "preview": {
                "path": "figure-preview.png", "format": "png", "sha256": digests["figure-preview.png"],
                "rendered_from": "figure.pdf", "width_px": 1051, "height_px": 700,
                "dpi": 300, "width_mm": 89,
            },
            "alt_text": "Observed response by condition with 95 percent intervals.",
            "visual_review": {
                "status": "pass", "reviewed_at": "2026-08-27T00:00:00Z",
                "reviewer_type": "human", "reviewer_id": "reviewer-1",
                "checks": ["axes", "legend", "uncertainty", "target-size"],
                "findings": [], "dispositions": [],
            },
            "validation_status": "pass",
        }
        return receipt, values

    def _run(self, receipt: dict, values: dict[str, bytes] | None = None, *, receipt_name: str = "F001"):
        self.assertTrue(VALIDATOR.is_file(), "figure validator has not been implemented")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for name, value in (values or {}).items():
                path = project / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            receipt_path = project / ".research" / "figures" / receipt_name / "figure_receipt.json"
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), "--receipt", str(receipt_path), "--project-root", str(project)],
                text=True, capture_output=True,
            )

    def test_complete_receipt_passes(self):
        receipt, values = self._fixture(Path("."))
        result = self._run(receipt, values)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")

    def test_missing_source_is_blocked(self):
        receipt, values = self._fixture(Path("."))
        receipt["source_refs"] = []
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")

    def test_hash_drift_is_blocked(self):
        receipt, values = self._fixture(Path("."))
        receipt["code"]["sha256"] = "sha256:" + "0" * 64
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")

    def test_malformed_contract_fails_closed_without_traceback(self):
        receipt, values = self._fixture(Path("."))
        receipt["figure_kind"] = []
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_receipt_location_and_id_are_bound(self):
        receipt, values = self._fixture(Path("."))
        receipt["figure_id"] = ""
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        result = self._run(receipt, values, receipt_name="OTHER")
        self.assertNotEqual(result.returncode, 0)

    def test_preview_must_be_independent_raster_with_review_evidence(self):
        receipt, values = self._fixture(Path("."))
        receipt["preview"] = {"path": "figure.pdf", "format": "pdf", "sha256": "sha256:" + hashlib.sha256(values["figure.pdf"]).hexdigest()}
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_vector_output_must_have_matching_file_header(self):
        receipt, values = self._fixture(Path("."))
        values["figure.pdf"] = b"not a PDF"
        receipt["outputs"][0]["sha256"] = "sha256:" + hashlib.sha256(values["figure.pdf"]).hexdigest()
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        values["figure-preview.png"] = b"not actually a PNG"
        receipt["preview"]["sha256"] = "sha256:" + hashlib.sha256(values["figure-preview.png"]).hexdigest()
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        receipt["visual_review"].pop("reviewer_id")
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_plan_and_ledger_hash_drift_or_missing_ids_block(self):
        receipt, values = self._fixture(Path("."))
        receipt["figure_plan"]["sha256"] = "sha256:" + "0" * 64
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        values[".research/claim_evidence_matrix.yml"] = b"claims: [C999]\nsources: [S999]\n"
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_plan_figure_id_and_binding_coverage_are_exact(self):
        receipt, values = self._fixture(Path("."))
        values[".research/figure_plan.yml"] = b"figures:\n  - id: F002\n    claims: [C001]\n    sources: [S001]\n"
        receipt["figure_plan"]["sha256"] = "sha256:" + hashlib.sha256(values[".research/figure_plan.yml"]).hexdigest()
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        receipt["claim_refs"] = ["C001", "C002"]
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_plan_and_matrix_relations_cannot_cross_contaminate_entries(self):
        receipt, values = self._fixture(Path("."))
        values[".research/figure_plan.yml"] = (
            b"figures:\n"
            b"  - id: F001\n"
            b"    claims: [C999]\n"
            b"    sources: [S001]\n"
            b"  - id: F002\n"
            b"    claims: [C001]\n"
            b"    sources: [S001]\n"
        )
        receipt["figure_plan"]["sha256"] = "sha256:" + hashlib.sha256(values[".research/figure_plan.yml"]).hexdigest()
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        values[".research/claim_evidence_matrix.yml"] = (
            b"claims:\n"
            b"  - claim_id: C001\n"
            b"    source_refs: [S999]\n"
            b"  - claim_id: C999\n"
            b"    source_refs: [S001]\n"
        )
        receipt["claim_evidence_matrix"]["sha256"] = "sha256:" + hashlib.sha256(values[".research/claim_evidence_matrix.yml"]).hexdigest()
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_duplicate_claim_binding_is_blocked(self):
        receipt, values = self._fixture(Path("."))
        receipt["claim_bindings"].append({"claim_ref": "C001", "source_refs": ["S001"]})
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_visual_findings_require_resolved_one_to_one_dispositions(self):
        receipt, values = self._fixture(Path("."))
        receipt["visual_review"]["findings"] = [{"id": "V001", "severity": "major", "summary": "axis labels clipped"}]
        receipt["visual_review"]["dispositions"] = [{"finding_id": "V001", "decision": "accepted_nonblocking", "reason": "ignored", "evidence": ["review note"]}]
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
        receipt, values = self._fixture(Path("."))
        receipt["visual_review"]["findings"] = [{"id": "V001", "severity": "minor", "summary": "minor spacing"}]
        receipt["visual_review"]["dispositions"] = [{"finding_id": "V002", "decision": "resolved", "reason": "fixed", "evidence": ["render"]}]
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)
    def test_image_panel_requires_raster_output(self):
        receipt, values = self._fixture(Path("."), figure_kind="image_panel")
        receipt["outputs"] = [{"path": "figure.pdf", "format": "pdf", "sha256": "sha256:" + hashlib.sha256(values["figure.pdf"]).hexdigest()}]
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)

    def test_nan_and_wrong_target_types_block(self):
        receipt, values = self._fixture(Path("."))
        receipt["target"]["width_mm"] = float("nan")
        receipt["target"]["venue"] = []
        receipt["visual_review"]["reviewed_at"] = {"not": "a timestamp"}
        result = self._run(receipt, values)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
