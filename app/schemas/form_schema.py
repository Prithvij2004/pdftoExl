from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


InlineQuestionType = Literal["Text Box", "Text Area", "Date", "Number", "Signature", "Dropdown", "None"]
FollowupQuestionType = Literal["Text Box", "Text Area", "Date", "Number", "Signature", "Dropdown", "Display", "Review Required"]
TableQuestionType = Literal[
    "Text Box",
    "Text Area",
    "Date",
    "Number",
    "Signature",
    "Radio Button",
    "Checkbox",
    "Dropdown",
    "Display",
    "Unknown",
]
ItemType = Literal[
    "display",
    "text_box",
    "text_area",
    "date",
    "number",
    "signature",
    "radio_group",
    "checkbox_item",
    "dropdown",
    "table",
    "section_marker",
    "review_required",
]


class InlineInput(BaseModel):
    label: str = ""
    question_type: InlineQuestionType = "None"
    source_text: str = ""
    needs_review: bool = False
    review_reason: str = ""


class Option(BaseModel):
    option_id: str
    option_text: str = ""
    inline_input: InlineInput | None = None
    source_pages: list[int] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""


class Followup(BaseModel):
    followup_id: str
    trigger: str = ""
    question_text: str = ""
    question_type: FollowupQuestionType = "Review Required"
    source_pages: list[int] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""


class TableColumn(BaseModel):
    column_name: str = ""
    question_type: TableQuestionType = "Unknown"
    answer_options: list[str] = Field(default_factory=list)


class TableDef(BaseModel):
    table_name: str = ""
    columns: list[TableColumn] = Field(default_factory=list)


class FormItem(BaseModel):
    item_id: str
    item_type: ItemType = "review_required"
    question_text: str = ""
    question_rule: str = ""
    display_text: str = ""
    row_group_id: str = ""
    options: list[Option] = Field(default_factory=list)
    followups: list[Followup] = Field(default_factory=list)
    table: TableDef | None = None
    source_text: str = ""
    source_pages: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    review_reason: str = ""


class FormSection(BaseModel):
    section_id: str
    section_title: str = ""
    source_pages: list[int] = Field(default_factory=list)
    items: list[FormItem] = Field(default_factory=list)


class DocumentExtraction(BaseModel):
    document_title: str = ""
    form_name: str = ""
    form_number: str = ""
    revision_date: str = ""
    sections: list[FormSection] = Field(default_factory=list)
