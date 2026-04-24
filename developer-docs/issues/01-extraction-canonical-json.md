# Issue 01 — Stage 1: PDF extraction to canonical JSON

## Summary

Stage 1 converts a PDF into a lossless, structured representation that downstream stages can reason about without re-parsing bytes. **No semantic interpretation happens here.** The output is a digital twin of the document: every visible block is captured with its bounding box, its position in reading order, and its parent-child containment in the document tree. Whether a "Lives in own home" line is a checkbox option or a heading is a *stage 2* concern; stage 1 just captures that there is a line of text at that bbox in that order on that page.

Getting this stage right is upstream of everything. A block silently dropped here will be silently absent from the final workbook. A reading-order error here means stage 2 receives a mis-shaped tree and propagates the error downstream. Any debug session that gets into stage 5+ usually traces back to here.

## Inputs / Outputs

**Input:** a single PDF file. Either native (text layer present) or scanned (image-only).

**Output:** a `CanonicalDocument` JSON.

```python
class Bbox(BaseModel):
    page: int          # 1-indexed
    x0: float; y0: float; x1: float; y1: float   # PDF user space coords
    coord_origin: Literal["top_left", "bottom_left"]   # MUST be explicit

class RawBlock(BaseModel):
    block_id: str                       # stable, deterministic from (page, bbox, content hash)
    bbox: Bbox
    reading_order: int                  # global, document-wide
    text: str                           # exact glyph-level text
    raw_kind: Literal[                  # what the parser saw, NOT a semantic type
        "paragraph", "heading", "list_item",
        "table_cell", "table_row", "table",
        "figure", "form_field", "checkbox_glyph",
        "underline_run", "page_header", "page_footer",
    ]
    parent_id: Optional[str]            # parent in the document tree
    children_ids: List[str]
    style: Optional[Style]              # font_name, font_size, bold, italic, color
    page_number: int

class CanonicalDocument(BaseModel):
    source_path: str
    source_sha256: str                  # hash of the input PDF — used as a cache key
    parser: str                         # e.g. "docling@2.x"
    parser_version: str
    extracted_at: datetime
    blocks: List[RawBlock]
    page_count: int
```

This schema is the **upstream half of the canonical contract**. Stage 2 reads it and only it.

## v1 design decision

v1 uses **Docling** (IBM, MIT) as the primary parser, with **Granite-Docling-258M** as the VLM fallback for layouts Docling's heuristics can't resolve, and **TableFormer** for table structure. Output is consumed via the **DocTags** markup format, which preserves reading order and parent-child structure faithfully.

Why Docling:
- MIT-licensed and runs locally — no per-page cost, no PII egress for the extraction step.
- Native handling of tables (TableFormer), figures, and form fields in one pass.
- Reading-order resolution for two-column layouts is meaningfully better than pdfplumber on the CHOICES form (the right-column "specify ___" continuation lines on page 1 attach to the correct checkbox option).
- DocTags is dump-friendly and round-trippable — easy to snapshot for fixtures.

Wrapped behind **docling-serve** (FastAPI) for a stable HTTP boundary so the stage 2+ pipeline can be developed against a mocked extractor without a Docling install.

## Design space considered

| Option | Why not (for v1) |
|---|---|
| **pdfplumber** | Excellent text extraction but weak on reading order for two-column forms; tables require manual row/col reconstruction; no VLM fallback. |
| **Unstructured.io** | Good multi-format support but the open-source path is slower than Docling on multi-page assessments and the hosted API would put PII outside our boundary. |
| **AWS Textract** | Strong on scanned forms and explicit form-field detection (key-value pairs), but per-page cost and PII egress make it the wrong default. **Should be revisited as a fallback** for scanned-only PDFs that Docling+VLM can't handle (see open question below). |
| **PyMuPDF (fitz)** | Fastest text extraction but no native semantic structure — we'd be rebuilding what Docling gives us for free. |
| **Pure VLM (Granite/Qwen-VL alone)** | Conceptually simplest but per-page cost and latency are 10–100× a deterministic parser, and reading-order hallucinations on long docs are a known failure mode. |

## Known failure modes

Concrete failures observed or anticipated on the two seed fixtures:

1. **CHOICES form, page 1, "Lives in other's home—specify relationship ___":** the trailing underscore is a separate text-input block whose semantic parent is the checkbox option to its left. If reading order serializes them as siblings instead of parent/child, stage 2 will mis-classify the underscore as orphan and stage 3 will route it to the LLM for parent-link recovery — survivable but wasteful. Stage 1 should preserve enough geometry (bbox proximity) for stage 2 to recover this without the LLM.
2. **Multi-page checkbox lists** (CHOICES pages 2–6 with 15 numbered safety-determination criteria, each with its own checkbox): list-item parent linkage must survive page breaks. Docling's default heuristics break the list at the page boundary; verify with `FX-CHOICES-001` that all 15 items end up as siblings under the same list parent.
3. **Signature blocks** (TX LTSS form has "Printed Name | Signature | Date" rows for applicant, witness, service coordinator): these are structurally tables but visually three short underlines. Easy to mis-extract as figure or as raw paragraphs. TableFormer should catch them; if not, raw_kind must at least be `form_field` so stage 2 can recover.
4. **Scanned PDFs**: not present in seed fixtures but inevitable in production. Docling needs OCR; quality drops sharply. **Open question:** do we add a Tesseract preprocessor or hand off to Textract?
5. **Header/footer repetition**: every CHOICES page repeats `"Applicant Name: ___ SSN: ___ DOB: ___"` and a form-revision footer. Stage 1 must mark these as `page_header` / `page_footer` so stage 2 can collapse duplicates rather than producing 11 copies of the same row.
6. **Glyph-level text errors**: ligatures (e.g. "ﬁ" → "fi"), curly quotes, em-dashes vs hyphens. Stage 1 normalizes to NFC Unicode and replaces common ligatures; everything else is preserved verbatim.
7. **Form fields with no visible label** (interactive PDF AcroForm fields that have a name but no rendered text): captured as `form_field` with empty `text` and the field name in `style.field_name`. Stage 2 then has the metadata to recover.

## Open questions for v1 implementer

1. **Scanned PDF strategy.** Tesseract preprocessor, AWS Textract fallback, or fail-loud and require human re-OCR? Recommend: fail-loud for v1 to keep scope bounded; revisit when first scanned fixture is reported.
2. **DocTags vs. native Docling JSON.** DocTags is friendlier for fixture snapshots; the native JSON exposes a few extra style fields (table cell merge info, list-marker text). Pick one and stick to it for the lifetime of the schema — switching mid-stream invalidates Eval A goldens.
3. **Bbox coordinate origin.** Docling exposes `top_left` by default; openpyxl/PDF-native is `bottom_left`. The schema requires `coord_origin` to be explicit so downstream code doesn't have to guess. Pick `top_left` everywhere and convert at the parser boundary.
4. **`block_id` derivation.** Hash of `(page, rounded_bbox, sha256(text))` is deterministic and stable across reruns of the same PDF. But it changes if Docling ever changes its bbox rounding. Acceptable? (Yes, because Eval A goldens are regenerated when `parser_version` changes.)
5. **Caching.** Should the canonical JSON be cached by `source_sha256` to avoid re-running Docling during dev iteration? Strong yes for dev ergonomics; specify a local on-disk cache under `.cache/extraction/`.

## Acceptance criteria

- For both seed fixtures (`FX-CHOICES-001`, `FX-TXLTSS-001`), the canonical JSON contains every visible block on every page (block-coverage metric in Eval A ≥ 0.98).
- Reading order matches a hand-curated golden ordering with Kendall-tau ≥ 0.95 (Eval A `sequence_correctness`).
- The same PDF run twice produces byte-identical canonical JSON (`block_id` stability).
- Page-header/footer detection rate ≥ 0.95 on `FX-CHOICES-001` (which has 11 repeated headers).
- All measurements logged per Issue 09's structured-log shape.

## Out of scope

- Semantic typing of blocks (issue 02).
- Confidence scoring (issue 02).
- Anything that requires the golden template's controlled vocabulary (issue 06).
- Persistent storage of canonical JSON beyond the dev cache (S3 lifecycle: issue 11).

## Cross-references

- Downstream consumer: `02-rule-based-classification.md`
- Boundary spec: `evals/contracts.md` (Boundary 0 — internal, but shape is documented)
- Eval that scores this stage: `evals/eval-A-enriched-json.md` (block-coverage and sequence-correctness sections)
- Cost considerations: `10-cost-and-latency-budget.md`
