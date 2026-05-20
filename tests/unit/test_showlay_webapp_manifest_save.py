from __future__ import annotations

import json
import sys
from pathlib import Path

SHOWLAY_DIR = Path(__file__).resolve().parents[2] / "SHOWLAY-APPROACH"
sys.path.insert(0, str(SHOWLAY_DIR))

import webapp  # noqa: E402


def _manifest() -> dict:
    return {
        "schema_version": "showlay.field_review.v1",
        "run_id": "test-job",
        "summary": {},
        "pages": [{"page": 1, "width": 612, "height": 792, "image_path": "/tmp/p1.png"}],
        "rows": [
            {
                "row_id": "row_0001",
                "row_index": 0,
                "sequence": 1,
                "page": 1,
                "row_confidence": 1,
                "risk_level": "low",
                "fields": {
                    "sequence": {"value": 1, "risk_level": "low", "review_reasons": []},
                    "question_type": {"value": "Text Box", "risk_level": "low", "review_reasons": []},
                    "question_text": {
                        "value": "Applicant Name",
                        "risk_level": "medium",
                        "review_reasons": ["manual_review"],
                    },
                    "branching_logic": {"value": "", "risk_level": "low", "review_reasons": []},
                    "answer_text": {"value": "", "risk_level": "low", "review_reasons": []},
                    "answer_validation": {"value": "", "risk_level": "low", "review_reasons": []},
                    "section": {"value": "", "risk_level": "low", "review_reasons": []},
                    "required": {"value": "", "risk_level": "low", "review_reasons": []},
                },
                "row": {},
                "source": {},
                "suggested_action": "Reviewer edited this row",
            }
        ],
    }


def test_normalize_saved_manifest_syncs_row_values_and_summary():
    manifest = webapp._normalize_saved_manifest(_manifest())

    row = manifest["rows"][0]
    assert row["row"]["question_text"] == "Applicant Name"
    assert row["risk_level"] == "medium"
    assert manifest["summary"]["row_count"] == 1
    assert manifest["summary"]["rows_needing_review"] == 1
    assert manifest["summary"]["field_risk_counts"]["medium"] == 1
    assert manifest["summary"]["review_reason_counts"]["manual_review"] == 1
    assert manifest["updated_at"]


def test_save_manifest_writes_normalized_json(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_DIR", tmp_path)
    path = tmp_path / "abc123_review_manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    saved = webapp._save_manifest("abc123", _manifest())
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert saved["summary"]["row_count"] == 1
    assert on_disk["rows"][0]["row_index"] == 0
    assert on_disk["rows"][0]["row"]["question_type"] == "Text Box"
