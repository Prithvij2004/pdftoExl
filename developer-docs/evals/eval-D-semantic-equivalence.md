# Eval D — Semantic equivalence

## Purpose

Eval B and Eval C compare strings. Strings are brittle: `Display if Q7 = "Lives in other's home"` and `Display if Q7 == 'Lives in other'\''s home'` are *semantically identical* but textually different. v1 happens to produce the first form; a v2 might produce the second and Eval B would mark it wrong even though the EAB tool would interpret them identically.

Eval D catches these false regressions. It also handles question-text rephrasings that preserve meaning (e.g. `"Date of Birth"` vs `"DOB"` — both are valid for the same source row depending on the form's printed label).

**Mandatory when v2 changes mapping or LLM behaviour. Skippable when v2 changes only the parser** (parser-only changes don't typically alter cell values, so B/C cover the surface area).

## Inputs

- For each fixture: candidate `.xlsx`, reference `.xlsx`.
- For Branching Logic: a deterministic DSL canonicalizer (rule-based; no LLM needed).
- For Question Text and Answer Text: an optional rubric-graded LLM judge (deterministic seed) with a fallback to exact match.

## Expected outputs

A per-fixture `equivalence_score` and a per-cell breakdown of equivalent-but-different cells.

## Metrics

### D1. `branching_logic_equivalence`

For each row with non-null `Branching Logic` in either workbook:

1. Canonicalize both expressions (whitespace, quote style, value normalization, conjunct ordering).
2. Parse both into AST form (boolean tree of equality/inequality terms over question references).
3. Compare ASTs structurally.

```
branching_logic_equivalence = |{ rows : ast(candidate) == ast(reference) }| / |rows|
```

Where Eval B's `branching_logic_exact_match` requires string equality, this metric requires AST equality. The latter ≥ the former by construction.

**Threshold (v1): ≥ 0.99.**

### D2. `question_text_equivalence` (LLM-judge-backed)

For each row:

1. If candidate and reference `Question Text` strings are exact-equal after Unicode normalization → equivalent.
2. Otherwise, send both to a deterministic-seed LLM judge with a strict rubric: "Do these two strings refer to the same question on the same form? Answer yes/no with a one-line reason."
3. Aggregate yes-rate.

```
question_text_equivalence = |{ rows : equivalent(candidate, reference) }| / |rows|
```

**Threshold (v1): ≥ 0.98.**

The judge runs only on cells that fail exact-match, keeping cost bounded. Judge output is cached by `(candidate_text, reference_text)` so reruns are free.

### D3. `answer_text_equivalence`

Same as D2 but for the `Answer Text` column. For Checkbox Group rows where Answer Text is pipe-joined options, compare as sets (order-independent).

**Threshold (v1): ≥ 0.98.**

### D4. `equivalence_uplift_over_eval_B` (diagnostic)

For each fixture:

```
uplift = per_column_equivalence - per_column_accuracy_from_eval_B
```

Per column. A large positive uplift on `Branching Logic` means v1 had many strings that *would have* been counted wrong but are actually equivalent — a sign that Eval B is over-strict on that column for the current v2.

## Pass thresholds (initial v1 bars)

A fixture passes Eval D if all of:

- `branching_logic_equivalence ≥ 0.99`
- `question_text_equivalence ≥ 0.98`
- `answer_text_equivalence ≥ 0.98`

## LLM judge configuration

- Model: same Bedrock provider as the pipeline LLM, but a fresh deterministic seed and a separate cache.
- Temperature: 0; top_p: 1.
- Prompt: rubric + the two strings + "yes/no + one-line reason" output.
- Cache key: `(candidate_text, reference_text, judge_model_id, judge_prompt_version)`.
- Cost budget per fixture: $0.01 (the judge runs only on disagreement; on the seed fixtures, expect <10 calls per fixture).

The judge is a *check on the eval*, not on the pipeline. Its determinism + caching keeps Eval D as reproducible as Evals A/B/C.

## Tooling-agnostic harness contract

```
for each fixture:
    diff_cells = eval_B_diff(candidate, reference)
    for cell in diff_cells:
        if cell.column == "Branching Logic":
            equivalent = ast_equivalent(cell.candidate, cell.reference)
        elif cell.column in {"Question Text", "Answer Text"}:
            equivalent = llm_judge(cell.candidate, cell.reference)  # cached
        else:
            equivalent = False  # other columns: exact match only
        record(fixture.id, cell.column, equivalent)
    metrics = aggregate_per_column(records)
    assert_thresholds(metrics)
```

## Why an LLM judge for text equivalence

Considered alternatives:
- **Exact match only**: re-creates Eval B; nothing new.
- **Embedding cosine similarity**: brittle thresholds; many false positives on superficially-similar but semantically-different strings.
- **Hand-curated equivalence list**: high precision but doesn't generalize.
- **LLM judge with rubric + cache**: high precision, generalizes, deterministic via cache, bounded cost.

The judge's prompt is short, version-pinned, and cached. Reproducibility is preserved.

## Failure-debugging guide

| Symptom | Likely cause |
|---|---|
| `branching_logic_equivalence` low but `branching_syntactic_validity` high | DSL drift in non-syntactic ways (different quote style, different conjunct order) — fix the canonicalizer or the LLM prompt |
| `question_text_equivalence` low | Stage 1 text normalization changed (encoding, punctuation); or LLM is rephrasing where it shouldn't |
| `equivalence_uplift_over_eval_B` large for any column | Eval B may be under-counting correct outputs — review whether the column's strict equality is appropriate |

## Out of scope

- Cost / latency of the pipeline (Eval E).
- Format preservation (Eval B).
- Boundary-1 JSON shape (Eval A).

## Cross-references

- String-strict eval: `eval-B-workbook.md`
- Aggregate distance: `eval-C-end-to-end.md`
- Glossary: `metrics-glossary.md`
- LLM stage in pipeline (different model/cache from judge): `issues/04-llm-refinement.md`
