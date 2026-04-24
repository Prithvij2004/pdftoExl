from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .contracts import EvalResult, FixtureManifest
from .fixtures import load_fixtures
from .metrics.eval_b import load_and_evaluate
from .metrics.eval_c import run_eval_c
from .report import write_report


def default_fixtures_yaml() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "fixtures.yaml"


def default_reports_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "reports"


def run_eval_b(
    fixture: FixtureManifest,
    candidate_xlsx: Path | None = None,
) -> EvalResult:
    candidate = candidate_xlsx or fixture.golden_xlsx_path
    result, _metrics = load_and_evaluate(
        candidate_path=candidate,
        reference_path=fixture.reference_xlsx_path,
        question_sheet=fixture.question_sheet,
        header_row=fixture.header_row,
        fixture_id=fixture.id,
    )
    return result


def run_all(
    eval_name: str,
    fixtures_yaml: Path | None = None,
    reports_dir: Path | None = None,
    candidate_xlsx: Path | None = None,
    pipeline: Callable[[Path], Path] | None = None,
) -> list[EvalResult]:
    fixtures = load_fixtures(fixtures_yaml or default_fixtures_yaml())
    out_dir = reports_dir or default_reports_dir()
    results: list[EvalResult] = []
    for f in fixtures:
        if eval_name == "B":
            r = run_eval_b(f, candidate_xlsx=candidate_xlsx)
        elif eval_name == "C":
            if pipeline is None:
                from ..pipeline import make_null_pipeline

                pipeline = make_null_pipeline(f.golden_xlsx_path)
            r = run_eval_c(f, pipeline)
            pipeline = None
        else:
            raise ValueError(f"Unsupported eval: {eval_name}")
        write_report(r, out_dir)
        results.append(r)
    return results
