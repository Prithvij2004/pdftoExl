from __future__ import annotations

from pathlib import Path

from app.config import PipelineConfig
from app.services.chunker import Chunk, SemanticChunker
from app.services.parser_path import (
    ChunkSeed,
    ContinuationHint,
    PageParserArtifact,
    RepeatedBandHint,
)
from app.services.semantic_hints import SemanticHint, SemanticPageModel


def _config(max_tokens: int = 3500) -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=220,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=max_tokens,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=Path("runtime/cache"),
    )


def _hint(
    page: int,
    block_index: int,
    role: str,
    text: str,
    *,
    nearby_heading: str | None = None,
    indent_level: int = 0,
) -> SemanticHint:
    block_id = f"p{page}_b{block_index:04d}"
    return SemanticHint(
        id=f"p{page}_b{block_index}",
        role=role,
        text=text,
        source_ids=[block_id],
        indent_level=indent_level,
        column=0,
        nearby_heading=nearby_heading,
    )


def _semantic_page(page: int, hints: list[SemanticHint]) -> SemanticPageModel:
    return SemanticPageModel(
        page_number=page,
        width=612,
        height=792,
        parser_version="test",
        hints=hints,
    )


def _seed(
    page: int,
    index: int,
    *,
    block_indexes: list[int],
    section_hint: str | None,
    chunk_type_hint: str = "section",
) -> ChunkSeed:
    return ChunkSeed(
        seed_id=f"p{page:02d}_seed{index:02d}",
        chunk_type_hint=chunk_type_hint,
        section_hint=section_hint,
        source_block_ids=[f"p{page}_b{i:04d}" for i in block_indexes],
    )


def _parser_artifact(
    *,
    page: int,
    chunk_seeds: list[ChunkSeed],
    repeated_header_hints: list[RepeatedBandHint] | None = None,
    repeated_footer_hints: list[RepeatedBandHint] | None = None,
    continuation_hints: list[ContinuationHint] | None = None,
    excluded_source_ids: list[str] | None = None,
) -> PageParserArtifact:
    return PageParserArtifact(
        page_number=page,
        parser_version="test",
        markdown=f"# Page {page}\n",
        continuation_hints=list(continuation_hints or []),
        repeated_header_hints=list(repeated_header_hints or []),
        repeated_footer_hints=list(repeated_footer_hints or []),
        chunk_seeds=list(chunk_seeds),
        excluded_source_ids=list(excluded_source_ids or []),
    )


def test_chunker_produces_chunk_ids_and_order_keys() -> None:
    hints = [
        _hint(1, 1, "section_heading", "Demographics"),
        _hint(1, 2, "question_stem", "Name", nearby_heading="Demographics"),
        _hint(1, 3, "blank_field", "Address: __________", nearby_heading="Demographics"),
    ]
    semantic_page = _semantic_page(1, hints)
    seeds = [
        _seed(1, 1, block_indexes=[1, 2, 3], section_hint="Demographics", chunk_type_hint="section"),
    ]
    artifact = _parser_artifact(page=1, chunk_seeds=seeds)

    chunks = SemanticChunker(_config()).build([artifact], [], [semantic_page])

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_id == "p01_c01"
    assert chunk.order_key == (1, 1)
    assert chunk.pages == [1]
    assert chunk.section_hint == "Demographics"
    assert "## Demographics" in chunk.markdown
    assert any("[p1_b1]" in line for line in chunk.structure_hints)
    assert chunk.chunk_type_hint == "section"


def test_chunker_drops_repeated_band_hints() -> None:
    hints = [
        _hint(1, 1, "repeated_header", "Confidential Form"),
        _hint(1, 2, "section_heading", "Demographics"),
        _hint(1, 3, "question_stem", "Name", nearby_heading="Demographics"),
    ]
    semantic_page = _semantic_page(1, hints)
    seeds = [
        _seed(
            1,
            1,
            block_indexes=[1, 2, 3],
            section_hint="Demographics",
            chunk_type_hint="section",
        ),
    ]
    band = RepeatedBandHint(
        text="Confidential Form",
        band="header",
        page_numbers=[1, 2],
        source_ids=["p1_b0001"],
    )
    artifact = _parser_artifact(page=1, chunk_seeds=seeds, repeated_header_hints=[band])

    chunks = SemanticChunker(_config()).build([artifact], [], [semantic_page])

    assert len(chunks) == 1
    chunk = chunks[0]
    assert "Confidential Form" not in chunk.markdown
    assert "p1_b0001" not in chunk.source_block_ids


def test_chunker_merges_continuation_across_pages() -> None:
    hints_one = [
        _hint(1, 1, "section_heading", "Notes"),
        _hint(1, 2, "instruction", "Continued on next page", nearby_heading="Notes"),
    ]
    hints_two = [
        _hint(2, 1, "instruction", "...continued from previous", nearby_heading="Notes"),
        _hint(2, 2, "question_stem", "Final question", nearby_heading="Notes"),
    ]
    page_one = _semantic_page(1, hints_one)
    page_two = _semantic_page(2, hints_two)

    seeds_one = [
        _seed(1, 1, block_indexes=[1, 2], section_hint="Notes", chunk_type_hint="section"),
    ]
    seeds_two = [
        _seed(2, 1, block_indexes=[1, 2], section_hint="Notes", chunk_type_hint="section"),
    ]

    to_next = ContinuationHint(
        direction="to_next",
        page_number=1,
        neighbor_page=2,
        text="Continued on next page",
        source_ids=["p1_b0002"],
    )
    from_previous = ContinuationHint(
        direction="from_previous",
        page_number=2,
        neighbor_page=1,
        text="...continued from previous",
        source_ids=["p2_b0001"],
    )

    artifact_one = _parser_artifact(
        page=1, chunk_seeds=seeds_one, continuation_hints=[to_next]
    )
    artifact_two = _parser_artifact(
        page=2, chunk_seeds=seeds_two, continuation_hints=[from_previous]
    )

    chunks = SemanticChunker(_config()).build(
        [artifact_one, artifact_two], [], [page_one, page_two]
    )

    assert len(chunks) == 1
    merged = chunks[0]
    assert merged.chunk_id == "p01_c01"
    assert merged.pages == [1, 2]
    assert merged.order_key == (1, 1)
    assert "Final question" in merged.markdown
    assert "Notes" in (merged.section_hint or "")


def test_chunker_splits_when_markdown_exceeds_budget() -> None:
    big_text = "A" * 600
    hints = [
        _hint(1, idx, "question_stem", f"{big_text} {idx}", nearby_heading="Body")
        for idx in range(1, 7)
    ]
    semantic_page = _semantic_page(1, hints)
    seeds = [
        _seed(
            1,
            1,
            block_indexes=list(range(1, 7)),
            section_hint="Body",
            chunk_type_hint="section",
        ),
    ]
    artifact = _parser_artifact(page=1, chunk_seeds=seeds)

    chunker = SemanticChunker(_config(max_tokens=200))
    chunks = chunker.build([artifact], [], [semantic_page])

    assert len(chunks) >= 2
    for index, chunk in enumerate(chunks, start=1):
        assert chunk.chunk_id == f"p01_c{index:02d}"
        assert chunk.order_key == (1, index)
    assert sorted(chunks, key=lambda c: c.order_key) == chunks


def test_chunker_returns_chunks_sorted_by_order_key() -> None:
    page_one_hints = [_hint(1, 1, "question_stem", "Q1")]
    page_three_hints = [_hint(3, 1, "question_stem", "Q3")]
    page_two_hints = [_hint(2, 1, "question_stem", "Q2")]

    artifacts = [
        _parser_artifact(
            page=3,
            chunk_seeds=[_seed(3, 1, block_indexes=[1], section_hint=None)],
        ),
        _parser_artifact(
            page=1,
            chunk_seeds=[_seed(1, 1, block_indexes=[1], section_hint=None)],
        ),
        _parser_artifact(
            page=2,
            chunk_seeds=[_seed(2, 1, block_indexes=[1], section_hint=None)],
        ),
    ]
    semantic_pages = [
        _semantic_page(1, page_one_hints),
        _semantic_page(2, page_two_hints),
        _semantic_page(3, page_three_hints),
    ]

    chunks = SemanticChunker(_config()).build(artifacts, [], semantic_pages)

    assert [chunk.order_key for chunk in chunks] == [(1, 1), (2, 1), (3, 1)]


def test_chunk_to_dict_serializes_order_key_as_list() -> None:
    chunk = Chunk(
        chunk_id="p01_c01",
        pages=[1],
        order_key=(1, 1),
        section_hint="Demographics",
        source_block_ids=["p1_b0001"],
        chunk_type_hint="section",
        markdown="# Page 1\n",
        structure_hints=["[p1_b1] role=question_stem, indent=0: Name"],
    )

    payload = chunk.to_dict()
    assert payload["chunk_id"] == "p01_c01"
    assert payload["order_key"] == [1, 1]
    assert payload["pages"] == [1]
