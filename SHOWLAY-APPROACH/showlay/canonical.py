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
