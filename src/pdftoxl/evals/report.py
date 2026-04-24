from __future__ import annotations

import json
from pathlib import Path

from .contracts import EvalResult


def write_json_report(result: EvalResult, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.fixture_id}.{result.eval_name}.json"
    path.write_text(result.model_dump_json(indent=2))
    return path


def write_markdown_report(result: EvalResult, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.fixture_id}.{result.eval_name}.md"
    lines: list[str] = []
    lines.append(f"# Eval {result.eval_name} - {result.fixture_id}")
    lines.append("")
    lines.append(f"- Passed: {result.passed}")
    if result.notes:
        lines.append(f"- Notes: {result.notes}")
    lines.append("")
    lines.append("| Metric | Value | Passed | Details |")
    lines.append("|---|---|---|---|")
    for m in result.metrics:
        value = m.value
        if isinstance(value, float):
            value_str = f"{value:.4f}"
        else:
            value_str = str(value)
        details = json.dumps(m.details) if m.details else ""
        details = details.replace("|", "\\|")
        lines.append(f"| {m.name} | {value_str} | {m.passed} | {details} |")
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def write_report(result: EvalResult, out_dir: Path) -> tuple[Path, Path]:
    return write_json_report(result, out_dir), write_markdown_report(result, out_dir)
