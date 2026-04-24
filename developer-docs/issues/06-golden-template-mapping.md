# Issue 06 — Stage 6: Map the enriched JSON against the golden template

## Summary

Stage 6 converts the document-shaped enriched JSON into row-shaped workbook data. Every question-bearing block becomes one row; every row has values for the 28 columns defined by the golden template's `Assessment v2` sheet header row. This stage owns the mapping logic, sequence assignment, Branching Logic string substitution, and the discipline that keeps controlled-vocabulary values valid.

The hardest part of this stage is *not* the mapping — it's that the golden template's column set and controlled vocabularies are the authoritative schema. This stage must not invent columns or values. If something doesn't fit, the template needs to change (issue 08), not the mapper.

## Inputs / Outputs

**Input:** `EnrichedDocument` from stage 5.

**Output:** `MappedWorkbook`:

```python
class MappedRow(BaseModel):
    section: Optional[str]
    alert: Literal["Yes", "No"] = "No"
    alternate_text: Literal["Yes", "No"] = "No"
    auto_populated: Literal["Yes", "No"] = "No"
    history: Literal["Yes", "No"] = "No"
    pre_populate: Literal["Yes", "No"] = "No"
    required: Literal["Yes", "No"] = "Yes"
    speech_to_text: Literal["Yes", "No"] = "No"
    submission_history: Literal["Yes", "No"] = "No"
    concept_code: Optional[str] = None              # preserved blank by default
    sequence: int
    question_rule: Optional[str] = None
    question_type: QuestionType
    question_text: str
    branching_logic: Optional[str] = None
    answer_text: Optional[str] = None
    answer_validation: Optional[str] = None
    answer_score_value: Optional[str] = None
    talking_points: Optional[str] = None
    auto_populate_with: Optional[str] = None
    auto_populate_field: Optional[str] = None
    auto_populate_rule: Optional[str] = None
    alternate_question_text: Optional[str] = None
    alternate_answer_text: Optional[str] = None
    alert_type: Optional[str] = None
    alert_text: Optional[str] = None
    token_id: Optional[str] = None                  # preserved blank by default
    it_notes: Optional[str] = None                  # preserved blank by default

class MappedWorkbook(BaseModel):
    header_meta: HeaderMeta                         # assessment title, program, dates
    rows: List[MappedRow]
    template_path: str
    template_version: str
```

## The 28 columns — grounded in the actual CHOICES reference workbook

v1's architecture doc calls this a "26-column schema." The **actual** header row of the `Assessment v2` sheet in `docs/support_docs/CHOICES Safety Determination Request Form Final_11_20.xlsx` (row 13) has **28 columns**. The discrepancy is documented here so v1 implementers don't chase the wrong number.

Exact header values, in column order:

| # | Header | Category | Default | Source |
|---|---|---|---|---|
| 1 | `Section` | Extractable | — | nearest `SectionHeader` block in reading order |
| 2 | `Alert\n(Yes / No)` | Default | `No` | |
| 3 | `Alternate Text \n(Yes / No)` | Default | `No` | |
| 4 | `Auto Populated\n(Yes / No)` | Default | `No` (but CHOICES demographic rows set `Yes`) | heuristic on question text (`Name`, `SSN`, `DOB`, etc.) |
| 5 | `History\n(Yes / No)` | Default | `No` | |
| 6 | `Pre Populate\n(Yes / No)  ` | Default | `No` | |
| 7 | `Required\n(Yes / No)  ` | Default | `Yes` | |
| 8 | `Speech to Text\n(Yes / No)` | Default | `No` | |
| 9 | `Submission History\n(Yes / No)` | Default | `No` | |
| 10 | `Concept Code\n(for Migration use only)` | Preserved blank | `null` | human reviewer fills in |
| 11 | `Sequence` | Extractable | — | assigned by sequence algorithm below |
| 12 | `Question Rule` | Extractable (rare) | `null` | section-level rule, if any |
| 13 | `QuestionType` | Extractable | — | controlled vocab: see below |
| 14 | `Question Text` | Extractable | — | block text, normalized |
| 15 | `Branching Logic` | Extractable (conditional) | `null` | substituted from `parent_link` + LLM output |
| 16 | `Answer Text` | Extractable (for options) | `null` | for `Radio Button` / `Checkbox Group`: pipe-joined option list |
| 17 | `Answer Validation` | Extractable | `null` | e.g. `"default characters = 100"`, `"Format is mm/dd/yyyy"` |
| 18 | `Answer Score Value` | Extractable (rare) | `null` | |
| 19 | `Talking Points` | Preserved blank | `null` | |
| 20 | `Auto Populate with:` | Extractable (conditional) | `null` | e.g. `"Demographics"` for the first few CHOICES rows |
| 21 | `Auto Populate Field:` | Extractable (conditional) | `null` | |
| 22 | `Auto Poulate Rule:` | Extractable (conditional) | `null` | — typo `"Poulate"` preserved verbatim from the template |
| 23 | `Alternate Question Text` | Preserved blank | `null` | |
| 24 | `Alternate Answer Text` | Preserved blank | `null` | |
| 25 | `Alert Type` | Preserved blank | `null` | |
| 26 | `Alert Text` | Preserved blank | `null` | |
| 27 | `Token ID\n(PDF Generation)` | Preserved blank | `null` | |
| 28 | `IT Notes ` | Preserved blank | `null` | — trailing space preserved verbatim |

**Controlled vocabulary (from `Values` sheet, column C):**
`Radio Button`, `Checkbox`, `Checkbox Group`, `Text Area`, `Text Box`, `Date`, `Display` (plus others — enumerate by reading the full `Values` sheet at implementation time and pinning to template_version).

Observed `Answer Validation` patterns on CHOICES rows 14–17: `"default characters = 100"` (Text Box), `"Format is mm/dd/yyyy"` (Date). These patterns are fixed strings the mapper emits by rule.

## BlockType → QuestionType mapping table

| `BlockType` | `QuestionType` | Notes |
|---|---|---|
| `SectionHeader` | — (header, not a row; emitted as `Section` value on following rows) | |
| `SubsectionHeader` | `Display` | Or optional `Section` grouping — pick one and document in code |
| `QuestionLabel` + child `TextInput` | `Text Box` | `answer_validation = "default characters = 100"` |
| `QuestionLabel` + child `TextArea` | `Text Area` | |
| `QuestionLabel` + child `DateInput` | `Date` | `answer_validation = "Format is mm/dd/yyyy"` |
| `QuestionLabel` with ≥2 `CheckboxOption` children | `Checkbox Group` | options pipe-joined into `Answer Text` |
| `QuestionLabel` with exactly 1 `CheckboxOption` child | `Checkbox` | |
| `QuestionLabel` with ≥2 `RadioOption` children | `Radio Button` | |
| `Display` | `Display` | |
| `SignatureField` | `Text Box` with `answer_validation` noting signature semantics | CHOICES form doesn't exercise this; TX LTSS does |
| `PageHeader` / `PageFooter` | — (filtered out, never a row) | |
| `TableCell` etc. | Context-dependent | Deferred; v1 flattens simple tables and raises on complex ones |

## Sequence assignment algorithm

1. Filter to **row-producing** blocks only: anything except `SectionHeader`, `PageHeader`, `PageFooter`, and `Unknown`.
2. Stable-sort by `(page, reading_order)`.
3. Assign `sequence = 1, 2, 3, …` contiguously.
4. After assignment, substitute sequence numbers into any `branching_logic` strings that reference block IDs (the LLM emits `Display if <block_id> = "x"`; stage 6 rewrites to `Display if Q<seq> = "x"`).

## Branching Logic substitution

The LLM (issue 04) emits `branching_logic` expressions that reference `parent_link.parent_block_id`, not sequence numbers. Stage 6:

1. Reads the expression.
2. Looks up `sequence` on the referenced parent block.
3. Rewrites to `Q<seq>`.
4. Re-validates the expression against the DSL grammar.

A parent with no `sequence` (e.g. it's a `Display`) is a bug caught in stage 5 validation; stage 6 trusts the upstream contract.

## HeaderMeta population

The workbook header block (rows 3–12 of `Assessment v2`) contains `Assessment Title`, `Questionnaire ID`, `Program`, and approval dates. v1 populates:

- `Assessment Title` ← first page heading of the source PDF (e.g. `"Safety Determination Request Form"`).
- `Program` ← from the document metadata or `doc_meta.program`. For CHOICES: `"CHOICES"`. For TX LTSS: `"STAR+PLUS HCBS"` (or the state/program inferred from the PDF).
- `Questionnaire ID`, `Concept Code`, approval dates: **preserved blank** for human reviewer.

## Known failure modes

1. **Section attribution across pages.** A question on page 3 whose section header is on page 1. Current rule (nearest-preceding `SectionHeader`) works as long as the header isn't filtered mid-document.
2. **Checkbox-vs-Checkbox-Group ambiguity** when a question has exactly 2 options. v1 maps to `Checkbox Group`; confirm with golden output.
3. **`Answer Text` format for Checkbox Group.** Pipe-joined vs. newline-joined vs. `\n` vs. `|`? Inspect the CHOICES reference workbook for examples and match the observed format exactly.
4. **The trailing-space / typo preservation** (`"IT Notes "` with trailing space, `"Auto Poulate Rule:"` typo). The mapper must write cells at the correct column *index* — it must not attempt to match by header string, because the template's headers contain quirks the mapper shouldn't "fix." Column indices are pinned to the template.
5. **Header auto-population heuristics** (cols 4 `Auto Populated = Yes` when question text matches demographic keywords). Keep the keyword list short and configurable; log whenever it fires.
6. **Rows that the rule engine thinks are questions but aren't** (e.g. the `"TC0175 (Rev. 8-2-16) RDA 2047"` form-id footer). If stage 2/4 correctly typed them as `PageFooter` / `Display`, they're filtered here. If not, they'll leak into the workbook as rogue rows. This is the main reason Eval B exists.

## Open questions for v1 implementer

1. **Should section grouping produce a literal "Section" separator row or only populate the `Section` column on data rows?** Inspect the CHOICES reference — it uses column-based grouping, no separator rows. v1 follows suit.
2. **Auto-populate keyword list for column 4.** Seed with `Name`, `SSN`, `DOB`, `Date of Birth`, `Medicaid ID`, `Address`. Document as a YAML config.
3. **What to do when `Section` is absent?** CHOICES starts with unheadered rows for demographics. Leave `Section` blank; don't invent "Demographics" unless the PDF says so.
4. **Column-index vs. header-name lookup.** v1 commits to column-index lookup. This is the single biggest source of bugs if the template shifts; pin `template_version` and fail loudly on mismatch.

## Acceptance criteria

- Every row in `MappedWorkbook` has a valid `QuestionType` ∈ controlled vocab (Eval B `controlled_vocab_validity` = 1.0).
- `sequence` is contiguous from 1 with no gaps or duplicates (Eval B `sequence_contiguity`).
- `branching_logic` strings parse against the DSL grammar (Eval B `branching_syntactic_validity` ≥ 0.97).
- Per-column accuracy against reference workbooks ≥ thresholds defined in Eval B.
- The mapper reads column definitions from a pinned `template_version`; swapping the template invalidates mapping until the version is re-blessed.

## Out of scope

- Writing the workbook file (issue 07).
- Template evolution (issue 08).
- LLM branching generation (issue 04).

## Cross-references

- Upstream: `05-merge-enriched-json.md`
- Downstream: `07-excel-writer-output.md`, `08-golden-template-contract.md`
- Eval that scores this stage: `evals/eval-B-workbook.md` (per-column accuracy, controlled-vocab validity)
- Related eval: `evals/eval-D-semantic-equivalence.md` (catches equivalent-but-different branching expressions)
