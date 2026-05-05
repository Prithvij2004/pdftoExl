"""
PDF -> Excel form extractor (single-file CLI).

Usage:
    export LLAMA_CLOUD_API_KEY=llx-...
    python extract_form.py path/to/form.pdf -o out.xlsx

Pipeline: LlamaExtract (per_doc, agentic) with a Pydantic schema modeled on
docs/requirements.md -> openpyxl writer producing the 7-column workbook.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def _load_env_file(path: Path) -> None:
        if not path.exists():
            return
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    _load_env_file(Path(__file__).parent / ".env")

# ----------------------- Schema (drives LlamaExtract) -----------------------

QuestionType = Literal[
    "Display",
    "Text Box",
    "Text Area",
    "Date",
    "Number",
    "Signature",
    "Radio Button",
    "Checkbox",
    "Checkbox Group",
    "Dropdown",
    "Group Table",
]


class FormRow(BaseModel):
    """One output row in the final Excel workbook.

    The model must produce rows in document order. Sequence is assigned later.
    """

    section: Optional[str] = Field(
        default=None,
        description=(
            "The section heading this row belongs to. As soon as a new "
            "section heading appears in the document, EVERY subsequent row "
            "(starting from the section-marker row itself) must carry the "
            "NEW heading text in this field — never the previous section's "
            "name. The value stays constant until the next section heading "
            "appears, at which point it switches again. Rows that appear "
            "before the very first section heading have a blank `section`."
        ),
    )
    question_rule: Optional[str] = Field(
        default=None,
        description=(
            "Optional rule/prelude that scopes the question. Populate from "
            "leading 'For <population>:' prefixes or italic prelude blocks "
            "that immediately precede the question. Otherwise leave blank."
        ),
    )
    question_type: QuestionType = Field(
        description=(
            "The canonical type of this row. MUST be exactly one of: "
            "Display, Text Box, Text Area, Date, Number, Signature, "
            "Radio Button, Checkbox, Checkbox Group, Dropdown, Group Table.\n"
            "Rules:\n"
            "- Display: any instruction, knowledge, narrative, header note, "
            "  or guidance block that does NOT ask the form-filler for an "
            "  answer. This includes any free-standing prose that appears "
            "  immediately after a section heading. For Display rows the "
            "  raw text goes into question_text VERBATIM and answer_text "
            "  MUST stay blank — do NOT invent a synthetic question and move "
            "  the prose into answer_text.\n"
            "- Text Box: single-line free-text input (e.g. 'Name: ____').\n"
            "- Text Area: multi-line free-text input.\n"
            "- Date: any field labeled date/dob/birth or formatted MM/DD/YYYY.\n"
            "- Number: fields labeled score/count/age/number/# or numeric-only.\n"
            "- Signature: a signature line (lone 'signature' label).\n"
            "- Radio Button: a question with mutually-exclusive options where "
            "  exactly one must be selected. question_text holds the prompt; "
            "  answer_text lists the options one per line.\n"
            "- Checkbox: a SINGLE standalone check item with no sibling "
            "  options (a lone yes/acknowledgement-style box).\n"
            "- Checkbox Group: a question whose answer set allows MULTIPLE "
            "  options to be selected simultaneously (independent flags, "
            "  'check all that apply' semantics). question_text holds the "
            "  prompt; answer_text lists the options one per line.\n"
            "- Dropdown: a select-one field with a discrete, mutually-"
            "  exclusive option list. PREFER Dropdown over Radio Button when "
            "  the option set is short (roughly <=7) AND clearly mutually "
            "  exclusive. Use Radio Button only when the form visually lays "
            "  the options out as bulleted/glyph-prefixed choices that "
            "  strongly imply radio rendering.\n"
            "- Group Table: a multi-row table. Emit ONE row whose "
            "  question_text is the table title; emit a separate following "
            "  row per column header (typed by column label). Do NOT extract "
            "  data rows inside the table.\n"
            "Composite 'X with Text Area' types are NOT allowed; split into a "
            "parent row plus a separate gated Text Area row.\n"
            "GLYPH-INDEPENDENCE (important): Do NOT classify a row as "
            "Checkbox, Checkbox Group, or Radio Button merely because the "
            "PDF shows a tick (✓), check (☑), square (☐), or bullet glyph. "
            "Glyphs are decorative and inconsistent across forms. Decide the "
            "type purely from semantics: does the row ASK for an answer "
            "(Checkbox / Checkbox Group / Radio Button / Dropdown), or is it "
            "informational prose (Display)? Pre-ticked or glyph-prefixed "
            "instruction lines that don't actually request input are Display."
        ),
    )
    question_text: str = Field(
        description=(
            "The prompt text shown to the form-filler. For Group Table this "
            "is the table title. For section-marker rows (see below) this is "
            "the literal string 'New Section'."
        ),
    )
    branching_logic: Optional[str] = Field(
        default=None,
        description=(
            "Branching gate for child rows. ONLY two templates are allowed:\n"
            "1. 'If Q{n} = checked(selected)' — when this row is gated by a "
            "   checkbox parent.\n"
            "2. 'Display if Q{n} = \"{option_text_verbatim}\"' — when this row "
            "   is gated by a specific radio/dropdown option of a parent. "
            "   Option text MUST be quoted exactly as it appears in the "
            "   parent's answer_text.\n"
            "Use Q{n} where n is the 1-based sequence number of the parent "
            "row in this output. Leave blank for ungated rows. Indent-based "
            "gating is inferred only within a single page; never across pages. "
            "Free-text expressions are forbidden."
        ),
    )
    answer_text: Optional[str] = Field(
        default=None,
        description=(
            "Row payload depending on question_type:\n"
            "- Radio Button / Dropdown / Checkbox Group: the option list, "
            "  one option per line, verbatim, in document order.\n"
            "- Section marker rows: the section heading text.\n"
            "- Display: MUST be blank. The instruction/knowledge text "
            "  belongs in question_text verbatim; never split prose between "
            "  question_text and answer_text.\n"
            "- Text Box / Text Area / Date / Number / Signature / Checkbox / "
            "  Group Table: leave blank.\n"
        ),
    )


class ExtractedForm(BaseModel):
    """Top-level extraction result.

    Emit rows in strict document order. The post-processor will assign dense
    1..N Sequence values.
    """

    rows: List[FormRow] = Field(
        description=(
            "All rows of the output workbook, in document order.\n\n"
            "Required behaviors:\n"
            "1. SECTIONS. When a section heading appears in the document, "
            "   emit a single MARKER row at that boundary with "
            "   question_type='Display', question_text='New Section', "
            "   answer_text=<section heading text>. The marker row itself "
            "   AND every subsequent row must carry the NEW heading text in "
            "   the `section` field — switch immediately, never keep the "
            "   previous section's name. The new value persists until the "
            "   next section heading. Rows before the first heading have a "
            "   blank `section`. Emit the marker only at the boundary, not "
            "   on every row.\n"
            "   Any free-standing instruction/prose that appears between a "
            "   section heading and the first real question is its OWN "
            "   Display row (question_text = the prose verbatim, "
            "   answer_text blank). Do NOT fold that prose into the section "
            "   marker's answer_text and do NOT invent a question for it.\n"
            "2. BLANK FIELDS. 'Label: ____' blanks become rows. Type by label "
            "   semantics: date|dob|birth -> Date; score|count|age|number|# "
            "   -> Number; lone 'signature' -> Signature; else Text Box.\n"
            "3. OPTIONS. Consecutive options after a question form ONE row "
            "   whose answer_text lists them (one per line). Decide the "
            "   parent's question_type semantically, NOT from glyph shape:\n"
            "   - Mutually-exclusive options, short list (<=7) -> Dropdown "
            "     (preferred).\n"
            "   - Mutually-exclusive options laid out as bulleted/glyph-"
            "     prefixed visual choices -> Radio Button.\n"
            "   - Multiple independent options that may all be selected "
            "     ('check all that apply', flag-style lists) -> Checkbox "
            "     Group (one row, options in answer_text).\n"
            "   - A single standalone box with no siblings -> Checkbox.\n"
            "   Tick / check / square glyphs are NOT signal — ignore them "
            "   when choosing the type.\n"
            "4. TABLES. A multi-row table becomes ONE Group Table row "
            "   (question_text = table title) followed by one row per column "
            "   header, each typed by column label. Data rows are dropped.\n"
            "5. CHECKBOX FOLLOW-UPS. An indented 'o'-bulleted prompt or "
            "   'Description of documentation attached: ____' line under a "
            "   checkbox is a SIBLING gated row (Text Area or Text Box), not "
            "   an option of the checkbox. Set its branching_logic to "
            "   'If Q{n} = checked(selected)'.\n"
            "6. SPECIFIERS. A 'Specify' / 'Specify other' field after an "
            "   option containing 'other'/'specify' becomes a SEPARATE Text "
            "   Box row gated on that exact option: branching_logic = "
            "   'Display if Q{n} = \"<option verbatim>\"'.\n"
            "7. REPEAT BANDS. Drop decorative repeats that appear on every "
            "   page (page numbers, form codes, agency footer strings). Keep "
            "   identifier banners only on the first page and promote them "
            "   to questions.\n"
            "8. CROSS-PAGE STITCHING. If a question's options spill onto the "
            "   next page, merge into ONE row.\n"
            "9. QUESTION RULE. Populate question_rule from leading "
            "   'For <population>:' prefixes or italic prelude blocks.\n"
            "10. ORDER. Rows must follow document reading order exactly."
        ),
    )


# ----------------------- LlamaExtract call -----------------------

SYSTEM_PROMPT = (
    "You are converting a government intake/assessment/eligibility PDF form "
    "into a strict 7-column row schema for downstream re-rendering. The PDF "
    "is digitally generated (not scanned) and may or may not have AcroForm "
    "widgets. Your job is to recover the form's logical structure — sections, "
    "questions, fields, options, tables, gated follow-ups — purely from "
    "layout, glyphs, indentation, and blank-field patterns. Never use form-"
    "specific knowledge. Follow the schema field descriptions exactly: the "
    "question_type taxonomy is closed, branching_logic has only two allowed "
    "templates, and section markers are emitted only at section boundaries. "
    "Emit every row in document reading order."
)


def run_extract(pdf_path: Path, api_key: str) -> ExtractedForm:
    from llama_cloud import LlamaCloud

    client = LlamaCloud(api_key=api_key)

    print(f"[1/3] Uploading {pdf_path.name}...", file=sys.stderr)
    file_obj = client.files.create(file=str(pdf_path), purpose="extract")

    print("[2/3] Submitting extraction job...", file=sys.stderr)
    job = client.extract.create(
        file_input=file_obj.id,
        configuration={
            "data_schema": ExtractedForm.model_json_schema(),
            "extraction_target": "per_doc",
            "tier": "agentic",
            "system_prompt": SYSTEM_PROMPT,
        },
    )

    print(f"[3/3] Polling job {job.id}...", file=sys.stderr)
    while job.status not in ("COMPLETED", "FAILED", "CANCELLED"):
        time.sleep(2)
        job = client.extract.get(job.id)
        print(f"      status={job.status}", file=sys.stderr)

    if job.status != "COMPLETED":
        raise RuntimeError(f"Extraction job ended with status={job.status}")

    return ExtractedForm.model_validate(job.extract_result)


# ----------------------- Excel writer -----------------------

HEADERS = [
    "Section",
    "Sequence",
    "Question Rule",
    "Question Type",
    "Question Text",
    "Branching Logic",
    "Answer Text",
]


def write_xlsx(form: ExtractedForm, out_path: Path) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Form"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    body_align = Alignment(vertical="top", wrap_text=True)

    ws.append(HEADERS)
    for col_idx, _ in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    widths = [22, 10, 24, 16, 60, 40, 60]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"

    for seq, row in enumerate(form.rows, start=1):
        ws.append([
            row.section or "",
            seq,
            row.question_rule or "",
            row.question_type,
            row.question_text or "",
            row.branching_logic or "",
            row.answer_text or "",
        ])
        for col_idx in range(1, len(HEADERS) + 1):
            ws.cell(row=seq + 1, column=col_idx).alignment = body_align

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return len(form.rows)


# ----------------------- CLI -----------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a PDF form to the 7-column .xlsx schema using LlamaExtract.")
    parser.add_argument("pdf", type=Path, help="Path to the input PDF.")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output .xlsx path (default: alongside the PDF).")
    parser.add_argument("--dump-json", type=Path, default=None, help="Also write the raw extraction JSON here for inspection.")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"error: {args.pdf} not found", file=sys.stderr)
        return 2

    api_key = os.environ.get("LLAMA_CLOUD_API_KEY")
    if not api_key:
        print("error: LLAMA_CLOUD_API_KEY is not set", file=sys.stderr)
        return 2

    out_path = args.output or args.pdf.with_suffix(".xlsx")

    form = run_extract(args.pdf, api_key)

    if args.dump_json:
        args.dump_json.parent.mkdir(parents=True, exist_ok=True)
        args.dump_json.write_text(form.model_dump_json(indent=2))

    n = write_xlsx(form, out_path)
    print(f"wrote {n} rows -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
