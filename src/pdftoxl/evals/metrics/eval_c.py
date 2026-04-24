from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..contracts import EvalResult, FixtureManifest, MetricResult
from ..workbook import read_workbook
from .eval_b import evaluate_workbook

COLUMN_WEIGHTS = {"critical": 5, "high": 3, "medium": 1, "low": 0, "unclassified": 0}

DISTANCE_THRESHOLD = 0.05


def compute_workbook_distance(metrics) -> tuple[float, dict[str, float]]:
    num = 0.0
    denom = 0.0
    contributions: dict[str, float] = {}
    for pc in metrics.per_column:
        w = COLUMN_WEIGHTS.get(pc.priority, 0)
        if w == 0:
            continue
        contrib = w * (1.0 - pc.accuracy)
        num += contrib
        denom += w
        contributions[pc.header] = contrib
    base = num / denom if denom else 0.0
    penalties = 0.0
    oov_penalty = min(0.10 * metrics.out_of_vocab_count, 0.30)
    penalties += oov_penalty
    if not metrics.sequence_contiguity:
        penalties += 0.10
    if not metrics.formatting_preservation:
        penalties += 0.20
    distance = base + penalties
    return distance, {
        "base": base,
        "penalties": penalties,
        "oov_penalty": oov_penalty,
    }


def run_eval_c(
    fixture: FixtureManifest,
    pipeline: Callable[[Path], Path],
) -> EvalResult:
    candidate_path = Path(pipeline(fixture.pdf_path))
    cand = read_workbook(candidate_path, fixture.question_sheet, fixture.header_row)
    ref = read_workbook(fixture.reference_xlsx_path, fixture.question_sheet, fixture.header_row)
    m = evaluate_workbook(cand, ref)
    distance, parts = compute_workbook_distance(m)
    passed = distance <= DISTANCE_THRESHOLD
    metrics = [
        MetricResult(
            name="workbook_distance",
            value=distance,
            details=parts,
            passed=passed,
        ),
        MetricResult(
            name="row_count_delta",
            value=m.row_count_delta,
            passed=abs(m.row_count_delta) <= 2,
        ),
    ]
    return EvalResult(
        fixture_id=fixture.id,
        eval_name="C",
        metrics=metrics,
        passed=passed,
    )
