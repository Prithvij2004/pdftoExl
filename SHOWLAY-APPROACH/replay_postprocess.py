"""
Replay script: re-runs the post-process / write / evaluate pipeline starting from
the cached `raw_vlm.json` so we can iterate on postprocess.py without paying the
~20 minute Bedrock VLM bill again.

Usage:
  python replay_postprocess.py            # runs both TXLTSS and CHOICES
  python replay_postprocess.py TXLTSS     # one only

Reports per-column accuracy (focus: question_type) before vs after this run.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent

from showlay.extract import vlm_dicts_to_rows
from showlay.postprocess import run_all as postprocess_all
from showlay.paths import (
    CHOICES_TEMPLATE_FILENAME,
    TXLTSS_TEMPLATE_FILENAME,
    support_doc_path,
)
from showlay.writer import write_workbook
from showlay.eval import evaluate


CASES = {
    "TXLTSS": {
        "truth": support_doc_path(TXLTSS_TEMPLATE_FILENAME, must_exist=False),
    },
    "CHOICES": {
        "truth": support_doc_path(CHOICES_TEMPLATE_FILENAME, must_exist=False),
    },
}


def replay(stem: str) -> dict:
    cfg = CASES[stem]
    raw_path = THIS_DIR / "runtime" / "extracted" / stem / "raw_vlm.json"
    out_dir = THIS_DIR / "runtime" / "output" / stem
    eval_dir = THIS_DIR / "eval_reports" / stem
    out_xlsx = out_dir / f"{stem}.xlsx"
    out_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    # raw_vlm.json was written with default open() on Windows -> cp1252 for TXLTSS,
    # but the CHOICES file contains UTF-8 bytes (smart quotes etc.) that don't decode
    # cleanly under cp1252. Try cp1252 first per the spec, fall back to utf-8.
    try:
        raw = json.loads(raw_path.read_text(encoding="cp1252"))
    except UnicodeDecodeError:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))

    rows = vlm_dicts_to_rows(raw)
    rows = postprocess_all(rows)

    truth_path = str(cfg["truth"])
    write_workbook(truth_path, str(out_xlsx), rows)
    summary = evaluate(str(out_xlsx), truth_path, str(eval_dir))
    return summary


def fmt(summary: dict) -> str:
    pc = summary["per_col"]
    lines = [
        f"  candidate={summary['candidate_rows']}  truth={summary['truth_rows']}  "
        f"matched={summary['matched']}",
        f"  row recall={summary['row_recall']:.1%}  precision={summary['row_precision']:.1%}",
        f"  overall fuzzy = {summary['overall_fuzzy_pct']:.1%}",
        "  per-column fuzzy %:",
    ]
    for f, v in pc.items():
        lines.append(f"    {f:<18} n={v['n']:>3}  exact={v['exact_pct']:.1%}  fuzzy={v['fuzzy_pct']:.1%}")
    return "\n".join(lines)


def main():
    stems = sys.argv[1:] or ["TXLTSS", "CHOICES"]
    for stem in stems:
        print(f"\n=== {stem} ===")
        s = replay(stem)
        print(fmt(s))


if __name__ == "__main__":
    main()
