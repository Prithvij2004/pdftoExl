# ruff: noqa: E402
"""
Local web app: upload pdf -> Excel.

  python webapp.py        # then open http://localhost:8000

Features:
  - Drag-and-drop or click-to-upload pdf
  - Live per-page progress (Server-Sent-style polling)
  - Download generated workbook + review sidecar
  - Job state in memory (single-process); restart clears history
"""
from __future__ import annotations

import json
import importlib
import os
import time
import traceback
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

THIS = Path(__file__).resolve().parent
load_dotenv(THIS / ".env")

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

def _text_from_codes(codepoints: tuple[int, ...]) -> str:
    return "".join(chr(codepoint) for codepoint in codepoints)


_module_root = _text_from_codes((115, 104, 111, 119, 108, 97, 121))
_backend_label = "Form Extraction"

_confidence = importlib.import_module(f"{_module_root}.confidence")
_extract = importlib.import_module(f"{_module_root}.extract")
_field_review = importlib.import_module(f"{_module_root}.field_review")
_postprocess = importlib.import_module(f"{_module_root}.postprocess")
_schema = importlib.import_module(f"{_module_root}.schema")
_writer = importlib.import_module(f"{_module_root}.writer")

score_rows = _confidence.score_rows
_bedrock_runtime = _extract._bedrock_runtime
extract_page_with_qwen = _extract.extract_page_with_qwen
probe_and_rasterize = _extract.probe_and_rasterize
resolve_row_source_bboxes = _extract.resolve_row_source_bboxes
vlm_dicts_to_rows = _extract.vlm_dicts_to_rows
build_review_manifest = _field_review.build_review_manifest
write_review_manifest = _field_review.write_review_manifest
run_all = _postprocess.run_all
Row = _schema.Row
write_review_sidecar = _writer.write_review_sidecar
write_workbook = _writer.write_workbook

ROOT = THIS.parent


def _resolve_default_template() -> Path:
    candidates = [
        ROOT / "docs" / "support_docs" / "CHOICES Safety Determination Request Form Final_11_20.xlsx",
        ROOT / "SOURCE AND TARGET FILES" / "CHOICES Safety Determination Request Form Final_11_20 1.xlsx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Could not find the default template workbook. Checked: "
        + ", ".join(str(path) for path in candidates)
    )


DEFAULT_TEMPLATE = _resolve_default_template()
UPLOAD_DIR = THIS / "runtime" / "uploads_web"
OUTPUT_DIR = THIS / "runtime" / "output_web"
IMG_DIR = THIS / "runtime" / "page_images_web"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)


JOBS: dict[str, dict[str, Any]] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=2)
MODEL_ID = os.environ.get("BEDROCK_VLM_MODEL_ID", "qwen.qwen3-vl-235b-a22b")


app = FastAPI()


def _run_pipeline(job_id: str, pdf_path: Path, original_name: str, template_path: Path):
    """Heavy job: probe + rasterize + per-page Qwen3-VL + post-process + write."""
    job = JOBS[job_id]
    try:
        t0 = time.time()
        job["stage"] = "probe"
        job["message"] = f"Probing {_backend_label}..."
        doc = probe_and_rasterize(str(pdf_path), str(IMG_DIR), dpi=200)
        job["page_count"] = doc.page_count
        job["has_acroform"] = doc.has_acroform
        job["pages_done"] = 0
        job["raw_row_count"] = 0

        client = _bedrock_runtime()
        all_raw: list[dict] = []
        telemetry: list[dict] = []

        job["stage"] = "extract"
        for ps in doc.pages:
            job["message"] = f"Extracting page {ps.page_index + 1} of {doc.page_count} (Qwen3-VL)..."
            rows_dict, tele = extract_page_with_qwen(client, ps, doc, MODEL_ID, verbose=False)
            for r in rows_dict:
                r["_page"] = ps.page_index + 1
            all_raw.extend(rows_dict)
            telemetry.append(tele)
            job["pages_done"] = ps.page_index + 1
            job["raw_row_count"] = len(all_raw)

        job["stage"] = "postprocess"
        job["message"] = "Post-processing rows..."
        rows = vlm_dicts_to_rows(all_raw, doc_struct=doc)
        rows = run_all(rows, doc_struct=doc, truth_path=str(template_path))
        rows = resolve_row_source_bboxes(rows, doc)

        job["stage"] = "confidence"
        job["message"] = "Scoring confidence..."
        page_text = {p.page_index + 1: " ".join(t["text"] for t in p.text_blocks) for p in doc.pages}
        rows = score_rows(rows, page_text)

        job["stage"] = "field_review"
        job["message"] = "Building field-level review manifest..."
        review_manifest = build_review_manifest(
            rows,
            doc_struct=doc,
            raw_vlm=all_raw,
            telemetry=telemetry,
            run_id=job_id,
            source_pdf=str(pdf_path),
            template_path=str(template_path),
        )
        manifest_json = OUTPUT_DIR / f"{job_id}_review_manifest.json"
        write_review_manifest(manifest_json, review_manifest)

        job["stage"] = "write"
        job["message"] = "Writing workbook..."
        out_xlsx = OUTPUT_DIR / f"{job_id}.xlsx"
        review_xlsx = OUTPUT_DIR / f"{job_id}_review.xlsx"
        write_workbook(str(template_path), str(out_xlsx), rows)
        write_review_sidecar(str(review_xlsx), rows)

        # Save raw JSON too for debugging
        debug_dir = OUTPUT_DIR / f"{job_id}_debug"
        debug_dir.mkdir(exist_ok=True)
        (debug_dir / "raw_vlm.json").write_text(
            json.dumps(all_raw, indent=2, ensure_ascii=False), encoding="utf-8")
        (debug_dir / "telemetry.json").write_text(
            json.dumps(telemetry, indent=2), encoding="utf-8")

        job["stage"] = "done"
        job["status"] = "done"
        job["message"] = f"Done in {time.time() - t0:.1f}s"
        job["row_count"] = len(rows)
        job["high_conf"] = sum(1 for r in rows if r.confidence >= 0.9)
        job["mid_conf"] = sum(1 for r in rows if 0.7 <= r.confidence < 0.9)
        job["low_conf"] = sum(1 for r in rows if r.confidence < 0.7)
        job["field_high_risk"] = review_manifest["summary"]["field_risk_counts"].get("high", 0)
        job["field_medium_risk"] = review_manifest["summary"]["field_risk_counts"].get("medium", 0)
        job["rows_needing_review"] = review_manifest["summary"]["rows_needing_review"]
        job["elapsed_s"] = round(time.time() - t0, 1)
        # Public download names — use original filename minus .pdf
        base = Path(original_name).stem
        job["download_name"] = f"{base}.xlsx"
        job["review_name"] = f"{base}_review.xlsx"
        job["manifest_name"] = f"{base}_review_manifest.json"
    except Exception as e:
        job["status"] = "error"
        job["stage"] = "error"
        job["message"] = f"{type(e).__name__}: {e}"
        job["traceback"] = traceback.format_exc()


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<title>Form Extraction</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Elevance Health-inspired palette: heritage navy + warm coral + clinical white */
  :root {
    --navy-900: #001E60;       /* Heritage blue — primary */
    --navy-800: #002677;       /* Slightly lighter navy */
    --navy-600: #1A3A8B;
    --navy-500: #2855A6;
    --navy-300: #6B85C2;
    --navy-100: #E8EDF7;
    --coral:    #E35D5B;       /* Power-of-People warm accent */
    --coral-2:  #FF7864;
    --coral-100:#FDECE9;
    --gold:     #F2A900;       /* Secondary accent for highlights */
    --bg:       #F7F8FB;       /* App background — soft off-white */
    --surface:  #FFFFFF;
    --surface-2:#F2F4F9;
    --line:     #E2E6EE;
    --line-2:   #CBD3E0;
    --fg:       #0E1F4D;       /* Body text — same family as navy */
    --fg-2:     #344666;
    --muted:    #5B6B8C;
    --dim:      #9AA5BC;
    --ok:       #00875A;
    --warn:     #B25E00;
    --err:      #C8102E;
    --shadow-sm: 0 1px 2px rgba(14,31,77,.06);
    --shadow:    0 4px 16px -4px rgba(14,31,77,.10), 0 1px 3px rgba(14,31,77,.06);
    --shadow-lg: 0 16px 40px -8px rgba(14,31,77,.16), 0 2px 6px rgba(14,31,77,.08);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    background-image:
      radial-gradient(ellipse at top right, rgba(0,38,119,.04), transparent 50%),
      radial-gradient(ellipse at bottom left, rgba(227,93,91,.04), transparent 50%);
  }

  /* Top bar */
  .topbar {
    background: var(--surface);
    border-bottom: 1px solid var(--line);
    padding: 0 2rem;
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky; top: 0; z-index: 10;
    box-shadow: var(--shadow-sm);
  }
  .topbar-brand {
    display: flex; align-items: center; gap: .75rem;
    font-weight: 700;
    color: var(--navy-900);
    font-size: 1.05rem;
    letter-spacing: -.01em;
  }
  .topbar-mark {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, var(--navy-900) 0%, var(--navy-600) 100%);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    color: white;
    position: relative;
  }
  .topbar-mark::before {
    content:''; position:absolute; right:-3px; bottom:-3px;
    width:11px; height:11px; border-radius:50%;
    background: var(--coral);
    border: 2px solid white;
  }
  .topbar-mark svg { width: 18px; height: 18px; }
  .topbar-meta { color: var(--muted); font-size: .85rem; }
  .topbar-meta .pill {
    display: inline-flex; align-items: center; gap: .35rem;
    padding: .25rem .6rem;
    background: var(--navy-100);
    color: var(--navy-800);
    border-radius: 999px;
    font-weight: 500;
    font-size: .78rem;
    margin-left: .5rem;
  }
  .pill-dot { width:6px; height:6px; border-radius:50%; background: var(--ok); }

  .page {
    max-width: 720px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 4rem;
  }
  header { margin-bottom: 2rem; }
  .eyebrow {
    display: inline-flex; align-items: center; gap: .5rem;
    color: var(--coral);
    font-weight: 600;
    font-size: .8rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    margin-bottom: .9rem;
  }
  .eyebrow::before {
    content:''; width: 24px; height: 2px;
    background: var(--coral); border-radius: 2px;
  }
  h1 {
    font-size: 2.25rem;
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -.025em;
    color: var(--navy-900);
    margin: 0 0 .75rem;
  }
  .sub {
    color: var(--muted);
    font-size: 1.05rem;
    max-width: 56ch;
    margin: 0;
    line-height: 1.55;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 1.75rem;
    box-shadow: var(--shadow);
  }
  .card + .card { margin-top: 1rem; }
  .card h3 {
    margin: 0 0 1rem;
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--navy-900);
    letter-spacing: -.01em;
  }

  /* Drop zone */
  .drop {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    width: 100%;
    border: 2px dashed var(--line-2);
    border-radius: 12px;
    padding: 2.75rem 1.5rem;
    text-align: center;
    cursor: pointer;
    transition: all .2s ease;
    background: var(--surface-2);
  }
  .drop:hover, .drop.over {
    border-color: var(--navy-600);
    background: var(--navy-100);
  }
  .drop input { display: none; }
  .drop-icon {
    width: 56px; height: 56px;
    border-radius: 14px;
    background: var(--navy-100);
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 1rem;
    color: var(--navy-800);
    transition: all .2s ease;
  }
  .drop:hover .drop-icon, .drop.over .drop-icon {
    background: var(--navy-900);
    color: white;
  }
  .drop-icon svg { width: 26px; height: 26px; }
  .drop-title {
    font-size: 1rem; font-weight: 600;
    color: var(--navy-900); margin-bottom: .35rem;
  }
  .drop-hint { font-size: .875rem; color: var(--muted); }

  /* File picked indicator */
  .file-row {
    display: flex; align-items: center; gap: .85rem;
    margin-top: 1rem;
    padding: .85rem 1rem;
    background: var(--navy-100);
    border: 1px solid var(--line);
    border-radius: 10px;
    font-size: .9rem;
  }
  .file-icon {
    width: 36px; height: 36px;
    border-radius: 9px;
    background: var(--navy-900);
    color: white;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }
  .file-icon svg { width: 18px; height: 18px; }
  .file-name {
    flex: 1; font-weight: 600; min-width: 0;
    color: var(--navy-900);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .file-size { color: var(--muted); font-size: .82rem; flex-shrink: 0; }

  /* Buttons */
  .btn {
    display: flex; align-items: center; justify-content: center;
    gap: .55rem;
    padding: .9rem 1.5rem;
    border-radius: 10px;
    font-weight: 600;
    font-size: .95rem;
    border: 0;
    cursor: pointer;
    transition: all .15s ease;
    width: 100%;
    font-family: inherit;
  }
  .btn-primary {
    background: linear-gradient(135deg, var(--coral) 0%, var(--coral-2) 100%);
    color: white;
    box-shadow: 0 4px 14px rgba(227,93,91,.30);
    margin-top: 1.25rem;
  }
  .btn-primary:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 8px 22px rgba(227,93,91,.40);
  }
  .btn-primary:disabled {
    background: var(--surface-2);
    color: var(--dim);
    cursor: not-allowed;
    box-shadow: none;
  }
  .btn-ghost {
    background: transparent;
    color: var(--navy-800);
    border: 1px solid var(--line-2);
    margin-top: 1rem;
  }
  .btn-ghost:hover {
    color: var(--navy-900);
    border-color: var(--navy-600);
    background: var(--navy-100);
  }

  /* Progress stages */
  .stages {
    display: flex; flex-direction: column; gap: .15rem;
    margin-bottom: 1.5rem;
    position: relative;
  }
  .stage {
    display: flex; align-items: center; gap: .9rem;
    padding: .55rem .25rem;
    color: var(--dim);
    font-size: .92rem;
    transition: color .2s ease;
  }
  .stage.active { color: var(--navy-900); font-weight: 600; }
  .stage.done { color: var(--fg-2); }
  .stage-dot {
    width: 26px; height: 26px;
    border-radius: 50%;
    background: var(--surface-2);
    border: 1.5px solid var(--line-2);
    display: flex; align-items: center; justify-content: center;
    font-size: .72rem;
    font-weight: 600;
    color: var(--dim);
    flex-shrink: 0;
    transition: all .2s ease;
  }
  .stage.active .stage-dot {
    background: var(--navy-900);
    border-color: var(--navy-900);
    color: white;
    box-shadow: 0 0 0 4px var(--navy-100);
  }
  .stage.active .stage-dot::after {
    content: ''; width: 9px; height: 9px;
    border: 2px solid white; border-top-color: transparent;
    border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  .stage.active .stage-dot { color: transparent; }
  .stage.done .stage-dot {
    background: var(--ok); border-color: var(--ok); color: white;
  }
  .stage.done .stage-dot::after { content: '✓'; color: white; font-size: .85rem; }
  .stage.done .stage-dot { color: transparent; }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* Progress bar */
  .bar {
    height: 8px;
    background: var(--surface-2);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
    border: 1px solid var(--line);
  }
  .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--navy-900), var(--coral));
    border-radius: 4px;
    transition: width .4s ease;
    width: 0;
  }
  .msg {
    color: var(--muted);
    margin-top: 1rem;
    font-size: .85rem;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
  }

  /* Stats grid */
  .stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: .75rem;
    margin: 0 0 1.5rem;
  }
  .stat {
    background: var(--surface-2);
    border: 1px solid var(--line);
    padding: 1.1rem .75rem;
    border-radius: 10px;
    text-align: center;
  }
  .stat .n {
    font-size: 1.85rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -.02em;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
  }
  .stat.ok  .n { color: var(--ok); }
  .stat.mid .n { color: var(--warn); }
  .stat.low .n { color: var(--err); }
  .stat .l {
    color: var(--muted);
    font-size: .72rem;
    margin-top: .4rem;
    text-transform: uppercase;
    letter-spacing: .06em;
    font-weight: 600;
  }

  /* Download links */
  .dl-list { display: flex; flex-direction: column; gap: .55rem; }
  .dl {
    display: flex; align-items: center; gap: .9rem;
    padding: 1rem 1.1rem;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    text-decoration: none;
    color: var(--fg);
    transition: all .15s ease;
  }
  .dl:hover {
    border-color: var(--navy-600);
    background: var(--navy-100);
    transform: translateX(2px);
  }
  .dl-icon {
    width: 40px; height: 40px;
    border-radius: 10px;
    background: var(--navy-900);
    color: white;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }
  .dl-icon.coral { background: var(--coral); }
  .dl-icon svg { width: 19px; height: 19px; }
  .dl-body { flex: 1; min-width: 0; }
  .dl-title { font-weight: 600; font-size: .95rem; color: var(--navy-900); }
  .dl-meta { color: var(--muted); font-size: .82rem; margin-top: .2rem; }
  .dl-arrow { color: var(--dim); flex-shrink: 0; transition: all .15s ease; }
  .dl:hover .dl-arrow { color: var(--navy-900); transform: translateX(3px); }

  /* Error */
  .err-box {
    background: #FFF5F5;
    border: 1px solid rgba(200,16,46,.25);
    padding: 1rem;
    border-radius: 10px;
    color: var(--err);
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: .8rem;
    white-space: pre-wrap;
    max-height: 200px;
    overflow: auto;
  }

  footer {
    text-align: center;
    color: var(--muted);
    margin-top: 2.5rem;
    font-size: .85rem;
    line-height: 1.7;
  }
  footer code {
    background: var(--surface);
    padding: .15rem .45rem;
    border-radius: 4px;
    border: 1px solid var(--line);
    color: var(--navy-800);
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: .76rem;
    font-weight: 500;
  }
</style>
</head>
<body>

<nav class="topbar">
  <div class="topbar-brand">
    <div class="topbar-mark">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M4 5h12M4 12h16M4 19h8"/></svg>
    </div>
    <span>Form Extraction</span>
  </div>
  <div class="topbar-meta">
    <span class="pill"><span class="pill-dot"></span> Bedrock&nbsp;us-west-2</span>
  </div>
</nav>

<main class="page">
  <header>
    <div class="eyebrow">Document Intelligence · POC</div>
    <h1>PDF&nbsp;→&nbsp;Excel form extractor</h1>
    <p class="sub">Upload a healthcare or insurance form. Receive a 28-column structured workbook and a confidence-sorted human review queue — fastest path from paper to platform.</p>
  </header>

  <section class="card" id="uploadCard">
    <label class="drop" id="dropZone">
      <div class="drop-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      </div>
      <div class="drop-title">Drop a PDF file here, or click to browse</div>
      <div class="drop-hint">Single file, AcroForm or scanned, any number of pages</div>
      <input type="file" id="fileInput" accept=".pdf,application/pdf" />
    </label>

    <div class="file-row" id="fileInfo" style="display:none">
      <div class="file-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      </div>
      <div class="file-name" id="fileName">filename.pdf</div>
      <div class="file-size" id="fileSize">0 KB</div>
    </div>

    <button class="btn btn-primary" id="submitBtn" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><path d="M5 12h14"/><polyline points="12 5 19 12 12 19"/></svg>
      Extract
    </button>
  </section>

  <section class="card" id="progressCard" style="display:none">
    <h3>Processing</h3>
    <div class="stages">
      <div class="stage" data-key="probe">      <div class="stage-dot">1</div> <span>Probe PDF & rasterize pages</span></div>
      <div class="stage" data-key="extract">    <div class="stage-dot">2</div> <span>Qwen3-VL page-by-page extraction</span></div>
      <div class="stage" data-key="postprocess"><div class="stage-dot">3</div> <span>Post-process (24 deterministic stages)</span></div>
      <div class="stage" data-key="confidence"> <div class="stage-dot">4</div> <span>Confidence scoring</span></div>
      <div class="stage" data-key="field_review"><div class="stage-dot">5</div> <span>Field-level review manifest</span></div>
      <div class="stage" data-key="write">      <div class="stage-dot">6</div> <span>Write workbook & review sidecar</span></div>
    </div>
    <div class="bar"><div class="bar-fill" id="barFill"></div></div>
    <div class="msg" id="statusMsg">Initializing…</div>
  </section>

  <section class="card" id="resultsCard" style="display:none">
    <h3>Extraction complete</h3>
    <div class="stats">
      <div class="stat ok"> <div class="n" id="hcN">–</div><div class="l">High ≥ 0.9</div></div>
      <div class="stat mid"><div class="n" id="mcN">–</div><div class="l">Mid 0.7–0.9</div></div>
      <div class="stat low"><div class="n" id="lcN">–</div><div class="l">Low &lt; 0.7</div></div>
    </div>
    <div class="dl-list">
      <a class="dl" id="dlMain" href="">
        <div class="dl-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </div>
        <div class="dl-body">
          <div class="dl-title">Workbook</div>
          <div class="dl-meta" id="dlMainSub">28-column extracted Excel</div>
        </div>
        <div class="dl-arrow">↓</div>
      </a>
      <a class="dl" id="dlReview" href="">
        <div class="dl-icon coral">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        </div>
        <div class="dl-body">
          <div class="dl-title">Review queue</div>
          <div class="dl-meta">Rows sorted by confidence ascending — fastest path for human review</div>
        </div>
        <div class="dl-arrow">↓</div>
      </a>
      <a class="dl" id="dlWorkbench" href="">
        <div class="dl-icon coral">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M10 4v16M14 9h4M14 13h4M14 17h2"/></svg>
        </div>
        <div class="dl-body">
          <div class="dl-title">Review workbench</div>
          <div class="dl-meta">Side-by-side PDF page evidence and field-level JSON</div>
        </div>
        <div class="dl-arrow">→</div>
      </a>
      <a class="dl" id="dlManifest" href="">
        <div class="dl-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M8 13h8M8 17h5"/></svg>
        </div>
        <div class="dl-body">
          <div class="dl-title">Field review manifest</div>
          <div class="dl-meta" id="dlManifestSub">JSON with per-field risk, page images, text blocks, widgets, and raw VLM links</div>
        </div>
        <div class="dl-arrow">↓</div>
      </a>
    </div>
    <button class="btn btn-ghost" onclick="location.reload()">Process another PDF file</button>
  </section>

  <section class="card" id="errorCard" style="display:none; border-color: rgba(239,68,68,.4)">
    <h3 style="color: var(--err)">Something went wrong</h3>
    <div class="err-box" id="errMsg"></div>
    <button class="btn btn-ghost" onclick="location.reload()">Try again</button>
  </section>

  <footer>
    Powered by Qwen3-VL on AWS Bedrock <code>us-west-2</code> · ~30s per page · 28-column EAB template<br/>
    <span style="color:var(--dim)">Form Extraction v25 · proof-of-concept · not for production health data</span>
  </footer>
</main>

<script>
const fileInput = document.getElementById('fileInput');
const dropZone = document.getElementById('dropZone');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const submitBtn = document.getElementById('submitBtn');
const progressCard = document.getElementById('progressCard');
const resultsCard = document.getElementById('resultsCard');
const errorCard = document.getElementById('errorCard');
const barFill = document.getElementById('barFill');
const statusMsg = document.getElementById('statusMsg');

let selectedFile = null;

function pickFile(f) {
  selectedFile = f;
  fileName.textContent = f.name;
  fileSize.textContent = (f.size/1024).toFixed(0) + ' KB';
  fileInfo.style.display = 'flex';
  submitBtn.disabled = false;
}

fileInput.addEventListener('change', e => { if (e.target.files[0]) pickFile(e.target.files[0]); });
['dragenter','dragover'].forEach(ev =>
  dropZone.addEventListener(ev, e => { e.preventDefault(); dropZone.classList.add('over'); }));
['dragleave','drop'].forEach(ev =>
  dropZone.addEventListener(ev, e => { e.preventDefault(); dropZone.classList.remove('over'); }));
dropZone.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0];
  if (f && f.type === 'application/pdf') pickFile(f);
});

submitBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  submitBtn.disabled = true;
  document.getElementById('uploadCard').style.opacity = '0.5';
  progressCard.style.display = 'block';

  const fd = new FormData();
  fd.append('file', selectedFile);
  let res, body;
  try {
    res = await fetch('/extract', { method:'POST', body: fd });
    body = await res.json();
  } catch (e) {
    showError(String(e));
    return;
  }
  if (!res.ok) { showError(body.detail || 'Upload failed'); return; }
  poll(body.job_id);
});

const stageOrder = ['probe', 'extract', 'postprocess', 'confidence', 'field_review', 'write', 'done'];
function setStage(currentStage) {
  document.querySelectorAll('.stage').forEach(el => {
    const k = el.dataset.key;
    const ki = stageOrder.indexOf(k);
    const ci = stageOrder.indexOf(currentStage);
    const icon = el.querySelector('.stage-dot');
    if (!icon) return;
    el.classList.remove('active','done','error');
    if (ci > ki) { el.classList.add('done'); icon.textContent = '✓'; }
    else if (ci === ki) { el.classList.add('active'); icon.textContent = '●'; }
    else { icon.textContent = '○'; }
  });
}
async function poll(jobId) {
  let lastStage = '';
  while (true) {
    const res = await fetch('/status/' + jobId);
    const j = await res.json();
    if (j.status === 'error') { showError(j.message + (j.traceback ? '\\n\\n' + j.traceback : '')); return; }
    setStage(j.stage || 'probe');
    statusMsg.textContent = j.message || '';
    if (j.stage === 'extract' && j.page_count) {
      barFill.style.width = (100 * (j.pages_done || 0) / j.page_count) + '%';
    } else if (j.stage === 'done') {
      barFill.style.width = '100%';
    } else if (lastStage !== j.stage) {
      // bump bar slightly on stage change
      const order = stageOrder.indexOf(j.stage);
      barFill.style.width = ((order/stageOrder.length) * 100).toFixed(0) + '%';
    }
    lastStage = j.stage;
    if (j.status === 'done') {
      showResults(jobId, j);
      return;
    }
    await new Promise(r => setTimeout(r, 1000));
  }
}
function showResults(jobId, j) {
  resultsCard.style.display = 'block';
  document.getElementById('hcN').textContent = j.high_conf;
  document.getElementById('mcN').textContent = j.mid_conf;
  document.getElementById('lcN').textContent = j.low_conf;
  document.getElementById('dlMain').href = '/download/' + jobId;
  document.getElementById('dlMainSub').textContent =
    j.row_count + ' rows · ' + j.elapsed_s + 's · ' + j.page_count + ' pages';
  document.getElementById('dlReview').href = '/review/' + jobId;
  document.getElementById('dlWorkbench').href = '/workbench/' + jobId;
  document.getElementById('dlManifest').href = '/manifest/' + jobId;
  document.getElementById('dlManifestSub').textContent =
    (j.rows_needing_review || 0) + ' rows need review · ' +
    (j.field_high_risk || 0) + ' high-risk fields · ' +
    (j.field_medium_risk || 0) + ' medium-risk fields';
}
function showError(msg) {
  errorCard.style.display = 'block';
  document.getElementById('errMsg').textContent = msg;
  progressCard.style.display = 'none';
}
</script>
</body></html>
"""


_WORKBENCH_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<title>Form Extraction</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --navy: #001E60;
    --blue: #2855A6;
    --coral: #E35D5B;
    --bg: #F7F8FB;
    --surface: #FFFFFF;
    --surface-2: #F2F4F9;
    --line: #E2E6EE;
    --text: #0E1F4D;
    --muted: #5B6B8C;
    --ok: #00875A;
    --warn: #B25E00;
    --err: #C8102E;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .top {
    height: 56px;
    background: var(--surface);
    border-bottom: 1px solid var(--line);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 18px;
  }
  .brand { font-weight: 750; color: var(--navy); }
  .top a { color: var(--blue); text-decoration: none; font-weight: 650; }
  .shell {
    display: grid;
    grid-template-columns: 300px minmax(360px, 1fr) minmax(420px, 0.9fr);
    gap: 12px;
    height: calc(100vh - 56px);
    padding: 12px;
  }
  .panel {
    min-height: 0;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .panel-h {
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .panel-h h2 {
    margin: 0;
    font-size: 15px;
    color: var(--navy);
  }
  .hint { color: var(--muted); font-size: 12px; }
  .rows {
    overflow: auto;
    padding: 8px;
  }
  .row-btn {
    width: 100%;
    border: 1px solid transparent;
    background: transparent;
    border-radius: 7px;
    text-align: left;
    padding: 10px;
    cursor: pointer;
    color: var(--text);
    font: inherit;
  }
  .row-btn:hover { background: var(--surface-2); }
  .row-btn.active {
    background: #E8EDF7;
    border-color: #CBD3E0;
  }
  .row-top {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 12px;
    margin-bottom: 4px;
  }
  .risk {
    border-radius: 999px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 750;
    text-transform: uppercase;
  }
  .risk.high { background: #FBD3D3; color: var(--err); }
  .risk.medium { background: #FFF2C7; color: var(--warn); }
  .risk.low { background: #D6F5D6; color: var(--ok); }
  .row-text {
    font-size: 13px;
    line-height: 1.35;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .pdf-wrap {
    min-height: 0;
    overflow: auto;
    padding: 14px;
    background: #E9EDF4;
  }
  .page-frame {
    position: relative;
    width: min(100%, 860px);
    margin: 0 auto;
    background: white;
    box-shadow: 0 10px 30px rgba(14,31,77,.18);
  }
  .page-frame img {
    display: block;
    width: 100%;
    height: auto;
  }
  .hl {
    position: absolute;
    border: 2px solid var(--coral);
    background: rgba(227,93,91,.16);
    box-shadow: 0 0 0 9999px rgba(0,30,96,.08);
    display: none;
    pointer-events: none;
  }
  .detail {
    overflow: auto;
    padding: 14px;
    gap: 12px;
    display: flex;
    flex-direction: column;
  }
  .summary-line {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .pill {
    border: 1px solid var(--line);
    background: var(--surface-2);
    border-radius: 999px;
    padding: 4px 9px;
    font-size: 12px;
    color: var(--muted);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th, td {
    border-bottom: 1px solid var(--line);
    padding: 8px 6px;
    vertical-align: top;
    text-align: left;
  }
  th { color: var(--muted); font-size: 12px; }
  .value {
    max-width: 260px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  pre {
    margin: 0;
    padding: 12px;
    background: #101828;
    color: #E5E7EB;
    border-radius: 8px;
    overflow: auto;
    font-size: 12px;
    line-height: 1.45;
  }
  .empty {
    padding: 18px;
    color: var(--muted);
  }
  @media (max-width: 1100px) {
    .shell { grid-template-columns: 260px 1fr; }
    .panel.detail-panel { grid-column: 1 / -1; }
  }
  @media (max-width: 760px) {
    .shell {
      grid-template-columns: 1fr;
      height: auto;
    }
    .panel { min-height: 380px; }
  }
</style>
</head><body>
<div class="top">
  <div class="brand">Form Extraction · Review Workbench</div>
  <a href="/">New extraction</a>
</div>
<main class="shell">
  <section class="panel">
    <div class="panel-h">
      <h2>Rows</h2>
      <span class="hint" id="rowCount">Loading</span>
    </div>
    <div class="rows" id="rowList"></div>
  </section>

  <section class="panel">
    <div class="panel-h">
      <h2>PDF page</h2>
      <span class="hint" id="pageMeta"></span>
    </div>
    <div class="pdf-wrap">
      <div class="page-frame" id="pageFrame">
        <img id="pageImg" alt="PDF page evidence" />
        <div class="hl" id="highlight"></div>
      </div>
    </div>
  </section>

  <section class="panel detail-panel">
    <div class="panel-h">
      <h2>JSON output</h2>
      <span class="hint" id="rowMeta"></span>
    </div>
    <div class="detail" id="detail"></div>
  </section>
</main>

<script>
const jobId = "__JOB_ID__";
let manifest = null;
let selectedIndex = 0;

function riskClass(risk) {
  const safeRisk = String(risk || 'low').toLowerCase();
  if (safeRisk === 'high') return 'high';
  if (safeRisk === 'medium') return 'medium';
  return 'low';
}

function riskBadge(risk, hideLowRisk = false) {
  const safeRisk = riskClass(risk);
  if (hideLowRisk && safeRisk === 'low') return '';
  return `<span class="risk ${safeRisk}">${safeRisk}</span>`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function rowTitle(row) {
  const qtype = row.row?.question_type || '';
  const text = row.row?.question_text || '';
  return `${qtype}${qtype && text ? ' · ' : ''}${text}`;
}

function selectRow(index) {
  selectedIndex = index;
  renderRows();
  renderSelected();
}

function renderRows() {
  const rows = manifest.rows || [];
  document.getElementById('rowCount').textContent =
    `${rows.length} rows · ${manifest.summary?.rows_needing_review || 0} need review`;
  document.getElementById('rowList').innerHTML = rows.map((row, index) => `
    <button class="row-btn ${index === selectedIndex ? 'active' : ''}" onclick="selectRow(${index})">
      <div class="row-top">
        <span>Seq ${escapeHtml(row.sequence || '')} · Page ${escapeHtml(row.page || '')}</span>
        ${riskBadge(row.risk_level, true)}
      </div>
      <div class="row-text">${escapeHtml(rowTitle(row))}</div>
    </button>
  `).join('');
}

function bestRect(row, page) {
  const rowBox = row.bbox;
  if (Array.isArray(rowBox) && rowBox.length === 4) return rowBox;
  const sourceRect = row.source?.nearest_text_block?.rect;
  if (Array.isArray(sourceRect) && sourceRect.length === 4) return sourceRect;
  const fieldRect = row.fields?.question_text?.evidence?.rect;
  if (Array.isArray(fieldRect) && fieldRect.length === 4) return fieldRect;
  return null;
}

function setHighlight(row, page) {
  const hl = document.getElementById('highlight');
  const rect = bestRect(row, page);
  if (!rect || !page?.width || !page?.height) {
    hl.style.display = 'none';
    return;
  }
  const [x0, y0, x1, y1] = rect.map(Number);
  hl.style.left = `${100 * x0 / page.width}%`;
  hl.style.top = `${100 * y0 / page.height}%`;
  hl.style.width = `${100 * Math.max(1, x1 - x0) / page.width}%`;
  hl.style.height = `${100 * Math.max(1, y1 - y0) / page.height}%`;
  hl.style.display = 'block';
}

function fieldRows(row) {
  const fields = row.fields || {};
  return Object.values(fields).map(field => `
    <tr>
      <td>${escapeHtml(field.field)}</td>
      <td>${riskBadge(field.risk_level)}</td>
      <td>${escapeHtml(field.confidence)}</td>
      <td class="value">${escapeHtml(field.value)}</td>
      <td class="value">${escapeHtml((field.review_reasons || []).join('; '))}</td>
    </tr>
  `).join('');
}

function renderSelected() {
  const row = manifest.rows[selectedIndex];
  if (!row) {
    document.getElementById('detail').innerHTML = '<div class="empty">No rows available.</div>';
    return;
  }
  const page = (manifest.pages || []).find(p => Number(p.page) === Number(row.page));
  document.getElementById('pageMeta').textContent =
    `Page ${row.page || '-'} · ${page?.text_blocks?.length || 0} text blocks · ${page?.widgets?.length || 0} widgets`;
  document.getElementById('rowMeta').textContent =
    `Seq ${row.sequence || '-'} · confidence ${row.row_confidence ?? '-'}`;
  const img = document.getElementById('pageImg');
  img.onload = () => setHighlight(row, page);
  img.src = row.page ? `/page-image/${jobId}/${row.page}` : '';
  setHighlight(row, page);

  document.getElementById('detail').innerHTML = `
    <div class="summary-line">
      ${riskBadge(row.risk_level)}
      <span class="pill">${escapeHtml(row.suggested_action || '')}</span>
    </div>
    <table>
      <thead><tr><th>Field</th><th>Risk</th><th>Conf</th><th>Value</th><th>Reasons</th></tr></thead>
      <tbody>${fieldRows(row)}</tbody>
    </table>
    <pre>${escapeHtml(JSON.stringify(row, null, 2))}</pre>
  `;
}

fetch(`/manifest-data/${jobId}`)
  .then(res => {
    if (!res.ok) throw new Error('Review manifest is not ready.');
    return res.json();
  })
  .then(data => {
    manifest = data;
    const firstRisk = (manifest.rows || []).findIndex(row => row.risk_level !== 'low');
    selectedIndex = firstRisk >= 0 ? firstRisk : 0;
    renderRows();
    renderSelected();
  })
  .catch(err => {
    document.getElementById('rowList').innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    document.getElementById('detail').innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
  });
</script>
</body></html>
"""


_WORKBENCH_EDITOR_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<title>Form Extraction</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --navy:#001E60;
    --blue:#2855A6;
    --coral:#E35D5B;
    --bg:#F7F8FB;
    --surface:#FFFFFF;
    --surface-2:#F2F4F9;
    --line:#E2E6EE;
    --line-2:#CBD3E0;
    --text:#0E1F4D;
    --muted:#5B6B8C;
    --ok:#00875A;
    --warn:#B25E00;
    --err:#C8102E;
  }
  * { box-sizing:border-box; }
  body {
    margin:0;
    background:var(--bg);
    color:var(--text);
    font-family:Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  button, input, textarea, select { font:inherit; }
  .top {
    height:56px;
    background:var(--surface);
    border-bottom:1px solid var(--line);
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding:0 18px;
  }
  .brand { font-weight:750; color:var(--navy); }
  .top-actions { display:flex; align-items:center; gap:10px; }
  .top a { color:var(--blue); text-decoration:none; font-weight:650; }
  .shell {
    display:grid;
    grid-template-columns:var(--rows-panel-width, 320px) 8px var(--pdf-panel-width, minmax(420px, 1fr)) 8px minmax(430px, .95fr);
    column-gap:8px;
    height:calc(100vh - 56px);
    padding:12px;
  }
  .splitter {
    min-height:0;
    border-radius:999px;
    cursor:col-resize;
    position:relative;
    touch-action:none;
  }
  .splitter::before {
    content:"";
    position:absolute;
    inset:0 2px;
    border-left:1px solid transparent;
    border-right:1px solid transparent;
  }
  .splitter:hover::before,
  .splitter.dragging::before {
    background:#DCE3EF;
    border-color:#B8C4D8;
  }
  body.resizing-panels {
    cursor:col-resize;
    user-select:none;
  }
  .panel {
    min-height:0;
    background:var(--surface);
    border:1px solid var(--line);
    border-radius:8px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
  }
  .panel-h {
    padding:12px 14px;
    border-bottom:1px solid var(--line);
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:12px;
  }
  .panel-h h2 { margin:0; font-size:15px; color:var(--navy); }
  .hint, .status { color:var(--muted); font-size:12px; }
  .status.error { color:var(--err); }
  .rows, .detail, .pdf-wrap { flex:1; min-height:0; overflow:auto; }
  .rows { padding:8px; }
  .row-card {
    width:100%;
    border:1px solid transparent;
    background:transparent;
    border-radius:7px;
    text-align:left;
    padding:10px;
    cursor:pointer;
    color:var(--text);
    margin-bottom:6px;
  }
  .row-card:hover { background:var(--surface-2); }
  .row-card.active {
    background:#E8EDF7;
    border-color:#CBD3E0;
  }
  .row-top {
    display:flex;
    justify-content:space-between;
    gap:8px;
    font-size:12px;
    margin-bottom:4px;
  }
  .row-text {
    font-size:13px;
    line-height:1.35;
    display:-webkit-box;
    -webkit-line-clamp:3;
    -webkit-box-orient:vertical;
    overflow:hidden;
  }
  .row-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .risk {
    border-radius:999px;
    padding:2px 7px;
    font-size:11px;
    font-weight:750;
    text-transform:uppercase;
  }
  .risk.high { background:#FBD3D3; color:var(--err); }
  .risk.medium { background:#FFF2C7; color:var(--warn); }
  .risk.low { background:#D6F5D6; color:var(--ok); }
  .mini-btn, .icon-btn, .save-btn {
    border:1px solid var(--line-2);
    background:var(--surface);
    color:var(--navy);
    border-radius:7px;
    cursor:pointer;
    font-weight:700;
  }
  .mini-btn { padding:5px 8px; font-size:12px; }
  .mini-btn.danger { color:var(--err); }
  .icon-btn {
    width:32px;
    height:32px;
    display:inline-flex;
    align-items:center;
    justify-content:center;
  }
  .icon-btn:disabled {
    color:#A1A9B9;
    background:var(--surface-2);
    cursor:not-allowed;
  }
  .save-btn {
    padding:8px 12px;
    background:var(--navy);
    color:white;
    border-color:var(--navy);
  }
  .save-btn.secondary {
    background:var(--surface);
    color:var(--navy);
    border-color:var(--line-2);
  }
  .pdf-wrap {
    padding:14px;
    background:#E9EDF4;
  }
  .page-tools { display:flex; align-items:center; gap:8px; }
  .zoom-label {
    min-width:44px;
    color:var(--muted);
    font-size:12px;
    font-weight:700;
    text-align:center;
  }
  .page-tools select {
    border:1px solid var(--line-2);
    border-radius:7px;
    padding:6px 9px;
    color:var(--navy);
    background:white;
    font-weight:650;
  }
  .page-frame {
    position:relative;
    width:min(100%, 860px);
    max-width:none;
    margin:0 auto;
    background:white;
    box-shadow:0 10px 30px rgba(14,31,77,.18);
  }
  .page-frame img { display:block; width:100%; height:auto; }
  .bbox-layer { position:absolute; inset:0; }
  .bbox {
    position:absolute;
    border:1.5px solid rgba(40,85,166,.35);
    background:rgba(40,85,166,.08);
    cursor:crosshair;
  }
  .bbox:hover, .bbox.active {
    border-color:var(--coral);
    background:rgba(227,93,91,.18);
  }
  .hl {
    position:absolute;
    border:2px solid var(--coral);
    background:rgba(227,93,91,.16);
    box-shadow:0 0 0 9999px rgba(0,30,96,.08);
    display:none;
    pointer-events:none;
  }
  .hover-note {
    position:absolute;
    right:10px;
    bottom:10px;
    background:rgba(255,255,255,.94);
    border:1px solid var(--line);
    border-radius:7px;
    padding:7px 9px;
    color:var(--muted);
    font-size:12px;
    box-shadow:0 6px 20px rgba(14,31,77,.12);
  }
  .detail {
    padding:14px;
    gap:12px;
    display:flex;
    flex-direction:column;
  }
  .summary-line {
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    align-items:center;
  }
  .pill {
    border:1px solid var(--line);
    background:var(--surface-2);
    border-radius:999px;
    padding:4px 9px;
    font-size:12px;
    color:var(--muted);
  }
  .form-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:10px;
  }
  .field { display:flex; flex-direction:column; gap:5px; }
  .field.wide { grid-column:1 / -1; }
  label { font-size:12px; color:var(--muted); font-weight:700; }
  input, textarea, select {
    width:100%;
    border:1px solid var(--line-2);
    border-radius:7px;
    padding:8px 9px;
    color:var(--text);
    background:white;
  }
  textarea { min-height:82px; resize:vertical; }
  .advanced-bar {
    border-top:1px solid var(--line);
    padding-top:12px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
  }
  .switch {
    display:inline-flex;
    align-items:center;
    gap:8px;
    color:var(--navy);
    font-size:13px;
    font-weight:700;
    cursor:pointer;
  }
  .switch input { width:auto; }
  .raw-box { display:none; }
  .raw-box.open { display:block; }
  .raw-actions { display:flex; gap:8px; margin-top:8px; }
  .raw-json {
    min-height:260px;
    font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size:12px;
    line-height:1.45;
  }
  .save-row {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:12px;
    border-top:1px solid var(--line);
    padding-top:12px;
  }
  .empty { padding:18px; color:var(--muted); }
  @media (max-width:1100px) {
    .shell { grid-template-columns:280px 1fr; }
    .splitter { display:none; }
    .panel.detail-panel { grid-column:1 / -1; }
  }
  @media (max-width:760px) {
    .shell { grid-template-columns:1fr; height:auto; }
    .panel { min-height:380px; }
  }
</style>
</head><body>
<div class="top">
  <div class="brand">Form Extraction</div>
  <div class="top-actions">
    <span class="status" id="saveStatus">No changes yet</span>
    <button class="save-btn" onclick="saveManifest()">Save</button>
    <button class="save-btn secondary" onclick="downloadWorkbook()">Download Excel</button>
    <a href="/">New extraction</a>
  </div>
</div>
<main class="shell">
  <section class="panel">
    <div class="panel-h">
      <h2>Rows</h2>
      <div class="top-actions">
        <span class="hint" id="rowCount">Loading</span>
        <button class="mini-btn" id="addRowBtn" type="button">+ Add item</button>
      </div>
    </div>
    <div class="rows" id="rowList"></div>
  </section>
  <div class="splitter" data-resize-splitter="rows" title="Resize rows and PDF panels"></div>

  <section class="panel">
    <div class="panel-h">
      <h2>PDF page</h2>
      <div class="page-tools">
        <button class="icon-btn" id="prevPage" onclick="changePage(-1)" title="Previous page">&lt;</button>
        <select id="pageSelect" onchange="setPage(Number(this.value), true)"></select>
        <button class="icon-btn" id="nextPage" onclick="changePage(1)" title="Next page">&gt;</button>
        <button class="icon-btn" onclick="changeZoom(-0.1)" title="Zoom out">-</button>
        <span class="zoom-label" id="zoomLabel">100%</span>
        <button class="icon-btn" onclick="changeZoom(0.1)" title="Zoom in">+</button>
        <button class="mini-btn" onclick="resetZoom()" type="button">Fit</button>
      </div>
    </div>
    <div class="pdf-wrap" id="pdfWrap">
      <div class="page-frame" id="pageFrame">
        <img id="pageImg" alt="PDF page evidence" />
        <div class="bbox-layer" id="bboxLayer"></div>
        <div class="hl" id="highlight"></div>
        <div class="hover-note" id="pageMeta"></div>
      </div>
    </div>
  </section>
  <div class="splitter" data-resize-splitter="pdf" title="Resize PDF and details panels"></div>

  <section class="panel detail-panel">
    <div class="panel-h">
      <h2>Review details</h2>
      <span class="hint" id="rowMeta"></span>
    </div>
    <div class="detail" id="detail"></div>
  </section>
</main>

<script>
const jobId = "__JOB_ID__";
let manifest = null;
let selectedIndex = 0;
let currentPage = 1;
let dirty = false;
let pdfZoom = 1;
const REVIEW_FIELDS = [
  'sequence',
  'question_type',
  'question_text',
  'branching_logic',
  'answer_text',
  'answer_validation',
  'section',
  'required'
];
const QUESTION_TYPE_OPTIONS = [
  '',
  'Text Box',
  'Text Area',
  'Display',
  'Checkbox',
  'Checkbox Group',
  'Radio Button',
  'Dropdown',
  'Drop Down',
  'Date',
  'Number',
  'Signature',
  'Group Table',
  'Section Header'
];

function riskClass(risk) {
  const safeRisk = String(risk || 'low').toLowerCase();
  if (safeRisk === 'high') return 'high';
  if (safeRisk === 'medium') return 'medium';
  return 'low';
}

function riskBadge(risk, hideLowRisk = false) {
  const safeRisk = riskClass(risk);
  if (hideLowRisk && safeRisk === 'low') return '';
  return `<span class="risk ${safeRisk}">${safeRisk}</span>`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fieldValue(row, field) {
  return row?.fields?.[field]?.value ?? row?.row?.[field] ?? row?.[field] ?? '';
}

function rowTitle(row) {
  const qtype = fieldValue(row, 'question_type');
  const text = fieldValue(row, 'question_text');
  return `${qtype}${qtype && text ? ' - ' : ''}${text}`;
}

function currentRow() {
  return (manifest?.rows || [])[selectedIndex];
}

function markDirty(message = 'Unsaved changes') {
  dirty = true;
  const status = document.getElementById('saveStatus');
  status.textContent = message;
  status.classList.remove('error');
}

function toNumberOrBlank(value) {
  if (value === '' || value === null || value === undefined) return '';
  const number = Number(value);
  return Number.isFinite(number) ? number : value;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function setpdfZoom(nextZoom) {
  pdfZoom = clamp(Number(nextZoom) || 1, 0.5, 3);
  const frame = document.getElementById('pageFrame');
  const label = document.getElementById('zoomLabel');
  frame.style.width = `${100 * pdfZoom}%`;
  frame.style.maxWidth = `${860 * pdfZoom}px`;
  if (label) label.textContent = `${Math.round(pdfZoom * 100)}%`;
  setHighlight(currentRow(), pageByNumber(currentPage));
}

function changeZoom(delta) {
  setpdfZoom(pdfZoom + delta);
}

function resetZoom() {
  setpdfZoom(1);
}

function initPanelResizers() {
  const shell = document.querySelector('.shell');
  const rowsPanel = shell?.children[0];
  const pdfPanel = shell?.children[2];
  const detailPanel = shell?.children[4];
  if (!shell || !rowsPanel || !pdfPanel || !detailPanel) return;

  const minRows = 240;
  const minpdf = 360;
  const minDetail = 360;

  document.querySelectorAll('[data-resize-splitter]').forEach(splitter => {
    splitter.addEventListener('pointerdown', event => {
      if (window.matchMedia('(max-width: 1100px)').matches) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add('dragging');
      document.body.classList.add('resizing-panels');

      const mode = splitter.dataset.resizeSplitter;
      const startX = event.clientX;
      const startRows = rowsPanel.getBoundingClientRect().width;
      const startpdf = pdfPanel.getBoundingClientRect().width;
      const startDetail = detailPanel.getBoundingClientRect().width;
      shell.style.setProperty('--rows-panel-width', `${Math.round(startRows)}px`);
      shell.style.setProperty('--pdf-panel-width', `${Math.round(startpdf)}px`);

      function availableForRows() {
        return shell.clientWidth - minpdf - minDetail - 48;
      }

      function availableForpdf(rowsWidth) {
        return shell.clientWidth - rowsWidth - minDetail - 48;
      }

      function onMove(moveEvent) {
        const delta = moveEvent.clientX - startX;
        if (mode === 'rows') {
          const nextRows = clamp(startRows + delta, minRows, availableForRows());
          const nextpdf = clamp(startpdf - (nextRows - startRows), minpdf, availableForpdf(nextRows));
          shell.style.setProperty('--rows-panel-width', `${Math.round(nextRows)}px`);
          shell.style.setProperty('--pdf-panel-width', `${Math.round(nextpdf)}px`);
        } else {
          const rowsWidth = rowsPanel.getBoundingClientRect().width;
          const maxpdf = rowsWidth + startpdf + startDetail - minDetail;
          const nextpdf = clamp(startpdf + delta, minpdf, Math.max(minpdf, maxpdf));
          shell.style.setProperty('--pdf-panel-width', `${Math.round(nextpdf)}px`);
        }
        setHighlight(currentRow(), pageByNumber(currentPage));
      }

      function onUp(upEvent) {
        splitter.releasePointerCapture(upEvent.pointerId);
        splitter.classList.remove('dragging');
        document.body.classList.remove('resizing-panels');
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
      }

      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    });
  });
}

function selectRow(index, syncPage = true) {
  if (!manifest?.rows?.length) return;
  if (index < 0 || index >= manifest.rows.length) return;
  const row = manifest.rows[index];
  const nextPage = syncPage && row?.page ? Number(row.page) : currentPage;
  const selectionChanged = selectedIndex !== index;
  const pageChanged = Number(currentPage) !== Number(nextPage);
  if (!selectionChanged && !pageChanged) {
    scrollSelectedRowIntoView();
    return;
  }
  selectedIndex = index;
  if (pageChanged) currentPage = nextPage;
  renderRows();
  scrollSelectedRowIntoView();
  renderPage();
  renderSelected();
}

function renderRows() {
  const rows = manifest.rows || [];
  document.getElementById('rowCount').textContent =
    `${rows.length} rows - ${manifest.summary?.rows_needing_review || 0} need review`;
  if (!rows.length) {
    document.getElementById('rowList').innerHTML =
      '<div class="empty">No rows yet. Use Add item to create a field.</div>';
    return;
  }
  document.getElementById('rowList').innerHTML = rows.map((row, index) => `
    <div class="row-card ${index === selectedIndex ? 'active' : ''}"
         data-row-index="${index}">
      <div class="row-top">
        <span>Seq ${escapeHtml(fieldValue(row, 'sequence') || row.sequence || '')}
          - Page ${escapeHtml(row.page || fieldValue(row, 'page') || '')}</span>
        ${riskBadge(row.risk_level, true)}
      </div>
      <div class="row-text">${escapeHtml(rowTitle(row))}</div>
      <div class="row-actions">
        <button class="mini-btn" type="button" data-row-action="add-before" data-row-index="${index}">+ Add before</button>
        <button class="mini-btn" type="button" data-row-action="add-after" data-row-index="${index}">+ Add after</button>
        <button class="mini-btn danger" type="button" data-row-action="remove" data-row-index="${index}">Remove</button>
      </div>
    </div>
  `).join('');
}

function scrollSelectedRowIntoView() {
  requestAnimationFrame(() => {
    const rowList = document.getElementById('rowList');
    const active = rowList?.querySelector('.row-card.active');
    if (!rowList || !active) return;

    const listRect = rowList.getBoundingClientRect();
    const activeRect = active.getBoundingClientRect();
    const isVisible = activeRect.top >= listRect.top && activeRect.bottom <= listRect.bottom;
    if (isVisible) return;

    rowList.scrollTop = Math.max(
      0,
      rowList.scrollTop + activeRect.top - listRect.top -
        ((rowList.clientHeight - activeRect.height) / 2)
    );
  });
}

function rowPage(row) {
  return Number(row?.page || fieldValue(row, 'page') || currentPage) || currentPage;
}

function bestRect(row) {
  const rowBox = row?.bbox || row?.row?.bbox;
  if (Array.isArray(rowBox) && rowBox.length === 4) return rowBox;
  const sourceRect = row?.source?.nearest_text_block?.rect;
  if (Array.isArray(sourceRect) && sourceRect.length === 4) return sourceRect;
  const fieldRect = row?.fields?.question_text?.evidence?.rect;
  if (Array.isArray(fieldRect) && fieldRect.length === 4) return fieldRect;
  return null;
}

function pageByNumber(pageNo) {
  return (manifest?.pages || []).find(p => Number(p.page) === Number(pageNo));
}

function rowsOnPage(pageNo) {
  return (manifest.rows || [])
    .map((row, index) => ({ row, index }))
    .filter(item => Number(item.row.page || item.row.row?.page || 0) === Number(pageNo));
}

function rectStyle(rect, page) {
  const [x0, y0, x1, y1] = rect.map(Number);
  return [
    `left:${100 * x0 / page.width}%`,
    `top:${100 * y0 / page.height}%`,
    `width:${100 * Math.max(1, x1 - x0) / page.width}%`,
    `height:${100 * Math.max(1, y1 - y0) / page.height}%`
  ].join(';');
}

function setHighlight(row, page) {
  const hl = document.getElementById('highlight');
  const rect = bestRect(row);
  if (!rect || !page?.width || !page?.height || Number(row?.page) !== Number(page.page)) {
    hl.style.display = 'none';
    return;
  }
  hl.style.cssText = rectStyle(rect, page);
  hl.style.display = 'block';
}

function renderPageSelector() {
  const select = document.getElementById('pageSelect');
  const pages = manifest.pages || [];
  select.innerHTML = pages.map(page =>
    `<option value="${escapeHtml(page.page)}">Page ${escapeHtml(page.page)}</option>`
  ).join('');
  select.value = String(currentPage);
  document.getElementById('prevPage').disabled = currentPage <= 1;
  document.getElementById('nextPage').disabled = currentPage >= pages.length;
}

function renderPage() {
  const page = pageByNumber(currentPage);
  const img = document.getElementById('pageImg');
  const layer = document.getElementById('bboxLayer');
  renderPageSelector();
  if (!page) {
    img.removeAttribute('src');
    layer.innerHTML = '';
    return;
  }
  document.getElementById('pageMeta').textContent =
    `Page ${page.page} - click a box to select the row`;
  img.onload = () => setHighlight(currentRow(), page);
  img.src = `/page-image/${jobId}/${page.page}`;
  layer.innerHTML = rowsOnPage(page.page).map(({ row, index }) => {
    const rect = bestRect(row);
    if (!rect) return '';
    return `<div class="bbox ${index === selectedIndex ? 'active' : ''}"
      title="${escapeHtml(rowTitle(row))}"
      style="${rectStyle(rect, page)}"
      onclick="selectRow(${index}, false)"></div>`;
  }).join('');
  setHighlight(currentRow(), page);
}

function fieldInput(field, label, row, options = {}) {
  const value = fieldValue(row, field);
  if (options.select) {
    return `<div class="field">
      <label for="${field}">${label}</label>
      <select id="${field}" onchange="updateField('${field}', this.value)">
        ${options.select.map(option =>
          `<option value="${escapeHtml(option)}" ${String(value) === option ? 'selected' : ''}>
            ${escapeHtml(option || 'Blank')}
          </option>`
        ).join('')}
      </select>
    </div>`;
  }
  if (options.large) {
    return `<div class="field wide">
      <label for="${field}">${label}</label>
      <textarea id="${field}" oninput="updateField('${field}', this.value)">${escapeHtml(value)}</textarea>
    </div>`;
  }
  return `<div class="field">
    <label for="${field}">${label}</label>
    <input id="${field}" type="${options.type || 'text'}" value="${escapeHtml(value)}"
      oninput="updateField('${field}', this.value)" />
  </div>`;
}

function ensureField(row, field) {
  row.fields = row.fields || {};
  if (!row.fields[field]) {
    row.fields[field] = {
      field,
      value: '',
      confidence: 1,
      risk_level: 'medium',
      review_reasons: ['manual_review'],
      evidence: {},
      suggested_action: 'Reviewer edited this field'
    };
  }
  return row.fields[field];
}

function updateField(field, value) {
  const row = currentRow();
  if (!row) return;
  const clean = ['sequence', 'page'].includes(field) ? toNumberOrBlank(value) : value;
  row.row = row.row || {};
  row.row[field] = clean;
  ensureField(row, field).value = clean;
  if (field === 'sequence') row.sequence = clean;
  if (field === 'page') {
    row.page = clean;
    currentPage = Number(clean) || currentPage;
    renderPage();
  }
  markDirty();
  renderRows();
  renderRawJson();
}

function renderRawJson() {
  const box = document.getElementById('rawJson');
  if (box && currentRow()) box.value = JSON.stringify(currentRow(), null, 2);
}

function renderSelected() {
  const row = currentRow();
  if (!row) {
    document.getElementById('detail').innerHTML = '<div class="empty">No rows available.</div>';
    return;
  }
  document.getElementById('rowMeta').textContent =
    `Seq ${row.sequence || '-'} - confidence ${row.row_confidence ?? '-'}`;
  document.getElementById('detail').innerHTML = `
    <div class="summary-line">
      ${riskBadge(row.risk_level)}
      <span class="pill">${escapeHtml(row.suggested_action || '')}</span>
    </div>
    <div class="form-grid">
      ${fieldInput('sequence', 'Sequence', row, { type: 'number' })}
      ${fieldInput('page', 'Page', row, { type: 'number' })}
      ${fieldInput('question_type', 'Question type', row, { select: QUESTION_TYPE_OPTIONS })}
      ${fieldInput('required', 'Required', row, { select: ['', 'Yes', 'No'] })}
      ${fieldInput('question_text', 'Question text', row, { large: true })}
      ${fieldInput('answer_text', 'Answer text', row, { large: true })}
      ${fieldInput('branching_logic', 'Branching logic', row)}
      ${fieldInput('answer_validation', 'Answer validation', row)}
      ${fieldInput('section', 'Section', row)}
    </div>
    <div class="advanced-bar">
      <label class="switch">
        <input type="checkbox" onchange="toggleAdvanced(this.checked)" />
        Advanced row data
      </label>
      <span class="hint">Use this only when the normal fields are not enough.</span>
    </div>
    <div class="raw-box" id="rawBox">
      <textarea class="raw-json" id="rawJson"></textarea>
      <div class="raw-actions">
        <button class="mini-btn" onclick="applyRawJson()">Apply row data</button>
      </div>
    </div>
    <div class="save-row">
      <span class="status">Editing row ${escapeHtml(selectedIndex + 1)} of
        ${escapeHtml((manifest.rows || []).length)}</span>
      <button class="save-btn" onclick="saveManifest()">Save review</button>
    </div>
  `;
  renderRawJson();
  setHighlight(row, pageByNumber(currentPage));
}

function toggleAdvanced(open) {
  document.getElementById('rawBox').classList.toggle('open', open);
  renderRawJson();
}

function applyRawJson() {
  const box = document.getElementById('rawJson');
  try {
    const parsed = JSON.parse(box.value);
    manifest.rows[selectedIndex] = parsed;
    markDirty('Advanced row data applied');
    renderRows();
    renderPage();
    renderSelected();
  } catch (err) {
    const status = document.getElementById('saveStatus');
    status.textContent = 'Row data is not valid JSON';
    status.classList.add('error');
  }
}

function createBlankRow(pageNo) {
  const row = {
    row_id: `manual_${Date.now()}`,
    row_index: 0,
    sequence: '',
    page: pageNo || currentPage,
    bbox: null,
    source_ids: [],
    row_confidence: 1,
    risk_level: 'medium',
    fields: {},
    row: {
      sequence: '',
      page: pageNo || currentPage,
      bbox: null,
      source_ids: [],
      confidence: 1,
      review_reasons: ['manual_row_added'],
      section: '',
      question_type: '',
      question_text: '',
      branching_logic: '',
      answer_text: '',
      answer_validation: '',
      required: ''
    },
    source: {
      page_image: '',
      nearest_text_block: {},
      manual: true
    },
    suggested_action: 'Reviewer added this row'
  };
  REVIEW_FIELDS.forEach(field => ensureField(row, field));
  return row;
}

function resequenceRows() {
  (manifest.rows || []).forEach((row, index) => {
    const sequence = index + 1;
    row.row_index = index;
    if (!row.row_id) row.row_id = `row_${String(index + 1).padStart(4, '0')}`;
    row.sequence = sequence;
    row.row = row.row || {};
    row.row.sequence = sequence;
    ensureField(row, 'sequence').value = sequence;
  });
}

function addRowAt(index = selectedIndex, offset = 1) {
  if (!manifest) return;
  manifest.rows = manifest.rows || [];
  const numericIndex = Number(index);
  const safeIndex = manifest.rows.length
    ? Math.max(-1, Math.min(Number.isFinite(numericIndex) ? numericIndex : selectedIndex, manifest.rows.length - 1))
    : -1;
  const pageNo = rowPage(manifest.rows[safeIndex]);
  const insertAt = Math.max(0, Math.min(safeIndex + offset, manifest.rows.length));
  manifest.rows.splice(insertAt, 0, createBlankRow(pageNo));
  selectedIndex = insertAt;
  currentPage = pageNo;
  resequenceRows();
  markDirty('New row added');
  renderRows();
  scrollSelectedRowIntoView();
  renderPage();
  renderSelected();
}

function addRowBefore(index = selectedIndex) {
  addRowAt(index, 0);
}

function addRowAfter(index = selectedIndex) {
  addRowAt(index, 1);
}

function removeRow(index) {
  if (!manifest?.rows?.length) return;
  const safeIndex = Number(index);
  if (!Number.isInteger(safeIndex) || safeIndex < 0 || safeIndex >= manifest.rows.length) return;
  const removedSelected = safeIndex === selectedIndex;
  const removedBeforeSelected = safeIndex < selectedIndex;
  manifest.rows.splice(safeIndex, 1);
  if (removedBeforeSelected) {
    selectedIndex -= 1;
  } else if (removedSelected) {
    selectedIndex = Math.min(safeIndex, manifest.rows.length - 1);
  }
  selectedIndex = Math.max(0, selectedIndex);
  resequenceRows();
  markDirty('Row removed');
  renderRows();
  scrollSelectedRowIntoView();
  renderPage();
  renderSelected();
}

function setPage(pageNo, selectFirstRow = false) {
  const page = pageByNumber(pageNo);
  if (!page) return;
  currentPage = pageNo;
  if (selectFirstRow) {
    const first = rowsOnPage(pageNo)[0];
    if (first) selectedIndex = first.index;
  }
  renderRows();
  if (selectFirstRow) scrollSelectedRowIntoView();
  renderPage();
  renderSelected();
}

function changePage(delta) {
  setPage(currentPage + delta, true);
}

document.getElementById('addRowBtn').addEventListener('click', event => {
  event.preventDefault();
  addRowAfter(selectedIndex);
});

document.getElementById('rowList').addEventListener('click', event => {
  const actionButton = event.target.closest('[data-row-action]');
  if (actionButton) {
    event.preventDefault();
    event.stopPropagation();
    const index = Number(actionButton.dataset.rowIndex);
    if (actionButton.dataset.rowAction === 'add-before') {
      addRowBefore(index);
    } else if (actionButton.dataset.rowAction === 'add-after') {
      addRowAfter(index);
    } else if (actionButton.dataset.rowAction === 'remove') {
      removeRow(index);
    }
    return;
  }

  const rowCard = event.target.closest('.row-card[data-row-index]');
  if (rowCard) selectRow(Number(rowCard.dataset.rowIndex));
});

async function saveManifest(successMessage = 'Saved and output updated') {
  if (!manifest) return;
  resequenceRows();
  const status = document.getElementById('saveStatus');
  status.textContent = 'Saving...';
  status.classList.remove('error');
  try {
    const res = await fetch(`/manifest-data/${jobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(manifest)
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Save failed');
    manifest = body.manifest;
    dirty = false;
    status.textContent = successMessage;
    renderRows();
    renderPage();
    renderSelected();
    return body;
  } catch (err) {
    status.textContent = err.message;
    status.classList.add('error');
    return null;
  }
}

async function downloadWorkbook() {
  if (!manifest) return;
  const body = await saveManifest('Saved. Starting workbook download...');
  if (!body) return;
  const status = document.getElementById('saveStatus');
  const link = document.createElement('a');
  link.href = `/download/${jobId}?t=${Date.now()}`;
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
  status.textContent = 'Workbook download started';
}

initPanelResizers();
setpdfZoom(1);

fetch(`/manifest-data/${jobId}`)
  .then(res => {
    if (!res.ok) throw new Error('Review manifest is not ready.');
    return res.json();
  })
  .then(data => {
    manifest = data;
    const firstRisk = (manifest.rows || []).findIndex(row => row.risk_level !== 'low');
    selectedIndex = firstRisk >= 0 ? firstRisk : 0;
    currentPage = Number(currentRow()?.page || manifest.pages?.[0]?.page || 1);
    renderRows();
    renderPage();
    renderSelected();
    scrollSelectedRowIntoView();
  })
  .catch(err => {
    document.getElementById('rowList').innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    document.getElementById('detail').innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
  });

document.getElementById('pdfWrap').addEventListener('wheel', event => {
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    changeZoom(event.deltaY > 0 ? -0.1 : 0.1);
  }
}, { passive:false });
</script>
</body></html>
"""


def _manifest_file(job_id: str) -> Path:
    return OUTPUT_DIR / f"{job_id}_review_manifest.json"


def _load_manifest(job_id: str) -> dict[str, Any]:
    path = _manifest_file(job_id)
    if not path.exists():
        raise HTTPException(404, "Review manifest not ready")
    return json.loads(path.read_text(encoding="utf-8"))


_REVIEW_FIELD_NAMES = (
    "sequence",
    "question_type",
    "question_text",
    "branching_logic",
    "answer_text",
    "answer_validation",
    "section",
    "required",
)


def _risk_rank(risk: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(risk or "low").lower(), 2)


def _normalize_saved_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("rows"), list):
        raise HTTPException(400, "Saved review must contain a rows list")

    field_risk_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    row_risk_counts: Counter[str] = Counter()
    rows_needing_review = 0
    field_count = 0

    for index, row in enumerate(manifest["rows"]):
        if not isinstance(row, dict):
            raise HTTPException(400, f"Row {index + 1} is not valid")

        row["row_index"] = index
        row.setdefault("row_id", f"row_{index + 1:04d}")
        row.setdefault("fields", {})
        row.setdefault("row", {})

        for field_name in _REVIEW_FIELD_NAMES:
            field = row["fields"].setdefault(
                field_name,
                {
                    "field": field_name,
                    "value": row["row"].get(field_name, ""),
                    "confidence": 1,
                    "risk_level": "medium",
                    "review_reasons": ["manual_review"],
                    "evidence": {},
                    "suggested_action": "Reviewer edited this field",
                },
            )
            field.setdefault("field", field_name)
            field.setdefault("review_reasons", [])
            field.setdefault("risk_level", "low")
            row["row"][field_name] = field.get("value", row["row"].get(field_name, ""))

        sequence = index + 1
        row["sequence"] = sequence
        row["row"]["sequence"] = sequence
        if isinstance(row["fields"].get("sequence"), dict):
            row["fields"]["sequence"]["value"] = sequence
        row["page"] = row["row"].get("page", row.get("page", ""))
        row["bbox"] = row["row"].get("bbox", row.get("bbox"))
        row["source_ids"] = row["row"].get("source_ids", row.get("source_ids", []))

        risks = [
            str(field.get("risk_level", "low")).lower()
            for field in row["fields"].values()
            if isinstance(field, dict)
        ]
        row["risk_level"] = min(risks or ["low"], key=_risk_rank)
        if row["risk_level"] != "low" or float(row.get("row_confidence") or 1) < 0.85:
            rows_needing_review += 1
        row_risk_counts[row["risk_level"]] += 1

        for field in row["fields"].values():
            if not isinstance(field, dict):
                continue
            field_count += 1
            risk = str(field.get("risk_level", "low")).lower()
            field_risk_counts[risk] += 1
            reason_counts.update(field.get("review_reasons") or [])

    manifest["summary"] = {
        **manifest.get("summary", {}),
        "row_count": len(manifest["rows"]),
        "rows_needing_review": rows_needing_review,
        "field_count": field_count,
        "field_risk_counts": dict(field_risk_counts),
        "review_reason_counts": dict(reason_counts),
        "high_risk_rows": row_risk_counts.get("high", 0),
        "medium_risk_rows": row_risk_counts.get("medium", 0),
        "low_risk_rows": row_risk_counts.get("low", 0),
    }
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    return manifest


def _nullable_int(value: Any) -> int | None:
    if value in ("", None):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rows_from_manifest(manifest: dict[str, Any]) -> list[Row]:
    rows: list[Row] = []
    for item in manifest.get("rows", []):
        snapshot = dict(item.get("row") or {})
        snapshot.setdefault("sequence", item.get("sequence"))
        snapshot.setdefault("page", item.get("page"))
        snapshot.setdefault("bbox", item.get("bbox"))
        snapshot.setdefault("source_ids", item.get("source_ids", []))
        snapshot.setdefault("confidence", item.get("row_confidence", 1.0))
        snapshot.setdefault("review_reasons", item.get("review_reasons", []))
        snapshot["sequence"] = _nullable_int(snapshot.get("sequence"))
        snapshot["page"] = _nullable_int(snapshot.get("page"))
        rows.append(Row(**snapshot))
    return rows


def _template_for_manifest(manifest: dict[str, Any]) -> Path:
    template_path = manifest.get("template_path")
    if template_path:
        template = Path(str(template_path))
        if template.is_file():
            return template
    return DEFAULT_TEMPLATE


def _refresh_outputs_from_manifest(job_id: str, manifest: dict[str, Any]) -> dict[str, str]:
    rows = _rows_from_manifest(manifest)
    out_xlsx = OUTPUT_DIR / f"{job_id}.xlsx"
    review_xlsx = OUTPUT_DIR / f"{job_id}_review.xlsx"
    write_workbook(str(_template_for_manifest(manifest)), str(out_xlsx), rows)
    write_review_sidecar(str(review_xlsx), rows)

    job = JOBS.get(job_id)
    if job is not None:
        job["row_count"] = len(rows)
        job["field_high_risk"] = manifest["summary"]["field_risk_counts"].get("high", 0)
        job["field_medium_risk"] = manifest["summary"]["field_risk_counts"].get("medium", 0)
        job["rows_needing_review"] = manifest["summary"]["rows_needing_review"]
        job["message"] = "Review saved and workbook updated"

    return {
        "workbook": str(out_xlsx),
        "review_workbook": str(review_xlsx),
    }


def _save_manifest(job_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    path = _manifest_file(job_id)
    if not path.exists():
        raise HTTPException(404, "Review manifest not ready")
    manifest = _normalize_saved_manifest(manifest)
    outputs = _refresh_outputs_from_manifest(job_id, manifest)
    manifest["outputs"] = {
        **manifest.get("outputs", {}),
        **outputs,
        "updated_at": manifest["updated_at"],
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def _render_page(template: str, **tokens: str) -> str:
    html = template
    for key, value in tokens.items():
        html = html.replace(f"__{key}__", value)
    return html


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_render_page(_INDEX_HTML))


@app.get("/workbench/{job_id}", response_class=HTMLResponse)
def workbench(job_id: str) -> HTMLResponse:
    _load_manifest(job_id)
    return HTMLResponse(_render_page(_WORKBENCH_EDITOR_HTML, JOB_ID=job_id))


@app.get("/manifest-data/{job_id}")
def manifest_data(job_id: str) -> JSONResponse:
    return JSONResponse(_load_manifest(job_id))


@app.post("/manifest-data/{job_id}")
def save_manifest_data(job_id: str, manifest: dict[str, Any]) -> JSONResponse:
    saved = _save_manifest(job_id, manifest)
    return JSONResponse(
        {
            "status": "saved_and_outputs_updated",
            "manifest": saved,
            "outputs": saved.get("outputs", {}),
        }
    )


@app.get("/page-image/{job_id}/{page_no}")
def page_image(job_id: str, page_no: int):
    manifest = _load_manifest(job_id)
    page = next((p for p in manifest.get("pages", []) if int(p.get("page") or 0) == page_no), None)
    if not page:
        raise HTTPException(404, "Page not found")
    image_path = Path(page.get("image_path") or "")
    if not image_path.exists():
        raise HTTPException(404, "Page image not found")
    return FileResponse(image_path, media_type="image/png")


@app.post("/extract")
async def extract(file: UploadFile = File(...)) -> dict:  # noqa: B008
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, f"Only {_backend_label} files are accepted.")
    job_id = uuid.uuid4().hex[:12]
    pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
    pdf_path.write_bytes(await file.read())
    JOBS[job_id] = {
        "status": "queued",
        "stage": "probe",
        "filename": file.filename,
        "message": "Queued",
        "page_count": 0,
        "pages_done": 0,
    }
    EXECUTOR.submit(_run_pipeline, job_id, pdf_path, file.filename, DEFAULT_TEMPLATE)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    output_path = OUTPUT_DIR / f"{job_id}.xlsx"
    if not output_path.is_file():
        raise HTTPException(404, "Not ready")
    return FileResponse(
        output_path,
        filename=job.get("download_name", f"{job_id}.xlsx") if job else f"{job_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/review/{job_id}")
def review(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    return FileResponse(
        OUTPUT_DIR / f"{job_id}_review.xlsx",
        filename=job.get("review_name", "review.xlsx"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/manifest/{job_id}")
def manifest(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    return FileResponse(
        OUTPUT_DIR / f"{job_id}_review_manifest.json",
        filename=job.get("manifest_name", "review_manifest.json"),
        media_type="application/json",
    )


if __name__ == "__main__":
    import uvicorn
    print(f"\n  {_backend_label} web app on http://localhost:8000")
    print(f"  Model: {MODEL_ID}")
    print(f"  Template: {DEFAULT_TEMPLATE.name}\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
