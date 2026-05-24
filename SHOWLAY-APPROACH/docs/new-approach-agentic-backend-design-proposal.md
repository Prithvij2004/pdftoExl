# Design Proposal — Agentic Backend for HIP Assessment Extraction

**Version:** 1.4
**Date:** 2026-05-22
**Companion to:** `requirement_analysis_corrected.md` v3.2
**Predecessor input:** `suggestions.md` (incorporated and extended)
**Goal:** Replace SHOWLAY's brittle page-by-page + 38-stage post-processor with a section-aware agentic backend that generalizes across healthcare assessment/configuration form templates.

**v1.1 changes — reviewer comments incorporated:**
- Stage 1 routing: widgets reframed as supporting evidence (not primary signal)
- Stage 3 prompt: "meaningful form artifact" + explicit chrome exclusion + examples-only-illustrative caveat
- Stage 4: `sparsify_section` removed from normalizer; v1.3 now writes dense section values in Stage 6 too
- Stage 4: `validate_branching_refs` corrected — forward references are valid; only orphans are flagged
- §5 Model selection: candidate model **families** with benchmark-driven final selection; specific versions/prices moved to advisory appendix
- §8 Textract positioning sharpened — document intelligence layer only, never the semantic extractor
- §1 / FR scope bounded to healthcare assessment/configuration form templates
- Evaluation criteria broken out into per-dimension metrics
- Gap closure and risks updated

**v1.2 changes — consistency cleanup only (no architectural change):**
- Hardcoded `Haiku 4.5` / `Sonnet 4.6` references in Stage 2, Stage 3, schemas, gap closure, and risks replaced with `low-cost tier` / `mid-tier` wording
- Tool-use schema field `recommended_model` renamed to `recommended_model_tier` with values `low_cost` | `mid_tier`
- Exact Claude model IDs removed in v1.4; §5 now names the supported Bedrock alternatives

**v1.3 changes — Excel output contract locked:**
- All generated Excel files use one canonical column set, regardless of PDF type
- Template/profile logic no longer controls output column names
- Profiles are replaced by one canonical Excel schema; PDF-specific variance stays in extraction, not in workbook headers

**v1.4 changes — no-Claude Bedrock model plan:**
- Claude-family placeholders replaced with us-west-2 supported Bedrock models
- Default cycle is Nova 2 Lite for profiling/simple extraction and Nova Pro for complex sections
- Qwen3-VL, Mistral Large 3, and Kimi K2.5 stay in the benchmark matrix for performance comparison and fallback planning
- Textract stays in the architecture as a future document-intelligence option, but v1 implementation must not depend on it because access is not available yet

---

## 1. Design Drivers

These constraints, in priority order, drove every architectural choice:

| # | Constraint | Source | Architectural consequence |
|---|---|---|---|
| 1 | **Open-ended PDF scope** (bounded to healthcare assessment/configuration form templates) | Team confirmation: "not specific to these PDFs" | Zero form-specific code in extraction; one fixed Excel column contract for every output |
| 2 | **Existing UI must keep working** | User decision: "I don't want the UI change if this would work fine" | Manifest JSON contract preserved (additive changes only); no backend feature requires UI changes in v1 |
| 3 | **MNLOC must work** | "Current code fails to generalize, MNLOC blank PDF fails" | Section-aware extraction; item-code branching preservation; complexity-based routing |
| 4 | **Cost-conscious POC** | "For POC they should have considered cost" | Default to Amazon Nova 2 Lite; escalate to Amazon Nova Pro only for complex sections; benchmark Qwen3-VL, Mistral Large 3, and Kimi K2.5 alternatives; Textract only when needed |
| 5 | **No golden-workbook leakage** | suggestions.md §4.3 and code review | `truth_path` removed from extraction; golden used only for offline eval |
| 6 | **Backend-owned Excel** | User: "Excel in backend, like current code" | Reuse SHOWLAY's writer.py as a backend-owned canonical Excel writer |
| 7 | **AWS Bedrock platform** | Team has access | All LLM and Textract calls go through AWS |

---

## 2. Architectural Verdict on SHOWLAY-APPROACH

After deep code review, SHOWLAY is a **successful POC and an unsuitable production base** as currently structured. Two failure modes are baked in:

### 2.1 Page-isolated extraction
`extract.extract_document()` does one Bedrock Converse call per page with no document-level context. For MNLOC's 34 pages this means:
- Shared coding legends defined in Section A are invisible when extracting Section H
- Skip-target items (e.g. `GG0115` referenced from Section B page 4) are unknowable when extracting earlier pages
- Section structure must be reconstructed in post-processing from individual page outputs
- Repeated header bands are extracted N times then deduped — wasted tokens

### 2.2 Form-specific cleanup in post-processing
`postprocess.py` (2560 lines, ~38 stages) encodes patterns specific to CHOICES + H1700-3:
- `_INSTANCE_CONTEXT_RE` lists words like `fall|visit|admission|episode|medication|service|diagnosis` — CHOICES vocabulary
- `_CHROME_TEXT_RE` hardcodes form titles (`safety determination request form`, `rda 2047`)
- `_truth_group_table_specs(truth_path)` reads the golden workbook in production
- `coerce_text_area_for_bullet_prompts`, `force_display_for_document_below`, `force_text_box_for_description_attached` — fixture-specific rules

These rules **inflate accuracy on the development set and degrade it on novel forms**. They are the root cause of "fails to generalize."

### 2.3 Selective reuse plan

| SHOWLAY component | Decision | Justification |
|---|---|---|
| `extract.probe_and_rasterize()` | **Keep** | Solid PDF probe + rasterize + widget walk + text-layout extraction |
| `extract._layout_rect_index()` + source_id resolution | **Keep** | Source-grounding mechanism for bbox traceability |
| `extract.extract_document()` (page loop) | **Replace** | Page-isolated; needs section-aware replacement |
| `schema.py` 28-column model | **Refactor** | Keep field aliases + Row model; drop 28-col coupling |
| `schema.FIELD_HEADER_ALIASES` | **Keep only for migration/import** | Useful for reading legacy inputs, not for deciding output column names |
| `postprocess.py` (38 stages) | **Replace** | Mostly form-specific; rewrite as ≤10 generic transforms |
| `confidence.score_rows()` | **Keep** | Schema gate + grounding + branching-ref check is right shape |
| `field_review.build_review_manifest()` | **Keep** | The JSON contract the UI consumes; additive extensions only |
| `writer.write_workbook()` + `map_template_columns()` | **Keep + simplify** | Writer stays, but output uses the fixed canonical Excel schema |
| `writer.write_review_sidecar()` | **Keep** | Review queue sidecar workbook |
| `webapp.py` | **Keep** | UI works; will be wired to new pipeline |
| `eval.py` | **Keep + extend** | Evaluation harness; clarify it's offline-only |

**Lines reused:** ~650
**Lines replaced:** ~3000
**Lines added new:** ~1800
**Net:** Smaller, cleaner, generalizes.

---

## 3. The Architecture

### 3.1 Pipeline overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 0: CANONICAL EXCEL SCHEMA                                    │
│  • Load one fixed output header set for every generated workbook    │
│  • No PDF-specific or workbook-specific output column names         │
│  • Optional template file is style-only, never a naming source      │
│  • Stages 1-5 are workbook-agnostic; Stage 6 writes the same schema │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 1: PDF INGESTION (deterministic, no LLM)                     │
│  Inputs: PDF file                                                   │
│  Source-of-truth rule: visible rendered text + spatial layout are   │
│   PRIMARY. Widgets are SUPPORTING evidence only (input affordance,  │
│   widget-type hints, traceability).                                 │
│  Steps:                                                             │
│   1.1 PyMuPDF probe — pages, AcroForm widgets, text+bboxes          │
│   1.2 Rasterize each page @ 200 DPI                                 │
│   1.3 Routing for document-intelligence layer:                      │
│       - widgets present, simple layout → widgets+text+layout enough │
│       - tables suspected → augment with Textract TABLES             │
│       - scanned / no text layer → Textract LAYOUT primary           │
│       - signatures / checkboxes mass detection → Textract FORMS     │
│   1.4 Optional Textract calls (per routing) — FORMS, TABLES, LAYOUT │
│       Textract provides OCR + layout + selection evidence ONLY.     │
│       Semantic HIP classification belongs to Stages 2-4.            │
│  Outputs: DocStructure {pages: [{image, widgets, text_blocks,       │
│                                  tables, layout_blocks}]}           │
│  Reuse: SHOWLAY's probe_and_rasterize() + extend with Textract path │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 2: DOCUMENT PROFILER (1 LLM call, low-cost model tier)       │
│  Input: first page image + last page image + text excerpts          │
│         + table-of-contents-style summary of all pages              │
│  Output:                                                            │
│   {                                                                 │
│     "form_title": "Medical Necessity and Level of Care Assessment", │
│     "form_version": "V.21 Effective 09/01/2025",                    │
│     "complexity": "simple" | "medium" | "complex",                  │
│     "sections": [                                                   │
│       {"name":"Section A. Identification Information","pages":[1,2]},│
│       {"name":"Section B. Hearing, Speech, and Vision","pages":[3]} │
│     ],                                                              │
│     "shared_legends": [                                             │
│       {"location":"Section A","content":"0=No, 1=Yes, 9=Unable..."} │
│     ],                                                              │
│     "extraction_strategy": "single_call" | "section_by_section",   │
│     "recommended_model_tier": "low_cost" | "mid_tier"               │
│   }                                                                 │
│  Decision rule:                                                     │
│   - pages ≤ 6 AND complexity != complex → single_call               │
│   - else → section_by_section                                       │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 2.5: EXTRACTION POLICY AGENT (1 LLM call, mid-tier model)    │
│  Input: full per-page text/widget evidence + profile + sections     │
│         + selected page images for layout clues                     │
│  Output: PDF-specific instructions for:                             │
│   • how this PDF shows each Question Taxonomy type                  │
│   • how each canonical field appears in this PDF                    │
│   • repeated chrome / false fields to ignore                        │
│   • section-specific exceptions and ambiguous patterns              │
│  Rule: this agent does NOT extract rows and does NOT add columns.    │
│  Its output is guidance pasted into Stage 3 prompts.                 │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 3: SECTION EXTRACTOR (N LLM calls, one per section OR one)   │
│  Per-section input:                                                 │
│   • Page images for this section                                    │
│   • Widgets + text blocks scoped to these pages                     │
│   • Textract output (tables/forms/layout) scoped to these pages     │
│   • Stage 2.5 extraction policy, narrowed to this section           │
│   • Document context:                                               │
│       - form_title                                                  │
│       - ALL section names (so agent knows where to point branches)  │
│       - this section's name and position                            │
│       - shared_legends carried forward from earlier sections        │
│   • Pydantic JSON schema defining the row contract                  │
│  Output: list of row objects per canonical schema (req-analysis §5) │
│  Model: low-cost tier default, mid-tier if profiler said "complex"  │
│  (specific Bedrock model IDs are configurable — see §5)             │
│  Reuse: SHOWLAY source_id grounding (T###/W### IDs)                 │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 4: GLOBAL NORMALIZER (deterministic, ~9 generic functions)   │
│  All functions are PDF-agnostic and contain ZERO form-specific code.│
│  Canonical JSON keeps `section` DENSE on every row. Stage 6 writes  │
│  section values on every row for consistent filtering/sorting.      │
│                                                                     │
│   4.1 concatenate_sections        — merge in document order         │
│   4.2 assign_global_sequence      — dense 1-based                   │
│   4.3 build_external_id_index     — {external_id: sequence}         │
│   4.4 resolve_item_code_branches  — Q-refs from item codes          │
│   4.5 apply_validation_defaults   — HIP defaults by type            │
│   4.6 validate_branching_refs     — referenced Q/external IDs must  │
│                                     resolve; FORWARD refs ALLOWED;  │
│                                     unresolved/orphan refs flagged  │
│   4.7 validate_choice_options     — \n\n separator, no empty opts   │
│   4.8 score_confidence            — reuse SHOWLAY confidence.py     │
│   4.9 emit_warnings               — global validation issues        │
│                                                                     │
│  Output: canonical JSON per req-analysis §5                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 5: REVIEW MANIFEST BUILDER                                   │
│  • Wraps canonical JSON into SHOWLAY-compatible manifest format    │
│  • Adds: page evidence, bbox per row, suggested actions, risk level │
│  • Backward-compatible with existing UI (additive fields only)     │
│  • Persisted to disk as <job_id>_review_manifest.json              │
│  Reuse: SHOWLAY's build_review_manifest, adapted to new schema     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 6: EXCEL EXPORT (deterministic, fixed-schema)                │
│  Input: canonical JSON + canonical Excel schema                     │
│  Steps:                                                             │
│   6.1 Write fixed headers in fixed order                            │
│   6.2 Use canonical question_type spelling                          │
│   6.3 Write source-preserving condition to Branching Logic          │
│   6.4 Write machine/actionable condition to Question Rule           │
│   6.5 Always write Section, Sequence, External ID when available    │
│   6.6 Generate workbook and review sidecar                          │
│  Reuse: SHOWLAY's writer.write_workbook(), simplified               │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  EXISTING SHOWLAY UI (untouched)                                    │
│  Consumes the manifest JSON, displays workbench, calls /export      │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Module structure

```
backend/
├── api/
│   └── routes.py                    # FastAPI endpoints (mirrors webapp.py contract)
├── pipeline/
│   ├── stage1_ingest.py             # PyMuPDF + Textract routing
│   ├── stage2_profiler.py           # Document profiler agent
│   ├── stage2b_policy.py            # PDF-specific extraction policy agent
│   ├── stage3_extractor.py          # Section extractor agent
│   ├── stage4_normalizer.py         # Generic normalization (10 fns)
│   ├── stage5_manifest.py           # Review manifest builder
│   └── stage6_export.py             # Fixed-schema Excel writer
├── agents/
│   ├── profiler_agent.py            # Bedrock client + prompt for Stage 2
│   ├── policy_agent.py              # Bedrock client + prompt for Stage 2.5
│   ├── extractor_agent.py           # Bedrock client + prompt for Stage 3
│   └── tool_schemas.py              # Pydantic schemas for JSON output
├── excel/
│   ├── canonical_columns.py         # Fixed output headers + schema version
│   ├── writer.py                    # Generate canonical workbook
│   └── review_sidecar.py            # Review workbook output
├── schemas/
│   ├── canonical.py                 # Pydantic models for canonical JSON
│   └── manifest.py                  # Manifest JSON contract (frozen)
├── services/
│   ├── pymupdf_service.py           # probe, rasterize, widgets, text-layout
│   ├── textract_service.py          # Textract LAYOUT + TABLES + FORMS
│   ├── bedrock_service.py           # Bedrock client wrapper
│   └── confidence_service.py        # Reused from SHOWLAY's confidence.py
├── eval/
│   ├── comparator.py                # Greedy match + per-column accuracy
│   ├── golden_loader.py             # OFFLINE ONLY — strict separation
│   └── reports.py                   # summary.md + summary.json
└── webapp.py                        # Reused from SHOWLAY (existing UI)
```

---

## 4. Stage Details

### 4.1 Stage 1 — PDF Ingestion

**Textract's role (locked):**
> Textract is part of the **document intelligence / layout evidence layer**. It provides OCR, layout, forms, tables, selection-element (checkbox state), and signature evidence. Textract is **never** the semantic extractor — HIP classification, question grouping, answer-option interpretation, and branching logic interpretation remain the responsibility of the LLM/agent (Stages 2-3) and the deterministic normalizer (Stage 4).

**Current access constraint:** Textract access is not available for the POC right now. Keep the routing interface and feature flag in the design, but do **not** implement Textract calls, require Textract IAM permissions, or make any v1 acceptance criterion depend on Textract output. The v1 path must work with PyMuPDF text/layout, widgets, page rasterization, and Bedrock vision models only. Textract can be added later behind the same interface when access is granted.

**When to invoke Textract:**
- Scanned PDFs without a text layer
- Table-heavy pages where structured cell grids would help
- Pages where checkbox/selection state needs to be detected reliably
- Signatures
- Cases where PyMuPDF's layout extraction is unreliable (corrupted xref, XFA forms)

**Routing decision matrix:**

| PDF condition | Use PyMuPDF for | Use Textract for |
|---|---|---|
| AcroForm widgets present, simple | Everything | (skip) |
| AcroForm widgets, complex tables | Widgets + text-layout | TABLES only, scoped to table pages |
| No widgets, text-extractable | Text-layout | LAYOUT for reading order |
| Scanned / no text layer | (just rasterize) | LAYOUT + FORMS + TABLES |

Per-page Textract pricing varies by region and feature; final cost projections are tracked in the Phase 4 benchmark report, not this design doc.

### 4.2 Stage 2 — Document Profiler

**Single LLM call.** Inputs:
- First page image (often contains form title, version)
- Last page image (often contains revision date, agency)
- Text snippets from each page (first 200 chars, gives ToC-like signal)

**Prompt structure:**
```
You are profiling a healthcare PDF form for extraction strategy.

Given:
- First page: <image>
- Last page: <image>
- Per-page text excerpts: <list>

Identify:
1. Form title and version
2. Sections (name + page range)
3. Shared coding legends (option scales referenced across multiple items)
4. Complexity: simple (1-page form, mostly text), medium (multi-page, fillable),
   complex (cross-page sections, skip logic, coded items)

Output valid JSON matching the provided schema.
```

**JSON schema (LangChain `ChatBedrockConverse`, Pydantic):**
```python
class DocumentProfile(BaseModel):
    form_title: str
    form_version: str
    complexity: Literal["simple", "medium", "complex"]
    sections: list[Section]
    shared_legends: list[Legend]
    extraction_strategy: Literal["single_call", "section_by_section"]
    recommended_model_tier: Literal["low_cost", "mid_tier"]
```

Profiler output is requested through LangChain `ChatBedrockConverse` and
validated with the `DocumentProfile` Pydantic schema.

### 4.2.5 Stage 2.5 — Extraction Policy Agent

This agent runs after section planning and before extraction. It uses LangChain
`ChatBedrockConverse` to read the full PDF evidence map, the profiler output,
planned sections, widgets, text blocks, source_ids, and selected page images.

Its output is not workbook rows. It is a structured policy that explains how
this PDF represents the canonical extraction target:

```python
class QuestionTypePolicy(BaseModel):
    question_type: str
    pdf_clues: list[str]              # how this type looks in the page images/text/widgets
    instruction: str                  # how to recognize this type in this PDF
    question_text_instruction: str    # how to choose the visible prompt/label/instruction text
    answer_text_instruction: str      # printed selectable option labels only, not filled values
    branching_instruction: str        # how this type carries skip/display/applicability wording

class SectionPromptPolicy(BaseModel):
    question_types: list[str]
    excel_columns: list[str]
    global_instructions: list[str]
    ignore_patterns: list[str]
    question_type_guidance: list[QuestionTypePolicy]
    field_guidance: list[FieldExtractionPolicy]
    section_guidance: list[SectionExtractionPolicy]

class ExtractionPolicy(BaseModel):
    policy: SectionPromptPolicy
    form_summary: str
    warnings: list[str]
```

Prompt rule:

```text
You are creating an extraction policy for a healthcare PDF form.
Your job is NOT to extract final rows.
Put the allowed question types, target Excel columns, and PDF-specific extraction
rules inside policy. form_summary and warnings are only for debugging.
Do not add workbook columns. Return only structured JSON.
```

Stage 3 receives only the nested `policy` object in every section prompt. If the
policy conflicts with visible evidence, the extractor follows visible evidence
and adds a warning.

### 4.3 Stage 3 — Section Extractor

For each section (or once for whole document if `single_call`):

**Inputs assembled per call:**
- Page images for this section
- Widget list scoped to these pages (with bboxes and labels)
- Text blocks scoped to these pages (with bboxes and source_ids T###/W###)
- Textract tables/forms/layout for these pages
- Nested `policy` object from Stage 2.5, narrowed to the current section
- Document context bundle:
  - form_title, all_section_names, this_section_name, this_section_position
  - shared_legends carried from earlier sections

**Prompt structure:**
```
You are extracting canonical rows from one section of a healthcare PDF form
template. Use the extraction policy as the source of truth for allowed question
types, target Excel columns, and PDF-specific field rules.

Document context:
- Form: <form_title>
- This is section: <name> (page <range>)
- All sections in this document: <list of all section names with page ranges>
- Shared coding legends seen in earlier sections: <legends>

Section content:
- Page images: <images>
- Widget table (AcroForm fields with bboxes and labels): <table>
- Text blocks (rendered text with source_ids T### and bboxes): <table>
- Textract tables (if any): <table>

Source-of-truth precedence:
- VISIBLE RENDERED TEXT and SPATIAL LAYOUT are primary.
- AcroForm widgets are SUPPORTING evidence (input affordance, type hints,
  bbox/traceability). Widget names like 'Text2' or 'undefined_3' are NOT
  question text. Widget values are NOT answer values (the templates are blank).

Generic extraction rules:
- Do not invent rows, question types, columns, answer options, branching,
  required markers, or source ids.
- Do not use question types or target columns outside the policy.
- If the policy conflicts with visible evidence in this section, follow visible
  evidence and add a warning.

Output valid JSON matching the provided schema.
```

**Tool schema:**
```python
class ExtractedRow(BaseModel):
    section: str
    question_type: Literal[...]   # canonical 12 types
    question_text: str
    answer_text: str = ""
    answer_validation: str = ""
    branching_logic: str = ""
    branching_source: str = ""
    question_rule: str = ""
    external_id: str = ""
    source_ids: list[str] = []
    confidence_hint: float = 1.0

class SectionExtraction(BaseModel):
    section_name: str
    rows: list[ExtractedRow]
    warnings: list[str] = []
```

### 4.4 Stage 4 — Global Normalizer

**Function: `apply_validation_defaults`**
```python
DEFAULTS = {
    "text_box":   "default characters = 100",
    "text_area":  "default characters = 600",
    "date":       "Format is mm/dd/yyyy",
    "number":     "only allow numeric characters",
    "signature":  "",
}

def apply_validation_defaults(rows):
    for r in rows:
        if r.answer_validation:
            continue  # PDF-explicit wins
        default = DEFAULTS.get(r.question_type.lower())
        if default:
            r.answer_validation = default
    return rows
```

**Function: `resolve_item_code_branches`** — uses `external_id` index to convert `"If B0100 = 1 (Yes), Skip to GG0115"` into `"If Q43 = 1 (Yes), Skip to Q67"`. Note: this may produce forward references (Q67 > Q43) — that is valid and expected for legitimate skip logic.

**Function: `validate_branching_refs`** — referenced Q numbers and external IDs must exist somewhere in the document. **Forward references are allowed** (the PDF can legitimately say "skip to a later question"). Only unresolved/orphan references — those pointing to sequences or item codes that do not exist in the document — are flagged in `review_reasons` for human attention. They are NOT silently dropped.

**Function: `apply_validation_defaults`** writes to canonical JSON. Section values stay dense: every exported row keeps its section value so all workbooks are readable and sortable in the same way.

All ~9 functions follow this pattern: take rows, return rows, **no form-specific knowledge anywhere**.

### 4.5 Stage 5 — Manifest Builder

Backward-compatible with the SHOWLAY manifest. Existing fields preserved (rows, pages, summary, outputs). New fields added:

```json
{
  "version": "2.0",
  "rows": [...],            // existing shape preserved
  "pages": [...],           // existing
  "summary": {...},         // existing
  "outputs": {...},         // existing
  "canonical": {            // NEW additive
    "form_title": "...",
    "form_version": "...",
    "page_count": 34,
    "detected_sections": [...],
    "excel_schema_version": "canonical_excel_v1",
    "extraction_strategy": "section_by_section"
  },
  "warnings": [...]         // NEW additive
}
```

UI ignores `canonical` and `warnings` — no UI changes needed.

### 4.6 Stage 6 — Excel Export (fixed canonical schema)

All generated Excel files use the same headers and the same order. The PDF type can change the row content, but it cannot change the column names.

```python
CANONICAL_EXCEL_COLUMNS = [
    "Sequence",
    "Section",
    "External ID",
    "Question Type",
    "Question Text",
    "Answer Text",
    "Answer Validation",
    "Branching Logic",
    "Question Rule",
    "Required",
    "Source Page",
    "Risk Level",
    "Review Notes",
]

def export(canonical_json, out_path):
    export_rows = []

    for r in canonical_json["rows"]:
        export_rows.append({
            "Sequence": r["sequence"],
            "Section": r["section"],
            "External ID": r.get("external_id", ""),
            "Question Type": r["question_type"],
            "Question Text": r["question_text"],
            "Answer Text": r["answer_text"],
            "Answer Validation": r["answer_validation"],
            "Branching Logic": r.get("branching_logic") or r.get("branching_source", ""),
            "Question Rule": r.get("question_rule", ""),
            "Required": r.get("required", ""),
            "Source Page": r.get("page", ""),
            "Risk Level": r.get("risk_level", ""),
            "Review Notes": "; ".join(r.get("warnings", [])),
        })

    write_canonical_workbook(out_path, CANONICAL_EXCEL_COLUMNS, export_rows)
```

---

## 5. Model Selection

Model selection is **configurable** and **benchmark-driven**. This project does not assume Claude access. The initial model set is limited to Bedrock models available from the target `us-west-2` setup, with exact IDs kept in configuration and compared during Phase 4.

### 5.1 Candidate model families

| Model | Model ID | Role suited for | Why |
|---|---|---|---|
| Amazon Nova 2 Lite | `us.amazon.nova-2-lite-v1:0` | Document profiler, simple/medium section extraction | Lowest-cost default among the preferred candidates; supports image input through Converse |
| Amazon Nova Pro | `us.amazon.nova-pro-v1:0` | Complex-section extraction, branching resolution, coded scales | Stronger multimodal reasoning than Nova 2 Lite; use only when profiler tags a section `complex` |
| Qwen3-VL | `qwen.qwen3-vl-235b-a22b` | Baseline and strict-region fallback | Already integrated in SHOWLAY; supports image input; benchmark again under the new section-aware pipeline |
| Mistral Large 3 | `mistral.mistral-large-3-675b-instruct` | Complex-section fallback / benchmark contender | Supports image input in `us-west-2`; useful if Nova Pro is unavailable or underperforms |
| Kimi K2.5 | `moonshotai.kimi-k2.5` | Hard retry / benchmark contender | Supports image input in `us-west-2`; use for difficult extraction retries before human review |

Use the `us.` Nova inference-profile IDs above. Direct IDs such as `amazon.nova-pro-v1:0` are not the default config because they may fail when on-demand throughput is unsupported. Exact model IDs are configurable via `BEDROCK_PROFILER_MODEL_ID`, `BEDROCK_EXTRACTOR_DEFAULT_MODEL_ID`, `BEDROCK_EXTRACTOR_COMPLEX_MODEL_ID`, and `BEDROCK_EXTRACTOR_RETRY_MODEL_ID`.

These candidates were validated in the target setup with Bedrock model listing plus Converse smoke tests for image input and JSON output. Re-check access during deployment because IAM, SCPs, and model lifecycle state can change.

### 5.2 Selection strategy

- **Default** to Amazon Nova 2 Lite for the document profiler and for sections marked `simple` or `medium`.
- **Escalate** to Amazon Nova Pro when Stage 2 profiler flags a section as `complex` (coded scales, heavy branching, large scoring instruments).
- **Retry** difficult or low-confidence complex sections with Mistral Large 3 or Kimi K2.5 before sending them to human review.
- **Keep Qwen3-VL** as the benchmark baseline because it is the current SHOWLAY model and already works with the existing rendered-page flow.
- **Override per call** via configuration when benchmarking.

### 5.3 Phase 4 benchmark sweep

Document per-form accuracy, latency, and cost in the **target AWS account and region** for at least these strategies:

1. Nova 2 Lite throughout
2. Nova 2 Lite + Nova Pro escalation on complex sections
3. Qwen3-VL throughout under the new section-aware pipeline
4. Qwen3-VL baseline using the existing SHOWLAY page-by-page pipeline
5. Nova 2 Lite + Mistral Large 3 escalation on complex sections
6. Nova 2 Lite + Kimi K2.5 retry for low-confidence complex sections
7. After Textract access is granted: document-intelligence-augmented variants using Textract (LAYOUT + TABLES + FORMS) with each top-performing semantic model

Compare accuracy along the per-dimension evaluation criteria in §9.5. Make the production stack decision from this evidence, not from this document.

### 5.4 Cost shape (advisory, not binding)

Rough relative cost per MNLOC-scale extraction (34 pages, ~19 sections, section-aware design): Nova 2 Lite is the low-cost default, Nova Pro is the first escalation tier, and Qwen3-VL / Mistral Large 3 / Kimi K2.5 are benchmarked against that default. Exact dollar figures depend on current Bedrock pricing, inference-profile routing, and account policy, so the benchmark report tracks real measured cost per model cycle.

---

### 5.5 JSON-first product, with backend Excel for v1 compatibility

The extraction engine's **primary product is canonical JSON**. Excel is a derived export.

For v1, backend Excel generation is retained because the existing SHOWLAY UI depends on it (the workbench's "Download Excel" button calls `POST /manifest-data` which triggers backend export, and `/download` serves the resulting xlsx). This keeps the UI working without changes.

Long-term, Excel export may move to the frontend (e.g. a React/Angular UI using SheetJS) without changing the extraction engine, because the canonical JSON contract is stable. v1 leaves that door open but does not require it.

## 6. Endpoint Contracts (Backend ↔ UI)

These exist in SHOWLAY today. The new backend implements them with the same response shapes.

| Endpoint | Method | Request | Response |
|---|---|---|---|
| `/` | GET | — | Upload page HTML (served from SHOWLAY's existing `_INDEX_HTML`) |
| `/extract` | POST | multipart `file: PDF` | `{"job_id": "<hex>"}` |
| `/status/{job_id}` | GET | — | `{"status": "queued|running|done|error", "stage": "...", "page_count": N, "pages_done": N, "message": "...", ...}` |
| `/page-image/{job_id}/{page_no}` | GET | — | PNG file |
| `/manifest-data/{job_id}` | GET | — | Manifest JSON (existing shape + additive `canonical`, `warnings`) |
| `/manifest-data/{job_id}` | POST | Edited manifest | `{"status": "saved_and_outputs_updated", "manifest": ..., "outputs": ...}` — triggers Excel regeneration |
| `/workbench/{job_id}` | GET | — | Workbench HTML (served from SHOWLAY's existing `_WORKBENCH_EDITOR_HTML`) |
| `/download/{job_id}` | GET | — | Final xlsx file |
| `/review/{job_id}` | GET | — | Review sidecar xlsx |
| `/manifest/{job_id}` | GET | — | Raw manifest JSON file |

**The `POST /manifest-data` flow is the key UI integration:** user edits rows in the workbench → POST sends edited manifest → backend regenerates Excel via Stage 6 → UI downloads via `/download`.

---

## 7. Manifest JSON Contract (Frozen External Interface)

Existing fields (from SHOWLAY) — **must not be removed or renamed**:

```json
{
  "run_id": "<job_id>",
  "source_pdf": "<path>",
  "template_path": "<path>",
  "summary": {
    "rows": <N>,
    "rows_needing_review": <N>,
    "field_risk_counts": {"high": N, "medium": N, "low": N}
  },
  "pages": [
    {
      "page": 1,
      "image_path": "<path to PNG>",
      "size": [<width>, <height>]
    }
  ],
  "rows": [
    {
      "sequence": 1,
      "page": 1,
      "bbox": [x0, y0, x1, y1],
      "risk_level": "low",
      "fields": {
        "section":           {"value": "...", "risk": "low"},
        "question_type":     {"value": "...", "risk": "low"},
        "question_text":     {"value": "...", "risk": "medium"},
        "branching_logic":   {"value": "...", "risk": "low"},
        "answer_text":       {"value": "...", "risk": "low"},
        "answer_validation": {"value": "...", "risk": "low"},
        "required":          {"value": "",    "risk": "low"}
      }
    }
  ],
  "outputs": {
    "workbook_path": "<path>",
    "review_workbook_path": "<path>",
    "manifest_path": "<path>"
  }
}
```

**Additive new fields (UI ignores; not breaking):**

```json
{
  "canonical": {
    "form_title": "...",
    "form_version": "...",
    "page_count": N,
    "detected_sections": [...],
    "excel_schema_version": "canonical_excel_v1",
    "extraction_strategy": "section_by_section"
  },
  "warnings": ["Branching ref Q67 not found", ...],
  "rows": [
    {
      "...existing fields...",
      "fields": {
        "...existing...",
        "question_rule":     {"value": "...", "risk": "low"},
        "external_id":       {"value": "B0100", "risk": "low"},
        "branching_source":  {"value": "If B0100 = 1...", "risk": "low"}
      }
    }
  ]
}
```

**Contract test (Phase 1 mandatory):** Feed a manifest produced by the new backend into SHOWLAY's existing workbench HTML. Verify all 3 panes (rows / page / details) render and all edits round-trip through `POST /manifest-data`. This guards against silent UI breakage.

---

## 8. Canonical Excel Output Schema

### 8.1 Standard column set

Every generated workbook uses `canonical_excel_v1`:

| Order | Column | Purpose |
|---:|---|---|
| 1 | `Sequence` | Stable row order generated by the backend |
| 2 | `Section` | Form section name; repeated on every row for filtering/sorting |
| 3 | `External ID` | Source item code when available, e.g. `B0100` or `GG0115` |
| 4 | `Question Type` | Canonical type such as `Radio Button`, `Dropdown`, `Text Box`, `Date` |
| 5 | `Question Text` | Main prompt or display text |
| 6 | `Answer Text` | Choices/options or answer/display content |
| 7 | `Answer Validation` | HIP validation/default rule text |
| 8 | `Branching Logic` | Human-readable, source-preserving skip/display condition |
| 9 | `Question Rule` | Machine/actionable visibility condition when confidently derived |
| 10 | `Required` | Required/optional indicator when known |
| 11 | `Source Page` | PDF page number used for traceability |
| 12 | `Risk Level` | `low`, `medium`, or `high` review risk |
| 13 | `Review Notes` | Warnings or reasons the row needs human review |

### 8.2 Rules

- Column names do not vary by PDF, template, customer, or workbook.
- New PDFs write into the same columns. If a field is not present in that PDF, the cell is blank.
- `Branching Logic` preserves the PDF's wording and item codes as much as possible.
- `Question Rule` is only for a cleaner machine/actionable rule; it is not a duplicate parking lot for branching text.
- If a future client requires a different workbook layout, that is a separate export adapter or a `canonical_excel_v2` decision, not a hidden per-PDF profile.

This is what "open-ended scope" means in practice: new PDFs work without code because extraction targets the canonical JSON and the backend always writes the same Excel schema.

---

## 9. Phased Delivery Plan

### Phase 1 — Foundation (2-3 weeks)

**Goals:** Plumbing in place; UI still works; no regression.

**Deliverables:**
- New backend module structure under `backend/`
- FastAPI app exposing the same endpoints as SHOWLAY's webapp.py
- Stage 1 (PyMuPDF probe + rasterize) ported from SHOWLAY
- Stage 1 Textract interface/feature flag documented, but implementation deferred until access is granted
- Pydantic schemas for canonical JSON and manifest
- Canonical Excel writer with `canonical_excel_v1` header test
- Manifest contract test that drives SHOWLAY's existing JS workbench against a fixture manifest

**Acceptance:**
- Existing SHOWLAY UI loads and shows the upload page when pointed at the new backend
- `POST /extract` accepts a PDF and returns a job_id
- `GET /page-image/{job_id}/{n}` serves rasterized PNGs from new backend
- Manifest contract test passes — workbench renders correctly with a fixed manifest

### Phase 2 — Simple-form path (3-4 weeks)

**Goals:** End-to-end extraction for 1-page and small multi-page forms.

**Deliverables:**
- Stage 2 document profiler agent + prompt + Pydantic-validated JSON schema
- Stage 2.5 extraction policy agent + prompt + Pydantic-validated JSON schema
- Stage 3 section extractor (single_call mode only) + prompt + schema
- Stage 4 generic normalizer (~9 functions)
- Stage 5 manifest builder
- Stage 6 Excel export using the fixed `canonical_excel_v1` schema
- Confidence scoring (reused from SHOWLAY's confidence.py)

**Acceptance (per-dimension, not single score — see §9.5):**
- H1700-3 and CHOICES meet the per-dimension Phase 2 targets in §9.5
- User can upload PDF → see rows in workbench → edit rows → download Excel — entire flow works end-to-end
- New backend can replace SHOWLAY's old pipeline for these 2 forms with no UI changes

### Phase 3 — Section-aware path (3-4 weeks)

**Goals:** MNLOC works.

**Deliverables:**
- Stage 2 profiler returns `section_by_section` strategy for complex docs
- Stage 3 extractor section_by_section mode with shared-legend carry-forward
- Stage 4 item-code branching resolver using external_id index
- MNLOC export assertions for `External ID`, source-preserving `Branching Logic`, and standard `Question Rule`
- Nova Pro escalation logic when profiler tags complex sections

**Acceptance (per-dimension, not single score — see §9.5):**
- MNLOC processes without crash, all 34 pages
- MNLOC meets the per-dimension Phase 3 targets in §9.5
- Branching uses item codes verbatim in the standard `Branching Logic` column
- Workbench can navigate all 19 sections

### Phase 4 — Evaluation + benchmarking (2 weeks)

**Goals:** Empirical model selection; production readiness signal.

**Deliverables:**
- Offline eval harness (`backend/eval/`) — strictly separated from extraction
- Benchmark sweep per §5.3 strategies
- Per-form, per-dimension accuracy + cost dashboards (see §9.5)
- Production model recommendation document
- At least one PDF outside the 5-sample set to validate open-ended scope

**Acceptance:**
- Documented per-dimension accuracy + cost across all 4 sample PDFs and ≥1 novel PDF
- Recommended production stack chosen on evidence, not assumption

### 9.5 Per-dimension evaluation criteria (replaces single overall score)

A single overall percentage hides weaknesses — for example, getting question text right while failing branching logic. Evaluate each of these dimensions independently and report them separately:

| # | Metric | What it measures |
|---|---|---|
| 1 | Row recall | Did we identify the expected questions, display rows, tables? (matched rows / golden rows) |
| 2 | Row precision | Are extracted rows real? (matched rows / candidate rows) |
| 3 | Question Text accuracy | Per-row fuzzy match of question_text |
| 4 | Question Type accuracy | Per-row exact match of question_type (after canonical normalization) |
| 5 | Answer Text completeness | For choice rows, fraction of expected options recovered |
| 6 | Branching Logic correctness | Per-row exact + fuzzy match of branching_logic |
| 7 | Answer Validation correctness | Per-row exact match (HIP defaults applied where expected) |
| 8 | Section assignment accuracy | Per-row correct section name (against dense golden) |
| 9 | Sequence/order correctness | Are matched rows in the same relative order? |
| 10 | Question Rule appropriateness | Of rows where the PDF clearly implies a visibility condition, fraction correctly emitting the standard `Question Rule` value |

**Targets** (per form, per dimension — calibrated by phase):

| Dimension | H1700-3 P2 | CHOICES P2 | MNLOC P3 |
|---|---:|---:|---:|
| Row recall | ≥95% | ≥90% | ≥80% |
| Question Text | ≥90% fuzzy | ≥90% fuzzy | ≥80% fuzzy |
| Question Type | ≥95% | ≥85% | ≥75% |
| Answer Text completeness | ≥90% | ≥90% | ≥75% |
| Branching Logic | ≥90% | ≥80% | ≥70% |
| Answer Validation | ≥95% | ≥90% | ≥75% |
| Section assignment | ≥95% | ≥95% | ≥85% |
| Sequence order | ≥95% | ≥90% | ≥85% |
| Question Rule appropriateness | n/a (no rule rows) | n/a (no rule rows) | ≥60% |

These targets are calibration points, not contract gates — Phase 4 may adjust them based on what the benchmark reveals about achievable quality vs cost.

---

## 10. Gap Closure Matrix

Cross-referencing every gap identified across `suggestions.md`, the requirement analysis, and our discussion:

| Gap | Closure |
|---|---|
| Page-isolated VLM extraction (suggestions §4.1) | Stage 3 section-aware with cross-section context |
| Question Rule not in prompt / extract.py / writer (suggestions §4.2, code review) | Canonical schema includes question_rule; default blank; agent emits only when PDF implies; fixed Excel schema writes it to `Question Rule` |
| truth_path leakage in postprocess (suggestions §4.3, code review) | Removed; eval is offline-only |
| 38-stage form-specific cleanup doesn't generalize (suggestions §4.4) | ~9 generic transforms in Stage 4; no form-specific code |
| Forward branching refs incorrectly flagged as errors (SHOWLAY confidence.py) | Stage 4 validate_branching_refs explicitly allows forward refs; only orphans flagged |
| Section sparsification baked into extraction | Moved to Stage 6 export only; canonical JSON is dense |
| Widgets treated as primary signal | Reframed as supporting evidence; visible rendered text is primary |
| "Every visible artifact" extracts chrome (page numbers, footers) | Stage 3 prompt narrows to "meaningful form artifacts" + explicit chrome exclusion list |
| Excel writing too central (suggestions §4.5) | Excel decoupled into Stage 6; canonical JSON is the agent output |
| Question Rule vs Branching Logic ambiguity (suggestions §2.4) | Both have fixed meanings: `Branching Logic` is source-preserving; `Question Rule` is machine/actionable |
| Form-specific 38-stage post-processor | Replaced — see above |
| MNLOC 34 pages + coded items + XFA | Stage 2 detects → Stage 3 section_by_section + item code preservation |
| Filled PDF values mistaken for data | Confirmed all blank; prompt explicitly downgrades field names to weak hint |
| AcroForm field names non-semantic | Explicit prompt instruction: rely on visible rendered text |
| HIP validation defaults missing/optional | Stage 4 applies standard validation wording unconditionally |
| Sequence column convention varies | Always generated and always written to `Sequence` |
| Question type vocabulary varies | Canonical output vocabulary is fixed |
| Bilingual templates (H1700-3) | English canonical headers stay fixed; bilingual source text remains in `Question Text` / `Answer Text` when present |
| Branching format varies (Q-ref vs item code) | `Branching Logic` keeps source wording/item codes; `Question Rule` carries the cleaner actionable rule when available |
| Item codes (B0100, GG0115) as field identity | external_id is a first-class canonical field |
| Open-ended scope | Zero form-specific code in pipeline; all PDFs export to the same canonical Excel schema |
| UI rebuild | Not needed; manifest contract preserved |
| Human-in-loop NL correction | Deferred — UI's inline cell edit + save flow works today |
| Cost concern for POC | Nova 2 Lite default, Nova Pro escalation only when profiler tags `complex`; Qwen3-VL, Mistral Large 3, and Kimi K2.5 benchmarked for performance comparison |
| AWS-locked platform | Bedrock + Textract; no cross-cloud dependencies |
| Brittle JSON parsing (Qwen3-VL string JSON) | `ChatBedrockConverse` with Pydantic-validated JSON |
| Golden workbook used during extraction | Removed completely; comparator (eval) uses it offline |
| Section detection across pages | Stage 2 profiler outputs sections with page ranges |
| Shared coding legends invisible to later pages | Stage 2 captures; Stage 3 carries forward per section |
| Skip-target items defined later in document | Stage 3 has all_section_names in context; Stage 4 resolves with external_id index |
| Confidence + review manifest preserved | Reused from SHOWLAY confidence.py + field_review.py |

---

## 11. Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Manifest contract drift breaks UI silently | High | Phase 1 mandatory contract test; freeze manifest schema |
| Section detection unreliable on novel forms | Medium | Stage 2 returns confidence; fall back to single_call on low confidence |
| New PDFs introduce fields outside the canonical schema | Medium | Keep unknown details in `Review Notes` for v1; promote only repeated, useful fields through a versioned `canonical_excel_v2` change |
| Textract cost on large docs | Low | Routing matrix — only invoke when needed |
| MNLOC XFA + corrupted xref crashes PyMuPDF | Medium | Tolerate warnings (already do); fall back to Bedrock vision extraction and human review for v1; add Textract fallback later when access is granted |
| Hallucinated branching refs in output | Medium | Stage 4 validator: forward refs are allowed; orphan refs (pointing to non-existent sequences or item codes) are flagged for human review, not silently dropped |
| Cost overrun if Nova Pro / retry escalation triggers too often | Low | Profiler returns single recommendation; can override per-call; Phase 4 benchmark calibrates escalation and retry thresholds |
| Extraction of page chrome (headers/footers/page numbers) inflates row count and confuses UI | Medium | Stage 3 prompt's explicit chrome exclusion list; reviewers expected to validate during P2/P3 acceptance |
| Specific model versions/prices in docs go stale | Low | Model selection is configurable via environment variables; only model families named in design doc; exact versions/prices in Phase 4 benchmark report |
| Bedrock latency on 19-section MNLOC | Low | Sections run in parallel (asyncio); low-cost-tier latency is acceptable for per-section calls; benchmark validates in target region |
| Tool-use schema rejection on edge cases | Low | Use Bedrock retries; validate Pydantic with `extra='allow'` for forward compatibility |

---

## 12. Decisions to Confirm with the Team

These are now recommended defaults. The team can override them, but v1 should not create PDF-specific Excel column names.

1. **MNLOC Question Rule policy**: Do not duplicate branching text just for MNLOC. `Branching Logic` keeps the source condition; `Question Rule` contains the cleaner actionable rule only when confidently derived.
2. **Sequence in MNLOC export**: Always write `Sequence`; it is part of the standard Excel schema.
3. **External ID for MNLOC**: Always write `External ID` when available; it is part of the standard Excel schema.
4. **Bilingual H1700-3**: Spanish columns left blank in v1 — confirm or schedule for v2.
5. **Production model after benchmark**: Phase 4 will recommend; team to lock.

---

## 13. Summary

This design replaces SHOWLAY's brittle 38-stage form-specific post-processor with a clean separation:

- **Universal extraction pipeline** (Stages 1–5): PDF ingestion → document profiling → section-aware extraction → canonical JSON → deterministic normalization → confidence/review metadata. Zero form-specific code.
- **Canonical Excel export** (Stage 6): one fixed column set for every output workbook, regardless of PDF type.
- **Offline evaluation**: golden workbook comparison with strict no-leakage rule; per-dimension metrics, not single scores.
- **Existing UI preserved** — the manifest JSON contract is the integration point.
- **Configurable model selection** — Nova 2 Lite default, Nova Pro complex-section escalation, Qwen3-VL/Mistral/Kimi benchmark cycles, Textract for document intelligence — final stack chosen via Phase 4 benchmark in the target environment.
- **MNLOC solved structurally**, not patched — section-by-section with carry-forward context, item-code preservation, item-code branching resolution, forward references allowed.

Adding a new healthcare assessment/configuration PDF: works out of the box if it fits the canonical JSON model, and it exports to the same Excel columns.
Adding a different customer-specific workbook layout: do not hide it behind per-PDF profiles; treat it as a separate adapter or a versioned schema change.
Switching production model: change a config, re-run eval.

The architecture eliminates the failure modes that make the current code "fail to generalize" without throwing away the parts that work.
