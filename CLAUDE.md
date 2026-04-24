# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

**pdftoxl** is an evaluation harness for a PDF → EAB-Excel form-extraction pipeline. It is **not** the pipeline itself — it scores candidate `.xlsx` outputs (and future enriched-JSON intermediates) against curated golden fixtures so any pipeline implementation can be measured with the same metrics.

Language: Python 3.11+. CLI framework: Typer. Excel I/O: openpyxl. Contracts: Pydantic v2.

## Directory Layout

```
pdftoExl/
├── src/pdftoxl/
│   ├── pipeline.py                 # Pipeline protocol
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
