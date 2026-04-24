from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BlockType(str, Enum):
    section_header = "section_header"
    subsection_header = "subsection_header"
    question_label = "question_label"
    checkbox_option = "checkbox_option"
    radio_option = "radio_option"
    text_input = "text_input"
    text_area = "text_area"
    date_input = "date_input"
    display = "display"
    signature_field = "signature_field"
    table_header = "table_header"
    table_cell = "table_cell"
    page_header = "page_header"
    page_footer = "page_footer"
    unknown = "unknown"


Relation = Literal["option_of", "input_of", "row_of", "child_of_section"]
ProvenanceSource = Literal["rule", "llm", "merge"]
CoordOrigin = Literal["top_left", "bottom_left"]


class BBox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x0: float
    y0: float
    x1: float
    y1: float
    coord_origin: CoordOrigin = "top_left"


class ParentLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_block_id: str
    relation: Relation


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ProvenanceSource


class EnrichedBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    page: int = Field(ge=1)
    reading_order: int
    bbox: BBox
    text: str
    block_type: BlockType
    confidence: float = Field(ge=0.05, le=0.99)
    parent_link: ParentLink | None = None
    branching_logic: str | None = None
    sequence: int | None = None
    question_type: str | None = None
    provenance: Provenance


class EnrichedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str
    page_count: int


class EnrichedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
