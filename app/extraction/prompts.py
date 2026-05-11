from __future__ import annotations

import re

from app.chunking.markdown_chunker import MarkdownChunk
from app.schemas.form_schema import DocumentExtraction


ESCAPED_BLANK_RE = re.compile(r"(?:\\_){8,}")
BLANK_RE = re.compile(r"_{8,}")
MANY_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _compact_markdown_for_llm(text: str) -> str:
    text = ESCAPED_BLANK_RE.sub("____", text or "")
    text = BLANK_RE.sub("____", text)
    text = MANY_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def extraction_prompt(chunk: MarkdownChunk) -> str:
    chunk_text = _compact_markdown_for_llm(chunk.chunk_text)
    return f"""
You are converting a Markdown representation of a healthcare PDF form into structured hierarchical JSON.

You must preserve form hierarchy, classify question types, preserve sections, group options under parent questions,
detect radio groups vs checkbox items, detect inline inputs like "Other - specify ____", detect follow-up questions
under checkbox items, detect tables and columns, merge paragraph continuation lines, split multi-field rows, and output
valid JSON only.

Never invent fields. Every item must be supported by source_text from the Markdown. Include source_pages.
If uncertain, set needs_review=true and explain review_reason. Do not create business/system fields.
Keep source_text concise: max 250 characters per item. Do not copy long underline/blank-fill runs; represent blanks as "____".
Keep display_text concise unless it is the actual display paragraph needed in the workbook.
Prefer fewer, cleaner items over huge verbose JSON.

Important CHOICES examples:
- "Applicant Name: ____ SSN: ____ DOB: ____" splits into Applicant Name text_box, SSN text_box, DOB date with same row_group_id.
- Current Living Arrangements / Applicant residence is usually a radio_group; options with specify blanks get inline_input.
- Safety justification criteria are checkbox_item. Detail/explanation prompts are followup Text Area. "Description of documentation attached" is followup Text Box.
- Additional Required Documentation is mostly display.
- Submitting Entity Attestation includes checkbox statements, Printed Name text_box, Signature signature, Credentials text_box, Date date.
- Fall Form repeating rows become table with typed columns.
- Display labels: if text is "Freedom of Choice: long paragraph", set question_text="Freedom of Choice" and display_text/source_text to the full paragraph.
- Signature blocks: do not emit generic repeated "Printed Name", "Signature", "Date" rows without context. Use the nearest signer label/context, for example "Applicant/Member or Authorized Representative Printed Name:", "Witness Signature:", "Service Coordinator Date Signed:".
- Date fields may be classified as date; downstream workbook uses "Date" for date/calendar fields.

Allowed item_type values: display, text_box, text_area, date, number, signature, radio_group, checkbox_item,
dropdown, table, section_marker, review_required.

Chunk metadata:
chunk_id={chunk.chunk_id}
source_pages={chunk.source_pages}
heading_path={chunk.heading_path}
previous_context_summary={chunk.previous_context_summary}

Markdown chunk:
{chunk_text}
""".strip()


def refinement_prompt(document: DocumentExtraction) -> str:
    return f"""
Review this existing hierarchical JSON and repair only supported issues. Do not re-extract from the PDF.
Do not invent fields not supported by source_text. Preserve IDs where possible.

Check:
- option groups are properly grouped
- radio_group vs checkbox_item
- inline specify inputs belong to exact options
- followups belong to correct checkbox/radio parent
- table columns are preserved and typed
- long instructions are display
- multi-field rows are split
- repeated page headers/footers are removed
- branching triggers are specific

Mark uncertain cases with needs_review=true.
Return valid JSON only matching the same DocumentExtraction schema.

Document JSON:
{document.model_dump_json(indent=2)}
""".strip()
