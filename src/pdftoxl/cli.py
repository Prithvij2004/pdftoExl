"""`pdftoxl` CLI — runs PipelineV1 end-to-end on a fixture."""
from __future__ import annotations

from pathlib import Path

import typer

from .adapters.env import load_env
from .adapters.logging import configure_logging
from .evals.fixtures import find_fixture, load_fixtures
from .pipeline_v1 import load_config
from .pipeline_v1.pipeline import PipelineContext, build_pipeline

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _root() -> None:
    """pdftoxl pipeline CLI."""


@app.command("run")
def run(
    fixture: str = typer.Option(..., "--fixture", help="Fixture ID (see evals/fixtures.yaml)."),
    out: Path = typer.Option(..., "--out", help="Path for the generated .xlsx."),
    config: Path | None = typer.Option(None, "--config", help="Pipeline config YAML."),
    fixtures_yaml: Path | None = typer.Option(None, "--fixtures-yaml"),
    with_llm: bool = typer.Option(False, "--with-llm", help="Enable Bedrock LLM stage."),
) -> None:
    load_env()
    cfg = load_config(config)
    configure_logging(level=cfg.logging.level, renderer=cfg.logging.renderer)

    fx_yaml = fixtures_yaml or cfg.paths.fixtures_yaml
    fixtures = load_fixtures(fx_yaml)
    fx = find_fixture(fixtures, fixture)

    ctx = PipelineContext(
        out_path=out,
        template_xlsx=fx.golden_xlsx_path,
        question_sheet=fx.question_sheet,
        header_row=fx.header_row,
    )
    pipeline = build_pipeline(cfg, ctx, with_llm=with_llm)
    written = pipeline(fx.pdf_path)
    typer.echo(str(written))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
