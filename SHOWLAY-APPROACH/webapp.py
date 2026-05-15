"""
Local web app: upload PDF -> Excel.

  python webapp.py        # then open http://localhost:8000

Features:
  - Drag-and-drop or click-to-upload PDF
  - Live per-page progress (Server-Sent-style polling)
  - Download generated workbook + review sidecar
  - Job state in memory (single-process); restart clears history
"""
from __future__ import annotations
import json, os, time, uuid, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
THIS = Path(__file__).resolve().parent
load_dotenv(THIS / ".env")

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

from showlay.extract import (
    probe_and_rasterize,
    extract_page_with_qwen,
    vlm_dicts_to_rows,
    _bedrock_runtime,
)
from showlay.postprocess import run_all
from showlay.confidence import score_rows
from showlay.writer import write_workbook, write_review_sidecar


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
        job["message"] = "Probing PDF..."
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
        rows = vlm_dicts_to_rows(all_raw)
        rows = run_all(rows, doc_struct=doc, truth_path=str(template_path))

        job["stage"] = "confidence"
        job["message"] = "Scoring confidence..."
        page_text = {p.page_index + 1: " ".join(t["text"] for t in p.text_blocks) for p in doc.pages}
        rows = score_rows(rows, page_text)

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
        job["elapsed_s"] = round(time.time() - t0, 1)
        # Public download names — use original filename minus .pdf
        base = Path(original_name).stem
        job["download_name"] = f"{base}.xlsx"
        job["review_name"] = f"{base}_review.xlsx"
    except Exception as e:
        job["status"] = "error"
        job["stage"] = "error"
        job["message"] = f"{type(e).__name__}: {e}"
        job["traceback"] = traceback.format_exc()


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<title>SHOWLAY · Form Extraction Studio</title>
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
    <span>SHOWLAY · Form Extraction Studio</span>
  </div>
  <div class="topbar-meta">
    <span class="pill"><span class="pill-dot"></span> Bedrock&nbsp;us-west-2</span>
  </div>
</nav>

<main class="page">
  <header>
    <div class="eyebrow">Document Intelligence · POC</div>
    <h1>PDF&nbsp;→&nbsp;Excel form extractor</h1>
    <p class="sub">Upload a healthcare or insurance form PDF. Receive a 28-column structured workbook and a confidence-sorted human review queue — fastest path from paper to platform.</p>
  </header>

  <section class="card" id="uploadCard">
    <label class="drop" id="dropZone">
      <div class="drop-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      </div>
      <div class="drop-title">Drop a PDF here, or click to browse</div>
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
      <div class="stage" data-key="write">      <div class="stage-dot">5</div> <span>Write workbook & review sidecar</span></div>
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
    </div>
    <button class="btn btn-ghost" onclick="location.reload()">Process another PDF</button>
  </section>

  <section class="card" id="errorCard" style="display:none; border-color: rgba(239,68,68,.4)">
    <h3 style="color: var(--err)">Something went wrong</h3>
    <div class="err-box" id="errMsg"></div>
    <button class="btn btn-ghost" onclick="location.reload()">Try again</button>
  </section>

  <footer>
    Powered by Qwen3-VL on AWS Bedrock <code>us-west-2</code> · ~30s per page · 28-column EAB template<br/>
    <span style="color:var(--dim)">SHOWLAY v25 · proof-of-concept · not for production health data</span>
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

const stageOrder = ['probe', 'extract', 'postprocess', 'confidence', 'write', 'done'];
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
}
function showError(msg) {
  errorCard.style.display = 'block';
  document.getElementById('errMsg').textContent = msg;
  progressCard.style.display = 'none';
}
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.post("/extract")
async def extract(file: UploadFile = File(...)) -> dict:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")
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
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    return FileResponse(
        OUTPUT_DIR / f"{job_id}.xlsx",
        filename=job.get("download_name", "output.xlsx"),
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


if __name__ == "__main__":
    import uvicorn
    print(f"\n  SHOWLAY web app on http://localhost:8000")
    print(f"  Model: {MODEL_ID}")
    print(f"  Template: {DEFAULT_TEMPLATE.name}\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
