# Metrics glossary

Single source of truth for every metric used in `evals/`. Each entry: definition, formula, worked example, where it appears.

A v2 implementer must be able to reproduce these calculations from this doc alone, with no reference to v1 code.

---

## `block_coverage` (Eval A)

Fraction of golden blocks present in the candidate enriched JSON.

**Formula:** `|matched| / |golden_blocks|`, where blocks match if `(page, normalized_bbox, sha256(text))` agrees within bbox tolerance ε=2 PDF points.

**Worked example:**
- Golden has 412 blocks. Candidate has 410 matched + 3 unmatched.
- `block_coverage = 410 / 412 = 0.995`.

---

## `type_accuracy` (Eval A)

Of matched blocks, fraction with matching `block_type`.

**Formula:** `|{ b : matched(b) ∧ candidate(b).block_type == golden(b).block_type }| / |matched|`.

**Worked example:**
- 410 matched blocks. Candidate types match golden on 380. `type_accuracy = 380 / 410 = 0.927`.

---

## `parent_link_f1` (Eval A)

F1 score over `(child_id, parent_id, relation)` triples.

**Formula:** `F1 = 2·P·R / (P+R)` with `P = TP/(TP+FP)`, `R = TP/(TP+FN)`.

**Worked example:**
- Golden has 80 parent-link triples. Candidate has 75. Of those 75, 70 match golden exactly.
- `TP = 70, FP = 5, FN = 10, P = 70/75 = 0.933, R = 70/80 = 0.875`.
- `F1 = 2·0.933·0.875 / (0.933+0.875) = 0.903`.

---

## `branching_logic_exact_match` (Eval A)

Of golden rows with non-null `branching_logic`, fraction exactly matching after canonicalization (whitespace + quote-style normalization).

**Worked example:** Golden has 12 rows with branching logic; 11 candidates exact-match. `0.917`.

---

## `sequence_correctness` (Eval A)

Kendall-tau between candidate `sequence` ordering and golden, restricted to matched blocks.

**Worked example:** 98 matched row-producing blocks. Of all `(i,j)` pairs, 97% are concordant. `τ = 0.97`.

---

## `row_count_delta` (Eval B)

Signed difference: `candidate.row_count - reference.row_count`.

**Worked example:** Candidate 100 rows, reference 98 rows. `delta = +2`.

---

## `per_column_accuracy[col]` (Eval B)

Per column: fraction of rows where the candidate cell equals the reference cell after normalization.

**Normalization:** strip trailing whitespace; Unicode NFC; treat `None` and empty string as equal; dates compare as dates.

**Worked example:** For `QuestionType` over 98 rows, candidate matches reference on 96. `0.980`.

---

## `controlled_vocab_validity` (Eval B)

Fraction of rows whose `QuestionType` value is in the controlled vocab from the template's `Values` sheet.

**Worked example:** 98 rows; 1 row has `"Textbox"` (typo) instead of `"Text Box"`. `97/98 = 0.990`. **Fails** the v1 threshold (= 1.0).

---

## `branching_syntactic_validity` (Eval B)

Of rows with non-null `Branching Logic`, fraction parsing against the EAB DSL grammar.

**DSL grammar (regex sketch):**
```
branching   := "Display if " term (" AND " term)*
term        := qref " " op " " value
qref        := "Q" digit+
op          := "=" | "!=" | " in "
value       := "\"" .*? "\"" | "(" value ("," value)* ")"
```

**Worked example:** 12 rows with branching; 12 parse. `1.000`.

---

## `sequence_contiguity` (Eval B)

Boolean: candidate `Sequence` column is `[1, 2, 3, …, n]` with no gaps or duplicates.

---

## `formatting_preservation` (Eval B)

Boolean AND over: sheet-name match; frozen-pane intact; data-validation dropdowns intact; merged-cell ranges intact; named ranges resolve.

---

## `workbook_distance` (Eval C)

Weighted aggregate per fixture.

**Formula:**
```
distance = (Σ_col w[col] × (1 - per_column_accuracy[col])) / (Σ_col w[col]) + penalties
```

with weights:
- Critical (`QuestionType`, `Question Text`, `Sequence`) = 5
- High (`Branching Logic`, `Section`, `Required`, `Auto Populated`, `Answer Validation`, `Answer Text`) = 3
- Medium (other Yes/No, `Auto Populate with:`, `Auto Populate Field:`) = 1
- Low (preserved-blank columns) = 0

and penalties:
- +0.10 per out-of-vocab `QuestionType`, capped at +0.30
- +0.10 if sequence non-contiguous
- +0.20 if formatting regressed

**Worked example:**
- `per_column_accuracy`: Critical avg 0.97, High avg 0.94, Medium avg 0.90.
- Weighted: `(5·0.03·3 + 3·0.06·6 + 1·0.10·6) / (5·3 + 3·6 + 1·6) = (0.45 + 1.08 + 0.60) / 39 = 0.054`.
- No penalties → `distance = 0.054`. Just over threshold (0.05).

---

## `e2e_pass_rate` (Eval C)

Fraction of fixtures with `workbook_distance ≤ 0.05`.

---

## `branching_logic_equivalence` (Eval D)

Fraction of branching-logic cells with equivalent ASTs (parsed from the DSL, conjuncts sorted, values normalized).

**Worked example:** Candidate emits `Display if Q3 = "yes" AND Q5 = "no"`; reference emits `Display if Q5 = "no" AND Q3 = "yes"`. Both ASTs are `AND({Q3="yes", Q5="no"})`. **Equivalent.** Eval B's exact-match would mark this as a miss.

---

## `question_text_equivalence` (Eval D)

Fraction of question-text cells judged equivalent. Exact match first; LLM judge with deterministic seed for the residual.

**Judge prompt (verbatim, version-pinned):**
```
You are evaluating whether two strings refer to the same question on the same form.
String A: <candidate>
String B: <reference>
Answer "yes" if and only if a reasonable form-reviewer would treat them as the same question.
Answer "no" otherwise.
Output exactly one line: "yes" or "no", followed by a one-sentence reason.
```

---

## `answer_text_equivalence` (Eval D)

Like D2 but for `Answer Text`. For Checkbox Group rows, compare as sets (split on `|`).

---

## `latency_s` (Eval E)

Wall-clock seconds from PDF read to .xlsx write.

---

## `cost_usd` (Eval E)

Sum of `cost_usd` across all log lines for the run. v1's source is exclusively stage 4 (Bedrock).

---

## `cost_per_block_usd` (Eval E)

`total_cost_usd / blocks_extracted`. Cross-fixture comparison aid.

---

## `route_rate` (Eval E, v1-specific)

`blocks_routed / blocks_extracted`. v2 implementations without a gate report 0.0.

---

## `cache_hit_rate` (Eval E)

LLM-cache hit fraction. Confirms eval reruns are deterministic.

---

## Conventions

- All metrics are **per-fixture**. Aggregations are convenience views.
- Boolean metrics are reported as `0.0`/`1.0` for table consistency.
- Thresholds live in the eval doc that introduces the metric, not here.
- "Normalized" means: strip trailing whitespace; Unicode NFC; treat `None` and empty-string as equal; dates compare as dates.
