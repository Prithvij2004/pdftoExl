from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    coord_origin: Literal["top_left", "bottom_left"] = "top_left"


class ParentLink(BaseModel):
    parent_block_id: str
    relation: Literal["option_of", "input_of", "row_of", "child_of_section"]


class Provenance(BaseModel):
    source: Literal["rule", "llm", "merge"]


class EnrichedBlock(BaseModel):
    block_id: str
    page: int = Field(ge=1)
    reading_order: int
    bbox: BBox
    text: str
    block_type: str
    confidence: float = Field(ge=0.05, le=0.99)
    parent_link: ParentLink | None = None
    branching_logic: str | None = None
    sequence: int | None = None
    question_type: str | None = None
    provenance: Provenance


class EnrichedSource(BaseModel):
    sha256: str
    page_count: int


class EnrichedDocument(BaseModel):
    schema_version: str
    source: EnrichedSource
    blocks: list[EnrichedBlock]


class FixtureManifest(BaseModel):
    id: str
    pdf_path: Path
    golden_xlsx_path: Path
    reference_xlsx_path: Path
    question_sheet: str
    header_row: int
    template_version: str | None = None
    schema_version: str | None = None
    notes: str | None = None


class MetricResult(BaseModel):
    name: str
    value: float | bool | int | str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    passed: bool | None = None


class EvalResult(BaseModel):
    fixture_id: str
    eval_name: str
    metrics: list[MetricResult]
    passed: bool
    notes: str | None = None
