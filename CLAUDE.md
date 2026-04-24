# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

**pdftoxl** contains two cooperating pieces:

1. **PipelineV1** (`src/pdftoxl/pipeline_v1/`) — the PDF → EAB-Excel form-extraction pipeline itself. Stages run end-to-end and emit an `.xlsx` in the EAB template shape.
2. **Evals harness** (`src/pdftoxl/evals/`) — pipeline-agnostic scoring that grades any candidate `.xlsx` (and future enriched-JSON intermediates) against curated golden fixtures.

Language: Python 3.11+. CLI framework: Typer. Excel I/O: openpyxl. Contracts: Pydantic v2. LLM: AWS Bedrock via boto3.

## Directory Layout

```
pdftoExl/
├── src/pdftoxl/
│   ├── cli.py                      # `pdftoxl` CLI — runs PipelineV1 on a fixture
│   ├── pipeline.py                 # Pipeline protocol
│   ├── adapters/                   # Third-party SDK wrappers (keep stages SDK-free)
│   │   ├── bedrock.py              # Bedrock Messages-API client
│   │   ├── env.py                  # .env → os.environ loader
│   │   └── logging.py              # structlog configuration
│   ├── pipeline_v1/                # Concrete pipeline implementation
│   │   ├── pipeline.py             # PipelineV1 + build_pipeline()
│   │   ├── config.py               # PipelineConfig (pydantic-settings) + load_config()
│   │   └── stages/                 # extraction → classification → gate → llm →
│   │       │                       #   merge → mapping → output
│   │       ├── extraction.py       # PDF → raw blocks
│   │       ├── classification.py   # Rule-based block typing
│   │       ├── gate.py             # Confidence gate; defers low-confidence to LLM
│   │       ├── llm.py              # Bedrock-backed enrichment for deferred blocks
│   │       ├── merge.py            # Combine accepted + enriched, drop low-conf
│   │       ├── mapping.py          # Blocks → 28-column EAB rows
│   │       └── output.py           # Write rows into template `.xlsx`
│   └── evals/
│       ├── cli.py                  # `pdftoxl-evals` Typer app
│       ├── runner.py               # run_eval_b, run_eval_c, run_all
│       ├── contracts.py            # Pydantic models (blocks, metrics, results)
│       ├── fixtures.py             # fixtures.yaml loader
│       ├── workbook.py             # Sheet read/compare helpers
│       ├── normalize.py            # Cell normalization
│       ├── dsl.py                  # Branching-logic DSL parser
│       ├── report.py               # Writes Markdown + JSON reports
│       ├── metrics/
│       │   ├── eval_a.py           # Enriched JSON validation (optional)
│       │   ├── eval_b.py           # Workbook cell-by-cell (mandatory)
│       │   ├── eval_c.py           # End-to-end weighted distance (mandatory)
│       │   ├── eval_d.py           # Semantic equivalence (stub)
│       │   └── eval_e.py           # Cost/latency telemetry (stub)
│       └── scripts/export_schema.py
├── config.yaml                     # Default PipelineV1 config (stages, thresholds, Bedrock)
├── .env.example                    # Copy to `.env` for AWS creds + overrides
├── evals/
│   ├── fixtures.yaml               # Fixture catalogue
│   ├── fixtures/
│   │   ├── FX-CHOICES-001/         # TennCare CHOICES (11pg, 106 questions)
│   │   └── FX-TXLTSS-001/          # TX LTSS signature page (1pg)
│   └── reports/                    # Eval output (JSON + Markdown per run)
├── tests/{unit,integration}/
├── developer-docs/evals/           # Eval specs — read before changing metrics
│   ├── README.md                   # Overview; start here
│   ├── contracts.md                # Boundary 1 (JSON) & Boundary 2 (.xlsx)
│   ├── fixtures.md                 # Fixture curation protocol
│   ├── metrics-glossary.md
│   ├── eval-{A,B,C,D,E}-*.md
│   └── schema/enriched.schema.json
├── docs/architecture/architecture_v1.md
├── pyproject.toml
└── Makefile
```

## Install & Common Commands

```bash
make install        # pip install -e '.[dev]'
make lint           # ruff check src tests
make test           # pytest
make eval           # Eval B + Eval C on all fixtures
```

## Running the Pipeline (PDF → .xlsx)

Entrypoint: `pdftoxl` (defined in `pyproject.toml` → `pdftoxl.cli:app`). PipelineV1 runs against a **fixture ID** — the fixture's `input.pdf` is the input and the fixture's `golden.xlsx` is reused as the output template (so the EAB sheet/columns/formatting are preserved).

```bash
# Offline run (stages 1–3 + 5–7; LLM stage skipped — no AWS creds needed)
pdftoxl run --fixture FX-TXLTSS-001 --out /tmp/txltss.xlsx

# Full run with the Bedrock LLM stage enabled
cp .env.example .env    # fill in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
pdftoxl run --fixture FX-CHOICES-001 --out /tmp/choices.xlsx --with-llm

# Custom pipeline config (stage toggles, thresholds, Bedrock model)
pdftoxl run --fixture FX-CHOICES-001 --out /tmp/out.xlsx --config ./config.yaml

# Custom fixtures catalogue
pdftoxl run --fixture FX-X --out /tmp/out.xlsx --fixtures-yaml path/to/fixtures.yaml
```

Scoring a produced workbook against its golden:

```bash
pdftoxl-evals run --eval B --fixture FX-CHOICES-001 --candidate /tmp/choices.xlsx
pdftoxl-evals run --eval C --fixture FX-CHOICES-001 --candidate /tmp/choices.xlsx
```

### Using a PDF that is not yet a fixture

The CLI only accepts fixture IDs, so to run an arbitrary PDF, register it as a fixture first (see **Adding a Fixture** below): drop `input.pdf` (and a template `golden.xlsx`) under `evals/fixtures/FX-<NAME>/`, add the entry to `evals/fixtures.yaml`, then `pdftoxl run --fixture FX-<NAME> --out …`.

### Configuration precedence

`--config` flag → `PDFTOXL_CONFIG` env → `./config.yaml` → built-in defaults. Any field can be overridden by env vars prefixed `PDFTOXL_` with `__` as the nested delimiter (e.g. `PDFTOXL_BEDROCK__MODEL_ID=...`, `PDFTOXL_STAGES__LLM=false`). AWS creds + `BEDROCK_MODEL_ID` come from `.env` (see `.env.example`).

### Pipeline stages

Each stage is a pure function taking the previous stage's artifact; toggle any of them in `config.yaml` under `stages:` while iterating. The `--with-llm` flag is required in addition to `stages.llm: true` for the Bedrock client to actually be built — this keeps tests and offline runs from needing AWS credentials.

## Running Evals

Entrypoint: `pdftoxl-evals` (defined in `pyproject.toml` → `pdftoxl.evals.cli:app`).

```bash
# Single eval, single fixture
pdftoxl-evals run --eval B --fixture FX-CHOICES-001
pdftoxl-evals run --eval C --fixture FX-TXLTSS-001

# All fixtures
pdftoxl-evals run --eval B --all
pdftoxl-evals run --eval C --all

# Score an external candidate workbook (Eval B only)
pdftoxl-evals run --eval B --fixture FX-CHOICES-001 \
  --candidate /path/to/candidate.xlsx

# Custom reports directory
pdftoxl-evals run --eval B --all --reports-dir /tmp/out
```

Reports land in `evals/reports/FX-<ID>.<EVAL>.{md,json}`.

## The Five Evals

| Eval | Boundary | Status | Purpose |
|------|----------|--------|---------|
| A | Enriched JSON | optional | Block typing, parent links, DSL syntax, sequence |
| **B** | `.xlsx` | **mandatory** | 28-column accuracy, vocab, branching syntax, formatting |
| **C** | PDF → `.xlsx` | **mandatory** | Weighted distance; customer-facing go/no-go |
| D | `.xlsx` | stub | Semantic equivalence (guard against false regressions) |
| E | telemetry | stub | Cost & latency budget tracking |

**Eval B** per-column accuracy thresholds (see `developer-docs/evals/eval-B-workbook.md`):
- Critical (≥0.97): QuestionType, Question Text, Sequence
- High (≥0.92): Branching Logic, Section, Required, Answer Text, …
- Medium (≥0.85): Auto Populate fields, Yes/No columns
- Low (≥0.99 blank): Concept Code, Token ID, IT Notes, …

**Eval C** collapses B's column scores into a weighted distance (Critical=5, High=3, Medium=1, Low=0) plus penalties (OOV ≤ +0.30, non-contiguous sequence ≤ +0.10, formatting loss = +0.20). Pass threshold: **≤ 0.05** per fixture.

## Adding a Fixture

1. Drop `input.pdf` and `golden.xlsx` under `evals/fixtures/FX-<NAME>/`.
2. Append an entry to `evals/fixtures.yaml` with `id`, paths, `question_sheet`, `header_row`, `schema_version`, `notes`.
3. Verify with `pdftoxl-evals run --eval B --fixture FX-<NAME>`.

See `developer-docs/evals/fixtures.md` for the curation protocol.

## Working Conventions

- Evals are pipeline-version-agnostic — don't bake pipeline-specific assumptions into metric code.
- Before changing a metric or threshold, read the matching spec in `developer-docs/evals/`.
- Stable boundaries are the enriched-JSON schema (`developer-docs/evals/schema/enriched.schema.json`) and the `.xlsx` template. Breaking either requires a spec update.
- No secrets or `.env` — all config lives in `fixtures.yaml` and CLI flags.
