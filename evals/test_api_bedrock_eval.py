from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from evals.utils import (
    EXPECTED_HEADERS,
    docs_available,
    iter_eval_cases,
    load_golden_rows_from_path,
    load_xlsx_rows_from_bytes,
    now_s,
    require_env_flag,
    score_rows,
    UnsupportedGoldenWorkbook,
)


pytestmark = pytest.mark.bedrock


@pytest.mark.skipif(not docs_available(), reason="Local docs/ fixtures not present (docs/ is gitignored).")
@pytest.mark.parametrize("case", iter_eval_cases(), ids=lambda case: case.case_id)
def test_extract_endpoint_matches_golden_workbooks_quality_and_perf(case) -> None:
    if not require_env_flag("RUN_BEDROCK_EVAL"):
        pytest.skip("Set RUN_BEDROCK_EVAL=1 to run end-to-end Bedrock/API evals.")

    min_coverage = 0.80
    min_type_accuracy = 0.85
    min_answer_similarity = 0.75

    with TestClient(app) as client:
        if not case.pdf_path.exists():
            pytest.skip(f"Missing PDF fixture: {case.pdf_path}")
        if not case.expected_xlsx_path.exists():
            pytest.skip(f"Missing expected xlsx fixture: {case.expected_xlsx_path}")

        try:
            expected_rows = load_golden_rows_from_path(case.expected_xlsx_path)
        except UnsupportedGoldenWorkbook as e:
            pytest.skip(str(e))

        t0 = now_s()
        with case.pdf_path.open("rb") as f:
            resp = client.post(
                "/extract",
                files={"file": (case.pdf_path.name, f, "application/pdf")},
            )
        elapsed = now_s() - t0

        assert resp.status_code == 200, f"{case.case_id}: status={resp.status_code}, body={resp.text[:500]}"
        ct = (resp.headers.get("content-type") or "").lower()
        assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in ct, f"{case.case_id}: content-type={ct}"

        actual_header, actual_rows = load_xlsx_rows_from_bytes(resp.content)
        assert actual_header == EXPECTED_HEADERS, f"{case.case_id}: returned headers were {actual_header}"

        score, matches, extras = score_rows(expected_rows, actual_rows)
        score.elapsed_s = elapsed

        rps = score.rows_per_second()
        summary = (
            f"{case.case_id}: elapsed={elapsed:.2f}s, "
            f"actual_rows={score.actual_rows}, expected_rows={score.expected_rows}, "
            f"coverage={score.coverage:.3f}, type_acc={score.type_accuracy:.3f}, ans_sim={score.avg_answer_similarity:.3f}"
        )
        if rps is not None:
            summary += f", rows/s={rps:.2f}"
        print(summary)

        missing_examples = [m for m in matches if m.actual is None][:8]
        extra_examples = extras[:8]

        assert score.coverage >= min_coverage, (
            f"{case.case_id}: coverage {score.coverage:.3f} < {min_coverage}. "
            f"Missing {score.missing_rows} / {score.expected_rows}. "
            f"Missing_examples={[(m.expected[0], m.expected[1][:80]) for m in missing_examples]}"
        )
        assert score.type_accuracy >= min_type_accuracy, (
            f"{case.case_id}: type accuracy {score.type_accuracy:.3f} < {min_type_accuracy}"
        )
        assert score.avg_answer_similarity >= min_answer_similarity, (
            f"{case.case_id}: answer similarity {score.avg_answer_similarity:.3f} < {min_answer_similarity}"
        )

        assert score.extra_rows <= max(50, int(0.25 * score.expected_rows)), (
            f"{case.case_id}: too many extra rows ({score.extra_rows}). "
            f"Extra_examples={[(r[0], r[1][:80]) for r in extra_examples]}"
        )

