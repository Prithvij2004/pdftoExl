from __future__ import annotations

from pathlib import Path

from app.config import PipelineConfig
from app.services.complexity_router import (
    BedrockQwenComplexityClassifier,
    ComplexityRouter,
    ContinuationRouting,
    PageComplexityRoute,
    VlmPageClassification,
)
from app.services.layout_parser import (
    BoundingBox,
    ChoiceGlyphCandidate,
    RawPageModel,
    TextBlock,
)
from app.services.page_preparation import PreparedPage


def _config(tmp_path: Path, threshold: float = 0.15) -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=72,
        complexity_uncertain_threshold=threshold,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=tmp_path / "cache",
    )


def _page(
    *,
    page_number: int = 1,
    blocks: list[TextBlock] | None = None,
    choices: list[ChoiceGlyphCandidate] | None = None,
) -> RawPageModel:
    return RawPageModel(
        page_number=page_number,
        width=600,
        height=800,
        parser_version="test",
        spans=[],
        lines=[],
        blocks=blocks or [],
        widgets=[],
        choice_glyph_candidates=choices or [],
        table_candidates=[],
    )


def _block(index: int, text: str, x0: float = 48, y0: float | None = None) -> TextBlock:
    top = y0 if y0 is not None else 60 + index * 24
    return TextBlock(
        id=f"p1_b{index:04d}",
        text=text,
        bbox=BoundingBox(x0=x0, y0=top, x1=x0 + 250, y1=top + 14),
        line_ids=[f"p1_l{index:04d}"],
        block_type="text",
    )


def _prepared_page(page_number: int = 1) -> PreparedPage:
    path = Path(__file__)
    return PreparedPage(
        page_number=page_number,
        width=600,
        height=800,
        image_path=path,
        page_hash="hash",
        image_format="png",
        render_dpi=72,
        cache_key="cache",
    )


def test_complexity_router_routes_regular_text_to_parser(tmp_path: Path) -> None:
    page = _page(
        blocks=[
            _block(1, "Section One"),
            _block(2, "Name: ____________________"),
            _block(3, "Date: ____________________"),
            _block(4, "Please complete this section."),
        ]
    )

    route = ComplexityRouter(_config(tmp_path)).route_page(page, _prepared_page())

    assert route.complexity == "simple"
    assert route.recommended_path == "parser"
    assert route.used_vlm is False
    assert route.to_dict()["continuation"] == {
        "from_previous": False,
        "to_next": False,
    }


def test_complexity_router_marks_unlabeled_empty_page_complex(tmp_path: Path) -> None:
    choices = [
        ChoiceGlyphCandidate(
            id=f"p1_d{index:04d}",
            glyph="box",
            bbox=BoundingBox(40, 40 + index * 20, 52, 52 + index * 20),
            source="drawing_shape",
            nearest_text=None,
            confidence=0.55,
        )
        for index in range(14)
    ]
    page = _page(choices=choices)

    route = ComplexityRouter(_config(tmp_path)).route_page(page, _prepared_page())

    assert route.complexity == "complex"
    assert route.recommended_path == "vlm"
    assert route.reason in {
        "low extracted text density",
        "many unlabeled boxes or glyphs",
    }


def test_complexity_router_calls_vlm_when_uncertain(tmp_path: Path) -> None:
    class FakeClassifier:
        def __init__(self) -> None:
            self.calls: list[PageComplexityRoute] = []

        def classify_page(
            self,
            **kwargs,
        ) -> VlmPageClassification:  # type: ignore[no-untyped-def]
            self.calls.append(kwargs["deterministic_route"])
            return VlmPageClassification(
                complexity="complex",
                recommended_path="vlm",
                uncertainty=0.07,
                reason="vision classifier detected dense visual grid",
                continuation=ContinuationRouting(from_previous=False, to_next=True),
            )

    classifier = FakeClassifier()
    page = _page(
        blocks=[
            _block(1, "Left column question", x0=40, y0=80),
            _block(2, "Left column detail", x0=45, y0=120),
            _block(3, "Right column question", x0=350, y0=82),
            _block(4, "Right column detail", x0=355, y0=122),
            _block(5, "More left text", x0=44, y0=160),
            _block(6, "More right text", x0=354, y0=162),
        ]
    )

    route = ComplexityRouter(
        _config(tmp_path, threshold=0.0),
        vlm_classifier=classifier,
    ).route_page(page, _prepared_page())

    assert classifier.calls
    assert route.used_vlm is True
    assert route.complexity == "complex"
    assert route.recommended_path == "vlm"
    assert route.continuation.to_next is True


def test_bedrock_qwen_classifier_returns_pydantic_ai_output(tmp_path: Path) -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    expected = VlmPageClassification(
        complexity="medium",
        recommended_path="parser",
        uncertainty=0.04,
        reason="clear text with simple table",
        continuation=ContinuationRouting(from_previous=False, to_next=False),
    )
    test_agent = Agent(
        TestModel(custom_output_args=expected.model_dump()),
        output_type=VlmPageClassification,
    )

    route = PageComplexityRoute(
        page=1,
        complexity="medium",
        recommended_path="parser",
        uncertainty=0.2,
        reason="multi-column layout",
        continuation=ContinuationRouting(from_previous=False, to_next=False),
    )

    classification = BedrockQwenComplexityClassifier(agent=test_agent).classify_page(
        page=_page(blocks=[_block(1, "Name")]),
        prepared_page=_prepared_page(),
        deterministic_route=route,
        config=_config(tmp_path),
    )

    assert classification.complexity == "medium"
    assert classification.recommended_path == "parser"
    assert classification.reason == "clear text with simple table"
