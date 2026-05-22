# SHOWLAY-APPROACH - PDF to 28-column Assessment Excel

Built 2026-05-07. Targets ≥90% row accuracy with confidence flagging for human review.

## Why this exists

The three existing branches (agentic-pydantic-ai, chunk-approach, sequence-sections) sit at <50% accuracy. The audit found a structural cap: they each emit only 5–7 of the 28 target columns, miss all 8 Yes/No flag columns, ignore AcroForm widgets, never expose confidence, and can't handle drastically different PDFs.

## Approach: multi-evidence hybrid + confidence gating

```
PDF
 │
 ├─ probe          (AcroForm? scanned? table-heavy? page count → routing)
 ├─ rasterize      (200 DPI PNG per page; reused by VLM)
 ├─ widgets        (PyMuPDF AcroForm walk → bboxes + field-types when interactive)
 ├─ text_layout    (PyMuPDF get_text("dict") → words with bboxes — coordinate-grounded text)
 │
 ├─ VLM extract    (Qwen3-VL-235B on Bedrock, one call per page, 28-column JSON schema in prompt;
 │                   structural hints from widgets + layout passed in as context)
 │
 ├─ post-process   (section forward-fill from "New Section" Display rows;
 │                   sequence assignment by visual reading order;
 │                   branching DSL normalization — REDCap-style "If Q<n> = checked(selected)")
 │
 ├─ confidence     (schema gate, span grounding, structural agreement → per-row confidence + review_reasons)
 │
 └─ write          (28-column Excel from source-template; companion *_review.xlsx for human queue)
```

Verifier pass with Claude Haiku 4.5 reserved for v2 — v1 ships baseline first.

## Key decisions (with sources)

- **Primary VLM = Qwen3-VL-235B on Bedrock us-west-2** — cheap, strong OCR, in-region inference. Verified live `qwen.qwen3-vl-235b-a22b` model card, 15s/page tested.
- **Verifier = Claude Haiku 4.5** — has native PDF document block, cheap, good at schema-locked output.
- **No native PDF for Qwen3-VL** — must rasterize ourselves; Converse caps at 5 images per call so we batch by page.
- **REDCap-style branching DSL** — `[Q<seq>] = '<value>'` is the lingua franca of healthcare assessment platforms; our gold uses the variant `If Q<seq> = checked(selected)` and `Display if Q<seq> = <literal>`.
- **Section forward-fill from "New Section" Display rows** — discovered in CHOICES gold: a Display row with `Question Text == "New Section"` and section title in `Answer Text` is the section delimiter. Subsequent rows have blank Section.

## Structure

```
SHOWLAY-APPROACH/
├── .env.example              # copy to .env for AWS credentials
├── pyproject.toml            # standalone install + test config
├── requirements.txt          # runtime dependencies
├── run.py                    # CLI: python run.py <pdf> <template_xlsx>
├── webapp.py                 # FastAPI app: python webapp.py
├── support_docs/             # bundled default templates and sample PDFs
├── showlay/                  # extraction, post-processing, review, writer code
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

The web app uses this bundled template by default:

```text
support_docs/CHOICES Safety Determination Request Form Final_11_20.xlsx
```

You can override it with `SHOWLAY_TEMPLATE_PATH=/path/to/template.xlsx`.

## Run CLI

```bash
cd "SHOWLAY-APPROACH"
python run.py "support_docs/CHOICES Safety Determination Form.pdf" \
              "support_docs/CHOICES Safety Determination Request Form Final_11_20.xlsx"
```

Outputs go to `runtime/output/<name>/`.

## Test

```bash
cd "SHOWLAY-APPROACH"
pip install -r requirements-dev.txt
pytest
```
