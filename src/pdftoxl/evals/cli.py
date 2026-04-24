from __future__ import annotations

from pathlib import Path

import typer

from .fixtures import find_fixture, load_fixtures
from .metrics.eval_c import run_eval_c
from .report import write_report
from .runner import default_fixtures_yaml, default_reports_dir, run_all, run_eval_b

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _root() -> None:
    """pdftoxl evals harness."""


@app.command("run")
def run(
    eval: str = typer.Option(..., "--eval", help="Eval to run: B or C."),
    fixture: str | None = typer.Option(None, "--fixture", help="Fixture ID."),
    all_fixtures: bool = typer.Option(False, "--all", help="Run over all fixtures."),
    candidate: Path | None = typer.Option(None, "--candidate", help="Candidate xlsx path (Eval B only)."),
    fixtures_yaml: Path | None = typer.Option(None, "--fixtures-yaml"),
    reports_dir: Path | None = typer.Option(None, "--reports-dir"),
) -> None:
    eval = eval.upper()
    if eval not in {"B", "C"}:
        raise typer.BadParameter("Only Eval B and C are implemented here.")
    if not fixture and not all_fixtures:
        raise typer.BadParameter("Pass --fixture <ID> or --all.")
    if all_fixtures:
        results = run_all(
            eval_name=eval,
            fixtures_yaml=fixtures_yaml,
            reports_dir=reports_dir,
            candidate_xlsx=candidate,
        )
    else:
        fxs = load_fixtures(fixtures_yaml or default_fixtures_yaml())
        fx = find_fixture(fxs, fixture)
        out_dir = reports_dir or default_reports_dir()
        if eval == "B":
            r = run_eval_b(fx, candidate_xlsx=candidate)
        else:
            from ..pipeline import make_null_pipeline

            r = run_eval_c(fx, make_null_pipeline(fx.golden_xlsx_path))
        write_report(r, out_dir)
        results = [r]

    any_failed = False
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        typer.echo(f"[{status}] Eval {r.eval_name} {r.fixture_id}")
        if not r.passed:
            any_failed = True
    raise typer.Exit(code=1 if any_failed else 0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
