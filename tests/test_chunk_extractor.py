from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.config import PipelineConfig
from app.services.chunk_extractor import (
    CANONICAL_QUESTION_TYPES,
    ChunkExtractionResult,
    ChunkExtractionService,
    ExtractedRow,
    _chunk_extraction_prompt,
    _coerce_row,
    _coerce_rows,
)
from app.services.chunker import Chunk


def _make_chunk(
    *,
    chunk_id: str = "p03_c02",
    pages: list[int] | None = None,
    markdown: str = "# Page 3\n\nHospital records attached\n",
    section_hint: str | None = "Medical Documentation",
    structure_hints: list[str] | None = None,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        pages=pages or [3],
        order_key=(3, 2),
        section_hint=section_hint,
        source_block_ids=["p3_b08", "p3_b09"],
        chunk_type_hint="checkbox_followups",
        markdown=markdown,
        structure_hints=structure_hints or ["[p3_b08] role=choice_option, indent=0: Hospital records attached"],
    )


def _make_config(*, max_concurrent: int = 4, region: str | None = None) -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=region,
        bedrock_vision_model_id="qwen-vl",
        bedrock_text_model_id="qwen-text",
        page_render_dpi=220,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=max_concurrent,
        extraction_cache_dir=Path("runtime/cache"),
    )


class _FakeExtractor:
    def __init__(self, rows_by_chunk: dict[str, list[dict]] | None = None) -> None:
        self.rows_by_chunk = rows_by_chunk or {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def extract(self, chunk: Chunk) -> ChunkExtractionResult:
        with self._lock:
            self.calls.append(chunk.chunk_id)
        rows: list[ExtractedRow] = []
        for raw in self.rows_by_chunk.get(chunk.chunk_id, []):
            row = _coerce_row(raw, chunk)
            if row is not None:
                rows.append(row)
        return ChunkExtractionResult(chunk_id=chunk.chunk_id, rows=rows)


class _SlowExtractor:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def extract(self, chunk: Chunk) -> ChunkExtractionResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.active -= 1
        return ChunkExtractionResult(chunk_id=chunk.chunk_id, rows=[])


class _FlakyExtractor:
    def __init__(self, fail_ids: set[str]) -> None:
        self.fail_ids = fail_ids

    def extract(self, chunk: Chunk) -> ChunkExtractionResult:
        if chunk.chunk_id in self.fail_ids:
            raise RuntimeError(f"boom for {chunk.chunk_id}")
        return ChunkExtractionResult(chunk_id=chunk.chunk_id, rows=[])


def test_extracted_row_to_dict_roundtrips() -> None:
    chunk = _make_chunk()
    row = _coerce_row(
        {
            "section": "ignored",
            "question_type": "Checkbox",
            "question_text": "Hospital records attached",
            "confidence": 0.91,
            "source_ids": ["p3_b08"],
        },
        chunk,
    )
    assert row is not None
    data = row.to_dict()
    assert data["chunk_id"] == "p03_c02"
    assert data["page_numbers"] == [3]
    assert data["sequence"] is None
    assert data["question_type"] == "Checkbox"


def test_coerce_row_rejects_invalid_question_type() -> None:
    chunk = _make_chunk()
    row = _coerce_row(
        {"question_type": "Freeform", "question_text": "x"},
        chunk,
    )
    assert row is None


def test_coerce_row_rejects_missing_question_text() -> None:
    chunk = _make_chunk()
    assert _coerce_row({"question_type": "Display"}, chunk) is None
    assert _coerce_row({"question_type": "Display", "question_text": "  "}, chunk) is None


def test_coerce_row_overrides_chunk_id_and_pages() -> None:
    chunk = _make_chunk(chunk_id="p05_c01", pages=[5])
    row = _coerce_row(
        {
            "question_type": "Display",
            "question_text": "Notice",
            "chunk_id": "evil",
            "page_numbers": [99],
        },
        chunk,
    )
    assert row is not None
    assert row.chunk_id == "p05_c01"
    assert row.page_numbers == [5]


def test_coerce_row_falls_back_to_chunk_section() -> None:
    chunk = _make_chunk(section_hint="Demographics")
    row = _coerce_row(
        {"question_type": "Text Box", "question_text": "Name"},
        chunk,
    )
    assert row is not None
    assert row.section == "Demographics"


def test_coerce_row_clamps_confidence() -> None:
    chunk = _make_chunk()
    high = _coerce_row(
        {"question_type": "Display", "question_text": "x", "confidence": 5},
        chunk,
    )
    low = _coerce_row(
        {"question_type": "Display", "question_text": "x", "confidence": -1},
        chunk,
    )
    bad = _coerce_row(
        {"question_type": "Display", "question_text": "x", "confidence": "nope"},
        chunk,
    )
    assert high is not None and high.confidence == 1.0
    assert low is not None and low.confidence == 0.0
    assert bad is not None and bad.confidence == 0.5


def test_coerce_rows_handles_non_list() -> None:
    chunk = _make_chunk()
    assert _coerce_rows(None, chunk) == []
    assert _coerce_rows("oops", chunk) == []


def test_prompt_builder_includes_chunk_metadata() -> None:
    chunk = _make_chunk(markdown="# Page 3\n\nQ1: Are you over 18?\n")
    prompt = _chunk_extraction_prompt(chunk)
    assert "Q1: Are you over 18?" in prompt
    assert "p03_c02" in prompt
    assert "Medical Documentation" in prompt
    for canonical in CANONICAL_QUESTION_TYPES:
        assert canonical in prompt
    assert 'If Q{n} = checked(selected)' in prompt


def test_service_preserves_input_order_under_concurrency() -> None:
    chunks = [_make_chunk(chunk_id=f"p01_c{i:02d}") for i in range(8)]
    extractor = _SlowExtractor(delay=0.05)
    service = ChunkExtractionService(_make_config(max_concurrent=4), extractor=extractor)

    results = service.extract_all(chunks)
    assert [r.chunk_id for r in results] == [c.chunk_id for c in chunks]
    assert extractor.max_active >= 2
    assert extractor.max_active <= 4


def test_service_isolates_per_chunk_errors() -> None:
    chunks = [_make_chunk(chunk_id=f"p01_c{i:02d}") for i in range(3)]
    extractor = _FlakyExtractor(fail_ids={"p01_c01"})
    service = ChunkExtractionService(_make_config(max_concurrent=2), extractor=extractor)

    results = service.extract_all(chunks)
    assert [r.chunk_id for r in results] == [c.chunk_id for c in chunks]
    assert results[0].error is None
    assert results[1].error is not None
    assert "boom for p01_c01" in results[1].error
    assert results[1].rows == []
    assert results[2].error is None


def test_service_with_no_extractor_raises_on_extract_all() -> None:
    service = ChunkExtractionService(_make_config(region=None))
    assert service.extractor is None
    with pytest.raises(RuntimeError):
        service.extract_all([_make_chunk()])


def test_service_with_no_extractor_returns_empty_for_no_chunks() -> None:
    service = ChunkExtractionService(_make_config(region=None))
    assert service.extract_all([]) == []


def test_service_routes_rows_through_fake_extractor() -> None:
    chunk = _make_chunk(chunk_id="p02_c01", pages=[2])
    fake = _FakeExtractor(
        rows_by_chunk={
            "p02_c01": [
                {
                    "question_type": "Radio Button",
                    "question_text": "Are you a US citizen?",
                    "answer_text": "Yes\nNo",
                    "confidence": 0.88,
                },
                {
                    "question_type": "BOGUS",
                    "question_text": "skipped",
                },
            ],
        }
    )
    service = ChunkExtractionService(_make_config(), extractor=fake)
    results = service.extract_all([chunk])
    assert len(results) == 1
    assert len(results[0].rows) == 1
    row = results[0].rows[0]
    assert row.question_type == "Radio Button"
    assert row.chunk_id == "p02_c01"
    assert row.page_numbers == [2]
    assert fake.calls == ["p02_c01"]


def test_chunk_extraction_result_to_dict() -> None:
    chunk = _make_chunk()
    row = _coerce_row(
        {"question_type": "Display", "question_text": "Notice"},
        chunk,
    )
    assert row is not None
    result = ChunkExtractionResult(chunk_id=chunk.chunk_id, rows=[row])
    data = result.to_dict()
    assert data["chunk_id"] == chunk.chunk_id
    assert data["error"] is None
    assert data["rows"][0]["question_type"] == "Display"
