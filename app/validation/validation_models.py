from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ValidationIssue(BaseModel):
    severity: Literal["info", "warning", "error"]
    item_id: str | None = None
    message: str
    suggested_action: str = ""
