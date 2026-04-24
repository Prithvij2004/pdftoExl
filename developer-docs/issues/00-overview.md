# Issue 00 — Pipeline overview, glossary, and the cross-stage contract

## Summary

This issue is the entry point for any developer building or auditing the PDF → EAB Excel pipeline. It defines the contract that every stage participates in, fixes the vocabulary used across `developer-docs/`, and tells you which other issues to read for any given change.

The pipeline takes a state-approved assessment PDF (TennCare CHOICES Safety Determination, TX LTSS ISP, etc.) and produces an EAB-ready Excel workbook. The output workbook is **structurally identical** to a hand-crafted reference (the golden template) — same column order, same controlled-vocabulary values, same formatting. The "intelligence" is in correctly classifying each block on the PDF and mapping it into the right row.

## The end-to-end contract (v1)

```
PDF  ──▶  Stage 1: Extraction         ──▶  Canonical JSON (raw blocks)
                                               │
         Stage 2: Rule classification ◀───────┘
                  │
                  ▼
         Stage 3: Ambiguity gate ────► (high-confidence) ──┐
                  │                                        │
                  ▼                                        │
         Stage 4: LLM refinement (low-confidence subset)   │
                  │                                        │
                  └──▶ Stage 5: Merge ◀────────────────────┘
                              │
                              ▼
                       Enriched JSON  ◀── Eval A boundary
                              │
                              ▼
              Stage 6: Golden-template mapping
                              │
                              ▼
              Stage 7: Excel writer
                              │
                              ▼
                       .xlsx workbook  ◀── Eval B / C / D boundary
```

Two boundaries are public:

1. **Enriched JSON** (post-stage-5) — see `evals/contracts.md` Boundary 1. This is what Eval A scores. v2 implementations may skip this boundary if they're end-to-end.
2. **Workbook** (post-stage-7) — see `evals/contracts.md` Boundary 2. This is what Evals B, C, and D score. **Mandatory for any version.**

Everything between the two boundaries is implementation detail. v1 chose a 7-stage hybrid pipeline because it's debuggable and bounds LLM cost. v2 may legitimately choose a different decomposition.

## Glossary

| Term | Definition |
|---|---|
| **Block** | A single semantic unit on the PDF — a paragraph, heading, checkbox option, table cell, signature line. Output of stage 1. |
| **Block type** | The semantic role assigned to a block. Examples: `SectionHeader`, `QuestionLabel`, `CheckboxOption`, `TextInput`, `Display`, `SignatureField`. Assigned in stages 2 and 4. |
| **QuestionType** | The EAB-template controlled vocabulary value: `Radio Button`, `Checkbox`, `Checkbox Group`, `Text Area`, `Text Box`, `Date`, `Display`, etc. (See `Values` sheet of the golden template.) Distinct from "block type" — block types are internal; QuestionType is the external contract. |
| **Branching Logic** | An EAB DSL expression that controls when a question is shown. Example: `Display if Q7 = "Lives in other's home"`. v1 generates these from parent-link information. |
| **Confidence** | A scalar in `[0, 1]` attached to every block-type classification. The gate (stage 3) routes blocks below threshold to the LLM. |
| **Golden template** | The versioned `.xlsx` file that defines the output schema — column order, formatting, controlled-vocabulary dropdowns, supporting sheets. The pipeline writes *into* this template; it never builds a workbook from scratch. See issue 08. |
| **Fixture** | A `(PDF, expected enriched JSON, reference workbook)` triple, identified by a stable ID (e.g. `FX-CHOICES-001`). See `evals/fixtures.md`. |
| **Eval A / B / C / D / E** | The five evaluation specs. A = JSON structure, B = workbook cells, C = end-to-end black box, D = semantic equivalence, E = cost/latency. See `evals/`. |
| **Boundary 1 / Boundary 2** | The two public contracts (Enriched JSON, Workbook). v2 must satisfy Boundary 2. |

## What every stage must emit (cross-cutting requirement)

Every stage MUST emit a structured log line per block it touches, with at minimum:

- `block_id` — stable across stages
- `stage` — `extract` | `classify` | `gate` | `llm` | `merge` | `map` | `write`
- `decision` — what this stage did (e.g. `classified=CheckboxOption`, `routed=llm`, `wrote_row=42`)
- `confidence` — if applicable
- `latency_ms`
- `cost_usd` — non-zero only for stage 4

This is what makes the per-block trace in issue 09 possible, and what Eval E aggregates over.

## Reading order for common tasks

| If you're… | Read |
|---|---|
| Building stage 2 rules from scratch | 02, 03, 06, eval-A |
| Tuning the LLM gate threshold | 03, 04, 10, eval-E |
| Adding a new column to the workbook | 06, 07, 08, eval-B |
| Onboarding a new state's assessment | fixtures.md, 01, 02, eval-C |
| Debugging a single bad row | 09, eval-B (per-column accuracy section) |
| Evaluating a v2 prototype | evals/README, evals/contracts, eval-C, eval-D |

## Cross-references

- v1 architecture: `docs/architecture/architecture_v1.md`
- Source fixtures: `docs/support_docs/`
