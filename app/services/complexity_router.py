from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import BinaryContent

from app.config import PipelineConfig
from app.services.bedrock_agent import build_bedrock_agent
from app.services.layout_parser import BoundingBox, RawPageModel, TextBlock, TextSpan
from app.services.page_preparation import PreparedPage


Complexity = Literal["simple", "medium", "complex"]
RecommendedPath = Literal["parser", "vlm"]


class ContinuationRouting(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_previous: bool = Field(description="True when this page continues content from the previous page.")
    to_next: bool = Field(description="True when this page continues into the next page.")

    def to_dict(self) -> dict[str, bool]:
        return {"from_previous": self.from_previous, "to_next": self.to_next}


class PageComplexityRoute(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: int = Field(description="1-based page number being routed.")
    complexity: str = Field(description="Page complexity tier: simple, medium, or complex.")
    recommended_path: str = Field(description="Pipeline path to use for the page: 'parser' or 'vlm'.")
    uncertainty: float = Field(description="Confidence-adjusted score in [0, 1] indicating how borderline the routing is.")
    reason: str = Field(description="Human-readable explanation of the dominant routing signal.")
    continuation: ContinuationRouting = Field(description="Whether the page bleeds into its neighbours.")
    deterministic_signals: dict[str, Any] = Field(default_factory=dict, description="Per-signal scores produced by the deterministic router.")
    used_vlm: bool = Field(default=False, description="True when the VLM classifier overrode the deterministic decision.")

    @field_validator("complexity")
    @classmethod
    def _validate_complexity(cls, value: str) -> str:
        if value not in {"simple", "medium", "complex"}:
            raise ValueError(f"Unknown complexity: {value}")
        return value

    @field_validator("recommended_path")
    @classmethod
    def _validate_recommended_path(cls, value: str) -> str:
        if value not in {"parser", "vlm"}:
            raise ValueError(f"Unknown recommended path: {value}")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "complexity": self.complexity,
            "recommended_path": self.recommended_path,
            "uncertainty": round(self.uncertainty, 3),
            "reason": self.reason,
            "continuation": self.continuation.to_dict(),
            "deterministic_signals": self.deterministic_signals,
            "used_vlm": self.used_vlm,
        }


class VlmPageClassification(BaseModel):
    """Schema returned by the VLM-based page complexity classifier."""

    model_config = ConfigDict(frozen=True)

    complexity: Complexity = Field(description="Page complexity tier: simple, medium, or complex.")
    recommended_path: RecommendedPath = Field(description="Pipeline path to use for the page: parser for clear text, vlm for image-heavy or ambiguous layouts.")
    uncertainty: float = Field(description="Confidence in the routing decision in [0, 1]; closer to 0 means more confident.")
    reason: str = Field(description="Short human-readable justification for the chosen path.")
    continuation: ContinuationRouting | None = Field(default=None, description="Whether the page continues content from or into its neighbours.")


class VlmComplexityClassifier(Protocol):
    def classify_page(
        self,
        *,
        page: RawPageModel,
        prepared_page: PreparedPage,
        deterministic_route: PageComplexityRoute,
        config: PipelineConfig,
    ) -> VlmPageClassification:
        """Classify a page with its rendered image when deterministic routing is uncertain."""


_VLM_ROUTER_SYSTEM_PROMPT = (
    "You classify rendered PDF form pages for downstream extraction routing. "
    "Return a structured object describing complexity, recommended pipeline path, "
    "uncertainty, a short reason, and whether the page continues to or from its "
    "neighbours. Prefer parser for clear text forms; prefer vlm for image-heavy "
    "pages, unclear reading order, dense visual tables, unlabeled controls, or "
    "low text-extraction confidence."
)


class BedrockQwenComplexityClassifier:
    def __init__(
        self,
        client: Any | None = None,
        *,
        agent: Any | None = None,
    ) -> None:
        self.client = client
        self.agent = agent

    def classify_page(
        self,
        *,
        page: RawPageModel,
        prepared_page: PreparedPage,
        deterministic_route: PageComplexityRoute,
        config: PipelineConfig,
    ) -> VlmPageClassification:
        agent = self.agent
        if agent is None:
            if config.bedrock_region is None and self.client is None:
                raise ValueError("BEDROCK_REGION is required for Bedrock VLM routing.")
            agent = build_bedrock_agent(
                model_id=config.bedrock_vision_model_id,
                region=config.bedrock_region,
                output_type=VlmPageClassification,
                system_prompt=_VLM_ROUTER_SYSTEM_PROMPT,
                bedrock_client=self.client,
                max_tokens=512,
            )
        result = agent.run_sync(
            [
                _vlm_router_prompt(page, deterministic_route),
                BinaryContent(
                    data=prepared_page.image_path.read_bytes(),
                    media_type=f"image/{prepared_page.image_format}",
                ),
            ]
        )
        return result.output


class ComplexityRouter:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        vlm_classifier: VlmComplexityClassifier | None = None,
    ) -> None:
        self.config = config
        self.vlm_classifier = vlm_classifier

    def route_pages(
        self,
        pages: list[RawPageModel],
        prepared_pages: list[PreparedPage],
    ) -> list[PageComplexityRoute]:
        prepared_by_number = {page.page_number: page for page in prepared_pages}
        routes: list[PageComplexityRoute] = []
        for index, page in enumerate(pages):
            previous_page = pages[index - 1] if index else None
            next_page = pages[index + 1] if index + 1 < len(pages) else None
            route = self.route_page(
                page,
                prepared_by_number[page.page_number],
                previous_page=previous_page,
                next_page=next_page,
            )
            routes.append(route)
        return routes

    def route_page(
        self,
        page: RawPageModel,
        prepared_page: PreparedPage,
        *,
        previous_page: RawPageModel | None = None,
        next_page: RawPageModel | None = None,
    ) -> PageComplexityRoute:
        deterministic_route = _deterministic_route(page, previous_page, next_page)
        if (
            deterministic_route.uncertainty
            <= self.config.complexity_uncertain_threshold
            or self.vlm_classifier is None
        ):
            return deterministic_route

        classification = self.vlm_classifier.classify_page(
            page=page,
            prepared_page=prepared_page,
            deterministic_route=deterministic_route,
            config=self.config,
        )
        return PageComplexityRoute(
            page=page.page_number,
            complexity=classification.complexity,
            recommended_path=classification.recommended_path,
            uncertainty=_clamp(classification.uncertainty),
            reason=classification.reason,
            continuation=classification.continuation or deterministic_route.continuation,
            deterministic_signals=deterministic_route.deterministic_signals,
            used_vlm=True,
        )


def _deterministic_route(
    page: RawPageModel,
    previous_page: RawPageModel | None,
    next_page: RawPageModel | None,
) -> PageComplexityRoute:
    signals = _score_signals(page, previous_page, next_page)
    weighted_score = (
        signals["empty_or_low_text_density"] * 0.24
        + signals["abnormal_reading_order"] * 0.16
        + signals["overlapping_spans"] * 0.14
        + signals["multi_column_layout"] * 0.12
        + signals["complex_table_grid"] * 0.16
        + signals["unlabeled_boxes"] * 0.10
        + signals["parser_disagreement"] * 0.12
        + signals["page_boundary_continuation"] * 0.06
    )
    weighted_score = _clamp(weighted_score)

    force_visual_path = (
        signals["empty_or_low_text_density"] >= 0.95
        and signals["unlabeled_boxes"] >= 0.75
    )

    if weighted_score >= 0.62 or force_visual_path:
        complexity = "complex"
        recommended_path = "vlm"
    elif weighted_score >= 0.34:
        complexity = "medium"
        recommended_path = "parser"
    else:
        complexity = "simple"
        recommended_path = "parser"

    uncertainty = _uncertainty(weighted_score, signals)
    continuation = ContinuationRouting(
        from_previous=_continues_from_previous(page, previous_page),
        to_next=_continues_to_next(page, next_page),
    )
    reason = _reason(signals)
    return PageComplexityRoute(
        page=page.page_number,
        complexity=complexity,
        recommended_path=recommended_path,
        uncertainty=uncertainty,
        reason=reason,
        continuation=continuation,
        deterministic_signals={key: round(value, 3) for key, value in signals.items()},
    )


def _vlm_router_prompt(
    page: RawPageModel,
    deterministic_route: PageComplexityRoute,
) -> str:
    return (
        f"Classify PDF form page {page.page_number} for extraction routing.\n"
        f"Deterministic route: {json.dumps(deterministic_route.to_dict())}\n"
    )


def _score_signals(
    page: RawPageModel,
    previous_page: RawPageModel | None,
    next_page: RawPageModel | None,
) -> dict[str, float]:
    text_chars = sum(len(line.text.strip()) for line in page.lines)
    page_area = max(page.width * page.height, 1)
    density = text_chars / (page_area / 1000)
    return {
        "empty_or_low_text_density": _clamp((0.45 - density) / 0.45),
        "abnormal_reading_order": _reading_order_score(page.blocks),
        "overlapping_spans": _overlap_score(page.spans),
        "multi_column_layout": _multi_column_score(page.blocks, page.width),
        "complex_table_grid": _table_score(page),
        "unlabeled_boxes": _unlabeled_box_score(page),
        "parser_disagreement": _parser_disagreement_score(page),
        "page_boundary_continuation": 1.0
        if _continues_from_previous(page, previous_page) or _continues_to_next(page, next_page)
        else 0.0,
    }


def _reading_order_score(blocks: list[TextBlock]) -> float:
    text_blocks = [block for block in blocks if block.text.strip()]
    if len(text_blocks) < 4:
        return 0.0
    inversions = 0
    for left, right in zip(text_blocks, text_blocks[1:]):
        if right.bbox.y0 + 6 < left.bbox.y0:
            inversions += 1
        elif abs(right.bbox.y0 - left.bbox.y0) <= 6 and right.bbox.x0 + 8 < left.bbox.x0:
            inversions += 1
    return _clamp(inversions / max(len(text_blocks) - 1, 1) * 2)


def _overlap_score(spans: list[TextSpan]) -> float:
    if len(spans) < 2:
        return 0.0
    overlaps = 0
    comparisons = 0
    sorted_spans = sorted(spans, key=lambda span: (span.bbox.y0, span.bbox.x0))
    for index, span in enumerate(sorted_spans):
        for other in sorted_spans[index + 1 : index + 8]:
            if other.bbox.y0 > span.bbox.y1 + 4:
                break
            comparisons += 1
            if _overlap_area(span.bbox, other.bbox) > 0:
                overlaps += 1
    if comparisons == 0:
        return 0.0
    return _clamp(overlaps / comparisons * 3)


def _multi_column_score(blocks: list[TextBlock], page_width: float) -> float:
    text_blocks = [block for block in blocks if block.text.strip()]
    if len(text_blocks) < 6 or page_width <= 0:
        return 0.0
    block_columns = [_third_column(block.bbox, page_width) for block in text_blocks]
    columns = set(block_columns)
    balanced_columns = len(columns) >= 2 and all(
        sum(1 for block_column in block_columns if block_column == column) >= 2
        for column in columns
    )
    return 1.0 if balanced_columns else 0.0


def _third_column(bbox: BoundingBox, page_width: float) -> int:
    midpoint = (bbox.x0 + bbox.x1) / 2
    return int(midpoint // (page_width / 3))


def _table_score(page: RawPageModel) -> float:
    if not page.table_candidates:
        return 0.0
    strongest = 0.0
    for table in page.table_candidates:
        rows = table.row_count or 0
        columns = table.column_count or 0
        strongest = max(strongest, _clamp(((rows * columns) - 8) / 24))
    return strongest


def _unlabeled_box_score(page: RawPageModel) -> float:
    boxes = [
        candidate
        for candidate in page.choice_glyph_candidates
        if not candidate.nearest_text
    ]
    widgets = [
        widget
        for widget in page.widgets
        if not (widget.field_label or widget.field_name)
    ]
    unlabeled = len(boxes) + len(widgets)
    return _clamp(unlabeled / 12)


def _parser_disagreement_score(page: RawPageModel) -> float:
    box_count = len(page.choice_glyph_candidates) + len(page.widgets)
    labelled_choice_lines = sum(
        1
        for line in page.lines
        if any(
            word in line.text.lower()
            for word in ("yes", "no", "check", "select")
        )
    )
    table_blocks = sum(
        1
        for block in page.blocks
        if "|" in block.text or "\t" in block.text
    )
    disagreement = 0
    if box_count >= 6 and labelled_choice_lines == 0:
        disagreement += 1
    if page.table_candidates and table_blocks == 0 and len(page.lines) > 35:
        disagreement += 1
    if page.widgets and len(page.blocks) < max(2, len(page.widgets) // 3):
        disagreement += 1
    return _clamp(disagreement / 2)


def _continues_from_previous(page: RawPageModel, previous_page: RawPageModel | None) -> bool:
    if previous_page is None:
        return False
    first_text = _first_text(page)
    return first_text.startswith(("continued", "cont.", ")")) or first_text[
        :1
    ].islower()


def _continues_to_next(page: RawPageModel, next_page: RawPageModel | None) -> bool:
    if next_page is None:
        return False
    last_text = _last_text(page)
    return last_text.endswith((",", ";", ":", "and", "or")) or (
        "continued" in last_text.lower()
    )


def _first_text(page: RawPageModel) -> str:
    for block in sorted(page.blocks, key=lambda block: (block.bbox.y0, block.bbox.x0)):
        if block.text.strip():
            return block.text.strip()
    return ""


def _last_text(page: RawPageModel) -> str:
    sorted_blocks = sorted(page.blocks, key=lambda block: (block.bbox.y0, block.bbox.x0))
    for block in reversed(sorted_blocks):
        if block.text.strip():
            return block.text.strip()
    return ""


def _uncertainty(weighted_score: float, signals: dict[str, float]) -> float:
    boundaries = (0.34, 0.62)
    boundary_distance = min(abs(weighted_score - boundary) for boundary in boundaries)
    boundary_uncertainty = _clamp((0.14 - boundary_distance) / 0.14)
    mean_signal = sum(signals.values()) / len(signals)
    mixed_signals = 1.0 if 0.2 <= mean_signal <= 0.55 else 0.0
    return _clamp(boundary_uncertainty * 0.75 + mixed_signals * 0.25)


def _reason(signals: dict[str, float]) -> str:
    labels = {
        "empty_or_low_text_density": "low extracted text density",
        "abnormal_reading_order": "unclear reading order",
        "overlapping_spans": "overlapping text spans",
        "multi_column_layout": "multi-column layout",
        "complex_table_grid": "dense table/grid structure",
        "unlabeled_boxes": "many unlabeled boxes or glyphs",
        "parser_disagreement": "parser signal disagreement",
        "page_boundary_continuation": "possible page-boundary continuation",
    }
    strongest = max(signals, key=signals.get)
    if signals[strongest] <= 0:
        return "regular text layout with stable parser signals"
    return labels[strongest]


def _overlap_area(left: BoundingBox, right: BoundingBox) -> float:
    width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    return width * height


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
