# Issue 02 — Stage 2: Rule-based block classification

## Summary

Stage 2 takes the raw blocks emitted by stage 1 and assigns each one a **block type** plus a **confidence score**. Most blocks on an assessment PDF are unambiguous — a line ending in a colon followed by underscores is a labeled text input; a glyph that looks like ☐ followed by short text is a checkbox option; a bold all-caps line at the top of a section is a section header. A well-built rule engine resolves 70–90% of blocks without ever calling the LLM. Only the residual goes through the gate (issue 03) and on to LLM refinement (issue 04).

This stage owns the cost/quality tradeoff for the whole pipeline. Every percentage point of recall it adds reduces LLM spend proportionally; every false positive it commits gets baked into the workbook unless caught by Eval A.

## Inputs / Outputs

**Input:** `CanonicalDocument` from stage 1.

**Output:** `ClassifiedDocument`:

```python
class ClassifiedBlock(BaseModel):
    block_id: str                       # carried from RawBlock
    raw: RawBlock                       # original, untouched
    block_type: BlockType
    confidence: float                   # in [0.0, 1.0]
    rule_id: Optional[str]              # which rule fired (for debugging + eval)
    parent_link: Optional[ParentLink]   # set if a structural rule could establish it
    notes: List[str]                    # human-readable triage hints

class BlockType(str, Enum):
    SectionHeader      = "section_header"
    SubsectionHeader   = "subsection_header"
    QuestionLabel      = "question_label"
    CheckboxOption     = "checkbox_option"
    RadioOption        = "radio_option"
    TextInput          = "text_input"          # short underscore field
    TextArea           = "text_area"           # multi-line / multi-row underscore field
    DateInput          = "date_input"
    Display            = "display"             # display-only instructional text
    SignatureField     = "signature_field"
    TableHeader        = "table_header"
    TableCell          = "table_cell"
    PageHeader         = "page_header"         # repeating
    PageFooter         = "page_footer"         # repeating
    Unknown            = "unknown"             # default; routes to LLM

class ParentLink(BaseModel):
    parent_block_id: str
    relation: Literal["option_of", "input_of", "row_of", "child_of_section"]
    confidence: float
```

## v1 design decision

Custom Python rule engine, repo-local. Pydantic for schema validation. A regex pattern library organized by `BlockType`. Optional spaCy for lightweight linguistic features (sentence boundary, all-caps detection that handles diacritics correctly).

Rules are pure functions `(RawBlock, neighbours, doc_context) -> Optional[(BlockType, confidence, rule_id, parent_link?)]`. The engine runs all applicable rules and picks the highest-confidence match; ties are broken by rule priority order. A block with no rule match is `Unknown`, confidence 0.0, and is auto-routed to the LLM.

Why rule-engine over a small classifier:
- **Debuggable.** When Eval A flags a misclassification, `rule_id` tells you exactly which rule to fix.
- **Cheap to evolve.** A new failure mode usually means a new rule, not retraining.
- **Auditable for regulated work.** State assessments are compliance-adjacent; reviewers want to see *why* a block was typed a certain way.

## Block-type taxonomy with concrete examples from seed fixtures

Grounding the taxonomy in real PDFs avoids the worst failure mode of rule engines: a taxonomy that doesn't match what's actually on the page.

| BlockType | CHOICES PDF example (page) | TX LTSS PDF example | Distinguishing structural cues |
|---|---|---|---|
| `SectionHeader` | "Current Living Arrangements:" (p1) | "Freedom of Choice:" (p1) | Bold or larger font, ends with `:`, isolated on its own line, followed by content |
| `SubsectionHeader` | "Applicant residence (if applicant currently resides in a NF, housing status prior to admission):" (p1) | (none in this short form) | Trailing `:`, slightly smaller than section, often parenthetical clarifier |
| `QuestionLabel` | "Total Acuity Score of PAE as submitted:" (p1) | "Applicant or Member Name" (p1) | Trailing `:`, followed immediately by an input glyph (underscores, blank box, or label-above-line pattern) |
| `CheckboxOption` | "☐ Lives in own home/apt (alone)" (p1) | (signature checkboxes minimal) | Leading checkbox glyph (`☐`/`◻`/empty box) or `raw_kind == "checkbox_glyph"` neighbour; short text |
| `RadioOption` | (CHOICES uses checkboxes throughout — radios appear in other state forms) | — | Leading `○` or `◯` glyph |
| `TextInput` | "specify relationship ___________" continuation (p1) | "Printed Name" cell (p1) | Trailing run of `_` (underscore) chars, or empty form_field with field name |
| `TextArea` | The 4-line underscore block on p2 ("Attach additional explanation if needed…") | (none) | ≥3 stacked underscore runs of similar length |
| `DateInput` | "DOB: ______________" (p1) | "Individual Service Plan Begin Date" (p1) | Label contains `Date` / `DOB` / `mm/dd/yyyy` and trailing input |
| `Display` | "TC0175 (Rev. 8-2-16) RDA 2047" (every page) | The two long Freedom-of-Choice / Acknowledgement paragraphs (p1) | Long paragraph with no trailing input; or short metadata line in page footer style |
| `SignatureField` | (CHOICES has none) | "Signature" cell in the Applicant/Witness/Service Coordinator rows (p1) | Cell of a signature table; or text "Signature" + adjacent underline run |
| `TableHeader` / `TableCell` | (CHOICES is mostly form-fields, not tables) | The signature table cells | TableFormer output from stage 1 |
| `PageHeader` | "Safety Determination Request Form / Applicant Name: ___ SSN: ___ DOB: ___" (every page) | "Form H1700-3 / September 2025" | `raw_kind == "page_header"` from stage 1, OR identical text repeated on ≥3 pages |
| `PageFooter` | Page number + RDA line | (none repeated) | `raw_kind == "page_footer"`, or short text in the bottom-margin geometric band |

## Confidence scoring scheme

**Multiplicative**, not additive. Each rule emits a base confidence; structural corroborations multiply it upward, contradictions multiply it downward. Capped at `[0.05, 0.99]` (never 0 — that's `Unknown`; never 1 — leave headroom for the LLM to override on context).

Rationale for multiplicative:
- A checkbox-glyph rule firing at 0.8 + a "short text + isolated line" rule firing at 0.7 should not sum to 1.5 (clamped to 1.0). They should compose: `0.8 + 0.7×0.2 = 0.94` (additive bounded) or `1 - (1-0.8)(1-0.7) = 0.94` (probabilistic OR). The probabilistic-OR form is what we use, because it generalizes cleanly to N corroborating signals.

```python
def combine(confidences: list[float]) -> float:
    p = 1.0
    for c in confidences:
        p *= (1 - c)
    return max(0.05, min(0.99, 1 - p))
```

For contradictions (e.g. a strong "this is a header" rule fires but font is identical to body text), apply a multiplier `<1` to the combined score. Document the multiplier in `rule_id` so eval triage can find it.

## Rule library structure

Recommended layout (not dictated by v1, but the structure that survives growth):

```
rules/
├── __init__.py
├── base.py                   # Rule protocol, combine(), priority
├── headers.py                # SectionHeader, SubsectionHeader rules
├── inputs.py                 # TextInput, TextArea, DateInput rules (underscore-pattern based)
├── choices.py                # CheckboxOption, RadioOption (glyph + neighbour-based)
├── questions.py              # QuestionLabel rules (trailing-colon + downstream-input pattern)
├── tables.py                 # TableHeader, TableCell (consume TableFormer output)
├── repetition.py             # PageHeader, PageFooter (cross-page identity detection)
├── displays.py               # Display (long paragraph, no input neighbour)
└── parents.py                # ParentLink resolution (geometry + reading order)
```

Each module exports a list of `Rule` objects. The engine sorts by priority, runs them all, combines confidences, picks the winner.

## Known failure modes

1. **The `Lives in other's home—specify relationship ___` pattern (CHOICES p1).** This is *one* logical question with two structural pieces: a checkbox option and an inline text input. The rule engine should be able to detect "checkbox option followed on the same line by a short underscore run" and emit a `CheckboxOption` block plus a `TextInput` child with `parent_link.relation = "input_of"`. If the rule misses, the `TextInput` becomes orphan and stage 3 routes it to the LLM. **Cost:** correct rule saves ~7 LLM calls per CHOICES form (one per "specify" pattern).
2. **Numbered safety-determination criteria (CHOICES p2–p6).** 15 items, each starting with a number and a checkbox. Rule must NOT confuse the number prefix for a `Sequence` value (that's stage 6's job). Block type is `CheckboxOption`; the number is part of the label text.
3. **The two long paragraphs on TX LTSS p1.** "Freedom of Choice…" and "Acknowledgement and Acceptance…" are `Display`. The rule must beat the (incorrect) "starts with capitalized phrase ending in colon → SectionHeader" rule. Resolution: add an "if next block is a long body paragraph and current is the first sentence of that paragraph, demote header confidence" rule.
4. **All-caps single-word lines** ("REQUIRED", "WITNESS, IF APPLICABLE"): can be header or display depending on context. Rule should look at neighbour patterns rather than text alone.
5. **Diacritics and smart quotes in TennCare forms** (apostrophe in "applicant's"): `’` vs `'`. Normalize in stage 1; rules should still tolerate either.

## Open questions for v1 implementer

1. **Rule priority vs. confidence-weighted voting.** Both work. Pick one and stick to it; mixed mental models cause rule-conflict bugs. Recommend confidence-weighted with priority as tiebreaker only.
2. **Where does parent-link resolution live — in the rule modules or in a dedicated post-pass?** Recommend post-pass (`parents.py`), because parent links often need full-document context (e.g. checkbox group spanning a page break).
3. **spaCy or no spaCy?** It's optional in v1. Adds 50ms startup and ~30MB. If we only need sentence-boundary and all-caps detection, regex is enough. Recommend skip until a rule actually needs it.
4. **Header detection threshold across font sizes** — fixed bp delta vs. relative-to-document-median? Relative is more portable across fixtures with different base font sizes; fixed is more predictable. Recommend relative.
5. **What's the minimum confidence to *not* go to the LLM gate?** Defined in issue 03 (default 0.70), but the rule-engine bar should be that *correctly-classified* blocks regularly score ≥0.85, leaving 0.70 as a comfortable margin.

## Acceptance criteria

- On the rule-only path (no LLM), per-block type accuracy ≥ 0.85 across `FX-CHOICES-001` and `FX-TXLTSS-001` (Eval A `type_accuracy` metric).
- ≥ 70% of blocks classified at confidence ≥ 0.70 (the gate threshold) — this is the cost lever for Eval E.
- Parent-link F1 ≥ 0.80 on the rule-only path (Eval A `parent_link_f1`).
- Every classification carries a non-null `rule_id` so eval triage can attribute regressions.
- Rule library has ≥80% line coverage in unit tests using synthetic mini-blocks.

## Out of scope

- LLM correction of low-confidence blocks (issue 04).
- Mapping `BlockType` → `QuestionType` controlled vocabulary (issue 06).
- Sequence assignment (issue 06).

## Cross-references

- Upstream: `01-extraction-canonical-json.md`
- Downstream: `03-ambiguity-decision-gate.md`
- Eval that scores this stage: `evals/eval-A-enriched-json.md` (`type_accuracy`, `parent_link_f1`)
- Cost lever: `10-cost-and-latency-budget.md`
- Logging shape: `09-observability-and-logging.md`
