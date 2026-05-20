"""
28-column EAB Assessment Template schema.

Header (0-indexed):
  0  Section
  1  Alert (Yes / No)
  2  Alternate Text (Yes / No)
  3  Auto Populated (Yes / No)
  4  History (Yes / No)
  5  Pre Populate (Yes / No)
  6  Required (Yes / No)
  7  Speech to Text (Yes / No)
  8  Submission History (Yes / No)
  9  Concept Code (for Migration use only)
 10  Sequence
 11  Question Rule
 12  QuestionType
 13  Question Text
 14  Branching Logic
 15  Answer Text
 16  Answer Validation
 17  Answer Score Value
 18  Talking Points
 19  Auto Populate with:
 20  Auto Populate Field:
 21  Auto Poulate Rule:        (typo intentional — matches gold)
 22  Alternate Question Text
 23  Alternate Answer Text
 24  Alert Type
 25  Alert Text
 26  Token ID (PDF Generation)
 27  IT Notes
"""
from __future__ import annotations

from pydantic import BaseModel, Field

COLUMNS_28 = [
    "Section",
    "Alert\n(Yes / No)",
    "Alternate Text \n(Yes / No)",
    "Auto Populated\n(Yes / No)",
    "History\n(Yes / No)",
    "Pre Populate\n(Yes / No)  ",
    "Required\n(Yes / No)  ",
    "Speech to Text\n(Yes / No)",
    "Submission History\n(Yes / No)",
    "Concept Code\n(for Migration use only)",
    "Sequence",
    "Question Rule",
    "QuestionType",
    "Question Text",
    "Branching Logic",
    "Answer Text",
    "Answer Validation",
    "Answer Score Value",
    "Talking Points",
    "Auto Populate with:",
    "Auto Populate Field:",
    "Auto Poulate Rule:",
    "Alternate Question Text",
    "Alternate Answer Text",
    "Alert Type",
    "Alert Text",
    "Token ID\n(PDF Generation)",
    "IT Notes ",
]
assert len(COLUMNS_28) == 28

# Vocabulary observed across both goldens. Keep loose (str) so the model can output
# new types (e.g. another form might use "Group Table" or "Drop Down" with a space).
QUESTION_TYPES = [
    "Text Box",
    "Text Area",
    "Display",
    "Checkbox",
    "Checkbox Group",
    "Radio button",
    "Radio Button",          # sister form variant
    "Dropdown",
    "Drop Down",
    "Date",
    "Number",
    "Signature",
    "Group Table",
    "Section Header",
]

YES_NO = ("Yes", "No", "")  # blank means N/A (different from "No")


# Map from internal Row field name → list of header strings we'll accept in a template.
# First entry is the canonical/preferred form. Match is case-insensitive and whitespace-collapsed.
# Some forms (e.g. TX LTSS bilingual) use English-prefixed columns ("English Section",
# "English Question/Index Text"). Some use the typo'd "Auto Poulate Rule:". Handle both.
FIELD_HEADER_ALIASES: dict[str, list[str]] = {
    "section":              ["Section", "English Section"],
    "alert":                ["Alert (Yes / No)", "Alert"],
    "alternate_text":       ["Alternate Text (Yes / No)"],
    "auto_populated":       ["Auto Populated (Yes / No)", "Auto Populated"],
    "history":              ["History (Yes / No)"],
    "pre_populate":         ["Pre Populate (Yes / No)", "Pre Populate"],
    "required":             ["Required (Yes / No)", "Required"],
    "speech_to_text":       ["Speech to Text (Yes / No)"],
    "submission_history":   ["Submission History (Yes / No)"],
    "concept_code":         ["Concept Code (for Migration use only)", "Concept Code"],
    "sequence":             ["Sequence"],
    "question_rule":        ["Question Rule"],
    "question_type":        ["QuestionType", "Question Type"],
    "question_text":        ["Question Text", "English Question/Index Text",
                             "English Question / Index Text"],
    "branching_logic":      ["Branching Logic"],
    "answer_text":          ["Answer Text", "English Answer Text"],
    "answer_validation":    ["Answer Validation"],
    "answer_score_value":   ["Answer Score Value"],
    "talking_points":       ["Talking Points"],
    "auto_populate_with":   ["Auto Populate with:", "Auto Populate with"],
    "auto_populate_field":  ["Auto Populate Field:", "Auto Populate Field"],
    "auto_populate_rule":   ["Auto Poulate Rule:", "Auto Populate Rule:", "Auto Populate Rule"],
    "alt_question_text":    ["Alternate Question Text", "Spanish Question/Index Text",
                             "Spanish Question / Index Text"],
    "alt_answer_text":      ["Alternate Answer Text", "Spanish Answer Text"],
    "alert_type":           ["Alert Type"],
    "alert_text":           ["Alert Text"],
    "token_id":             ["Token ID (PDF Generation)", "Token ID"],
    "it_notes":             ["IT Notes"],
}


def _canon(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def map_template_columns(header_row_values: dict[int, str]) -> dict[str, int]:
    """Given {col_idx: header_string} from the template, return {Row_field_name: col_idx}.
    Fields not present in the template are simply absent from the result."""
    canon_to_col: dict[str, int] = {}
    for cidx, hdr in header_row_values.items():
        if isinstance(hdr, str):
            canon_to_col[_canon(hdr)] = cidx
    out: dict[str, int] = {}
    for field_name, aliases in FIELD_HEADER_ALIASES.items():
        for alias in aliases:
            cidx = canon_to_col.get(_canon(alias))
            if cidx is not None:
                out[field_name] = cidx
                break
    return out


class Row(BaseModel):
    """One question row of the 28-column workbook."""
    section: str = ""
    alert: str = ""                       # Yes / No / blank
    alternate_text: str = ""
    auto_populated: str = ""
    history: str = ""
    pre_populate: str = ""
    required: str = ""
    speech_to_text: str = ""
    submission_history: str = ""
    concept_code: str = ""
    sequence: int | None = None
    question_rule: str = ""
    question_type: str = ""               # one of QUESTION_TYPES (loose)
    question_text: str = ""
    branching_logic: str = ""
    answer_text: str = ""
    answer_validation: str = ""
    answer_score_value: str = ""
    talking_points: str = ""
    auto_populate_with: str = ""
    auto_populate_field: str = ""
    auto_populate_rule: str = ""
    alt_question_text: str = ""
    alt_answer_text: str = ""
    alert_type: str = ""
    alert_text: str = ""
    token_id: str = ""
    it_notes: str = ""

    # bookkeeping for confidence pass (NOT written to gold-shape sheet)
    page: int | None = None
    bbox: list[float] | None = None    # [x0,y0,x1,y1] PDF coords
    confidence: float = 0.0
    review_reasons: list[str] = Field(default_factory=list)

    def to_excel_cells(self) -> list:
        """Return 28-cell list in column order matching the gold template."""
        return [
            self.section,
            self.alert,
            self.alternate_text,
            self.auto_populated,
            self.history,
            self.pre_populate,
            self.required,
            self.speech_to_text,
            self.submission_history,
            self.concept_code,
            self.sequence if self.sequence is not None else "",
            self.question_rule,
            self.question_type,
            self.question_text,
            self.branching_logic,
            self.answer_text,
            self.answer_validation,
            self.answer_score_value,
            self.talking_points,
            self.auto_populate_with,
            self.auto_populate_field,
            self.auto_populate_rule,
            self.alt_question_text,
            self.alt_answer_text,
            self.alert_type,
            self.alert_text,
            self.token_id,
            self.it_notes,
        ]


# JSON schema description handed to Qwen3-VL. Hand-written to be compact + clear.
_LEGACY_EXTRACTION_SCHEMA_DESCRIPTION = """
Each question on the form becomes ONE JSON object with ONLY these keys:

  section            the section banner text this row belongs under, or "" for header rows
                     before any section. Examples: "Current Living Arrangements",
                     "Justification for Safety Determination Request:".
  sequence           integer 1-based reading-order index. Include EVERY visible artifact
                     (input widgets AND static instructional/legal text/section banners)
                     because the gold encodes both.
  question_type      one of: Text Box, Text Area, Display, Checkbox, Checkbox Group,
                     Radio Button, Dropdown, Date, Number, Signature,
                     Group Table, Section Header
  question_text      verbatim label from the PDF (preserve "(header)" prefix on running-header
                     fields like Applicant Name/SSN/DOB if present; keep multi-line text)
  branching_logic    "" usually. Use "If Q<seq> = checked(selected)" when the question only
                     appears if a prior Checkbox is ticked. Use "Display if Q<seq> = <literal>"
                     when conditional on a Radio/Dropdown answer (literal must match an
                     answer_text option of question seq <seq> verbatim).
  answer_text        for Choice types (Radio/Dropdown/Checkbox Group): options exactly
                     as printed, separated by "\\n\\n";
                     for the Display "New Section" rows: the section title;
                     for plain Display paragraphs: usually the same paragraph text
                     (mirror of question_text) OR blank - match what the form expects;
                     for Text Box / Text Area / Date / Number / Signature:
                     LEAVE BLANK unless the PDF visually shows a hint string (e.g., a
                     watermark "mm/dd/yyyy" beside the field) - do NOT invent values
                     like "Signature area" or "default characters = 100" without visual
                     evidence. The downstream config layer fills validation later.
  required           "Yes", "No", or "" - blank is allowed and DIFFERENT from "No"
                     (use "" for conditionally-shown rows whose required-ness depends on parent)
  section            the section banner text this row belongs under, or "" for header rows
                     before any section. Examples: "Current Living Arrangements",
                     "Justification for Safety Determination Request:".
  sequence           integer 1-based reading-order index. Include EVERY visible artifact
                     (input widgets AND static instructional/legal text/section banners)
                     because the gold encodes both.
  branching_logic    "" usually. Use "If Q<seq> = checked(selected)" when the question only
                     appears if a prior Checkbox is ticked. Use "Display if Q<seq> = <literal>"
                     when conditional on a Radio/Dropdown answer (literal must match an
                     answer_text option of question seq <seq> verbatim).
  answer_validation  usually ""
  auto_populated     "Yes" only for repeating header fields (Applicant Name/SSN/DOB shown
                     on every page of a multi-page form); otherwise ""
  page               1-based PDF page number where this question first appears

A "New Section" boundary is encoded as a Display row whose question_text is exactly
"New Section" and answer_text is the section title (e.g. "Current Living Arrangements:").
Place it BEFORE the first row of that section.

Static instructional paragraphs and legal-text blocks are also encoded as Display rows
(question_text = the paragraph text, answer_text = "" or "No answers displayed").

A multi-row table on the PDF (e.g. "Recent hospital admissions" with columns Admit Date /
Discharge Date / Reason) is a "Group Table" parent row followed by N child rows - one per
COLUMN of the table - each with its own question_type (Date, Text Box, etc.) and
question_text equal to the column header.

==== STRICT EXCLUSIONS - DO NOT EMIT ROWS FOR ANY OF THESE ====

1. Page CHROME: form titles repeated in a banner, agency names ("TN Division of Health
   Care Finance & Administration"), form IDs ("TC0175 (Rev. 8-2-16)"), regulatory codes
   ("RDA 2047"), version dates ("Form H1700-3 September 2025"), and page numbers.

2. Recurring header bands: "Applicant Name: ____ SSN: ____ DOB: ____" appears at the top
   of every page on multi-page forms. Emit ONCE on the page where it first appears, NOT
   again on subsequent pages.

3. Pure answer-space lines: an underline like "_______________" or row of dashes by
   itself is the ANSWER SPACE of the previous question, not a separate Text Area. Only
   emit a Text Area row when there's a question or label associated with it.

4. Already-present row: do NOT emit the same workbook field twice just because the
   paper repeats capacity slots. If a block prints the same field set for Entry 1,
   Entry 2, Fall 1, Fall 2, Visit 1, Visit 2, etc., emit each unique field once for
   the schema. The downstream system captures multiple user-entered instances.

5. Role/person repeats: keep repeated labels when the local role changes the meaning.
   Example: Applicant Signature / Applicant Date, Witness Signature / Witness Date,
   and Service Coordinator Signature / Service Coordinator Date are distinct rows.
   Include the role in question_text so the repeated "Signature" or "Date" labels
   are not ambiguous.

6. Repeated table instances: emit ONE Group Table parent and ONE child row per unique
   column/field. Do not emit the table again for each printed row or numbered item.

7. Question splitting: a multi-line question or paragraph is ONE row. Do NOT split a
   single labeled question into two Display rows just because the text wraps.

==== TYPE DISAMBIGUATION (READ CAREFULLY - GLYPH ALONE IS NOT THE SIGNAL) ====

A list of options preceded by box glyphs is NOT automatically Checkbox.
The semantics of the QUESTION PROMPT decide:

  - If the prompt asks for ONE answer (e.g., "Applicant residence (if applicant
    currently resides in a NF, housing status prior to admission):" - there is
    exactly one residence; or "Sex:" / "Gender:" / "Choose one:" / a single noun
    asking for one value):
        -> emit ONE row of question_type = "Radio Button" (or "Dropdown" if the
          options are short and look like a Yes/No/short menu).
        -> answer_text = the option labels joined by EXACTLY "\\n\\n".
        -> DO NOT emit each option as its own Checkbox row.

  - If the prompt explicitly says "check all that apply" or "select all that apply"
    or "(check and complete all that apply)" or similar multi-select wording:
        -> emit ONE row of question_type = "Checkbox Group".
        -> answer_text = the option labels joined by EXACTLY "\\n\\n".
        -> DO NOT emit each option as its own Checkbox row.

  - If a single standalone box-glyph precedes a single sentence/declaration with
    NO peer options (e.g., a list of 8 attestation statements where each is its
    own checkable claim - pages 7-8 of safety forms):
        -> emit one Checkbox row per box, question_text = the sentence text,
          answer_text = "".

  - If an option in a Radio Button / Checkbox Group has a trailing
    "- specify _____" / "- please specify _____" tail, that becomes a CHILD
    Text Box row in addition to keeping the option in the parent's answer_text.
    The child row has question_text like "Specify Relationship" or "Specify Other"
    and branching_logic = "Display if Q<seq> = <option literal>".

OTHER TYPE RULES:
  - Underline `____` after a single label  ->  Text Box
  - Multiple long underlined lines / a tall blank box  ->  Text Area
  - A blank labelled "Date" / "DOB" / "Begin Date" / "End Date" / "Revision Date"
    / a mm/dd/yyyy hint  ->  Date.
  - Numeric-only blank ("Acuity Score: __", "Total Score: __", numeric
    questionnaire response)  ->  Number  (NOT Text Box).
  - Signature line  ->  Signature

==== HEADER BANDS (multi-page running headers) ====

If the very top of a multi-page form has a horizontal band like
   "Applicant Name: __________ SSN: __________ DOB: __________"
that is THREE separate questions on one visual line, NOT one Display row.
Emit THREE rows - one per labelled blank - with question_text "(header) Applicant
Name:", "(header) SSN:", "(header) DOB:" (preserve the "(header)" prefix and the
trailing colon). Mark these with auto_populated = "Yes". Emit them ONCE (on page 1
only) - DO NOT re-emit them on each page; the downstream system reuses the value.

Output ONLY a JSON array. No markdown, no commentary. Each element follows the keys above.
""".strip()

# Current extraction scope for this step. Later configuration stages own all other
# workbook columns such as required, alert, and auto-populate metadata.
EXTRACTION_SCHEMA_DESCRIPTION = """
Each question on the form becomes ONE JSON object with ONLY these keys:

  section            the section banner text this row belongs under, or "" for header rows
                     before any section. Examples: "Current Living Arrangements",
                     "Justification for Safety Determination Request:".
  sequence           integer 1-based reading-order index. Include EVERY visible artifact
                     (input widgets AND static instructional/legal text/section banners)
                     because the target workbook encodes both.
  question_type      one of: Text Box, Text Area, Display, Checkbox, Checkbox Group,
                     Radio Button, Dropdown, Date, Number, Signature,
                     Group Table, Section Header
  question_text      verbatim label from the PDF. Keep multi-line text together.
  branching_logic    "" usually. Use "If Q<seq> = checked(selected)" when the question only
                     appears if a prior Checkbox is ticked. Use "Display if Q<seq> = <literal>"
                     when conditional on a Radio/Dropdown answer.
  answer_text        for Choice types: options exactly as printed, separated by "\\n\\n";
                     for Display "New Section" rows: the section title;
                     for Text Box / Text Area / Date / Number / Signature:
                     usually blank.
  answer_validation  for Text Box / Text Area / Date / Number / Signature
                     only: format or input constraint text such as "default characters = 100",
                     "default characters = 600", "Format is mm/dd/yyyy", numeric-only
                     hints, or signature-area hints. Leave blank for selectable options.

Use AcroForm widgets as the primary signal when they are present:
  - Radio/checkbox widgets with one visible prompt and multiple options become one
    Radio Button or Checkbox Group row, with options in answer_text.
  - Wording like "check all" / "select all that apply" means Checkbox Group.
  - A single Yes/No decision should be Radio Button, not Dropdown, unless the PDF
    visibly uses a dropdown/select widget.
  - A table of independent statement rows with Yes/No choices is not a Group Table;
    emit each statement as its own Radio Button row. Use Group Table only for true
    repeated data grids.
  - If an option has an attached write-in text widget/blank on the same option line,
    keep the clean option in the parent answer_text and emit a separate Text Box row
    immediately after it with branching logic tied to that option.
  - A tall text widget or stacked same-label text widgets indicate Text Area.
  - Do not combine a parent decision and its dependent follow-up controls into one
    Answer Text list. Emit the parent as its own row, then emit each dependent prompt
    as its own child row with branching logic tied to the parent answer.

Repeated blank slots are capacity, not separate workbook fields:
  - If a table or repeating block prints the same field set for Entry 1, Entry 2,
    Fall 1, Fall 2, Visit 1, Visit 2, etc., emit each unique field once.
  - Emit one Group Table parent and one child row per unique column/field. Do not
    emit the same table again for each printed row or numbered item.
  - Preserve repeated fields only when the local role/person changes the meaning.
    For example, Applicant Signature, Witness Signature, and Service Coordinator
    Signature are distinct rows, and their Date rows are distinct too.
  - When preserving role/person repeats, put that context in question_text.

Do NOT output or infer any other columns in this extraction step. In particular, do
NOT output alert, required, auto-populated, pre-populate, history, score, token,
notes, or auto-populate field/rule values. Those belong to a later manual or
automated configuration step.

A "New Section" boundary is encoded as a Display row whose question_text is exactly
"New Section" and answer_text is the section title. Place it before the first row of
that section.

Static instructional paragraphs and legal-text blocks are also encoded as Display rows.

For multi-page running headers like "Applicant Name: ____ SSN: ____ DOB: ____", emit
one row per labelled blank once on the first page where it appears. Do not emit any
auto-populate metadata for those rows.

Output ONLY a JSON array. No markdown, no commentary.
""".strip()
