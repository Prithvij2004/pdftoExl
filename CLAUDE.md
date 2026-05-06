# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Backend (Python, run from repo root with `.venv` activated):

- Install deps: `pip install -r requirements.txt`
- Run API: `uvicorn app.main:app --reload`
- Run pipeline on a PDF (no API): `python -m app.run_pipeline path/to.pdf --pretty -o out.json`
- All tests: `pytest`
- Single test file: `pytest tests/test_chunker.py`
- Single test: `pytest tests/test_chunker.py::test_name`

`pytest.ini` sets `pythonpath = .` so tests import `app.*` directly.

Frontend (`frontend/`):

- Dev server: `npm run dev` (Vite, defaults to `http://localhost:5173`)
- Build: `npm run build` (runs `tsc -b && vite build`)

The backend's CORS allowlist defaults to `http://localhost:5173`; override with the `CORS_ORIGINS` env var (comma-separated).

## Configuration

Config is loaded by `app.config.load_pipeline_config()` from environment (`.env` is auto-loaded by `app/main.py`). Key vars: `BEDROCK_REGION` (or `AWS_REGION`), `BEDROCK_VISION_MODEL_ID` (default `qwen.qwen3-vl-235b-a22b`), `BEDROCK_TEXT_MODEL_ID` (default `qwen.qwen3-235b-a22b-2507-v1:0`), `PAGE_RENDER_DPI`, `COMPLEXITY_UNCERTAIN_THRESHOLD`, `MAX_CHUNK_INPUT_TOKENS`, `MAX_CONCURRENT_BEDROCK_CALLS`, `EXTRACTION_CACHE_DIR` (default `runtime/cache`).

When `bedrock_region` is unset, the pipeline still runs but skips Bedrock-dependent stages (VLM classifier, VLM page extractor, semantic merger): code paths in `services/pipeline.py` instantiate Bedrock clients only when a region is configured. Tests rely on this — they construct the pipeline with no region and inject fakes.

## Architecture

The product is a staged hybrid PDF→XLSX form extraction pipeline. The full design contract lives in `plan.md` (data shapes, canonical question types, branching templates, acceptance targets) — read it before changing pipeline semantics.

`POST /api/extract` (in `app/main.py`) accepts a PDF, runs `CurrentExtractionPipeline.extract_to_workbook`, and streams the resulting XLSX. The pipeline orchestration is in `app/services/pipeline.py`; each phase is its own service module:

1. `page_preparation.py` — hashes the PDF, renders pages to images at `PAGE_RENDER_DPI` into the cache dir; produces `PreparedPdf`.
2. `layout_parser.py` — cheap PyMuPDF text/widget/table extraction → `RawPdfModel`.
3. `semantic_hints.py` — converts raw layout to `SemanticPageModel` with stable block IDs (`p3_b08`) and role hints (section_heading, choice_option, table, etc.). This is the LLM-friendly representation; raw coordinates are not exposed to the LLM.
4. `complexity_router.py` — deterministic per-page complexity score; uncertain pages are escalated to a Bedrock vision classifier (`BedrockQwenComplexityClassifier`) which decides parser vs. vlm path.
5. `parser_path.py` — for `parser`-routed pages: builds Markdown + semantic page artifacts.
6. `vlm_path.py` — for `vlm`-routed pages: calls `qwen.qwen3-vl-235b-a22b` on the rendered image, normalizes output into the same `SemanticPageModel` so downstream stages are path-agnostic. After this stage the pipeline merges VLM-derived semantic pages over parser-derived ones.
7. `chunker.py` (`SemanticChunker`) — builds `Chunk`s with stable `chunk_id` and `order_key` from the unified semantic page list; respects token budget, keeps choice groups/tables intact, merges cross-page continuations.
8. `chunk_extractor.py` — runs `qwen.qwen3-235b-a22b-2507-v1:0` per chunk, returns structured rows; concurrency bounded by `MAX_CONCURRENT_BEDROCK_CALLS`.
9. `merge_resolve.py` — sorts by `order_key`, runs deterministic dedupe/sequence assignment/branching-template validation; uses `BedrockQwenSemanticMerger` only for genuinely semantic conflicts.
10. `xlsx_writer.py` — writes the seven canonical columns (`Section | Sequence | Question Rule | Question Type | Question Text | Branching Logic | Answer Text`) with the styling fixed by golden tests.

Internal row fields (`source_ids`, `chunk_id`, `page_numbers`, `confidence`) flow through the pipeline but are dropped at the XLSX boundary.

`CurrentExtractionPipeline.__init__` accepts overrides for every stage — this is the seam tests use to inject deterministic fakes for Bedrock-dependent components. Prefer that pattern over monkey-patching.

Reference materials in `docs/` (`requirements.md`, `agentic_extractor_review.md`, sample PDFs and golden XLSX files) define the acceptance targets; `runtime/` holds intermediate artifacts and the render cache.

## Folder structure

```text
pdftoExl/
├── app/                          # FastAPI backend + pipeline
│   ├── main.py                   # FastAPI app, /api/health, /api/extract
│   ├── config.py                 # PipelineConfig + load_pipeline_config()
│   ├── run_pipeline.py           # CLI: python -m app.run_pipeline
│   ├── parse_layout.py           # standalone layout-parse helpers
│   ├── prepare_pages.py          # standalone page-prep helpers
│   ├── api/                      # (HTTP-layer helpers)
│   └── services/                 # one module per pipeline stage
│       ├── pipeline.py           # CurrentExtractionPipeline orchestrator
│       ├── page_preparation.py   # phase 1: render + cache
│       ├── layout_parser.py      # phase 2: cheap PyMuPDF parse
│       ├── semantic_hints.py     # phase 3: SemanticPageModel
│       ├── complexity_router.py  # phase 4: parser-vs-vlm routing
│       ├── parser_path.py        # phase 5: parser artifacts
│       ├── vlm_path.py           # phase 6: Qwen VLM page extraction
│       ├── chunker.py            # phase 7: SemanticChunker
│       ├── chunk_extractor.py    # phase 8: per-chunk Qwen extraction
│       ├── merge_resolve.py      # phase 9: dedupe/merge/validate
│       ├── xlsx_writer.py        # phase 10: workbook writer
│       └── prompts.py            # shared LLM prompts
├── tests/                        # pytest suite, one test_*.py per service
│   └── test_pipeline_e2e.py      # end-to-end with injected fakes
├── frontend/                     # Vite + React 19 + TypeScript SPA
│   ├── src/main.tsx              # app entry
│   ├── src/styles.css
│   ├── index.html
│   ├── vite.config.ts
│   └── tsconfig.json
├── docs/                         # requirements, review notes, golden XLSX, sample PDFs
├── runtime/                      # render cache, generated artifacts, uploads (gitignored content)
├── evals/                        # evaluation harness/data
├── extract_form.py               # legacy single-pass extractor (pre-pipeline)
├── plan.md                       # authoritative pipeline design contract
├── requirements.txt
├── pytest.ini                    # sets pythonpath = .
└── CLAUDE.md
```
