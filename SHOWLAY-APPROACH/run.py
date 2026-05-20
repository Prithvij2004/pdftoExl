# ruff: noqa: E402
"""
End-to-end driver:

  python run.py <pdf_path> <truth_xlsx> [--name <stem>] [--no-eval]

Outputs (under SHOWLAY-APPROACH/runtime/output/<stem>/):
  - <stem>.xlsx                 final 28-column workbook (template-cloned)
  - <stem>_review.xlsx          companion human-review queue
  - <stem>_review_manifest.json field-level review artifact with page evidence
  - <stem>_raw_vlm.json         raw per-page VLM JSON (for debug)
  - <stem>_telemetry.json       per-page latency + token usage
And under SHOWLAY-APPROACH/eval_reports/<stem>/:
  - summary.json, summary.md    eval vs truth
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# Load .env from this script's directory before importing boto3-touching modules
from dotenv import load_dotenv

THIS_DIR = Path(__file__).resolve().parent
load_dotenv(THIS_DIR / ".env")

from showlay.confidence import score_rows
from showlay.eval import evaluate
from showlay.extract import extract_document, probe_and_rasterize, resolve_row_source_bboxes, vlm_dicts_to_rows
from showlay.field_review import build_review_manifest, write_review_manifest
from showlay.postprocess import run_all as postprocess_all
from showlay.writer import write_review_sidecar, write_workbook


def main():
    p = argparse.ArgumentParser()
    p.add_argument("pdf", help="path to source PDF")
    p.add_argument("truth", help="path to truth Excel (used as TEMPLATE for output AND for eval)")
    p.add_argument("--name", default=None, help="output stem; defaults to pdf basename")
    p.add_argument("--no-eval", action="store_true")
    p.add_argument("--dpi", type=int, default=200)
    args = p.parse_args()

    pdf_path = str(Path(args.pdf).resolve())
    truth_path = str(Path(args.truth).resolve())
    stem = args.name or Path(pdf_path).stem.replace(" ", "_")[:60]

    out_dir = THIS_DIR / "runtime" / "output" / stem
    img_dir = THIS_DIR / "runtime" / "page_images"
    debug_dir = THIS_DIR / "runtime" / "extracted" / stem
    eval_dir = THIS_DIR / "eval_reports" / stem
    for d in (out_dir, debug_dir, eval_dir):
        d.mkdir(parents=True, exist_ok=True)

    print(f"\n>>> SHOWLAY pipeline:  {Path(pdf_path).name}")
    print(f"    truth template:     {Path(truth_path).name}")

    t0 = time.time()
    # 1+2. probe + rasterize + structural
    print("\n[1/7] probe + rasterize + structural ...")
    doc = probe_and_rasterize(pdf_path, str(img_dir), dpi=args.dpi)
    print(f"      {doc.page_count} page(s), AcroForm={doc.has_acroform}")

    # 3. VLM extract
    print("\n[2/7] Qwen3-VL extraction ...")
    model_id = os.environ.get("BEDROCK_VLM_MODEL_ID", "qwen.qwen3-vl-235b-a22b")
    raw, telemetry = extract_document(doc, model_id=model_id, verbose=True)
    (debug_dir / "raw_vlm.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    (debug_dir / "telemetry.json").write_text(json.dumps(telemetry, indent=2), encoding="utf-8")

    rows = vlm_dicts_to_rows(raw, doc_struct=doc)
    print(f"      VLM produced {len(rows)} row dicts")

    # 4. post-process
    print("\n[3/7] post-process (sequence, sections, branching) ...")
    rows = postprocess_all(rows, doc_struct=doc, truth_path=truth_path)
    rows = resolve_row_source_bboxes(rows, doc)
    print(f"      after post-process: {len(rows)} rows")

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
        template_path=truth_path,
    )
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
    write_workbook(truth_path, str(out_xlsx), rows)
    write_review_sidecar(str(review_xlsx), rows)
    print(f"      {out_xlsx}")
    print(f"      {review_xlsx}")
    print(f"      {review_manifest_path}")

    # 7. eval
    if not args.no_eval:
        print("\n[7/7] eval vs truth ...")
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
