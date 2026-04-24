# developer-docs

Working documentation for building **v1** of the PDF → EAB Excel pipeline (see `docs/architecture/architecture_v1.md`) and for evaluating any future **v2** against the same bar.

This folder is the source of truth for:
- **What** to build (`issues/`) — verbose design tickets, one per pipeline stage plus cross-cutting concerns.
- **How we know it works** (`evals/`) — version-agnostic eval specs. v2 may swap the parser, the rule engine, the LLM provider, or even collapse the pipeline into one model — these evals score the contract, not the implementation.

## How to navigate

Read in this order on first pass:

1. `issues/00-overview.md` — pipeline contract, glossary, how the pieces fit.
2. `evals/contracts.md` — the two stable boundaries (Enriched JSON, Workbook) every version must respect.
3. Any single stage issue (e.g. `issues/02-rule-based-classification.md`) and its referenced evals.

## File index

### `issues/` — implementation-facing design tickets

| File | Purpose |
|---|---|
| `00-overview.md` | Cross-pipeline context, glossary, the contract every stage participates in |
| `01-extraction-canonical-json.md` | Stage 1 — PDF → structured block tree (Docling in v1) |
| `02-rule-based-classification.md` | Stage 2 — deterministic block typing with confidence scores |
| `03-ambiguity-decision-gate.md` | Stage 3 — route ambiguous blocks to LLM, bypass the rest |
| `04-llm-refinement.md` | Stage 4 — Bedrock LLM refinement on the ambiguous subset |
| `05-merge-enriched-json.md` | Stage 5 — merge rule + LLM outputs into validated canonical JSON |
| `06-golden-template-mapping.md` | Stage 6 — enriched JSON → 28-column workbook rows |
| `07-excel-writer-output.md` | Stage 7 — write into the golden template `.xlsx`, preserve formatting |
| `08-golden-template-contract.md` | The `.xlsx`-as-schema discipline and how to evolve it |
| `09-observability-and-logging.md` | Structured log shape, per-block traceability |
| `10-cost-and-latency-budget.md` | Per-PDF cost/latency budget and where it's spent |
| `11-security-pii-handling.md` | PII in assessments, what not to log, S3 boundaries |

### `evals/` — version-agnostic evaluation specs

| File | Purpose |
|---|---|
| `README.md` | Eval philosophy and how a v2 implementation plugs in |
| `contracts.md` | The two stable interface boundaries (JSON + Workbook) |
| `fixtures.md` | Fixture catalogue, IDs, curation protocol |
| `eval-A-enriched-json.md` | Structure check on the canonical JSON (optional for v2) |
| `eval-B-workbook.md` | Cell-by-cell workbook check (mandatory) |
| `eval-C-end-to-end.md` | PDF → xlsx black-box eval (mandatory, the v2 regression bar) |
| `eval-D-semantic-equivalence.md` | Catch false regressions when v2 produces equivalent-but-different output |
| `eval-E-cost-latency.md` | Non-functional budget tracking |
| `metrics-glossary.md` | Exact definition of every metric, with worked examples |

## Conventions used across these docs

- **v1 = the pipeline described in `architecture_v1.md`.** When a doc says "v1 chose X," that's a citation, not a constraint on v2.
- **The golden template `.xlsx` is the schema contract.** Column names, controlled vocabularies, and formatting all flow from it. See `issues/08-golden-template-contract.md`.
- **Fixture IDs** are stable across the whole repo. `FX-CHOICES-001` and `FX-TXLTSS-001` are the two seed fixtures. See `evals/fixtures.md`.
- **"28-column" workbook** — the v1 architecture doc says 26 columns, but the actual `Assessment v2` sheet in the CHOICES reference workbook has 28 question-row columns (cols 1–28 starting at "Section"). This is documented in `issues/06-golden-template-mapping.md` and `issues/08-golden-template-contract.md`.
