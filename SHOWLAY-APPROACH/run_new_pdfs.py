"""Run SHOWLAY pipeline on the 3 new PDFs. No truth ground-truth available, so:
  - clone the CHOICES truth as the default 28-col template
  - skip eval
  - produce both <stem>.xlsx and <stem>_review.xlsx
"""
from __future__ import annotations
import json, os, time
from pathlib import Path
from dotenv import load_dotenv

THIS = Path(__file__).resolve().parent
load_dotenv(THIS / ".env")

from showlay.extract import probe_and_rasterize, extract_document, resolve_row_source_bboxes, vlm_dicts_to_rows
from showlay.postprocess import run_all
from showlay.confidence import score_rows
from showlay.writer import write_workbook, write_review_sidecar

ROOT = THIS.parent
TEMPLATE = ROOT / "SOURCE AND TARGET FILES" / "CHOICES Safety Determination Request Form Final_11_20 1.xlsx"
SOURCE_DIR = ROOT / "new pdf files"
OUT_DIR = THIS / "runtime" / "output"

PDFS = [
    ("HCBS_Applicant_Tool", "HCBSApplicantTool.pdf 7.28.17 Fillable.pdf"),
    ("MNLOC", "MNLOC Blank (1).pdf"),
    ("Multiple_Complex_Health_Conditions", "Multiple Complex Health Conditions  Form -FINAL rev 12.1.16_Fillable (1).pdf"),
]

img_dir = THIS / "runtime" / "page_images"

for stem, fname in PDFS:
    pdf = SOURCE_DIR / fname
    print(f"\n{'='*70}")
    print(f">>> {stem}  ({pdf.name})")
    print('='*70)

    out_dir = OUT_DIR / stem
    debug_dir = THIS / "runtime" / "extracted" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"[1/5] probe + rasterize ...")
    doc = probe_and_rasterize(str(pdf), str(img_dir), dpi=200)
    print(f"      {doc.page_count} page(s), AcroForm={doc.has_acroform}")

    print(f"[2/5] Qwen3-VL extraction ({doc.page_count} pages, ~30s/page expected) ...")
    model_id = os.environ.get("BEDROCK_VLM_MODEL_ID", "qwen.qwen3-vl-235b-a22b")
    raw, telemetry = extract_document(doc, model_id=model_id, verbose=True)
    (debug_dir / "raw_vlm.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    (debug_dir / "telemetry.json").write_text(json.dumps(telemetry, indent=2), encoding="utf-8")

    rows = vlm_dicts_to_rows(raw, doc_struct=doc)
    print(f"      VLM produced {len(rows)} raw rows")

    print(f"[3/5] post-process ...")
    rows = run_all(rows, doc_struct=doc, truth_path=str(TEMPLATE))
    rows = resolve_row_source_bboxes(rows, doc)
    print(f"      after post-process: {len(rows)} rows")

    print(f"[4/5] confidence ...")
    page_text_by_page = {p.page_index + 1: " ".join(t["text"] for t in p.text_blocks)
                         for p in doc.pages}
    rows = score_rows(rows, page_text_by_page)
    n_high = sum(1 for r in rows if r.confidence >= 0.9)
    n_low = sum(1 for r in rows if r.confidence < 0.7)
    print(f"      conf high>=0.9: {n_high}  low<0.7: {n_low}")

    print(f"[5/5] write workbooks ...")
    out_xlsx = out_dir / f"{stem}.xlsx"
    review_xlsx = out_dir / f"{stem}_review.xlsx"
    write_workbook(str(TEMPLATE), str(out_xlsx), rows)
    write_review_sidecar(str(review_xlsx), rows)
    print(f"      {out_xlsx}")
    print(f"      {review_xlsx}")

    elapsed = time.time() - t0
    print(f"[done] {elapsed/60:.1f} min")

print(f"\n{'='*70}\nALL 3 PDFs done.\n{'='*70}")
