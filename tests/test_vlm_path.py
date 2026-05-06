from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import PipelineConfig
from app.services.complexity_router import ContinuationRouting, PageComplexityRoute
from app.services.layout_parser import (
    BoundingBox,
    RawPageModel,
    RawPdfModel,
    TextBlock,
    TextLine,
    TextSpan,
)
from app.services.page_preparation import PreparedPage, PreparedPdf
from app.services.pipeline import CurrentExtractionPipeline
from app.services.vlm_path import (
    VLM_PATH_VERSION,
    PageVlmArtifact,
    VlmPath,
    vlm_artifact_summary,
)


class _StubExtractor:
    def __init__(self, payload_by_page: dict[int, dict[str, Any]]) -> None:
        self.payload_by_page = payload_by_page
        self.calls: list[int] = []

    def extract_page(self, *, page, prepared_page, route, config) -> dict[str, Any]:
        self.calls.append(page.page_number)
        return self.payload_by_page[page.page_number]


def test_vlm_path_skips_parser_routed_pages(tmp_path: Path) -> None:
    raw_pdf = RawPdfModel(
        pdf_path=Path("synthetic.pdf"),
        parser_version="test",
        pages=[_raw_page(1)],
    )
    prepared = _prepared_pdf(tmp_path, [1])
    routes = [_route(1, recommended_path="parser")]

    extractor = _StubExtractor({})
    artifacts = VlmPath(_config(tmp_path), extractor=extractor).build_pages(
        raw_pdf, prepared, routes
    )

    assert artifacts == []
    assert extractor.calls == []


def test_vlm_path_normalizes_payload_into_semantic_page(tmp_path: Path) -> None:
    raw_pdf = RawPdfModel(
        pdf_path=Path("synthetic.pdf"),
        parser_version="test",
        pages=[_raw_page(2)],
    )
    prepared = _prepared_pdf(tmp_path, [2])
    routes = [
        PageComplexityRoute(
            page=2,
            complexity="complex",
            recommended_path="vlm",
            uncertainty=0.6,
            reason="dense",
            continuation=ContinuationRouting(from_previous=True, to_next=False),
        )
    ]
    payload = {
        "markdown": "# Page 2\n\n## Medical\n\n- [ ] Hospital records attached",
        "structure": {
            "hints": [
                {
                    "id": "p2_v001",
                    "role": "section_heading",
                    "text": "Medical",
                    "indent_level": 0,
                    "column": 0,
                    "source_ids": ["p2_b1"],
                },
                {
                    "id": "p2_v002",
                    "role": "choice_option",
                    "text": "Hospital records attached",
                    "indent_level": 1,
                    "column": 0,
                    "group_candidate": "g1",
                    "source_ids": ["p2_b2"],
                },
                {
                    "role": "bogus_role",
                    "text": "Should be coerced to unknown",
                    "indent_level": "x",
                    "column": -3,
                },
            ],
            "chunk_seeds": [
                {
                    "seed_id": "p2_vseed01",
                    "chunk_type_hint": "checkbox_followups",
                    "section_hint": "Medical",
                    "source_block_ids": ["p2_b1", "p2_b2"],
                }
            ],
        },
        "continuation": {"from_previous": True, "to_next": False},
        "repeated": {
            "headers": [{"text": "ACME Form", "source_ids": ["p2_b0"]}],
            "footers": ["Page #"],
        },
    }

    extractor = _StubExtractor({2: payload})
    artifacts = VlmPath(_config(tmp_path), extractor=extractor).build_pages(
        raw_pdf, prepared, routes
    )

    assert extractor.calls == [2]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert isinstance(artifact, PageVlmArtifact)
    assert artifact.parser_version == VLM_PATH_VERSION
    assert artifact.markdown.startswith("# Page 2")
    assert artifact.markdown.endswith("\n")

    semantic = artifact.semantic_page
    roles = [hint.role for hint in semantic.hints]
    assert roles == ["section_heading", "choice_option", "unknown"]
    coerced = semantic.hints[2]
    assert coerced.indent_level == 0
    assert coerced.column == 0
    choice = semantic.hints[1]
    assert choice.nearby_heading == "Medical"
    assert choice.group_candidate == "g1"

    directions = {hint.direction for hint in artifact.continuation_hints}
    assert directions == {"from_previous"}

    assert artifact.repeated_header_hints[0].text == "ACME Form"
    assert artifact.repeated_footer_hints[0].text == "Page #"

    assert len(artifact.chunk_seeds) == 1
    assert artifact.chunk_seeds[0].chunk_type_hint == "checkbox_followups"
    assert artifact.chunk_seeds[0].section_hint == "Medical"

    summary = vlm_artifact_summary(artifacts)
    assert summary == {"pages": 1, "hints": 3, "chunk_seeds": 1}


def test_vlm_path_falls_back_when_payload_is_sparse(tmp_path: Path) -> None:
    raw_pdf = RawPdfModel(
        pdf_path=Path("synthetic.pdf"),
        parser_version="test",
        pages=[_raw_page(3)],
    )
    prepared = _prepared_pdf(tmp_path, [3])
    routes = [_route(3, recommended_path="vlm", complexity="complex")]
    extractor = _StubExtractor({3: {}})

    artifacts = VlmPath(_config(tmp_path), extractor=extractor).build_pages(
        raw_pdf, prepared, routes
    )

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.markdown == "# Page 3\n"
    assert artifact.semantic_page.hints == []
    assert artifact.continuation_hints == []
    assert artifact.repeated_header_hints == []
    assert artifact.chunk_seeds == []


def test_pipeline_replaces_semantic_page_for_vlm_routes(tmp_path: Path) -> None:
    pipeline = CurrentExtractionPipeline(
        _config(tmp_path),
        complexity_router=_StubRouter([
            _route(1, recommended_path="vlm", complexity="complex"),
        ]),
        vlm_extractor=_StubExtractor({
            1: {
                "markdown": "# Page 1\n",
                "structure": {
                    "hints": [
                        {
                            "id": "p1_v001",
                            "role": "section_heading",
                            "text": "Demographics",
                            "indent_level": 0,
                            "column": 0,
                        }
                    ]
                },
            }
        }),
    )
    result = pipeline.run(Path("docs/sample-input-1.pdf"))

    assert result.vlm_artifacts
    assert result.parser_artifacts == []
    semantic_for_page_one = next(
        page for page in result.semantic_pages if page.page_number == 1
    )
    assert semantic_for_page_one.parser_version == VLM_PATH_VERSION
    assert any(hint.role == "section_heading" for hint in semantic_for_page_one.hints)


class _StubRouter:
    def __init__(self, routes: list[PageComplexityRoute]) -> None:
        self.routes = routes

    def route_pages(self, pages, prepared_pages):
        return [self.routes[0] for _ in pages[:1]] + [
            _route(page.page_number, recommended_path="parser")
            for page in pages[1:]
        ]


def _config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=72,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=tmp_path / "cache",
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


def _raw_page(page_number: int) -> RawPageModel:
    block = TextBlock(
        id=f"p{page_number}_b0001",
        text="Body",
        bbox=BoundingBox(x0=72, y0=80, x1=400, y1=94),
        line_ids=[f"p{page_number}_l0001"],
        block_type="text",
    )
    span = TextSpan(
        id=f"p{page_number}_s0001",
        text="Body",
        bbox=block.bbox,
        font="Helvetica",
        size=10,
        flags=0,
        color=None,
        is_bold=False,
        is_italic=False,
    )
    line = TextLine(
        id=f"p{page_number}_l0001",
        text="Body",
        bbox=block.bbox,
        span_ids=[span.id],
    )
    return RawPageModel(
        page_number=page_number,
        width=612,
        height=792,
        parser_version="test",
        spans=[span],
        lines=[line],
        blocks=[block],
        widgets=[],
        choice_glyph_candidates=[],
        table_candidates=[],
    )


def _prepared_pdf(tmp_path: Path, pages: list[int]) -> PreparedPdf:
    prepared_pages: list[PreparedPage] = []
    for page_number in pages:
        image_path = tmp_path / f"page-{page_number}.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        prepared_pages.append(
            PreparedPage(
                page_number=page_number,
                width=612,
                height=792,
                image_path=image_path,
                page_hash=f"hash{page_number}",
                image_format="png",
                render_dpi=72,
                cache_key=f"key{page_number}",
            )
        )
    return PreparedPdf(
        pdf_path=Path("synthetic.pdf"),
        pdf_hash="pdfhash",
        render_dpi=72,
        renderer_version="test",
        pages=prepared_pages,
    )
