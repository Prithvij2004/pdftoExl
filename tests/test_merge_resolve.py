from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PipelineConfig
from app.services.chunk_extractor import ChunkExtractionResult, ExtractedRow
from app.services.chunker import Chunk
from app.services.merge_resolve import (
    WORKBOOK_COLUMNS,
    FinalRow,
    MergeResolver,
)


def _config() -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=220,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=Path("runtime/cache"),
    )


def _chunk(chunk_id: str, order_key: tuple[int, int], section: str | None = None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        pages=[order_key[0]],
        order_key=order_key,
        section_hint=section,
        source_block_ids=[],
        chunk_type_hint="page",
        markdown="",
        structure_hints=[],
    )


def _row(
    chunk_id: str,
    question_type: str,
    question_text: str,
    *,
    section: str | None = None,
    answer_text: str | None = None,
    branching_logic: str | None = None,
    question_rule: str | None = None,
    page_numbers: list[int] | None = None,
) -> ExtractedRow:
    return ExtractedRow(
        section=section,
        sequence=None,
        question_rule=question_rule,
        question_type=question_type,
        question_text=question_text,
        branching_logic=branching_logic,
        answer_text=answer_text,
        source_ids=[],
        chunk_id=chunk_id,
        page_numbers=page_numbers or [1],
        confidence=0.9,
    )


def test_sort_by_order_key():
    chunks = [_chunk("p02_c01", (2, 1)), _chunk("p01_c01", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="p02_c01",
            rows=[_row("p02_c01", "Text Box", "Second", section="A", page_numbers=[2])],
        ),
        ChunkExtractionResult(
            chunk_id="p01_c01",
            rows=[_row("p01_c01", "Text Box", "First", section="A", page_numbers=[1])],
        ),
    ]
    out = MergeResolver(_config()).resolve(chunks, results)
    questions = [r.question_text for r in out if r.question_text != "New Section"]
    assert questions == ["First", "Second"]


def test_section_marker_inserted_for_first_and_changed_section():
    chunks = [_chunk("c1", (1, 1)), _chunk("c2", (1, 2))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[
                _row("c1", "Text Box", "Q1", section="Alpha"),
                _row("c1", "Text Box", "Q2", section="Alpha"),
            ],
        ),
        ChunkExtractionResult(
            chunk_id="c2",
            rows=[_row("c2", "Text Box", "Q3", section="Beta")],
        ),
    ]
    out = MergeResolver(_config()).resolve(chunks, results)
    summary = [(r.question_text, r.answer_text, r.section) for r in out]
    assert summary[0] == ("New Section", "Alpha", "Alpha")
    # Beta marker is at index 3 (after Q1, Q2).
    marker_indexes = [i for i, r in enumerate(out) if r.question_text == "New Section"]
    assert len(marker_indexes) == 2
    beta_marker = out[marker_indexes[1]]
    assert beta_marker.answer_text == "Beta"
    assert beta_marker.section == "Beta"


def test_dense_sequence_no_gaps():
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[
                _row("c1", "Text Box", "Q1", section="Alpha"),
                _row("c1", "Text Box", "Q2", section="Alpha"),
                _row("c1", "Text Box", "Q3", section="Alpha"),
            ],
        ),
    ]
    out = MergeResolver(_config()).resolve(chunks, results)
    sequences = [r.sequence for r in out]
    assert sequences == list(range(1, len(out) + 1))


def test_invalid_branching_template_dropped():
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[
                _row("c1", "Checkbox", "Parent", section="S"),
                _row(
                    "c1",
                    "Text Box",
                    "Child",
                    section="S",
                    branching_logic="when parent is selected please show",
                ),
            ],
        ),
    ]
    out = MergeResolver(_config()).resolve(chunks, results)
    child = next(r for r in out if r.question_text == "Child")
    assert child.branching_logic is None


def test_valid_branching_templates_preserved():
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[
                _row("c1", "Checkbox", "P", section="S"),
                _row(
                    "c1",
                    "Text Box",
                    "Child A",
                    section="S",
                    branching_logic="If Q{n} = checked(selected)",
                ),
                _row(
                    "c1",
                    "Text Box",
                    "Child B",
                    section="S",
                    branching_logic='Display if Q2 = "Yes"',
                ),
            ],
        ),
    ]
    out = MergeResolver(_config()).resolve(chunks, results)
    child_a = next(r for r in out if r.question_text == "Child A")
    child_b = next(r for r in out if r.question_text == "Child B")
    assert child_a.branching_logic == "If Q{n} = checked(selected)"
    assert child_b.branching_logic == 'Display if Q2 = "Yes"'


def test_display_cleanup_blanks_answer_text():
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[
                _row(
                    "c1",
                    "Display",
                    "Some instruction",
                    section="S",
                    answer_text="leftover",
                ),
            ],
        ),
    ]
    out = MergeResolver(_config()).resolve(chunks, results)
    instruction = next(r for r in out if r.question_text == "Some instruction")
    assert instruction.answer_text is None


def test_workbook_dict_columns_in_order():
    row = FinalRow(
        section="S",
        sequence=1,
        question_rule=None,
        question_type="Text Box",
        question_text="Q",
        branching_logic=None,
        answer_text=None,
    )
    keys = list(row.to_workbook_dict().keys())
    assert tuple(keys) == WORKBOOK_COLUMNS


class _IdentityMerger:
    def __init__(self) -> None:
        self.called = False

    def merge(self, rows):
        self.called = True
        return list(rows)


class _DropFirstMerger:
    def merge(self, rows):
        return list(rows[1:])


def test_semantic_merger_invoked_when_region_set():
    cfg = PipelineConfig(
        bedrock_region="us-east-1",
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=220,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=Path("runtime/cache"),
    )
    merger = _IdentityMerger()
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[_row("c1", "Text Box", "Q1", section="S")],
        ),
    ]
    out = MergeResolver(cfg, semantic_merger=merger).resolve(chunks, results)
    assert merger.called
    assert any(r.question_text == "Q1" for r in out)


def test_semantic_merger_can_drop_rows():
    cfg = PipelineConfig(
        bedrock_region="us-east-1",
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=220,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=Path("runtime/cache"),
    )
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[
                _row("c1", "Text Box", "Q1", section="S"),
                _row("c1", "Text Box", "Q2", section="S"),
            ],
        ),
    ]
    out = MergeResolver(cfg, semantic_merger=_DropFirstMerger()).resolve(chunks, results)
    questions = [r.question_text for r in out if r.question_text != "New Section"]
    assert questions == ["Q2"]


def test_semantic_merger_skipped_without_region():
    chunks = [_chunk("c1", (1, 1))]
    results = [
        ChunkExtractionResult(
            chunk_id="c1",
            rows=[_row("c1", "Text Box", "Q1", section="S")],
        ),
    ]
    merger = _IdentityMerger()
    out = MergeResolver(_config(), semantic_merger=merger).resolve(chunks, results)
    assert merger.called is False
    assert any(r.question_text == "Q1" for r in out)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
