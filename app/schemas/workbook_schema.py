from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


WorkbookQuestionType = Literal[
    "Display",
    "Text Box",
    "Text Area",
    "Date",
    "Number",
    "Signature",
    "Radio Button",
    "Checkbox",
    "Dropdown",
    "Group Table",
]


class WorkbookRow(BaseModel):
    section: str = ""
    sequence: int = Field(ge=1)
    question_rule: str = ""
    question_type: WorkbookQuestionType
    question_text: str
    branching_logic: str = ""
    answer_text: str = ""
    needs_review: bool = False
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    review_reason: str = ""
