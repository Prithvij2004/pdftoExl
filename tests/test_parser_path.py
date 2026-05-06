from __future__ import annotations

from pathlib import Path

from app.services.complexity_router import ContinuationRouting, PageComplexityRoute
from app.services.layout_parser import (
    BoundingBox,
    RawPageModel,
    RawPdfModel,
    TextBlock,
    TextLine,
    TextSpan,
)
from app.services.docling_parser import DoclingPageMarkdown
from app.services.parser_path import (
    PARSER_PATH_VERSION,
    ParserPath,
    repeated_band_summary,
)
from app.services.semantic_hints import SemanticHintBuilder


class _FakeDoclingParser:
    version = "fake-docling-v1"

    def __init__(self, pages_by_number: dict[int, str]) -> None:
        self.pages_by_number = pages_by_number
        self.calls: list[Path] = []

    def parse(self, pdf_path: Path) -> list[DoclingPageMarkdown]:
        self.calls.append(pdf_path)
        return [
            DoclingPageMarkdown(page_number=page_number, markdown=text)
            for page_number, text in sorted(self.pages_by_number.items())
        ]


def test_parser_path_emits_markdown_and_chunk_seed_for_simple_page() -> None:
    page = _raw_page(
        page_number=1,
        blocks=[
            _block(1, "Demographics", 72, 50, bold=True),
            _block(2, "Please complete this section.", 72, 80),
            _block(3, "Name: __________________", 72, 110),
        ],
    )
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=[page])
    semantic_pages = [SemanticHintBuilder().build_page(page)]
    routes = [_route(1, recommended_path="parser", complexity="simple")]

    artifacts = ParserPath().build_pages(raw_pdf, semantic_pages, routes)

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.parser_version == PARSER_PATH_VERSION
    assert artifact.markdown.startswith("# Page 1")
    assert "## Demographics" in artifact.markdown
    assert "[field]" in artifact.markdown or "field" in artifact.markdown
    assert len(artifact.chunk_seeds) == 1
    seed = artifact.chunk_seeds[0]
    assert seed.chunk_type_hint == "page"
    assert seed.section_hint == "Demographics"


def test_parser_path_skips_pages_routed_to_vlm() -> None:
    page = _raw_page(
        page_number=2,
        blocks=[_block(1, "Body", 72, 50)],
    )
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=[page])
    semantic_pages = [SemanticHintBuilder().build_page(page)]
    routes = [_route(2, recommended_path="vlm", complexity="complex")]

    artifacts = ParserPath().build_pages(raw_pdf, semantic_pages, routes)

    assert artifacts == []


def test_parser_path_detects_repeated_header_and_footer_across_pages() -> None:
    pages = [
        _raw_page(
            page_number=index,
            height=800,
            blocks=[
                _block(1, "ACME Form 1234 — Confidential", 60, 12),
                _block(2, f"Body paragraph {index} mentioning the date.", 60, 200),
                _block(3, f"Page {index} of 3", 60, 770),
            ],
        )
        for index in range(1, 4)
    ]
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=pages)
    semantic_pages = [SemanticHintBuilder().build_page(page) for page in pages]
    routes = [
        _route(page_number, recommended_path="parser", complexity="simple")
        for page_number in (1, 2, 3)
    ]

    artifacts = ParserPath().build_pages(raw_pdf, semantic_pages, routes)

    assert len(artifacts) == 3
    for artifact in artifacts:
        assert artifact.repeated_header_hints
        assert artifact.repeated_footer_hints
        header = artifact.repeated_header_hints[0]
        footer = artifact.repeated_footer_hints[0]
        assert header.text == "ACME Form # — Confidential"
        assert footer.text == "page"
        assert sorted(header.page_numbers) == [1, 2, 3]
        assert "ACME Form 1234 — Confidential" not in artifact.markdown
        assert "Page 1 of 3" not in artifact.markdown

    summary = repeated_band_summary(artifacts)
    assert summary == {"headers": 3, "footers": 3}


def test_parser_path_emits_continuation_hints_and_uses_route_neighbors() -> None:
    pages = [
        _raw_page(
            page_number=1,
            blocks=[
                _block(1, "Section A", 72, 50, bold=True),
                _block(2, "First paragraph of section A continued ", 72, 100),
            ],
        ),
        _raw_page(
            page_number=2,
            blocks=[
                _block(1, "and concluded on this page.", 72, 50),
                _block(2, "Final note.", 72, 100),
            ],
        ),
    ]
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=pages)
    semantic_pages = [SemanticHintBuilder().build_page(page) for page in pages]
    routes = [
        PageComplexityRoute(
            page=1,
            complexity="simple",
            recommended_path="parser",
            uncertainty=0.0,
            reason="continuation forced",
            continuation=ContinuationRouting(from_previous=False, to_next=True),
        ),
        PageComplexityRoute(
            page=2,
            complexity="simple",
            recommended_path="parser",
            uncertainty=0.0,
            reason="continuation forced",
            continuation=ContinuationRouting(from_previous=True, to_next=False),
        ),
    ]

    artifacts = ParserPath().build_pages(raw_pdf, semantic_pages, routes)

    page_one_continuations = artifacts[0].continuation_hints
    page_two_continuations = artifacts[1].continuation_hints

    directions_one = {hint.direction for hint in page_one_continuations}
    directions_two = {hint.direction for hint in page_two_continuations}

    assert "to_next" in directions_one
    assert "from_previous" in directions_two
    to_next = next(hint for hint in page_one_continuations if hint.direction == "to_next")
    assert to_next.neighbor_page == 2
    assert "continued" in to_next.text.lower()


def test_parser_path_chunks_dense_pages_by_section_and_table() -> None:
    page = _raw_page(
        page_number=1,
        blocks=[
            _block(1, "Demographics", 72, 50, bold=True),
            _block(2, "Name: __________", 72, 80),
            _block(3, "Address: __________", 72, 100),
            _block(4, "Service Schedule", 72, 200, bold=True),
            _block(5, "Notes: __________", 72, 300),
        ],
    )
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=[page])
    semantic_pages = [SemanticHintBuilder().build_page(page)]
    routes = [_route(1, recommended_path="parser", complexity="medium")]

    artifacts = ParserPath().build_pages(raw_pdf, semantic_pages, routes)

    seeds = artifacts[0].chunk_seeds
    assert len(seeds) >= 2
    section_hints = [seed.section_hint for seed in seeds]
    assert "Demographics" in section_hints
    assert "Service Schedule" in section_hints
    assert all(seed.seed_id.startswith("p01_seed") for seed in seeds)


def test_parser_path_excludes_repeated_band_block_ids_from_markdown() -> None:
    pages = [
        _raw_page(
            page_number=index,
            height=400,
            blocks=[
                _block(1, "Confidential Header", 60, 8),
                _block(2, f"Real content {index}", 60, 150),
            ],
        )
        for index in (1, 2)
    ]
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=pages)
    semantic_pages = [SemanticHintBuilder().build_page(page) for page in pages]
    routes = [_route(page_number, recommended_path="parser") for page_number in (1, 2)]

    artifacts = ParserPath().build_pages(raw_pdf, semantic_pages, routes)

    for artifact in artifacts:
        assert artifact.excluded_source_ids
        assert "Confidential Header" not in artifact.markdown


def test_parser_path_uses_docling_markdown_when_parser_injected() -> None:
    page = _raw_page(
        page_number=1,
        blocks=[
            _block(1, "Demographics", 72, 50, bold=True),
            _block(2, "Name: __________________", 72, 110),
        ],
    )
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=[page])
    semantic_pages = [SemanticHintBuilder().build_page(page)]
    routes = [_route(1, recommended_path="parser", complexity="simple")]

    docling = _FakeDoclingParser({1: "## Docling-Demographics\n\nName: ____\n"})
    artifacts = ParserPath(docling_parser=docling).build_pages(
        raw_pdf, semantic_pages, routes
    )

    assert docling.calls == [Path("synthetic.pdf")]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.parser_version == "fake-docling-v1"
    assert artifact.markdown.startswith("# Page 1")
    assert "Docling-Demographics" in artifact.markdown
    assert "[field]" not in artifact.markdown


def test_parser_path_skips_docling_when_no_parser_routes() -> None:
    page = _raw_page(page_number=1, blocks=[_block(1, "Body", 72, 50)])
    raw_pdf = RawPdfModel(pdf_path=Path("synthetic.pdf"), parser_version="test", pages=[page])
    semantic_pages = [SemanticHintBuilder().build_page(page)]
    routes = [_route(1, recommended_path="vlm", complexity="complex")]

    docling = _FakeDoclingParser({})
    artifacts = ParserPath(docling_parser=docling).build_pages(
        raw_pdf, semantic_pages, routes
    )

    assert artifacts == []
    assert docling.calls == []


def _raw_page(
    *,
    page_number: int,
    blocks: list[TextBlock],
    width: float = 612,
    height: float = 792,
) -> RawPageModel:
    spans: list[TextSpan] = []
    lines: list[TextLine] = []
    for block in blocks:
        line_id = block.line_ids[0]
        span_id = f"p{page_number}_s{line_id[-4:]}"
        is_bold = block.id.endswith("bold")
        spans.append(
            TextSpan(
                id=span_id,
                text=block.text,
                bbox=block.bbox,
                font="Helvetica-Bold" if is_bold else "Helvetica",
                size=14 if is_bold else 10,
                flags=16 if is_bold else 0,
                color=None,
                is_bold=is_bold,
                is_italic=False,
            )
        )
        lines.append(
            TextLine(
                id=line_id,
                text=block.text,
                bbox=block.bbox,
                span_ids=[span_id],
            )
        )
    return RawPageModel(
        page_number=page_number,
        width=width,
        height=height,
        parser_version="test",
        spans=spans,
        lines=lines,
        blocks=blocks,
        widgets=[],
        choice_glyph_candidates=[],
        table_candidates=[],
    )


def _block(index: int, text: str, x0: float, y0: float, *, bold: bool = False) -> TextBlock:
    suffix = "bold" if bold else ""
    return TextBlock(
        id=f"p1_b{index:04d}{suffix}",
        text=text,
        bbox=BoundingBox(x0=x0, y0=y0, x1=x0 + 320, y1=y0 + 14),
        line_ids=[f"p1_l{index:04d}"],
        block_type="text",
    )


def _route(
    page_number: int,
    *,
    recommended_path: str = "parser",
    complexity: str = "simple",
) -> PageComplexityRoute:
    return PageComplexityRoute(
        page=page_number,
        complexity=complexity,
        recommended_path=recommended_path,
        uncertainty=0.0,
        reason="synthetic",
        continuation=ContinuationRouting(from_previous=False, to_next=False),
    )
