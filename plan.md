# Implementation Plan: Hybrid PDF Form Extraction Pipeline

## Summary

Replace the current single-pass extraction approach with a staged pipeline that uses cheap PDF parsing first, routes complex pages to a vision model, extracts structured chunk results, merges them, validates them, and writes the required XLSX.

Model defaults:

- Vision model: `qwen.qwen3-vl-235b-a22b`
- Non-vision/formal text model: `qwen.qwen3-235b-a22b-2507-v1:0`

End-to-end flow:

```text
PDF
 -> page preparation + render cache
 -> cheap text/layout parse
 -> deterministic complexity scoring
 -> optional Qwen vision router for uncertain pages
 -> parser path or Qwen VLM path
 -> normalized semantic page model
 -> semantic chunks with stable IDs/order keys
 -> Qwen structured extraction per chunk
 -> Qwen merge for semantic conflicts
 -> deterministic validation/normalization
 -> styled XLSX
```

## Configuration

Add runtime configuration for:

- `BEDROCK_REGION`
- `BEDROCK_VISION_MODEL_ID=qwen.qwen3-vl-235b-a22b`
- `BEDROCK_TEXT_MODEL_ID=qwen.qwen3-235b-a22b-2507-v1:0`
- `PAGE_RENDER_DPI`, default `220`
- `COMPLEXITY_UNCERTAIN_THRESHOLD`, default `0.15`
- `MAX_CHUNK_INPUT_TOKENS`, default `3500`
- `MAX_CONCURRENT_BEDROCK_CALLS`, default `4`
- `EXTRACTION_CACHE_DIR`, default `runtime/cache`

Configuration should be read from environment variables and passed into the pipeline explicitly.

## Data Contracts

### Final Workbook Columns

The final XLSX must contain exactly these columns, in order:

```text
Section | Sequence | Question Rule | Question Type | Question Text | Branching Logic | Answer Text
```

### Structured Row Fields

Chunk extraction and final merge should use this row model. Only the seven workbook columns are written to XLSX; internal fields are retained for merge, validation, debugging, and review.

- `section`: string or null. Active section heading for this row. Rows before the first section use null/blank.
- `sequence`: integer or null. Assigned only after merge/normalization. Final values must be dense `1..N`.
- `question_rule`: string or null. Rule/prelude that scopes the question, such as `For children under age 18:`.
- `question_type`: enum. Must be one of the canonical values below.
- `question_text`: string. Visible prompt, label, table name, instruction text, or `New Section`.
- `branching_logic`: string or null. Final value must use one approved template.
- `answer_text`: string or null. Options, section name for marker rows, or blank for non-option rows.
- `source_ids`: internal list of strings. Supporting block/line/widget IDs.
- `chunk_id`: internal string. Producing chunk ID.
- `page_numbers`: internal list of integers. Source pages.
- `confidence`: internal float from `0.0` to `1.0`.

### Canonical Question Types

Use this final taxonomy:

```text
Display, Text Box, Text Area, Date, Number, Signature,
Radio Button, Checkbox, Dropdown, Group Table
```

- `Display`: Static instruction, notice, explanatory text, or synthetic section marker. Normal Display rows leave `answer_text` blank. Section markers use `question_text = "New Section"` and `answer_text = <section name>`.
- `Text Box`: Single-line free-text input.
- `Text Area`: Multi-line free-text input or explicit narrative/explanation area.
- `Date`: Date-like input: date, DOB, birth date, effective date, review date, signature date, month/year.
- `Number`: Numeric-only input: age, score, count, amount, percentage, number, or `#`.
- `Signature`: Signature, initials, signer name in a signature block, or authorized representative signature line.
- `Radio Button`: Single-select question with mutually exclusive options. Put the stem in `question_text` and options in `answer_text`.
- `Checkbox`: Standalone checkbox or independent check item.
- `Dropdown`: Single-select dropdown/list control. Put visible options in `answer_text`.
- `Group Table`: Parent row for a multi-row/multi-column table. Follow with one row per column header; do not extract table data rows.

### Branching Logic

Only these final templates are valid:

```text
If Q{n} = checked(selected)
Display if Q{n} = "{option_text_verbatim}"
```

Parent references are resolved after final sequence assignment.

## Implementation Phases

### Phase 1: Page Preparation

Implement a page preparation component that:

- hashes the source PDF
- splits the PDF into page-level work units
- renders each page to a Bedrock-supported image format at `PAGE_RENDER_DPI`
- stores rendered images under the cache directory
- exposes page metadata: page number, width, height, image path, and page hash

The render cache key should include PDF hash, page number, render DPI, and renderer version.

### Phase 2: Cheap Text/Layout Parser

Implement a parser component using PyMuPDF/pdfplumber-style extraction that produces a raw page model:

- text spans
- grouped lines
- grouped blocks
- basic bounding boxes
- font size/style hints
- AcroForm widgets where present
- checkbox/radio glyph candidates
- table/grid candidates

Do not send this raw model directly to the LLM.

### Phase 3: Semantic Hint Builder

Convert raw layout into compact LLM-friendly hints:

- `section_heading`
- `instruction`
- `question_stem`
- `blank_field`
- `choice_option`
- `choice_group`
- `followup_field`
- `table`
- `table_column`
- `signature_block`
- `repeated_header`
- `repeated_footer`
- `unknown`

The semantic page model should include stable IDs like `p3_b08`, relative layout facts, and human-readable hints:

```text
[p3_b08] role=checkbox_candidate, indent=0, group=g1:
Hospital records attached

[p3_b09] role=followup_blank, indent=1, possible_parent=p3_b08, field_shape=single_line_blank:
Description of documentation attached: ______
```

Use relative signals such as `indent_level`, `column`, `nearby_heading`, `field_shape`, `possible_parent`, and `group_candidate`; avoid exposing raw coordinates except for debugging or image crop targeting.

### Phase 4: Complexity Routing

Implement deterministic complexity scoring before any VLM call.

Signals:

- low extracted text density or suspiciously empty page
- abnormal reading order
- dense overlapping spans
- multi-column layout
- complex table/grid structure
- many unlabeled boxes/glyphs
- parser disagreement between text/table/widget signals
- possible page-boundary continuation

Router output:

```json
{
  "page": 3,
  "complexity": "simple | medium | complex",
  "recommended_path": "parser | vlm",
  "uncertainty": 0.22,
  "reason": "dense table with unclear reading order",
  "continuation": {
    "from_previous": false,
    "to_next": true
  }
}
```

If deterministic uncertainty is above `COMPLEXITY_UNCERTAIN_THRESHOLD`, call `qwen.qwen3-vl-235b-a22b` with the rendered page image to classify the page and recommend `parser` or `vlm`.

### Phase 5: Parser Path

For pages routed to `parser`, generate:

- page Markdown
- semantic page model
- continuation hints
- repeated header/footer hints

Simple pages usually become one chunk. Medium pages may be chunked by section, table, or question group.

### Phase 6: Qwen VLM Path

For pages routed to `vlm`, call `qwen.qwen3-vl-235b-a22b` with the rendered page image.

The VLM prompt must request both clean Markdown and semantic structure:

- sections
- questions
- choice groups
- options
- blank fields
- tables and column headers
- follow-up/indent relationships
- continuation hints
- repeated visual bands

Normalize the VLM output into the same semantic page model used by the parser path so chunking and extraction are shared.

### Phase 7: Semantic Chunking

Build chunks from normalized semantic page models.

Rules:

- simple page: one chunk per page unless token budget is exceeded
- dense page: split by section or table
- long section: split into question groups
- choice group: keep stem, options, and follow-ups together
- table: keep title and headers together; ignore data rows
- continuation: merge page tail with next page head before extraction
- repeated header/footer: drop before extraction
- tiny adjacent chunks in the same section: merge until token budget is reached

Chunk contract:

```json
{
  "chunk_id": "p03_c02",
  "pages": [3],
  "order_key": [3, 2],
  "section_hint": "Medical Documentation",
  "source_block_ids": ["p3_b08", "p3_b09", "p3_b10"],
  "chunk_type_hint": "checkbox_followups",
  "markdown": "...",
  "structure_hints": ["..."]
}
```

### Phase 8: Chunk Structured Extraction

Use `qwen.qwen3-235b-a22b-2507-v1:0` for non-vision structured extraction and other formal text-only use cases.

For each chunk, send:

- chunk Markdown
- compact structure hints
- chunk metadata
- structured row schema
- canonical question type definitions
- instruction to extract only from that chunk

Require structured JSON/Pydantic output:

```json
{
  "chunk_id": "p03_c02",
  "rows": [
    {
      "section": "Medical Documentation",
      "sequence": null,
      "question_rule": null,
      "question_type": "Checkbox",
      "question_text": "Hospital records attached",
      "branching_logic": null,
      "answer_text": null,
      "source_ids": ["p3_b08"],
      "page_numbers": [3],
      "confidence": 0.91
    }
  ]
}
```

Run chunk extraction concurrently with a semaphore controlled by `MAX_CONCURRENT_BEDROCK_CALLS`.

### Phase 9: Merge, Dedupe, and Resolve

Sort chunk outputs by `order_key`.

Use deterministic logic for:

- sequence assignment
- final column projection
- valid question type enforcement
- branch template validation
- section marker placement
- Display row cleanup
- table parent/column row shape

Use `qwen.qwen3-235b-a22b-2507-v1:0` only for semantic merge decisions:

- cross-page continuation stitching
- split option-list merging
- duplicate repeated-template detection
- section conflict resolution
- low-confidence conflict review

After semantic merge, run deterministic validation again.

### Phase 10: XLSX Writer Integration

Write the final rows to a single-sheet workbook with:

- exact seven columns in order
- styled dark-blue bold header
- frozen header row
- dense `Sequence`
- wrapped text
- top-aligned data rows
- consistent column widths with current goldens

Do not write internal fields such as `source_ids`, `chunk_id`, `page_numbers`, or `confidence`.

## API Integration

Keep the existing public API shape:

- `POST /api/extract`: accepts a PDF and returns the XLSX response/download artifact.
- `GET /api/health`: liveness check.

Internally, replace the extraction implementation behind the API with the new staged pipeline. API behavior should remain compatible for the frontend.

## Test Plan

Add tests for:

- page render cache key stability
- raw parser to semantic hint conversion
- complexity scoring decisions for simple, medium, complex, and uncertain pages
- Qwen router fallback when deterministic complexity is uncertain
- semantic chunk IDs and order keys
- continuation chunk merging
- structured row schema validation
- branch template validation
- section marker insertion
- table parent + column row generation
- XLSX column order and styling
- golden workbook row-level match for reference PDFs

Acceptance targets:

- final workbook has exactly the required seven columns
- `Sequence` is dense and gap-free
- all final `Question Type` values are canonical
- every detected section has one `New Section` marker row
- branching logic uses only approved templates
- repeated headers/footers are removed
- cross-page continuations are merged
- row-level match reaches the quality bar in `docs/requirements.md`

## Implementation Order

1. Add configuration and Bedrock client wrappers for Qwen vision and Qwen text.
2. Add page preparation and render cache.
3. Add cheap parser/layout extraction.
4. Add semantic hint builder.
5. Add deterministic complexity scorer.
6. Add optional Qwen router for uncertain pages.
7. Add Qwen VLM page-to-Markdown-plus-structure path.
8. Add normalized semantic page model shared by parser and VLM paths.
9. Add semantic chunker with stable IDs/order keys.
10. Add Qwen chunk-level structured extraction.
11. Add merge/dedupe/continuation resolution.
12. Add deterministic final validators.
13. Integrate with XLSX writer and API.
14. Add golden evaluation tests.
