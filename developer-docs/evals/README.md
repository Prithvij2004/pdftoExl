# Evals — version-agnostic evaluation suite

This folder defines how we measure whether a PDF→EAB-Excel pipeline works. The specs are deliberately decoupled from v1's implementation choices: a v2 that swaps Docling for AWS Textract, replaces the rule engine with a fine-tuned classifier, or collapses everything into a single end-to-end model can be scored on the **same fixtures** with the **same metrics** as v1.

If a metric here mentions "Docling," "Bedrock," "openpyxl," or "Pydantic," that's a bug — file it.

## The five evals

| Eval | Boundary | Mandatory? | Catches |
|---|---|---|---|
| **A** — Enriched JSON structure | Boundary 1 (post-merge JSON) | Optional for v2 | Block typing errors, parent-link drift, branching syntax errors, sequence drift |
| **B** — Workbook cell-by-cell | Boundary 2 (.xlsx) | **Mandatory** | Mapping bugs, controlled-vocab violations, formatting regressions |
| **C** — End-to-end black-box | PDF in / .xlsx out | **Mandatory** | Anything that affects the final output, regardless of internal stages |
| **D** — Semantic equivalence | .xlsx | Mandatory if v2 changes mapping | Catches *false* regressions when v2 produces equivalent-but-different output |
| **E** — Cost and latency | Telemetry | Tracked, not pass/fail | Budget regressions |

## How v2 plugs in

A v2 implementation must:

1. Read the same fixture catalogue (`fixtures.md`) — the inputs and references don't change.
2. Produce a workbook compatible with **Boundary 2** (the golden template `.xlsx` schema). See `contracts.md`.
3. Emit the run-level telemetry described in `eval-E-cost-latency.md` so cost/latency are comparable.
4. **Optionally**, produce a Boundary-1 enriched JSON if it wants Eval A coverage. End-to-end models that have no JSON intermediate skip Eval A and rely on B/C/D.

A v2 implementation does **not** need to use Python, Pydantic, openpyxl, or any other v1-specific tool. The eval harness reads `.xlsx` files and JSON; the implementation language is irrelevant.

## Eval philosophy

- **Goldens are versioned with `template_version` and `schema_version`.** A schema bump invalidates goldens; the regeneration is part of the bump PR.
- **Pass/fail per metric, not per fixture.** A fixture passes if every per-metric threshold is met. A regression on one metric is visible even if the others improve.
- **Per-fixture scores are kept** — never averaged into a single number that hides the actual failure. Aggregate views are convenience; the per-fixture table is canonical.
- **Hand-curated goldens.** Approved production outputs become fixtures (curation protocol in `fixtures.md`). Eval coverage grows with use.
- **Reproducibility comes first.** Every eval run is deterministic given fixture inputs + pipeline version + cache. LLM nondeterminism is bounded by prompt-response cache.

## Reading order on first pass

1. `contracts.md` — what's stable across versions.
2. `fixtures.md` — what we test against.
3. `metrics-glossary.md` — what every metric means, with worked examples.
4. `eval-B-workbook.md` and `eval-C-end-to-end.md` — the mandatory ones.
5. `eval-A-enriched-json.md`, `eval-D-semantic-equivalence.md`, `eval-E-cost-latency.md`.
