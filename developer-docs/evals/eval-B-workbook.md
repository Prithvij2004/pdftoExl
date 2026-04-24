# Eval B — Workbook cell-by-cell check

## Purpose

Score the produced `.xlsx` against the reference `.xlsx` cell by cell. Catches mapping bugs, controlled-vocabulary violations, and formatting regressions that would slip past Eval A.

**Mandatory for every pipeline version.**

## Inputs

- For each fixture, the candidate `.xlsx` produced by the pipeline-under-test.
- The reference `.xlsx` at `fixture.reference_workbook_path`.
- The pinned `template_version` for that fixture (`fixtures.md`).

## Expected outputs

A per-fixture report with the metrics below, plus a side-channel HTML cell-diff for human review on failure.

## Metrics

All metrics scoped to the question sheet (`Assessment v2` for the CHOICES template; `Assessment` for older templates). Supporting sheets are checked separately under `formatting_preservation`.

### B1. `row_count_delta`

```
row_count_delta = candidate.question_row_count - reference.question_row_count
```

**Threshold (v1): |delta| ≤ 2.** Tolerates minor differences from edge-case rows that legitimately changed; larger deltas indicate systematic mapping bug or rogue rows.

### B2. `per_column_accuracy`

For each of the 28 columns, fraction of rows where the candidate cell value equals the reference cell value.

```
per_column_accuracy[col] = |{ r : normalize(candidate[r,col]) == normalize(reference[r,col]) }| / row_count
```

`normalize` strips trailing whitespace, normalizes Unicode (NFC), and treats `None`/empty string as equal. Date cells compare as dates, not as strings.

**Thresholds (v1), per column priority class:**

| Priority | Columns | Threshold |
|---|---|---|
| Critical | `QuestionType`, `Question Text`, `Sequence` | ≥ 0.97 |
| High | `Branching Logic`, `Section`, `Required`, `Auto Populated`, `Answer Validation`, `Answer Text` | ≥ 0.92 |
| Medium | `Auto Populate with:`, `Auto Populate Field:`, the other Yes/No columns | ≥ 0.85 |
| Low (preserved blank) | `Concept Code`, `Token ID`, `IT Notes`, `Talking Points`, `Alert Type`, `Alert Text`, `Alternate Question Text`, `Alternate Answer Text` | ≥ 0.99 (must remain blank) |

A failed Critical column fails the fixture. High/Medium failures lower the fixture grade but don't block.

### B3. `controlled_vocab_validity`

Fraction of rows whose `QuestionType` value is in the controlled vocabulary (read from the template's `Values` sheet at runtime).

**Threshold (v1): = 1.0.** Any out-of-vocab value is a hard fail.

Sub-metric: same for the `Yes/No` columns (set should be exactly `{"Yes", "No", null}`).

### B4. `branching_syntactic_validity`

Of rows with a non-null `Branching Logic` value, fraction that parses against the EAB DSL grammar (regex parser).

**Threshold (v1): ≥ 0.97.**

### B5. `sequence_contiguity`

The candidate's `Sequence` column should be contiguous from 1 with no duplicates.

```
contiguous_sequence = (sorted(sequences) == list(range(1, n+1)))
```

**Threshold (v1): true (binary).**

### B6. `formatting_preservation`

Verifies the output workbook preserves template formatting:

- Sheet count and names match the template.
- Frozen panes intact on the question sheet.
- Data validation dropdowns present on `QuestionType` and `Yes/No` columns.
- Header block (rows 3–12) merged cells intact.
- Named ranges referenced by dropdowns resolve.

Each sub-check is binary; `formatting_preservation` is the AND.

**Threshold (v1): all sub-checks true.**

### B7. `header_meta_accuracy` (diagnostic)

Compare `Assessment Title`, `Program`, dates in the metadata header block. Reported but not pass/fail; many of these are intentionally human-filled and may differ from the reference.

## Pass thresholds (initial v1 bars)

A fixture passes Eval B if all of:

- `|row_count_delta| ≤ 2`
- `per_column_accuracy[critical] ≥ 0.97` for every Critical column
- `controlled_vocab_validity = 1.0`
- `branching_syntactic_validity ≥ 0.97`
- `sequence_contiguity = true`
- `formatting_preservation = true (all sub-checks)`

High and Medium column thresholds are tracked and gated on regression (any drop > 5pp from the previous run blocks merge).

## Tooling-agnostic harness contract

```
for each fixture:
    candidate_xlsx = run_pipeline(fixture.pdf)
    reference_xlsx = load_xlsx(fixture.reference_workbook_path)
    metrics = compare_workbook(candidate_xlsx, reference_xlsx, fixture.template_version)
    write_per_fixture_row(fixture.id, metrics)
    if any_critical_metric_fails(metrics):
        emit_html_cell_diff(candidate_xlsx, reference_xlsx, fixture.id)
        fail_fixture(fixture.id)
```

`compare_workbook` reads both `.xlsx` files, normalizes cells, computes the metrics. Any tool that can read `.xlsx` works (openpyxl, ExcelJS, Aspose, etc.).

## Failure-debugging guide

| Symptom | Likely cause |
|---|---|
| `row_count_delta` large positive | Rogue rows leaked through — likely a `PageHeader`/`PageFooter` mis-classification (issue 02) |
| `row_count_delta` large negative | Genuine rows filtered out — likely a `BlockType` not in the row-producing set (issue 06 mapping table) |
| Critical column fails on `QuestionType` | Mapping table out of sync with controlled vocab (issue 06) |
| Critical column fails on `Question Text` | Stage 1 text normalization changed; check encoding |
| `branching_syntactic_validity` drops | LLM DSL drift; or stage 6 substitution broken |
| `formatting_preservation` fails dropdowns | Sheet renamed or named range deleted (issue 08) |

## Out of scope

- Equivalent-but-different cell values (Eval D).
- Cost / latency (Eval E).
- Whether the *interpretation* of the PDF is correct (Eval A captures this on the JSON side).

## Cross-references

- Template contract: `issues/08-golden-template-contract.md`
- Mapping logic: `issues/06-golden-template-mapping.md`
- Writer: `issues/07-excel-writer-output.md`
- Glossary: `metrics-glossary.md`
