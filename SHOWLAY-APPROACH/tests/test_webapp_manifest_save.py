from __future__ import annotations

import json
import sys
from pathlib import Path

SHOWLAY_DIR = Path(__file__).resolve().parents[1]
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


def test_save_manifest_writes_normalized_json_and_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_DIR", tmp_path)
    template = tmp_path / "template.xlsx"
    monkeypatch.setattr(webapp, "DEFAULT_TEMPLATE", template)
    calls = {}

    def fake_write_workbook(template_path, out_path, rows):
        calls["workbook"] = (template_path, out_path, rows)
        Path(out_path).write_text("workbook", encoding="utf-8")
        return out_path

    def fake_write_review_sidecar(out_path, rows):
        calls["review_workbook"] = (out_path, rows)
        Path(out_path).write_text("review", encoding="utf-8")
        return out_path

    monkeypatch.setattr(webapp, "write_workbook", fake_write_workbook)
    monkeypatch.setattr(webapp, "write_review_sidecar", fake_write_review_sidecar)

    path = tmp_path / "abc123_review_manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    saved = webapp._save_manifest("abc123", _manifest())
    on_disk = json.loads(path.read_text(encoding="utf-8"))

    assert saved["summary"]["row_count"] == 1
    assert on_disk["rows"][0]["row_index"] == 0
    assert on_disk["rows"][0]["row"]["question_type"] == "Text Box"
    assert saved["outputs"]["workbook"] == str(tmp_path / "abc123.xlsx")
    assert saved["outputs"]["review_workbook"] == str(tmp_path / "abc123_review.xlsx")
    assert saved["outputs"]["updated_at"] == saved["updated_at"]
    assert calls["workbook"][0] == str(template)
    assert calls["workbook"][1] == str(tmp_path / "abc123.xlsx")
    assert calls["workbook"][2][0].question_text == "Applicant Name"
    assert calls["review_workbook"][0] == str(tmp_path / "abc123_review.xlsx")


def test_workbench_view_has_export_and_edit_route(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_DIR", tmp_path)
    (tmp_path / "abc123_review_manifest.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )

    view = webapp.workbench("abc123")
    view_html = view.body.decode()
    assert 'id="exportWorkbook"' in view_html
    assert 'href="/workbench/abc123/edit"' in view_html
    assert 'id="pageSelect"' in view_html
    assert "function changePage(delta)" in view_html
    assert "function rowPage(row)" in view_html
    assert "JSON output" in view_html

    editor = webapp.workbench_edit("abc123")
    editor_html = editor.body.decode()
    assert "Review details" in editor_html
    assert "saved=1" in editor_html


def test_download_serves_current_workbook_without_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(webapp, "JOBS", {})
    (tmp_path / "abc123.xlsx").write_bytes(b"updated workbook")

    response = webapp.download("abc123")

    assert Path(response.path) == tmp_path / "abc123.xlsx"
    assert response.headers["cache-control"] == "no-store"
