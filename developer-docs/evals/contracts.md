# Eval contracts — the two stable boundaries

The evaluation suite scores against two boundaries. Any pipeline version (v1, v2, vN) that respects these boundaries can be evaluated by the same harness on the same fixtures. Implementation choices upstream of, downstream of, or around these boundaries are unconstrained.

## Boundary 1 — Enriched JSON

The canonical, semantically-typed representation of the input PDF. Used by Eval A.

A document conforming to Boundary 1 is a JSON object matching this schema (described abstractly; an exact JSON Schema export lives at `developer-docs/evals/schema/enriched.schema.json`, regenerated whenever `schema_version` bumps):

```
EnrichedDocument:
  schema_version: string         # e.g. "1.0.0"
  source:
    sha256: string               # hash of source PDF bytes
    page_count: integer
  blocks: array of EnrichedBlock

EnrichedBlock:
  block_id: string               # stable across reruns of the same input
  page: integer (1-indexed)
  reading_order: integer (document-wide)
  bbox: { x0, y0, x1, y1, coord_origin: "top_left"|"bottom_left" }
  text: string                   # exact glyph-level
  block_type: string             # one of the BlockType enum values
  confidence: number in [0.05, 0.99]
  parent_link:
    parent_block_id: string
    relation: "option_of" | "input_of" | "row_of" | "child_of_section"
  branching_logic: string | null # EAB DSL expression
  sequence: integer | null
  question_type: string | null   # one of the QuestionType controlled-vocab values
  provenance:
    source: "rule" | "llm" | "merge"
```

**BlockType controlled vocabulary:** `section_header`, `subsection_header`, `question_label`, `checkbox_option`, `radio_option`, `text_input`, `text_area`, `date_input`, `display`, `signature_field`, `table_header`, `table_cell`, `page_header`, `page_footer`, `unknown`.

**QuestionType controlled vocabulary:** read from the golden template's `Values` sheet (column C). The fixture's `template_version` pins the exact set.

### Boundary 1 is optional for v2

A v2 with no equivalent intermediate (e.g. an end-to-end VLM that emits .xlsx directly) skips Eval A. It is then entirely judged by Evals B, C, D, E. The team accepts that we lose stage-localized regression detection in exchange for whatever v2 wins.

A v2 that *does* have a JSON intermediate but with a different schema can opt into Eval A by writing a one-time adapter from its native shape to Boundary 1. The adapter is reviewed in PR; the eval scores are run unchanged.

## Boundary 2 — Workbook (.xlsx)

The final output artifact. Used by Evals B, C, D.

A workbook conforming to Boundary 2:

1. Is a valid `.xlsx` file readable by openpyxl, LibreOffice, and Excel.
2. Is structurally identical to the golden template `.xlsx` referenced by `template_version` — same sheets, same column header row, same controlled-vocabulary dropdowns, same formatting.
3. Has the question rows appended starting at the row immediately after the column header row of the question sheet (`Assessment v2` for the CHOICES template).
4. Every question row populates the columns per `issues/06-golden-template-mapping.md`'s mapping table, with controlled-vocabulary values exactly matching the `Values` sheet.
5. The metadata header block (rows 3–12) is populated per the same table; preserved-blank fields are blank.

The Boundary-2 contract is anchored to the template, not to a separate spec doc. To know what's required, read the template. The template lives at `docs/support_docs/CHOICES Safety Determination Request Form Final_11_20.xlsx` and is versioned via `template_version` (issue 08).

### Boundary 2 is mandatory

Every pipeline version must produce a Boundary-2 workbook. Evals B, C, D are all driven from this artifact. Evals B and C are mandatory; Eval D becomes mandatory when v2 introduces non-trivial mapping changes that can produce equivalent-but-different output.

## What the contracts deliberately do not specify

- **Number of stages.** v1 has 7; v2 may have 1.
- **Whether the LLM is in the loop.** v2 may be rule-only or model-only.
- **Programming language or framework.**
- **Where intermediate artifacts live.** v1 caches in `.cache/`; v2 may stream end-to-end.
- **How parent-link or branching logic is computed internally.** Only the final emitted form is scored.
- **Cost or latency.** Tracked by Eval E but not part of the correctness contract.

## Schema versioning rules

- `schema_version` bump = the JSON Schema for Boundary 1 changed.
- `template_version` bump = the .xlsx schema for Boundary 2 changed (issue 08).
- Both versions are pinned per fixture in `fixtures.md`. A version bump invalidates that fixture's golden until regenerated.
