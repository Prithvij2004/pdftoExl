# Eval C — End-to-end black-box

## Purpose

The single eval v2 absolutely cannot regress on. Eval C scores PDF in / `.xlsx` out with no peeking at intermediate artifacts. It collapses Eval B's 28-column scores into a weighted distance, prioritizing the columns that matter most to EAB reviewers.

If Eval A is the engineer's eval and Eval B is the QA's eval, **Eval C is the customer's eval**.

**Mandatory for every pipeline version. v2's go/no-go decision lives here.**

## Inputs

- For each fixture: source PDF.
- Pipeline-under-test (v1 or v2), invoked as `pipeline(pdf_path) -> xlsx_path`.
- Reference `.xlsx` from the fixture catalogue.

## Expected outputs

A per-fixture `workbook_distance` score in `[0, 1]` (0 = identical, 1 = maximally different) and an aggregate weighted score.

## Metrics

### C1. `workbook_distance`

Per-fixture, computed as a weighted sum over Eval B's per-column accuracies:

```
workbook_distance(fixture) = Σ_col w[col] × (1 - per_column_accuracy[col])
                            ─────────────────────────────────────────────
                                            Σ_col w[col]
```

With column weights:

| Class | Weight | Columns |
|---|---|---|
| Critical | 5 | `QuestionType`, `Question Text`, `Sequence` |
| High | 3 | `Branching Logic`, `Section`, `Required`, `Auto Populated`, `Answer Validation`, `Answer Text` |
| Medium | 1 | `Auto Populate with:`, `Auto Populate Field:`, other Yes/No columns |
| Low | 0 | preserved-blank columns (covered by Eval B independently) |

Plus a fixed penalty for hard-fail conditions:
- Out-of-vocab `QuestionType` value: +0.10 per occurrence (capped at +0.30).
- Non-contiguous sequence: +0.10.
- Formatting regression (dropdowns lost): +0.20.

**Threshold (v1): per-fixture `workbook_distance ≤ 0.05`.**

### C2. `e2e_pass_rate`

Fraction of fixtures with `workbook_distance` under threshold.

```
e2e_pass_rate = |{ fix : workbook_distance(fix) ≤ 0.05 }| / |fixtures|
```

**Threshold (v1, with two seed fixtures): = 1.00 (both pass).**

For v2 evaluation: `e2e_pass_rate(v2) ≥ e2e_pass_rate(v1)` is the **regression bar**. v2 may individually score worse on some fixtures if it scores better on others, but the overall pass rate must not drop.

### C3. `workbook_distance_delta` (v1 → v2 only)

For comparing pipeline versions on the same fixtures:

```
delta(fix) = workbook_distance(v2, fix) - workbook_distance(v1, fix)
```

Negative is improvement, positive is regression. v2 should report this per fixture; an aggregate average hides per-fixture regressions and is not a substitute.

## Pass thresholds (initial v1 bars)

- Per-fixture `workbook_distance ≤ 0.05`.
- `e2e_pass_rate = 1.00`.

For v2:

- Per-fixture `workbook_distance(v2) ≤ workbook_distance(v1) + 0.02` (small per-fixture regressions allowed if compensated by Eval D semantic equivalence).
- `e2e_pass_rate(v2) ≥ e2e_pass_rate(v1)`.

## Tooling-agnostic harness contract

```
for each fixture:
    candidate_xlsx = run_pipeline(fixture.pdf)
    reference_xlsx = load_xlsx(fixture.reference_workbook_path)
    per_col = compute_per_column_accuracy(candidate_xlsx, reference_xlsx)
    penalties = compute_penalties(candidate_xlsx)
    distance = weighted_sum(per_col, COLUMN_WEIGHTS) + penalties
    record(fixture.id, distance)
    assert distance <= 0.05
```

The eval is implementable in any language that can read `.xlsx`. It deliberately does not look at the pipeline's internals, intermediate JSONs, or logs. The pipeline is a black box.

## Why this eval exists separately from Eval B

Eval B asserts per-metric thresholds in a way that's actionable for engineering ("the `Required` column dropped to 0.88, look there"). Eval C aggregates those into a single number that's actionable for product ("v2 ships if Eval C is at least as good as v1").

The two are complementary and both run on every commit.

## Failure-debugging guide

A high `workbook_distance` is decomposable:

```
contribution[col] = w[col] × (1 - per_column_accuracy[col]) / Σ_col w[col]
```

Sort columns by contribution descending; the top contributors are where to look. From there, the playbook is Eval B's failure-debugging guide (each contributing column points to a stage).

## Out of scope

- Cost / latency (Eval E).
- Internal stage attribution (Eval A).
- Semantically-equivalent variants (Eval D).

## Cross-references

- Per-column metrics: `eval-B-workbook.md`
- Equivalence catch: `eval-D-semantic-equivalence.md`
- Telemetry: `eval-E-cost-latency.md`
- Glossary: `metrics-glossary.md`
