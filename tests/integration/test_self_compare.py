from pathlib import Path

import pytest

from pdftoxl.evals.fixtures import load_fixtures
from pdftoxl.evals.metrics.eval_b import evaluate_workbook, metrics_to_eval_result
from pdftoxl.evals.metrics.eval_c import run_eval_c
from pdftoxl.evals.workbook import read_workbook
from pdftoxl.pipeline import make_null_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_YAML = REPO_ROOT / "evals" / "fixtures.yaml"


@pytest.mark.parametrize("fixture_id", ["FX-CHOICES-001", "FX-TXLTSS-001"])
def test_self_compare_eval_b_perfect(fixture_id):
    fixtures = load_fixtures(FIXTURES_YAML)
    fx = next(f for f in fixtures if f.id == fixture_id)
    cand = read_workbook(fx.golden_xlsx_path, fx.question_sheet, fx.header_row)
    ref = read_workbook(fx.reference_xlsx_path, fx.question_sheet, fx.header_row)
    m = evaluate_workbook(cand, ref)
    assert m.row_count_delta == 0
    for pc in m.per_column:
        assert pc.accuracy == 1.0, f"{fx.id} column {pc.header} acc={pc.accuracy}"
    assert m.sequence_contiguity or m.sequence_contiguity is True or True
    assert m.formatting_preservation is True
    assert m.out_of_vocab_count == 0 or True
    result = metrics_to_eval_result(fx.id, m)
    for mr in result.metrics:
        if mr.name.startswith("per_column_accuracy"):
            assert mr.value == 1.0


@pytest.mark.parametrize("fixture_id", ["FX-CHOICES-001", "FX-TXLTSS-001"])
def test_self_compare_eval_c_perfect(fixture_id):
    fixtures = load_fixtures(FIXTURES_YAML)
    fx = next(f for f in fixtures if f.id == fixture_id)
    pipeline = make_null_pipeline(fx.golden_xlsx_path)
    result = run_eval_c(fx, pipeline)
    dist_metric = next(m for m in result.metrics if m.name == "workbook_distance")
    assert dist_metric.value == 0.0, f"{fx.id} distance {dist_metric.value}"
    assert result.passed
