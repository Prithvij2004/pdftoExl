from __future__ import annotations

import json
import re
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.config import PipelineConfig
from app.services.bedrock_agent import build_bedrock_agent
from app.services.chunk_extractor import (
    CANONICAL_QUESTION_TYPES,
    ChunkExtractionResult,
    ExtractedRow,
)
from app.services.chunker import Chunk


MERGE_RESOLVER_VERSION = "merge-resolver-v1"


WORKBOOK_COLUMNS: tuple[str, ...] = (
    "Section",
    "Sequence",
    "Question Rule",
    "Question Type",
    "Question Text",
    "Branching Logic",
    "Answer Text",
)


_BRANCHING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'^If Q(?:\{[^}]+\}|\d+) = checked\(selected\)$'),
    re.compile(r'^Display if Q(?:\{[^}]+\}|\d+) = "[^"]+"$'),
)

_BRANCHING_TOKEN_RE = re.compile(r'Q\{([^}0-9][^}]*)\}')


class FinalRow(BaseModel):
    """Final row written to the workbook."""

    model_config = ConfigDict(frozen=True)

    section: str | None = Field(description="Section heading the row belongs to.")
    sequence: int | None = Field(description="1-based document-wide sequence number assigned during merge.")
    question_rule: str | None = Field(description="Optional question rule (validation, formatting hint).")
    question_type: str = Field(description=f"Canonical question type. One of: {', '.join(CANONICAL_QUESTION_TYPES)}.")
    question_text: str = Field(description="Verbatim question stem text.")
    branching_logic: str | None = Field(description="Branching rule expressed as a canonical template, or None when unset.")
    answer_text: str | None = Field(description="Visible answer text or option list, when present.")
    source_ids: list[str] = Field(default_factory=list, description="Layout block IDs that back this row.")
    chunk_id: str = Field(default="", description="Originating chunk ID.")
    page_numbers: list[int] = Field(default_factory=list, description="Pages the row spans.")
    confidence: float = Field(default=0.0, description="Extractor self-reported confidence in [0, 1].")

    def to_workbook_dict(self) -> dict[str, Any]:
        return {
            "Section": self.section or "",
            "Sequence": self.sequence if self.sequence is not None else "",
            "Question Rule": self.question_rule or "",
            "Question Type": self.question_type,
            "Question Text": self.question_text,
            "Branching Logic": self.branching_logic or "",
            "Answer Text": self.answer_text or "",
        }


class SemanticMerger(Protocol):
    def merge(self, rows: list[FinalRow]) -> list[FinalRow]:
        ...


class MergeResolver:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        semantic_merger: SemanticMerger | None = None,
    ) -> None:
        self.config = config
        self.semantic_merger = semantic_merger

    def resolve(
        self,
        chunks: list[Chunk],
        chunk_results: list[ChunkExtractionResult],
    ) -> list[FinalRow]:
        rows = _flatten_and_sort(chunks, chunk_results)
        rows = _drop_empty_rows(rows)
        if self.semantic_merger is not None and self.config.bedrock_region is not None:
            try:
                merged = self.semantic_merger.merge(list(rows))
            except Exception:  # noqa: BLE001
                merged = rows
            if isinstance(merged, list) and all(isinstance(r, FinalRow) for r in merged):
                rows = merged
        rows = _insert_section_markers(rows)
        rows = _display_cleanup(rows)
        rows = _validate_branching_templates(rows)
        rows = _assign_dense_sequence(rows)
        rows = _resolve_branching_refs(rows)
        return rows


def _replace(row: FinalRow, **overrides: Any) -> FinalRow:
    return row.model_copy(update=overrides)


def _row_from_extracted(row: ExtractedRow) -> FinalRow:
    return FinalRow(
        section=row.section,
        sequence=None,
        question_rule=row.question_rule,
        question_type=row.question_type,
        question_text=row.question_text,
        branching_logic=row.branching_logic,
        answer_text=row.answer_text,
        source_ids=list(row.source_ids),
        chunk_id=row.chunk_id,
        page_numbers=list(row.page_numbers),
        confidence=row.confidence,
    )


def _flatten_and_sort(
    chunks: list[Chunk],
    chunk_results: list[ChunkExtractionResult],
) -> list[FinalRow]:
    order_by_id: dict[str, tuple[int, int]] = {
        chunk.chunk_id: chunk.order_key for chunk in chunks
    }
    indexed: list[tuple[tuple[int, int], int, int, FinalRow]] = []
    for result_index, result in enumerate(chunk_results):
        order_key = order_by_id.get(result.chunk_id, (10**6, result_index))
        for inner_index, raw_row in enumerate(result.rows):
            indexed.append((order_key, result_index, inner_index, _row_from_extracted(raw_row)))
    indexed.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in indexed]


def _drop_empty_rows(rows: list[FinalRow]) -> list[FinalRow]:
    out: list[FinalRow] = []
    for row in rows:
        if not row.question_text or not row.question_text.strip():
            continue
        if row.question_type not in CANONICAL_QUESTION_TYPES:
            continue
        out.append(row)
    return out


def _is_section_marker(row: FinalRow) -> bool:
    return (
        row.question_type == "Display"
        and row.question_text.strip().lower() == "new section"
    )


def _insert_section_markers(rows: list[FinalRow]) -> list[FinalRow]:
    out: list[FinalRow] = []
    current_section: str | None = None
    for row in rows:
        if _is_section_marker(row):
            section_name = row.answer_text or row.section
            if section_name and section_name != current_section:
                current_section = section_name
                out.append(_replace(row, section=section_name, answer_text=section_name))
            continue
        if row.section and row.section != current_section:
            current_section = row.section
            marker = FinalRow(
                section=current_section,
                sequence=None,
                question_rule=None,
                question_type="Display",
                question_text="New Section",
                branching_logic=None,
                answer_text=current_section,
                source_ids=[],
                chunk_id=row.chunk_id,
                page_numbers=list(row.page_numbers),
                confidence=row.confidence,
            )
            out.append(marker)
        out.append(_replace(row, section=current_section))
    return out


def _display_cleanup(rows: list[FinalRow]) -> list[FinalRow]:
    out: list[FinalRow] = []
    for row in rows:
        if row.question_type == "Display" and not _is_section_marker(row):
            out.append(_replace(row, answer_text=None))
        else:
            out.append(row)
    return out


def _validate_branching_templates(rows: list[FinalRow]) -> list[FinalRow]:
    out: list[FinalRow] = []
    for row in rows:
        bl = row.branching_logic
        if not bl:
            out.append(row)
            continue
        text = bl.strip()
        if any(p.match(text) for p in _BRANCHING_PATTERNS):
            out.append(_replace(row, branching_logic=text))
        else:
            out.append(_replace(row, branching_logic=None))
    return out


def _assign_dense_sequence(rows: list[FinalRow]) -> list[FinalRow]:
    return [_replace(row, sequence=index) for index, row in enumerate(rows, start=1)]


def _resolve_branching_refs(rows: list[FinalRow]) -> list[FinalRow]:
    out: list[FinalRow] = list(rows)
    for index, row in enumerate(out):
        if not row.branching_logic:
            continue
        text = row.branching_logic
        match = _BRANCHING_TOKEN_RE.search(text)
        if match is None:
            continue
        token = match.group(1).strip().lower()
        if token in {"", "n"}:
            continue
        target_seq: int | None = None
        for prior in reversed(out[:index]):
            qt = (prior.question_text or "").strip().lower()
            if token in qt and prior.sequence is not None:
                target_seq = prior.sequence
                break
        if target_seq is None:
            out[index] = _replace(row, branching_logic=None)
        else:
            resolved = _BRANCHING_TOKEN_RE.sub(f"Q{target_seq}", text, count=1)
            out[index] = _replace(row, branching_logic=resolved)
    return out


class MergeOverride(BaseModel):
    """Per-row override emitted by the semantic merger."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="Chunk ID of the row being kept (must match an input row).")
    question_text: str = Field(description="Question text of the row being kept (must match an input row verbatim).")
    section: str | None = Field(default=None, description="Override section, when changing.")
    question_rule: str | None = Field(default=None, description="Override question_rule, when changing.")
    branching_logic: str | None = Field(default=None, description="Override branching_logic, when changing.")
    answer_text: str | None = Field(default=None, description="Override answer_text, when changing.")


class MergeOutput(BaseModel):
    """Top-level schema returned by the semantic merger."""

    model_config = ConfigDict(frozen=True)

    rows: list[MergeOverride] = Field(default_factory=list, description="Rows to keep, in document order.")


_SEMANTIC_MERGER_SYSTEM_PROMPT = (
    "You merge structured form rows extracted from PDF chunks. "
    "De-duplicate repeated templates, stitch cross-page continuations, and "
    "drop redundant rows. Preserve document order. Reference each kept row by "
    "its (chunk_id, question_text) pair. Do not invent rows or rename existing ones."
)


class BedrockQwenSemanticMerger:
    def __init__(
        self,
        client: Any | None = None,
        *,
        config: PipelineConfig | None = None,
        agent: Any | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.agent = agent

    def merge(self, rows: list[FinalRow]) -> list[FinalRow]:
        if not rows:
            return rows
        agent = self.agent
        if agent is None:
            config = self.config
            if config is None:
                return rows
            if config.bedrock_region is None and self.client is None:
                return rows
            agent = build_bedrock_agent(
                model_id=config.bedrock_text_model_id,
                region=config.bedrock_region,
                output_type=MergeOutput,
                system_prompt=_SEMANTIC_MERGER_SYSTEM_PROMPT,
                bedrock_client=self.client,
                max_tokens=4096,
            )
        try:
            result = agent.run_sync(_semantic_merge_prompt(rows))
        except Exception:  # noqa: BLE001
            return rows
        merged = _apply_merge_overrides(result.output, rows)
        if not merged:
            return rows
        return merged


def _apply_merge_overrides(
    output: MergeOutput,
    originals: list[FinalRow],
) -> list[FinalRow]:
    by_signature: dict[tuple[str, str], FinalRow] = {}
    for row in originals:
        by_signature.setdefault((row.chunk_id, row.question_text.strip()), row)
    out: list[FinalRow] = []
    for override in output.rows:
        question_text = override.question_text.strip()
        original = by_signature.get((override.chunk_id, question_text))
        if original is None:
            continue
        out.append(
            _replace(
                original,
                section=override.section if override.section is not None else original.section,
                question_rule=override.question_rule if override.question_rule is not None else original.question_rule,
                question_text=question_text or original.question_text,
                branching_logic=override.branching_logic if override.branching_logic is not None else original.branching_logic,
                answer_text=override.answer_text if override.answer_text is not None else original.answer_text,
            )
        )
    return out


def _semantic_merge_prompt(rows: list[FinalRow]) -> str:
    payload = [
        {
            "chunk_id": row.chunk_id,
            "section": row.section,
            "question_type": row.question_type,
            "question_text": row.question_text,
            "answer_text": row.answer_text,
            "branching_logic": row.branching_logic,
            "page_numbers": row.page_numbers,
            "confidence": row.confidence,
        }
        for row in rows
    ]
    return f"input_rows: {json.dumps(payload)}\n"
