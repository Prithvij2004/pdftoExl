from __future__ import annotations

import pytest

from evals.utils import (
    docs_available,
    iter_eval_cases,
    load_golden_rows_from_path,
    UnsupportedGoldenWorkbook,
    score_rows,
)


@pytest.mark.skipif(not docs_available(), reason="Local docs/ fixtures not present (docs/ is gitignored).")
@pytest.mark.parametrize("case", iter_eval_cases(), ids=lambda case: case.case_id)
def test_golden_workbooks_have_expected_shape(case) -> None:
    assert case.expected_xlsx_path.exists(), f"Missing expected workbook: {case.expected_xlsx_path}"
    try:
        rows = load_golden_rows_from_path(case.expected_xlsx_path)
    except UnsupportedGoldenWorkbook as e:
        pytest.skip(str(e))
    assert len(rows) > 0, f"{case.expected_xlsx_path} contained no usable golden rows"


def test_scoring_is_sane_on_identical_rows() -> None:
    expected = [
        ("Text Box", "Name", ""),
        ("Calendar", "Date of birth", "[Date input]"),
        ("Display", "Instructions", ""),
    ]
    actual = list(expected)
    score, matches, extra = score_rows(expected, actual)
    assert score.coverage == 1.0
    assert score.type_accuracy == 1.0
    assert score.avg_answer_similarity == 1.0
    assert score.missing_rows == 0
    assert score.extra_rows == 0
    assert all(m.actual is not None for m in matches)
    assert extra == []

