# Eval A — Enriched JSON structure check

## Purpose

Catch regressions in block typing, parent-link resolution, sequence assignment, and branching-logic generation **before** they propagate into the workbook. This eval scores Boundary 1 (the post-merge enriched JSON; see `contracts.md`).

Eval A localizes failures to a stage. If a workbook row is wrong, Eval B tells you the row is wrong. Eval A tells you whether the underlying JSON was wrong (an upstream pipeline bug) or correct (a stage 6/7 mapping bug).

**Optional for v2** if v2 has no Boundary-1 intermediate.

## Inputs

- Each fixture's source PDF (run through the pipeline).
- Each fixture's golden enriched JSON at `developer-docs/evals/goldens/{FX_ID}.enriched.json`.

## Expected outputs

The pipeline-under-test emits an enriched JSON document conforming to Boundary 1. Eval A diffs it against the golden.

## Metrics

All metrics are **per-fixture**. Aggregate views are convenience; per-fixture is canonical.

### A1. `block_coverage`

Fraction of golden blocks present in the candidate output.

```
block_coverage = |candidate_blocks ∩ golden_blocks_by_id| / |golden_blocks|
```

Block identity uses `(page, normalized_bbox, sha256(text))`. Two blocks match if all three agree (within bbox tolerance ε=2 PDF points).

**Threshold (v1): ≥ 0.98.**

### A2. `type_accuracy`

Of the candidate blocks that match a golden block, the fraction whose `block_type` matches.

```
type_accuracy = |{ b in matched : candidate(b).block_type == golden(b).block_type }| / |matched|
```

**Threshold (v1, full pipeline): ≥ 0.92. Rule-only path: ≥ 0.85.**

Sub-metric: `type_accuracy_per_block_type` — same calculation grouped by golden type. Surfaces which BlockType is regressing.

### A3. `parent_link_f1`

Treat each block's `parent_link` as a (child_id, parent_id, relation) triple. Compute precision and recall vs. golden, then F1.

```
TP = triples present in both
FP = triples in candidate but not golden
FN = triples in golden but not candidate
precision = TP / (TP + FP); recall = TP / (TP + FN)
F1 = 2·precision·recall / (precision + recall)
```

**Threshold (v1, full pipeline): ≥ 0.85. Rule-only path: ≥ 0.80.**

### A4. `branching_logic_exact_match`

Of golden blocks with `branching_logic != null`, fraction where the candidate's expression string matches exactly after canonicalization (whitespace-normalized, quote-style-normalized, case-preserved on values).

**Threshold (v1): ≥ 0.95.** Cross-reference: Eval D catches semantically-equivalent variants that don't exact-match.

### A5. `sequence_correctness`

Kendall-tau between the candidate's `sequence` ordering and the golden's, restricted to matched blocks.

**Threshold (v1): ≥ 0.97.**

### A6. `confidence_calibration` (diagnostic only, no threshold)

Bin candidate blocks by `confidence` (0.0–0.2, 0.2–0.4, …, 0.8–1.0). For each bin, report observed `type_accuracy`. A well-calibrated pipeline has accuracy roughly equal to the bin midpoint. Diagnostic for tuning the gate threshold (issue 03).

## Pass thresholds (initial v1 bars)

A fixture **passes** Eval A if all of:

- `block_coverage ≥ 0.98`
- `type_accuracy ≥ 0.92`
- `parent_link_f1 ≥ 0.85`
- `branching_logic_exact_match ≥ 0.95`
- `sequence_correctness ≥ 0.97`

A fixture **passes Rule-only Eval A** if:

- `type_accuracy ≥ 0.85`
- `parent_link_f1 ≥ 0.80`
- (other thresholds unchanged)

Thresholds are ratcheted upward only when both seed fixtures pass at the new bar.

## Tooling-agnostic harness contract

The harness is conceptually:

```
for each fixture in catalogue:
    candidate = run_pipeline(fixture.pdf)             # pipeline-under-test
    candidate_enriched = candidate.enriched_json      # may be None for end-to-end v2
    if candidate_enriched is None:
        skip_eval_A(fixture)
        continue
    golden = load_json(fixture.golden_enriched_path)
    metrics = compare_enriched(candidate_enriched, golden)
    write_per_fixture_row(fixture.id, metrics)
    assert_thresholds(metrics, thresholds)
```

`compare_enriched` is implementable in any language. v1 uses `pytest` + `deepdiff` for the harness and `scikit-learn` for F1; v2 may use anything.

## Failure-debugging guide

When a metric drops:

| Metric drop | Most likely cause | Where to look |
|---|---|---|
| `block_coverage` | Stage 1 dropped blocks | Issue 01 known failure modes; check page-header dedup logic |
| `type_accuracy` (specific BlockType) | Rule for that type changed; or LLM started disagreeing | Filter logs by `block_type=X`, check `provenance.source` distribution |
| `parent_link_f1` precision low | Spurious links | Check stage 2 parent post-pass logic |
| `parent_link_f1` recall low | Missing links | Likely orphan-detection routing failure (issue 03) |
| `branching_logic_exact_match` | LLM DSL drift | Update few-shot bank; consider Eval D |
| `sequence_correctness` | Reading order broke | Stage 1 (parser regression) or stage 6 (sequence algorithm) |

## Out of scope

- Whether the workbook renders correctly (Eval B).
- Cost or latency (Eval E).
- Equivalent-but-different branching expressions (Eval D).

## Cross-references

- Boundary spec: `contracts.md` (Boundary 1)
- Schema export: `developer-docs/evals/schema/enriched.schema.json`
- Stage-level acceptance criteria: `issues/01-`, `02-`, `04-`, `05-`
- Glossary: `metrics-glossary.md`
