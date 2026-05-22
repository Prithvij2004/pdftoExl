# SHOWLAY-APPROACH - PDF to Assessment Template Excel

Built 2026-05-07. Targets ≥90% row accuracy with confidence flagging for human review.

## Why this exists

The three existing branches (agentic-pydantic-ai, chunk-approach, sequence-sections) sit at <50% accuracy. The audit found a structural cap: they each emit only 5–7 of the 28 target columns, miss all 8 Yes/No flag columns, ignore AcroForm widgets, never expose confidence, and can't handle drastically different PDFs.

## Approach: LangGraph section-aware agent + confidence gating

```
PDF
 │
 ├─ probe          (AcroForm? scanned? table-heavy? page count → routing)
 ├─ rasterize      (200 DPI PNG per page; reused by VLM)
 ├─ widgets        (PyMuPDF AcroForm walk → bboxes + field-types when interactive)
 ├─ text_layout    (PyMuPDF get_text("dict") → words with bboxes — coordinate-grounded text)
 │
 ├─ profile        (LangGraph node calls Bedrock tool-use for title, sections, complexity)
 │
 ├─ section extract (LangGraph loop calls Bedrock tool-use per section or one call for small forms)
 │
 ├─ normalize      (LangGraph node densifies sections, sequences, branching refs, validation defaults)
 │
 ├─ confidence     (schema gate, span grounding, structural agreement → per-row confidence + review_reasons)
 │
 └─ write          (CHOICES workbook `Assessment` sheet structure; companion *_review.xlsx for human queue)
```

The profiler, section planner, section extractor, and normalizer now run through
`showlay.agentic.document_extraction_graph`, a compiled LangGraph `StateGraph`.
`run.py`, `run_new_pdfs.py`, and `webapp.py` keep using `extract_document_agentic`,
so CLI and web behavior stay stable while the orchestration is graph-based.

Textract is not required in this implementation. The interface can be added later, but the current path works from PyMuPDF evidence plus Bedrock vision/tool-use.

## Key decisions (with sources)

- **Default profiler = Amazon Nova 2 Lite on Bedrock us-west-2** via `us.amazon.nova-2-lite-v1:0`.
- **Default section extractor = Amazon Nova Pro** via `us.amazon.nova-pro-v1:0`, with env overrides still supported.
- **Extraction orchestration uses LangGraph `StateGraph`** so profiling, section planning, extraction, and normalization are explicit nodes.
- **Structured output uses Bedrock tool-use + Pydantic schemas**, not loose JSON parsing.
- **Excel output uses the bundled CHOICES HIP workbook's `Assessment` sheet**, not `Assessment v2`.
- **Forward branching refs are allowed** because skip logic can point to later questions.

## Structure

```
SHOWLAY-APPROACH/
├── .env.example              # copy to .env for AWS credentials
├── pyproject.toml            # standalone install + test config
├── requirements.txt          # runtime dependencies
├── run.py                    # CLI: python run.py <pdf> [template_xlsx]
├── webapp.py                 # FastAPI app: python webapp.py
├── docs/                     # new approach design and architecture docs
├── support_docs/             # bundled default templates and sample PDFs
├── showlay/                  # agent, canonical schemas, normalization, review, writer code
├── tests/                    # SHOWLAY unit tests
└── runtime/                  # generated files; ignored by git
```

The folder is now self-contained. It does not need files from the parent repo.

## Setup

```bash
cd "SHOWLAY-APPROACH"
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with AWS Bedrock credentials.

## Run Web App

```bash
cd "SHOWLAY-APPROACH"
python webapp.py
```

Open `http://localhost:8000`.

The web app writes into the bundled workbook's `Assessment` sheet structure:

```text
support_docs/choices-safety-determination-hip-workbook.xlsx
```

You can override it with `SHOWLAY_TEMPLATE_PATH=/path/to/template.xlsx`.

## Run CLI

```bash
cd "SHOWLAY-APPROACH"
python run.py "support_docs/choices-safety-determination-source.pdf" \
              "support_docs/choices-safety-determination-hip-workbook.xlsx"
```

Outputs go to `runtime/output/<name>/`.

For offline golden comparison only:

```bash
python run.py "support_docs/tx-ltss-h1700-3-signature-page-source.pdf" \
  --eval-truth "support_docs/tx-ltss-h1700-3-signature-page-hip-workbook.xlsx"
```

## Test

```bash
cd "SHOWLAY-APPROACH"
pip install -r requirements-dev.txt
pytest
```
