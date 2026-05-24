"""Canonical agent schemas for the section-aware SHOWLAY pipeline."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

QuestionType = Literal[
    "Text Box",
    "Text Area",
    "Display",
    "Checkbox",
    "Checkbox Group",
    "Radio Button",
    "Dropdown",
    "Date",
    "Number",
    "Signature",
    "Group Table",
]

CANONICAL_QUESTION_TYPE_TAXONOMY = [
    {
        "question_type": "Text Box",
        "meaning": "A short free-text answer field, usually one line or a compact blank next to a label.",
        "question_text": "Use the printed label that names the blank field.",
        "answer_text": "Leave blank because there are no printed selectable options.",
    },
    {
        "question_type": "Text Area",
        "meaning": "A longer free-text response area, usually a multi-line box or large blank space.",
        "question_text": "Use the printed prompt that asks for the long response.",
        "answer_text": "Leave blank because there are no printed selectable options.",
    },
    {
        "question_type": "Display",
        "meaning": "Instruction, explanation, attestation, section marker, or static text that should appear as content but has no input.",
        "question_text": "Use the full visible static text or section marker.",
        "answer_text": "Leave blank unless the downstream workbook convention explicitly stores marker text there.",
    },
    {
        "question_type": "Checkbox",
        "meaning": "One standalone binary declaration or acknowledgement, not one option inside a group.",
        "question_text": "Use the declaration/label attached to that one checkbox.",
        "answer_text": "Usually blank for an empty template; do not write checked/unchecked as answer text.",
    },
    {
        "question_type": "Checkbox Group",
        "meaning": "A multi-select list where one shared prompt has many independent printed options.",
        "question_text": "Use the shared group prompt.",
        "answer_text": "Use the printed option labels, separated by exactly two newlines.",
    },
    {
        "question_type": "Radio Button",
        "meaning": "A single-select choice where one shared prompt has mutually exclusive printed options.",
        "question_text": "Use the shared prompt or row label.",
        "answer_text": "Use the printed peer option labels, separated by exactly two newlines.",
    },
    {
        "question_type": "Dropdown",
        "meaning": "A select-list field whose choices are hidden in a dropdown or listed as a controlled picklist.",
        "question_text": "Use the printed label for the dropdown.",
        "answer_text": "Use printed/available option labels when visible or known from field metadata; otherwise leave blank.",
    },
    {
        "question_type": "Date",
        "meaning": "A field where the user enters a date.",
        "question_text": "Use the printed date label; add nearby context when repeated labels like Date appear more than once.",
        "answer_text": "Leave blank because the user-entered date is not printed on a blank template.",
    },
    {
        "question_type": "Number",
        "meaning": "A field where the user enters a numeric value.",
        "question_text": "Use the printed numeric field label.",
        "answer_text": "Leave blank because the user-entered number is not printed on a blank template.",
    },
    {
        "question_type": "Signature",
        "meaning": "A signature capture/line field for a named signer.",
        "question_text": "Use the printed signer label, not the underline itself.",
        "answer_text": "Leave blank because the signature value is not printed on a blank template.",
    },
    {
        "question_type": "Group Table",
        "meaning": "A repeated grid/table where rows or columns form a structured group of related fields.",
        "question_text": "Use the table title or row/column prompt that identifies the grouped item.",
        "answer_text": "Use printed table options only when the table is itself a choice table; otherwise leave blank.",
    },
]

CANONICAL_OUTPUT_FIELD_TAXONOMY = [
    {"field_name": "Sequence", "meaning": "Workbook row order, assigned after extraction."},
    {"field_name": "Section", "meaning": "The visible or inferred section name containing the row."},
    {"field_name": "Question Type", "meaning": "One canonical type from the question taxonomy."},
    {"field_name": "Question Text", "meaning": "The visible label, prompt, instruction, row text, or signer label."},
    {"field_name": "Answer Text", "meaning": "Printed selectable option labels only; not filled-in user values."},
    {"field_name": "Answer Validation", "meaning": "Validation/default entry constraint such as default characters or date/number format."},
    {"field_name": "Branching Logic", "meaning": "Machine-readable display/skip condition when confidently known."},
    {"field_name": "Question Rule", "meaning": "Condition controlling whether the current question displays."},
    {"field_name": "Required", "meaning": "Required marker when the PDF clearly says a field is required."},
    {"field_name": "Source Page", "meaning": "The PDF page where the row appears."},
    {"field_name": "Risk Level", "meaning": "Review risk assigned after extraction."},
    {"field_name": "Review Notes", "meaning": "Human-review notes or warnings."},
]


class SectionProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(
        description=(
            "Visible major content section title or a compact generated content-section name. "
            "Use Unsectioned Content for extractable content without a visible section heading. "
            "Do not use page chrome, repeated document titles, footers, or generic header/introduction names."
        )
    )
    pages: list[int] = Field(description="1-based page numbers covered by this section.")


class SharedLegend(BaseModel):
    model_config = ConfigDict(extra="allow")

    location: str = ""
    content: str = ""


class DocumentProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    form_title: str = ""
    form_version: str = ""
    complexity: Literal["simple", "medium", "complex"] = "medium"
    sections: list[SectionProfile] = Field(default_factory=list)
    shared_legends: list[SharedLegend] = Field(default_factory=list)
    extraction_strategy: Literal["single_call", "section_by_section"] = "single_call"
    recommended_model_tier: Literal["low_cost", "mid_tier"] = "low_cost"


class QuestionTypePolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    question_type: QuestionType | str = Field(description="Canonical question type this guidance applies to.")
    pdf_clues: list[str] = Field(
        default_factory=list,
        description=(
            "Visible clues in the page images, text blocks, and widgets that show how this "
            "question type appears in this PDF."
        ),
    )
    instruction: str = Field(
        default="",
        description=(
            "PDF-specific instruction for recognizing this question type from the evidence "
            "passed to the section extractor."
        ),
    )
    question_text_instruction: str = Field(
        default="",
        description=(
            "PDF-specific instruction for identifying the visible question, prompt, label, "
            "instruction, or shared group text."
        ),
    )
    answer_text_instruction: str = Field(
        default="",
        description=(
            "PDF-specific instruction for collecting printed selectable option labels into "
            "Answer Text. This is not for filled-in user values. Usually blank for non-choice "
            "types; important for Radio Button, Checkbox Group, and Checkbox patterns."
        ),
    )
    branching_instruction: str = Field(
        default="",
        description="PDF-specific instruction for branching or applicability wording on this question type.",
    )
    where_it_appears: list[str] = Field(
        default_factory=list,
        description="PDF-specific page/area/source-id locations where this question type appears.",
    )
    distinguishing_features: list[str] = Field(
        default_factory=list,
        description=(
            "Specific visual/layout/widget features that distinguish this question type from "
            "similar-looking types in this PDF."
        ),
    )
    do_not_confuse_with: list[str] = Field(
        default_factory=list,
        description="PDF-specific warnings about nearby or similar patterns that are a different question type.",
    )
    examples: list[QuestionTypeExample] = Field(
        default_factory=list,
        description="Concrete examples from this PDF with source ids and reasoning.",
    )


class QuestionTypeExample(BaseModel):
    model_config = ConfigDict(extra="allow")

    page: int | None = None
    source_ids: list[str] = Field(default_factory=list)
    nearby_widget_ids: list[str] = Field(default_factory=list)
    visible_text: str = ""
    why_this_type: str = ""
    question_text_rule: str = ""
    answer_text_rule: str = ""


class FieldExtractionPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    field_name: str = Field(description="Canonical output field or internal metadata field.")
    pdf_clues: list[str] = Field(
        default_factory=list,
        description="Visible clues in this PDF that explain where this field appears.",
    )
    instruction: str = Field(
        default="",
        description="PDF-specific instruction for filling this field.",
    )


class SectionExtractionPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    section_name: str = ""
    pages: list[int] = Field(default_factory=list)
    instructions: list[str] = Field(
        default_factory=list,
        description="Section-specific instructions or exceptions for extraction.",
    )


class SectionPromptPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    question_types: list[str] = Field(
        default_factory=list,
        description="Question Type values the section extractor may emit for this PDF.",
    )
    excel_columns: list[str] = Field(
        default_factory=list,
        description="Canonical Excel columns or output fields the section extractor should target.",
    )
    global_instructions: list[str] = Field(
        default_factory=list,
        description="PDF-specific instructions that apply to all sections.",
    )
    ignore_patterns: list[str] = Field(
        default_factory=list,
        description="Repeated chrome, headings, or decorative content to ignore.",
    )
    question_type_guidance: list[QuestionTypePolicy] = Field(default_factory=list)
    field_guidance: list[FieldExtractionPolicy] = Field(default_factory=list)
    section_guidance: list[SectionExtractionPolicy] = Field(default_factory=list)


class ExtractionPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    policy: SectionPromptPolicy = Field(
        default_factory=SectionPromptPolicy,
        description="Only this object is passed to the section extractor.",
    )
    form_summary: str = ""
    warnings: list[str] = Field(default_factory=list)


class ExtractedRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    section: str = ""
    question_type: QuestionType | str = ""
    question_text: str = ""
    answer_text: str = ""
    answer_validation: str = ""
    branching_logic: str = Field(
        default="",
        description="Machine/actionable condition if known, preferably using Q sequence refs.",
    )
    branching_source: str = Field(
        default="",
        description="Verbatim source skip/display wording from the PDF, preserving item codes.",
    )
    question_rule: str = ""
    external_id: str = Field(default="", description="Source item code such as B0100 or GG0115.")
    required: str = ""
    page: int | None = None
    bbox: list[float] | None = None
    source_ids: list[str] = Field(default_factory=list)
    confidence_hint: float = 1.0
    warnings: list[str] = Field(default_factory=list)


class SectionExtraction(BaseModel):
    model_config = ConfigDict(extra="allow")

    section_name: str = ""
    rows: list[ExtractedRow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


CANONICAL_EXCEL_SCHEMA_VERSION = "choices_assessment_template_v1"

CANONICAL_EXCEL_COLUMNS = [
    "Sequence",
    "Section",
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
