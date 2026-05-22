"""
Head-to-head: run the legacy `agentic-pydantic-ai` pipeline on each PDF, then run our eval
against the same truth files we use for SHOWLAY. Produces a side-by-side comparison table.

The agentic branch ships with `BEDROCK_MODEL_ID=us.amazon.nova-pro-v1:0` as its tested model
because its Pydantic-AI agent passes raw PDF bytes via Bedrock's `document` block — which
Qwen3-VL does NOT support. We force Nova-Pro for a fair comparison.
"""
from __future__ import annotations
import asyncio, json, os, sys, time
from pathlib import Path

THIS = Path(__file__).resolve().parent
AGENTIC = Path(
    os.environ.get("AGENTIC_BRANCH_DIR", THIS / "external" / "agentic-pydantic-ai")
).expanduser()

# Credentials come from .env / process environment — do NOT hardcode.
# Force Nova-Pro for the agentic branch (it uses Bedrock document block, which
# Qwen3-VL doesn't support).
from dotenv import load_dotenv
load_dotenv(THIS / ".env")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["BEDROCK_MODEL_ID"] = "us.amazon.nova-pro-v1:0"

from showlay.eval import evaluate
from showlay.paths import (
    CHOICES_PDF_FILENAME,
    CHOICES_TEMPLATE_FILENAME,
    TXLTSS_PDF_FILENAME,
    TXLTSS_TEMPLATE_FILENAME,
    support_doc_path,
)


PAIRS = [
    {
        "name": "TXLTSS",
        "pdf": support_doc_path(TXLTSS_PDF_FILENAME, must_exist=False),
        "truth": support_doc_path(TXLTSS_TEMPLATE_FILENAME, must_exist=False),
    },
    {
        "name": "CHOICES",
        "pdf": support_doc_path(CHOICES_PDF_FILENAME, must_exist=False),
        "truth": support_doc_path(CHOICES_TEMPLATE_FILENAME, must_exist=False),
    },
]


def _load_agentic_services():
    if not AGENTIC.exists():
        raise SystemExit(
            "Missing agentic branch checkout. Set AGENTIC_BRANCH_DIR to the "
            "agentic-pydantic-ai checkout if you want to run this comparison."
        )

    sys.path.insert(0, str(AGENTIC))

    # app/config.py calls load_dotenv(".env"). Env vars above stay in place because
    # load_dotenv defaults to override=False.
    from app.services.agentic_extractor import extract_rows_from_pdf_agentic
    from app.services.excel_writer import write_rows_to_xlsx
    from app.services.normalize import assign_sequence, normalize_rows, resolve_branching_logic
    from app.services.semantic_pass_agent import llm_semantic_pass

    return (
        extract_rows_from_pdf_agentic,
        write_rows_to_xlsx,
        assign_sequence,
        normalize_rows,
        resolve_branching_logic,
        llm_semantic_pass,
    )


async def run_agentic(pdf_path: Path, out_xlsx: Path, services):
    (
        extract_rows_from_pdf_agentic,
        write_rows_to_xlsx,
        assign_sequence,
        normalize_rows,
        resolve_branching_logic,
        llm_semantic_pass,
    ) = services
    t0 = time.time()
    rows = await extract_rows_from_pdf_agentic(pdf_path)
    rows = normalize_rows(rows)
    rows = await llm_semantic_pass(rows)
    rows = assign_sequence(rows)
    rows = resolve_branching_logic(rows)
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    write_rows_to_xlsx(rows, out_xlsx)
    return time.time() - t0, len(rows)


def main():
    services = _load_agentic_services()
    out_dir = THIS / "runtime" / "output_agentic"
    out_dir.mkdir(parents=True, exist_ok=True)
    eval_dir = THIS / "eval_reports"

    summaries = {}

    for case in PAIRS:
        name = case["name"]
        pdf = case["pdf"]
        truth = case["truth"]
        print(f"\n>>> AGENTIC pipeline: {name}  ({pdf.name})")
        out_xlsx = out_dir / f"{name}.xlsx"
        try:
            elapsed, n_rows = asyncio.run(run_agentic(pdf, out_xlsx, services))
            print(f"    {n_rows} rows in {elapsed:.1f}s")
            ev_dir = eval_dir / f"{name}_agentic"
            summary = evaluate(str(out_xlsx), str(truth), str(ev_dir))
            print(f"    cand={summary['candidate_rows']} truth={summary['truth_rows']} "
                  f"matched={summary['matched']}")
            print(f"    ROW recall={summary['row_recall']:.1%}  "
                  f"precision={summary['row_precision']:.1%}")
            print(f"    OVERALL fuzzy column acc: {summary['overall_fuzzy_pct']:.1%}")
            summaries[name] = {**summary, "elapsed_s": round(elapsed, 1)}
        except Exception as e:
            print(f"    [ERROR] {type(e).__name__}: {e}")
            summaries[name] = {"error": f"{type(e).__name__}: {e}"}

    # Now collect SHOWLAY summaries from earlier runs
    showlay_summaries = {}
    for case in PAIRS:
        sj = THIS / "eval_reports" / case["name"] / "summary.json"
        if sj.exists():
            showlay_summaries[case["name"]] = json.loads(sj.read_text())

    # Comparison table
    print("\n" + "=" * 78)
    print("HEAD-TO-HEAD COMPARISON")
    print("=" * 78)
    print(f"{'Form':<10}{'Pipeline':<14}{'Cand':>6}{'Truth':>7}{'Match':>7}"
          f"{'Recall':>10}{'Prec':>10}{'Col Acc':>10}")
    print("-" * 78)
    for case in PAIRS:
        name = case["name"]
        for label, src in [("SHOWLAY", showlay_summaries.get(name)),
                           ("agentic", summaries.get(name))]:
            if not src:
                print(f"{name:<10}{label:<14} (no data)")
                continue
            if "error" in src:
                print(f"{name:<10}{label:<14} ERROR: {src['error'][:60]}")
                continue
            print(f"{name:<10}{label:<14}"
                  f"{src['candidate_rows']:>6}{src['truth_rows']:>7}{src['matched']:>7}"
                  f"{src['row_recall']:>10.1%}{src['row_precision']:>10.1%}"
                  f"{src['overall_fuzzy_pct']:>10.1%}")
    print("=" * 78)

    # Save comparison json
    out = {"showlay": showlay_summaries, "agentic": summaries}
    (eval_dir / "comparison.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved: {eval_dir / 'comparison.json'}")


if __name__ == "__main__":
    main()
