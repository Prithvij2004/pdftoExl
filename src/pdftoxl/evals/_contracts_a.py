"""Minimal Boundary 1 Pydantic models for Eval A.

Temporary module to avoid collision with the core worktree's contracts.py.
Will be reconciled at merge. Mirrors the schema in
developer-docs/evals/contracts.md (Boundary 1).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

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
    coord_origin: CoordOrigin


class ParentLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_block_id: str
    relation: Relation


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ProvenanceSource


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str
    page_count: int


class EnrichedBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    page: int = Field(ge=1)
    reading_order: int
    bbox: BBox
    text: str
    block_type: BlockType
    confidence: float = Field(ge=0.05, le=0.99)
    parent_link: Optional[ParentLink] = None
    branching_logic: Optional[str] = None
    sequence: Optional[int] = None
    question_type: Optional[str] = None
    provenance: Provenance


class EnrichedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    source: Source
    blocks: list[EnrichedBlock]
