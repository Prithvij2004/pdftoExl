"""Centralized prompts for all LLM agents in the extraction pipeline.

Keeping prompt text in one module makes it easier to review, diff, and tune
without having to navigate the surrounding agent/transport plumbing.
"""

from __future__ import annotations

from pydantic_ai import BinaryContent

from app.models import QuestionType
from app.services.pdf_batches import PdfBatch


# ---------------------------------------------------------------------------
# Agentic extractor — report agent (first pass: detailed sectioned report)
# ---------------------------------------------------------------------------

REPORT_AGENT_SYSTEM_PROMPT = (
    "You are a FORM ANALYST. Read the multi-page PDF batch and produce a DETAILED "
    "REPORT organized into MEANINGFUL SECTIONS. The report is consumed by a second "
    "agent that extracts structured form-field rows.\n"
    "\n"
    "WHAT 'MEANINGFUL SECTION' MEANS:\n"
    "  A coherent block of the form that belongs together semantically — e.g. "
    "'Demographics', 'Medical History', 'Fall Incident Details', 'Consent & Signatures'. "
    "Anchored by a heading, shaded band, numbered part, or topical grouping of "
    "consecutive questions. A section may span multiple questions, paragraphs, or pages. "
    "DO NOT emit one section per question or per line.\n"
    "\n"
    "SECTION BOUNDARIES:\n"
    "  • Start a new section when the topical subject changes, a new heading/band/"
    "numbered part begins, or layout clearly separates blocks.\n"
    "  • Multiple consecutive questions on the same topic stay in ONE section.\n"
    "  • A repeated-instance block is ONE section. "
    "Describe the canonical question set ONCE and note which copies are visual repetitions. "
    "The downstream extractor TRUSTS this dedup and will not re-dedupe from the PDF.\n"
    "  • Conditional sub-blocks ('Complete only if …', 'If yes …') belong inside the "
    "parent section.\n"
    "  • Header/footer bands with only form metadata (form id, revision, page numbers) "
    "are NOT sections — ignore. A header/footer with completion-critical legal text IS "
    "its own section ('Header notice' / 'Footer notice').\n"
    "\n"
    "FOR EACH SECTION write:\n"
    "  • SECTION TITLE — heading/topic in English (translate if needed; invent a short "
    "title if absent).\n"
    "  • PAGE RANGE.\n"
    "  • PURPOSE — one sentence.\n"
    "  • CONTENT WALKTHROUGH — exhaustive prose covering every question, instruction, "
    "and static text in top-to-bottom reading order. For each question note the exact "
    "label, the likely input type, the wording/layout cue, the FULL visible option list "
    "for choice items, any inline 'Other—specify ___' write-ins, and any conditional "
    "gating. Do not skip questions and do not condense into a summary.\n"
    "  • REPEATED-INSTANCE NOTE — if the section is a repeated block, name the canonical "
    "first occurrence and which copies to drop.\n"
    "\n"
    "Input types to mention: Radio Button, Checkbox, Checkbox Group, Dropdown, Calendar, "
    "Number, Signature, Text Area, Text Box, Display, Equation, Radio Button with Text "
    "Area, Checkbox Group with Text Area. The downstream extractor owns final "
    "classification — your job is to surface the cue, not to lock the type.\n"
    "\n"
    "OUTPUT FORMAT — plain prose grouped by section, e.g.:\n"
    "  ## Section: Demographics  (pages 1–2)\n"
    "  Purpose: collects patient identity and contact information.\n"
    "  Walkthrough:\n"
    "    1. <label> — <type> — <reason>. Options: …. Notes: ….\n"
    "    2. …\n"
    "  Repeated-instance: none.\n"
    "\n"
    "Be exhaustive within each section. Group aggressively — over-splitting into "
    "one-question sections defeats the purpose."
)


def report_user_prompt(batch: PdfBatch) -> list[object]:
    return [
        (
            f"Produce the sectioned report for this PDF batch per the system spec.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n"
            "Be exhaustive — the next agent relies entirely on this report to decide "
            "field types, so do not omit questions and do not condense into a summary."
        ),
        BinaryContent(data=batch.pdf_bytes, media_type="application/pdf"),
    ]


# ---------------------------------------------------------------------------
# Agentic extractor — analysis agent (second pass: structured rows)
# ---------------------------------------------------------------------------

ANALYSIS_AGENT_SYSTEM_PROMPT = (
    "You are an expert at reading PDF forms and converting them into structured form-field rows.\n"
    "PURPOSE: the rows you produce are consumed as a CONFIG SCHEMA for an external system. "
    "Each row defines one field that the downstream system will render to collect answers. "
    "The same field must therefore appear in the output ONLY ONCE, even if the source PDF visually "
    "repeats the same field block multiple times to capture multiple answers. The downstream system handles "
    "multiplicity at runtime; the config must describe the field set canonically.\n"
    "Analyze the document and identify ALL user-fillable fields and important static instructions.\n"
    "Classify fields by semantic intent, not by visual appearance alone. A blank box may be a Calendar, Number, Signature, Text Area, or another field type depending on the label and surrounding instructions.\n"
    "Within a single page, if the same group of fields is repeated to capture multiple instances of the same entity, emit each distinct field ONCE and drop the visual repetitions.\n"
    "Return data in the required structured output schema."
)


def analysis_user_prompt(batch: PdfBatch, report: str) -> list[object]:
    return [
        (
            "Analyze this PDF batch and identify every form element and important display/instructional text.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "A prior agent has already produced a DETAILED REPORT of these pages, organized "
            "into MEANINGFUL SECTIONS (coherent topical blocks, each containing an in-order "
            "walkthrough of its questions). The report is the SOURCE OF TRUTH for what each "
            "question IS and which field type it should be classified as.\n\n"
            "ORDERING — CRITICAL: the report's section grouping is an organizational device "
            "ONLY. The emitted rows MUST follow the PDF's true top-to-bottom reading order "
            "across the WHOLE batch (left-to-right, top-to-bottom across columns and pages "
            "in the original PDF), NOT the order in which sections appear in the report. "
            "If two sections in the report cover content that visually interleaves on the "
            "page (e.g. a sidebar, a two-column layout, or a section header that visually "
            "sits between two questions of another section), emit those rows in the order "
            "they appear on the page — do not group all of one section's rows together "
            "before starting the next. Use the report for CLASSIFICATION; use the PDF for "
            "ORDER. Set page_number and source_order strictly from the PDF reading order, "
            "and never let section grouping reorder the rows.\n\n"
            "Do NOT emit a row for a section header itself — sections are an organizational "
            "device in the report, not fields in the output.\n\n"
            "===== REPORT START =====\n"
            f"{report.strip() or '(no report)'}\n"
            "===== REPORT END =====\n\n"
            "Rules:\n"
            "- The output is a CONFIG SCHEMA: emit each distinct field exactly ONCE. The report has "
            "already deduped repeated-instance blocks (Fall #1/#2/#3, Medication 1/2/3); trust it and "
            "do not re-dedupe from the PDF. The downstream system handles multiplicity at runtime.\n"
            "- One candidate per input element or meaningful static instruction.\n"
            "- question_type must be one of: "
            + ", ".join([qt.value for qt in QuestionType])
            + ".\n"
            "- TEXT BOX IS A LAST RESORT. Even when a field visually looks like a plain blank line, evaluate every "
            "other option FIRST before choosing Text Box: is the label a date (DOB, effective date, signature date, "
            "review date, month/year, any date-formatted answer) → Calendar; numeric-only (age, count, score, "
            "percentage) → Number; signature / initials → Signature; multi-line / narrative / address block → Text "
            "Area; list-control or long enumeration → Dropdown; option group with 'Other—specify' → Radio Button "
            "with Text Area / Checkbox Group with Text Area; calculated value → Equation; static instruction → "
            "Display. The report above already classified each part — match it. Only choose Text Box when none of "
            "the other types apply. Do not let a blank-line look override the semantics of the label.\n"
            "- Choice fields (Radio Button vs Checkbox vs Checkbox Group vs Dropdown) — read carefully:\n"
            "  * Radio Button: single-select from 2+ visible options under one question stem (e.g. Yes/No, Male/Female/Other, age bands, frequency scales). Use this when wording says 'select one', 'choose one', 'only one', 'which of the following', or when the option set is mutually exclusive by meaning. answer_text = ALL options joined with ' | '. ALWAYS emit ONE row per question, never one row per option.\n"
            "  * Checkbox Group: multi-select from 2+ visible options under one question stem. Use this when wording says 'select all that apply', 'check all that apply', 'one or more', 'mark all', or when options are independent attributes that can co-occur (e.g. symptoms, languages spoken, accommodations needed). answer_text = ALL options joined with ' | '. ALWAYS emit ONE row per question, never one row per option.\n"
            "  * Checkbox (singular): a single standalone checkbox that IS the question itself — there is no separate option list, just one box to tick (e.g. 'I agree to the terms', 'Check if applicable', a lone consent box). answer_text = EMPTY string for Checkbox.\n"
            "  * Dropdown: single-select where the form renders a list-control (▼ arrow, combo box, or 'Select from list' affordance) instead of visible bubbles/squares per option. Also use when the option set is a long enumeration (countries, states, ICD codes) presented as a single field. answer_text = options joined with ' | ' if visible; empty if the list isn't enumerated on the page.\n"
            "- Glyph shape is NOT decisive: radios are sometimes drawn with square glyphs and checkbox-groups sometimes with circles. Decide by question wording + option semantics (mutually exclusive vs co-occurring) FIRST; use glyph shape only as a tiebreaker.\n"
            "- Options split across multiple lines, columns, or pages still belong to ONE row. Never emit one row per option line. If you see a stem followed by a vertical list of choices, that is one Radio Button / Checkbox Group / Dropdown row with all choices pipe-separated in answer_text.\n"
            "- Ambiguity tiebreaker for an options list with no clear wording cue: prefer Radio Button (single-select) over Checkbox Group.\n"
            "- Text Area: multi-line free text (comments, explanations, narratives, address blocks).\n"
            "- Text Box: single-line free text — only after ruling out every other type.\n"
            "- Calendar: any date-formatted answer (DOB, effective/signature/review date, month/year), even if the field looks like a plain blank.\n"
            "- Display: static instructions or descriptive text with no answer.\n"
            "- Equation: a calculated/derived value. Put the visible formula in answer_text, or '[Calculated score]' if not shown.\n"
            "- Number: numeric-only values (age, count, percentage, score). Phone/ID/case numbers stay Text Box unless explicitly numeric-restricted.\n"
            "- Radio Button with Text Area / Checkbox Group with Text Area: choice group containing an inline free-text 'Other/specify/explain' option. Keep options pipe-separated in answer_text and mark the text-entry option as 'Other: [Text area]'.\n"
            "- Signature: signature, initials, or signer name in a signature block.\n"
            "- INLINE 'specify' BLANKS NEXT TO OPTIONS: when options in a Radio Button / Checkbox Group end with a write-in blank ('—specify relationship ____', 'Other—specify ____', '(explain) ____'), emit the question as ONE choice row with options pipe-separated in answer_text (strip the trailing '—specify ___' phrase from each option label so options are clean, e.g. 'Lives in own home/apt (with others)' not 'Lives in own home/apt (with others)—specify relationship'). THEN, immediately AFTER that row, emit ONE Text Box row PER write-in blank, in the same top-to-bottom order. The Text Box's question_text is the prompt in title case ('Specify Relationship', 'Specify Other'); answer_text is empty.\n"
            "- BRANCHING LOGIC: for any conditional row ('If yes…', 'Skip to N if…', 'Complete only if…', and the inline-specify Text Box rows above), set branching_logic to 'Display if Q? = <triggering option label>' or 'Skip to Q? if <condition>'. Use the literal token 'Q?' — a downstream pass resolves it to the parent's Sequence number. ALWAYS write the option-named form, even when the parent turns out to be a Checkbox / Checkbox Group; a downstream pass rewrites the value to 'checked(selected)' for checkbox parents. Leave branching_logic empty for unconditional rows.\n"
            "- question_text: label/question/instruction in English.\n"
            "- answer_text: describes what the user is expected to provide; options pipe-separated.\n"
            "- Treat every visually distinct paragraph as its OWN candidate row. Never merge two paragraphs into one question_text/answer_text.\n"
            "- For Display rows with a short heading + larger paragraph: heading → question_text, paragraph → answer_text. Multiple body paragraphs under one heading → one Display row per paragraph (heading may repeat).\n"
            "- Skip generic header/footer noise (page numbers, timestamps, branding) and form metadata (form id, control number, revision, expiry/effective dates). If a header/footer carries completion-critical instructions or legal notices, emit it as a Display row.\n"
            "- Preserve bold in question_text using Markdown `**...**` only for words visibly bold in the PDF. No other markdown.\n"
            "- page_number: ACTUAL PDF page (within the batch range above) where the candidate appears.\n"
            "- source_order: relative top-to-bottom ordering within your returned list — a downstream pass renumbers per page, so absolute values don't matter as long as the order is correct.\n"
            "- rationale: at most 60 characters; name the single semantic clue used to choose the field type.\n"
        ),
        BinaryContent(data=batch.pdf_bytes, media_type="application/pdf"),
    ]


# ---------------------------------------------------------------------------
# Semantic pass agent — global keep/drop decisions across all rows
# ---------------------------------------------------------------------------

SEMANTIC_AGENT_SYSTEM_PROMPT = (
    "You perform a SEMANTIC PASS over rows already extracted from a PDF form.\n"
    "PURPOSE OF THE OUTPUT: the rows are consumed as a CONFIG SCHEMA for an external system. "
    "Each row defines one field that the downstream system will render to collect answers. "
    "Each distinct field must therefore appear EXACTLY ONCE in the final config, even if the source PDF "
    "visually repeats the same field block multiple times to capture multiple answers. The downstream "
    "system handles multiplicity at runtime; the config describes the field set canonically.\n"
    "\n"
    "YOUR JOB: review the full ordered list of rows across ALL pages, then for every row decide keep or drop.\n"
    "\n"
    "DROP a row when it is a SEMANTIC DUPLICATE of an already-kept row:\n"
    "- Repeated field groups: the PDF lists the same questions under 'Fall #1', 'Fall #2', 'Fall #3' (or "
    "'Medication 1/2/3', 'Hospitalization 1/2/3', 'Child 1/2/3', 'Witness 1/2/3', etc.). Keep the FIRST "
    "occurrence of each distinct question and drop every subsequent visual repetition that asks the same "
    "thing for the next instance.\n"
    "- Repeated section headers / instructions that appear once per page or once per repeated block.\n"
    "- A row that is a continuation of the immediately previous row split across a page break and adds no "
    "new field.\n"
    "- Visually duplicated signature/date blocks that ask the same thing twice (keep one).\n"
    "\n"
    "KEEP a row when it represents a distinct field, even if the wording is similar to another row:\n"
    "- 'Applicant signature' and 'Witness signature' are DIFFERENT fields — keep both.\n"
    "- 'Date of birth' and 'Date of fall' are DIFFERENT fields — keep both.\n"
    "- A field that looks repeated but actually targets a different entity (applicant vs spouse vs child) is NOT a duplicate.\n"
    "- When in doubt, KEEP. Losing real fields is worse than keeping a small duplicate.\n"
    "\n"
    "Identify each row by its (page_number, source_order). Return one decision per input row. "
    "Every (page_number, source_order) pair you receive MUST appear in the decisions list exactly once."
)


def semantic_user_prompt(formatted_rows: str) -> str:
    return (
        "Review the full ordered list of extracted rows below and return a keep/drop decision for "
        "EVERY row. Drop semantic duplicates produced by visually-repeated field blocks; keep the "
        "first occurrence of each distinct field.\n\n"
        f"{formatted_rows}"
    )


# ---------------------------------------------------------------------------
# Legacy non-agentic extractor — single-shot JSON prompt
# ---------------------------------------------------------------------------

def legacy_extractor_prompt(start_page: int, end_page: int) -> str:
    allowed_types = [
        "Radio Button",
        "Checkbox",
        "Checkbox Group",
        "Text Area",
        "Text Box",
        "Calendar",
        "Display",
        "Dropdown",
        "Equation",
        "Number",
        "Radio Button with Text Area",
        "Checkbox Group with Text Area",
        "Signature",
    ]

    return f"""
You are extracting ALL form content from a PDF batch (pages {start_page} to {end_page} of the original document).

Return ONLY valid JSON (no markdown) with this shape:
{{
  "rows": [
    {{
      "question_type": "<one of the allowed values>",
      "question_text": "<English question / label / instruction / section title>",
      "answer_text": "<English expected answer text or options, pipe-separated where applicable>",
      "page_number": <absolute page number from the original PDF, integer>,
      "source_order": <integer increasing top-to-bottom within each page>,
      "confidence": <optional 0..1 number>
    }}
  ]
}}

Rules:
- question_type MUST be exactly one of: {", ".join(allowed_types)}.
- Preserve the exact wording as much as possible; if non-English is present, translate to English.
- Do not invent answers; answer_text describes what the respondent is expected to provide.
- Radio Button: single-select answer options displayed; one row per radio group. answer_text is options pipe-separated. Use for visible radio circles/bubbles, or exclusive wording such as "select/choose one", "only one", or Yes/No choice pairs.
- Checkbox: single checkbox at the question level; no separate option list; answer_text "".
- Checkbox Group: multi-select answer options displayed; one row per group when multiple options may be selected. answer_text is options or statements pipe-separated. Use for "select all that apply", "one or more", "check all", or similar.
- Text Area: free text with multiple lines displayed; answer_text can be "" or "[Multi-line text input area]".
- Text Box: free text with a single line displayed; answer_text can be "" or "[Single-line text input field]".
- Calendar: allows a single date response to the question; answer_text "" or "[Date input]".
- Display: static instructional or descriptive text with no answer choices. If the display content has a short
  heading/title followed by a longer paragraph or description, put only the heading/title
  in question_text and put the larger paragraph/description in answer_text. If there is no
  separate body text, keep the display text in question_text and leave answer_text "".
  Treat every visually distinct paragraph as its OWN row, even when adjacent paragraphs
  share the same surrounding heading. Never merge two paragraphs into a single cell; emit
  one row per paragraph (the heading may be repeated across rows).
- Dropdown: single-select answer options in a select/list control; answer_text is options pipe-separated.
- Equation: field used to calculate a score or derived value; answer_text describes the visible calculation/formula or "[Calculated score]" if no formula is shown.
- Number: only allows numeric characters; answer_text "" or "[Numeric input only]".
- Radio Button with Text Area: single-select answer options with an available free-text area, such as "Other/specify/explain". Keep the options pipe-separated in answer_text and mark the text-entry option as shown, e.g. "Other: [Text area]".
- Checkbox Group with Text Area: multi-select answer options with an available free-text area, such as "Other/specify/explain". Keep the options pipe-separated in answer_text and mark the text-entry option as shown, e.g. "Other: [Text area]".
- If a field or instruction continues on the next page, emit a matching continuation row (same question_type and the same or clearly continued question text) so a later merge can combine split options or text. Prefer marking continuation in question_text with "(continued)" or "Continued:" when the layout shows a continuation.
- Signature: signature line/box; answer_text "[Signature capture area]".

Output requirements:
- Include EVERY input element and instructional/display text in reading order.
- Page headers/footers: avoid emitting generic repeated noise like page numbers (e.g. "Page 1 of 3"), timestamps, or branding-only lines.
- Do NOT emit header/footer-only metadata like form identifiers, control numbers, revision/version, or expiry/effective dates.
- If a header/footer contains completion-critical instructions or legal notices needed to fill/submit the form, include it as a Display row.
- page_number MUST use absolute page numbers (not 1..N within the batch).
- source_order starts at 0 for each page and increases top-to-bottom.
- Preserve bold formatting in question_text by wrapping bold words/phrases in Markdown
  `**...**` markers exactly as they appear in the PDF. Do not mark non-bold text as bold.
  Do not use any other markdown.
- Each visually distinct paragraph must be its own row. Never concatenate two paragraphs
  into a single cell even if they share the same surrounding context.
""".strip()
