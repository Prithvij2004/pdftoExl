from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    TEXT_BOX = "Text Box"
    TEXT_AREA = "Text Area"
    DATE = "Date"
    NUMBER = "Number"
    RADIO_BUTTON = "Radio Button"
    CHECKBOX = "Checkbox"
    CHECKBOX_GROUP = "Checkbox Group"
    DROPDOWN = "Dropdown"
    DISPLAY = "Display"
    SIGNATURE = "Signature"
    GROUP_TABLE = "Group Table"
    GROUP = "Group"


class ExtractedRow(BaseModel):
    question_type: QuestionType
    question_text: str = Field(min_length=1)
    answer_text: str = ""

    page_number: int = Field(ge=1)
    source_order: int = Field(ge=0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # optional raw/debug fields (ignored by excel output)
    meta: dict[str, Any] = Field(default_factory=dict)


class ExtractionResult(BaseModel):
    rows: list[ExtractedRow]


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None

