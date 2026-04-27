from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class QuestionType(str, Enum):
    RADIO_BUTTON = "Radio Button"
    CHECKBOX = "Checkbox"
    CHECKBOX_GROUP = "Checkbox Group"
    TEXT_AREA = "Text Area"
    TEXT_BOX = "Text Box"
    CALENDAR = "Calendar"
    DATE = "Calendar"  # Backwards-compatible enum alias for old code references.
    DISPLAY = "Display"
    DROPDOWN = "Dropdown"
    EQUATION = "Equation"
    NUMBER = "Number"
    RADIO_BUTTON_WITH_TEXT_AREA = "Radio Button with Text Area"
    CHECKBOX_GROUP_WITH_TEXT_AREA = "Checkbox Group with Text Area"
    SIGNATURE = "Signature"


class ExtractedRow(BaseModel):
    question_type: QuestionType
    question_text: str = Field(min_length=1)
    answer_text: str = ""

    page_number: int = Field(ge=1)
    source_order: int = Field(ge=0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # optional raw/debug fields (ignored by excel output)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("question_type", mode="before")
    @classmethod
    def _normalize_legacy_question_type(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "Date":
            return QuestionType.CALENDAR
        return v


class ExtractionResult(BaseModel):
    rows: list[ExtractedRow]


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None

