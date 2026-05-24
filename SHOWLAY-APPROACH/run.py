# ruff: noqa: E402
"""
End-to-end driver:

  python run.py <pdf_path> [template_xlsx] [--name <stem>] [--eval-truth <truth_xlsx>]

Outputs (under SHOWLAY-APPROACH/runtime/output/<stem>/):
  - <stem>.xlsx                 final workbook using the template's Assessment sheet
  - <stem>_review.xlsx          companion human-review queue
  - <stem>_review_manifest.json field-level review artifact with page evidence
  - raw_agent_rows.json         raw section-agent rows (for debug)
  - <stem>_telemetry.json       per-page latency + token usage
And under SHOWLAY-APPROACH/eval_reports/<stem>/:
  - summary.json, summary.md    eval vs truth
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

# Load .env from this script's directory before importing boto3-touching modules
from dotenv import load_dotenv

THIS_DIR = Path(__file__).resolve().parent
load_dotenv(THIS_DIR / ".env")

from showlay.agentic import extract_document_agentic
from showlay.confidence import score_rows
from showlay.eval import evaluate
from showlay.extract import probe_and_rasterize
from showlay.field_review import build_review_manifest, write_review_manifest
from showlay.paths import RUNTIME_DIR, default_template_path
from showlay.writer import write_review_sidecar, write_workbook


def main():
    p = argparse.ArgumentParser()
    p.add_argument("pdf", help="path to source PDF")
    p.add_argument("template", nargs="?", default=None, help="optional HIP workbook template path")
    p.add_argument("--name", default=None, help="output stem; defaults to pdf basename")
    p.add_argument("--eval-truth", default=None, help="optional golden workbook for offline eval only")
    p.add_argument("--dpi", type=int, default=200)
    args = p.parse_args()

    pdf_path = str(Path(args.pdf).resolve())
    template_path = str(Path(args.template).resolve()) if args.template else str(default_template_path())
    stem = args.name or Path(pdf_path).stem.replace(" ", "_")[:60]

    out_dir = RUNTIME_DIR / "output" / stem
    img_dir = RUNTIME_DIR / "page_images"
    debug_dir = RUNTIME_DIR / "extracted" / stem
    eval_dir = THIS_DIR / "eval_reports" / stem
    for d in (out_dir, debug_dir, eval_dir):
        d.mkdir(parents=True, exist_ok=True)

    print(f"\n>>> SHOWLAY pipeline:  {Path(pdf_path).name}")
    print(f"    template metadata:  {Path(template_path).name}")

    t0 = time.time()
    # 1+2. probe + rasterize + structural
    print("\n[1/7] probe + rasterize + structural ...")
    doc = probe_and_rasterize(pdf_path, str(img_dir), dpi=args.dpi)
    print(f"      {doc.page_count} page(s), AcroForm={doc.has_acroform}")

    print("\n[2/7] section-aware Bedrock agent ...")

    def progress(stage: str, message: str, done: int | None, total: int | None) -> None:
        suffix = f" ({done}/{total})" if done is not None and total else ""
        print(f"      {stage}: {message}{suffix}")

    result = extract_document_agentic(doc, progress=progress)
    raw = result.raw_rows
    telemetry = result.telemetry
    rows = result.rows
    (debug_dir / "raw_agent_rows.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    (debug_dir / "telemetry.json").write_text(json.dumps(telemetry, indent=2), encoding="utf-8")
    (debug_dir / "profile.json").write_text(
        json.dumps(result.profile.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (debug_dir / "extraction_policy.json").write_text(
        json.dumps(result.extraction_policy.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"      agent produced {len(rows)} normalized rows")

    # 5. confidence
    print("\n[4/7] confidence scoring ...")
    page_text_by_page = {p.page_index + 1: " ".join(t["text"] for t in p.text_blocks)
                         for p in doc.pages}
    rows = score_rows(rows, page_text_by_page)
    n_low = sum(1 for r in rows if r.confidence < 0.7)
    n_mid = sum(1 for r in rows if 0.7 <= r.confidence < 0.9)
    n_high = sum(1 for r in rows if r.confidence >= 0.9)
    print(f"      conf  high(>=0.9)={n_high}  mid={n_mid}  low(<0.7)={n_low}")

    print("\n[5/7] field-level review manifest ...")
    review_manifest = build_review_manifest(
        rows,
        doc_struct=doc,
        raw_vlm=raw,
        telemetry=telemetry,
        run_id=stem,
        source_pdf=pdf_path,
        template_path=template_path,
    )
    review_manifest["canonical"] = result.canonical_meta(doc)
    review_manifest["warnings"] = result.warnings
    review_manifest_path = out_dir / f"{stem}_review_manifest.json"
    write_review_manifest(review_manifest_path, review_manifest)
    summary = review_manifest["summary"]
    print(
        "      fields high={high}  medium={medium}  rows_to_review={rows_to_review}".format(
            high=summary["field_risk_counts"].get("high", 0),
            medium=summary["field_risk_counts"].get("medium", 0),
            rows_to_review=summary["rows_needing_review"],
        )
    )

    # 6. write
    out_xlsx = out_dir / f"{stem}.xlsx"
    review_xlsx = out_dir / f"{stem}_review.xlsx"
    print("\n[6/7] writing workbooks ...")
    write_workbook(template_path, str(out_xlsx), rows)
    write_review_sidecar(str(review_xlsx), rows)
    print(f"      {out_xlsx}")
    print(f"      {review_xlsx}")
    print(f"      {review_manifest_path}")

    # 7. eval
    if args.eval_truth:
        print("\n[7/7] offline eval vs truth ...")
        truth_path = str(Path(args.eval_truth).resolve())
        summary = evaluate(str(out_xlsx), truth_path, str(eval_dir))
        print(f"      candidate={summary['candidate_rows']}  truth={summary['truth_rows']}  "
              f"matched={summary['matched']}")
        print(f"      ROW recall={summary['row_recall']:.1%}  "
              f"precision={summary['row_precision']:.1%}")
        print(f"      OVERALL fuzzy column acc (matched rows): "
              f"{summary['overall_fuzzy_pct']:.1%}")

    elapsed = time.time() - t0
    print(f"\n[done] total {elapsed:.1f}s   -> output: {out_dir}")


if __name__ == "__main__":
    main()
