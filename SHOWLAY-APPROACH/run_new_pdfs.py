# ruff: noqa: E402
"""Run the section-aware SHOWLAY pipeline on the 3 new PDFs. No truth workbook is used, so:
  - keep the default template path only as UI metadata
  - skip eval
  - produce both <stem>.xlsx and <stem>_review.xlsx
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

THIS = Path(__file__).resolve().parent
load_dotenv(THIS / ".env")

from showlay.agentic import extract_document_agentic
from showlay.confidence import score_rows
from showlay.extract import probe_and_rasterize
from showlay.paths import RUNTIME_DIR, app_path, default_template_path
from showlay.writer import write_review_sidecar, write_workbook

TEMPLATE = default_template_path()
SOURCE_DIR = Path(os.environ.get("SHOWLAY_NEW_PDF_DIR", app_path("new_pdf_files")))
OUT_DIR = RUNTIME_DIR / "output"

PDFS = [
    ("HCBS_Applicant_Tool", "HCBSApplicantTool.pdf 7.28.17 Fillable.pdf"),
    ("MNLOC", "MNLOC Blank (1).pdf"),
    ("Multiple_Complex_Health_Conditions", "Multiple Complex Health Conditions  Form -FINAL rev 12.1.16_Fillable (1).pdf"),
]

img_dir = RUNTIME_DIR / "page_images"

for stem, fname in PDFS:
    pdf = SOURCE_DIR / fname
    print(f"\n{'='*70}")
    print(f">>> {stem}  ({pdf.name})")
    print('='*70)

    out_dir = OUT_DIR / stem
    debug_dir = RUNTIME_DIR / "extracted" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print("[1/4] probe + rasterize ...")
    doc = probe_and_rasterize(str(pdf), str(img_dir), dpi=200)
    print(f"      {doc.page_count} page(s), AcroForm={doc.has_acroform}")

    print("[2/4] section-aware Bedrock agent ...")
    result = extract_document_agentic(doc)
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

    print("[3/4] confidence ...")
    page_text_by_page = {p.page_index + 1: " ".join(t["text"] for t in p.text_blocks)
                         for p in doc.pages}
    rows = score_rows(rows, page_text_by_page)
    n_high = sum(1 for r in rows if r.confidence >= 0.9)
    n_low = sum(1 for r in rows if r.confidence < 0.7)
    print(f"      conf high>=0.9: {n_high}  low<0.7: {n_low}")

    print("[4/4] write workbooks ...")
    out_xlsx = out_dir / f"{stem}.xlsx"
    review_xlsx = out_dir / f"{stem}_review.xlsx"
    write_workbook(str(TEMPLATE), str(out_xlsx), rows)
    write_review_sidecar(str(review_xlsx), rows)
    print(f"      {out_xlsx}")
    print(f"      {review_xlsx}")

    elapsed = time.time() - t0
    print(f"[done] {elapsed/60:.1f} min")

print(f"\n{'='*70}\nALL 3 PDFs done.\n{'='*70}")
