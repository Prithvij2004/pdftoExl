from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import BinaryContent

from app.config import PipelineConfig
from app.services.bedrock_agent import build_bedrock_agent
from app.services.complexity_router import PageComplexityRoute
from app.services.layout_parser import RawPageModel, RawPdfModel
from app.services.page_preparation import PreparedPage, PreparedPdf
from app.services.parser_path import (
    ChunkSeed,
    ContinuationHint,
    RepeatedBandHint,
)
from app.services.semantic_hints import (
    SEMANTIC_ROLES,
    SemanticHint,
    SemanticPageModel,
)


VLM_PATH_VERSION = "vlm-path-v1"


SemanticRoleLiteral = Literal[
    "section_heading",
    "instruction",
    "question_stem",
    "blank_field",
    "choice_option",
    "choice_group",
    "followup_field",
    "table",
    "table_column",
    "signature_block",
    "repeated_header",
    "repeated_footer",
    "unknown",
]


class VlmHintPayload(BaseModel):
    """One semantic hint produced by the VLM page extractor."""

    model_config = ConfigDict(frozen=True)

    id: str | None = Field(default=None, description="Stable identifier; auto-assigned downstream when absent.")
    role: SemanticRoleLiteral = Field(description="Semantic role of the block.")
    text: str = Field(description="Visible text for the hint.")
    source_ids: list[str] = Field(default_factory=list, description="Layout block IDs backing this hint, when known.")
    indent_level: int = Field(default=0, description="Heuristic indent level (0–12) preserving hierarchy.")
    column: int = Field(default=0, description="0-based column index for multi-column layouts.")
    nearby_heading: str | None = Field(default=None, description="Nearest section heading text, when applicable.")
    field_shape: str | None = Field(default=None, description="Shape descriptor for fields (single_line_blank, checkbox_or_radio, etc.).")
    possible_parent: str | None = Field(default=None, description="Parent hint identifier (e.g. table id) when applicable.")
    group_candidate: str | None = Field(default=None, description="Synthetic group id for related choice options.")


class VlmChunkSeedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    seed_id: str | None = Field(default=None, description="Stable seed identifier; auto-assigned downstream when absent.")
    chunk_type_hint: str = Field(default="page", description="Coarse chunk kind: page, section, table, checkbox_followups, questions, etc.")
    section_hint: str | None = Field(default=None, description="Section heading the seed sits under, if known.")
    source_block_ids: list[str] = Field(default_factory=list, description="Layout block IDs assigned to this seed.")


class VlmStructurePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    hints: list[VlmHintPayload] = Field(default_factory=list, description="Semantic hints describing the page in reading order.")
    chunk_seeds: list[VlmChunkSeedPayload] = Field(default_factory=list, description="Chunk seeds proposed for downstream chunking.")


class VlmContinuationPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_previous: bool = Field(default=False, description="True when the page continues content from the previous page.")
    to_next: bool = Field(default=False, description="True when the page continues into the next page.")


class VlmRepeatedBandPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(description="Visible text of the repeated band.")
    source_ids: list[str] = Field(default_factory=list, description="Source IDs backing the band, when known.")


class VlmRepeatedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    headers: list[VlmRepeatedBandPayload] = Field(default_factory=list, description="Repeated header bands.")
    footers: list[VlmRepeatedBandPayload] = Field(default_factory=list, description="Repeated footer bands.")


class VlmPagePayload(BaseModel):
    """Top-level schema returned by the VLM page extractor."""

    model_config = ConfigDict(frozen=True)

    markdown: str = Field(description="Clean Markdown for the visible page (no raw coordinates).")
    structure: VlmStructurePayload = Field(default_factory=VlmStructurePayload, description="Hints and chunk seeds for the page.")
    continuation: VlmContinuationPayload = Field(default_factory=VlmContinuationPayload, description="Page continuation flags.")
    repeated: VlmRepeatedPayload = Field(default_factory=VlmRepeatedPayload, description="Repeated visual bands.")


class PageVlmArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int = Field(description="1-based page number.")
    parser_version: str = Field(description="Identifier of the VLM path version that produced this artefact.")
    markdown: str = Field(description="Per-page Markdown for downstream chunking.")
    semantic_page: SemanticPageModel = Field(description="Normalised semantic page model produced from the VLM output.")
    continuation_hints: list[ContinuationHint] = Field(description="Continuation hints linking to neighbours.")
    repeated_header_hints: list[RepeatedBandHint] = Field(description="Repeated header bands detected by the VLM.")
    repeated_footer_hints: list[RepeatedBandHint] = Field(description="Repeated footer bands detected by the VLM.")
    chunk_seeds: list[ChunkSeed] = Field(description="Chunk seeds produced for the chunker.")
    raw_response: dict[str, Any] = Field(default_factory=dict, description="Raw normalised payload returned by the extractor.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "parser_version": self.parser_version,
            "markdown": self.markdown,
            "semantic_page": self.semantic_page.to_dict(),
            "continuation_hints": [hint.to_dict() for hint in self.continuation_hints],
            "repeated_header_hints": [hint.to_dict() for hint in self.repeated_header_hints],
            "repeated_footer_hints": [hint.to_dict() for hint in self.repeated_footer_hints],
            "chunk_seeds": [seed.to_dict() for seed in self.chunk_seeds],
        }


class VlmPageExtractor(Protocol):
    def extract_page(
        self,
        *,
        page: RawPageModel,
        prepared_page: PreparedPage,
        route: PageComplexityRoute,
        config: PipelineConfig,
    ) -> dict[str, Any]:
        """Return Markdown + structured semantic payload for one rendered page."""


_VLM_EXTRACTOR_SYSTEM_PROMPT = (
    "You extract one PDF form page for a downstream structured pipeline. "
    "Return a structured payload with: markdown (clean Markdown for the page, no "
    "raw coordinates); structure.hints describing every visible block with role, "
    "text, indent_level, column, optional nearby_heading, field_shape, "
    "possible_parent, and group_candidate; structure.chunk_seeds proposing how to "
    "split the page; continuation flags between neighbouring pages; and repeated "
    "header/footer bands. Group choice options under a shared group_candidate; "
    "link follow-up blanks via possible_parent. Tables: emit one 'table' parent "
    "before its 'table_column' rows, and do not extract data rows."
)


class BedrockQwenVlmExtractor:
    def __init__(
        self,
        client: Any | None = None,
        *,
        agent: Any | None = None,
    ) -> None:
        self.client = client
        self.agent = agent

    def extract_page(
        self,
        *,
        page: RawPageModel,
        prepared_page: PreparedPage,
        route: PageComplexityRoute,
        config: PipelineConfig,
    ) -> dict[str, Any]:
        agent = self.agent
        if agent is None:
            if config.bedrock_region is None and self.client is None:
                raise ValueError("BEDROCK_REGION is required for Bedrock VLM extraction.")
            agent = build_bedrock_agent(
                model_id=config.bedrock_vision_model_id,
                region=config.bedrock_region,
                output_type=VlmPagePayload,
                system_prompt=_VLM_EXTRACTOR_SYSTEM_PROMPT,
                bedrock_client=self.client,
                max_tokens=4096,
            )
        result = agent.run_sync(
            [
                _vlm_extraction_prompt(page, route),
                BinaryContent(
                    data=prepared_page.image_path.read_bytes(),
                    media_type=f"image/{prepared_page.image_format}",
                ),
            ]
        )
        return result.output.model_dump()


class VlmPath:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        extractor: VlmPageExtractor | None = None,
        parser_version: str = VLM_PATH_VERSION,
    ) -> None:
        self.config = config
        self.extractor = extractor
        self.parser_version = parser_version

    def build_pages(
        self,
        raw_pdf: RawPdfModel,
        prepared_pdf: PreparedPdf,
        complexity_routes: list[PageComplexityRoute],
    ) -> list[PageVlmArtifact]:
        if self.extractor is None:
            return []

        raw_by_number = {page.page_number: page for page in raw_pdf.pages}
        prepared_by_number = {page.page_number: page for page in prepared_pdf.pages}
        routes_by_number = {route.page: route for route in complexity_routes}

        artifacts: list[PageVlmArtifact] = []
        for route in complexity_routes:
            if route.recommended_path != "vlm":
                continue
            raw_page = raw_by_number[route.page]
            prepared_page = prepared_by_number[route.page]

            payload = self.extractor.extract_page(
                page=raw_page,
                prepared_page=prepared_page,
                route=route,
                config=self.config,
            )
            artifacts.append(
                _artifact_from_payload(
                    payload,
                    raw_page=raw_page,
                    route=route,
                    routes_by_number=routes_by_number,
                    parser_version=self.parser_version,
                )
            )
        return artifacts


def _artifact_from_payload(
    payload: dict[str, Any],
    *,
    raw_page: RawPageModel,
    route: PageComplexityRoute,
    routes_by_number: dict[int, PageComplexityRoute],
    parser_version: str,
) -> PageVlmArtifact:
    markdown = _extract_markdown(payload, raw_page.page_number)
    structure = payload.get("structure") or {}

    semantic_page = _semantic_page_from_structure(
        raw_page=raw_page,
        structure=structure,
        parser_version=parser_version,
    )
    continuation_hints = _continuation_hints_from_payload(
        payload,
        route,
        semantic_page,
        routes_by_number,
    )
    header_hints, footer_hints = _repeated_band_hints_from_payload(
        payload,
        page_number=raw_page.page_number,
    )
    chunk_seeds = _chunk_seeds_from_structure(structure, semantic_page)

    return PageVlmArtifact(
        page_number=raw_page.page_number,
        parser_version=parser_version,
        markdown=markdown,
        semantic_page=semantic_page,
        continuation_hints=continuation_hints,
        repeated_header_hints=header_hints,
        repeated_footer_hints=footer_hints,
        chunk_seeds=chunk_seeds,
        raw_response=payload,
    )


def _extract_markdown(payload: dict[str, Any], page_number: int) -> str:
    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        markdown = f"# Page {page_number}\n"
    if not markdown.endswith("\n"):
        markdown = markdown + "\n"
    return markdown


def _semantic_page_from_structure(
    *,
    raw_page: RawPageModel,
    structure: dict[str, Any],
    parser_version: str,
) -> SemanticPageModel:
    hints: list[SemanticHint] = []
    counter = 0
    active_section: str | None = None

    for raw_hint in structure.get("hints") or []:
        if not isinstance(raw_hint, dict):
            continue
        role = str(raw_hint.get("role") or "unknown")
        if role not in SEMANTIC_ROLES:
            role = "unknown"
        text = str(raw_hint.get("text") or "").strip()
        if not text:
            continue
        counter += 1
        hint_id = str(raw_hint.get("id") or "").strip() or _vlm_hint_id(
            raw_page.page_number, counter
        )
        if role == "section_heading":
            active_section = text
            nearby = None
        else:
            nearby_raw = raw_hint.get("nearby_heading")
            nearby = str(nearby_raw).strip() if isinstance(nearby_raw, str) else active_section

        hint = SemanticHint(
            id=hint_id,
            role=role,
            text=text,
            source_ids=_string_list(raw_hint.get("source_ids")) or [hint_id],
            indent_level=_clamp_int(raw_hint.get("indent_level"), 0, 12, default=0),
            column=_clamp_int(raw_hint.get("column"), 0, 8, default=0),
            nearby_heading=nearby,
            field_shape=_optional_string(raw_hint.get("field_shape")),
            possible_parent=_optional_string(raw_hint.get("possible_parent")),
            group_candidate=_optional_string(raw_hint.get("group_candidate")),
            facts={"source": "vlm"},
        )
        hints.append(hint)

    return SemanticPageModel(
        page_number=raw_page.page_number,
        width=raw_page.width,
        height=raw_page.height,
        parser_version=parser_version,
        hints=hints,
    )


def _continuation_hints_from_payload(
    payload: dict[str, Any],
    route: PageComplexityRoute,
    semantic_page: SemanticPageModel,
    routes_by_number: dict[int, PageComplexityRoute],
) -> list[ContinuationHint]:
    raw_continuation = payload.get("continuation") or {}
    from_previous = bool(raw_continuation.get("from_previous", route.continuation.from_previous))
    to_next = bool(raw_continuation.get("to_next", route.continuation.to_next))

    hints: list[ContinuationHint] = []
    if not semantic_page.hints:
        return hints

    if from_previous:
        leader = semantic_page.hints[0]
        previous_page = route.page - 1
        hints.append(
            ContinuationHint(
                direction="from_previous",
                page_number=route.page,
                neighbor_page=previous_page if previous_page in routes_by_number else None,
                text=leader.text,
                source_ids=list(leader.source_ids),
            )
        )
    if to_next:
        trailer = semantic_page.hints[-1]
        next_page = route.page + 1
        hints.append(
            ContinuationHint(
                direction="to_next",
                page_number=route.page,
                neighbor_page=next_page if next_page in routes_by_number else None,
                text=trailer.text,
                source_ids=list(trailer.source_ids),
            )
        )
    return hints


def _repeated_band_hints_from_payload(
    payload: dict[str, Any],
    *,
    page_number: int,
) -> tuple[list[RepeatedBandHint], list[RepeatedBandHint]]:
    repeated = payload.get("repeated") or {}
    header_hints = _band_list(repeated.get("headers"), band="header", page_number=page_number)
    footer_hints = _band_list(repeated.get("footers"), band="footer", page_number=page_number)
    return header_hints, footer_hints


def _band_list(
    raw_items: Any,
    *,
    band: str,
    page_number: int,
) -> list[RepeatedBandHint]:
    if not isinstance(raw_items, list):
        return []
    hints: list[RepeatedBandHint] = []
    for item in raw_items:
        text: str | None = None
        source_ids: list[str] = []
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            raw_text = item.get("text")
            if isinstance(raw_text, str):
                text = raw_text.strip()
            source_ids = _string_list(item.get("source_ids"))
        if not text:
            continue
        hints.append(
            RepeatedBandHint(
                text=text,
                band=band,
                page_numbers=[page_number],
                source_ids=source_ids,
            )
        )
    return hints


def _chunk_seeds_from_structure(
    structure: dict[str, Any],
    semantic_page: SemanticPageModel,
) -> list[ChunkSeed]:
    raw_seeds = structure.get("chunk_seeds")
    seeds: list[ChunkSeed] = []
    if isinstance(raw_seeds, list):
        for index, raw_seed in enumerate(raw_seeds, start=1):
            if not isinstance(raw_seed, dict):
                continue
            seed_id = str(raw_seed.get("seed_id") or "").strip() or _vlm_seed_id(
                semantic_page.page_number, index
            )
            chunk_type = str(raw_seed.get("chunk_type_hint") or "page")
            section_hint = _optional_string(raw_seed.get("section_hint"))
            source_ids = _string_list(raw_seed.get("source_block_ids"))
            seeds.append(
                ChunkSeed(
                    seed_id=seed_id,
                    chunk_type_hint=chunk_type,
                    section_hint=section_hint,
                    source_block_ids=source_ids,
                )
            )
    if seeds:
        return seeds

    if not semantic_page.hints:
        return []
    section_hint: str | None = None
    for hint in semantic_page.hints:
        if hint.role == "section_heading":
            section_hint = hint.text
            break
        if hint.nearby_heading:
            section_hint = hint.nearby_heading
            break
    return [
        ChunkSeed(
            seed_id=_vlm_seed_id(semantic_page.page_number, 1),
            chunk_type_hint="page",
            section_hint=section_hint,
            source_block_ids=[hint.source_ids[0] for hint in semantic_page.hints if hint.source_ids],
        )
    ]


def _vlm_hint_id(page_number: int, counter: int) -> str:
    return f"p{page_number}_v{counter:03d}"


def _vlm_seed_id(page_number: int, index: int) -> str:
    return f"p{page_number:02d}_vseed{index:02d}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int))]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clamp_int(value: Any, low: int, high: int, *, default: int) -> int:
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, as_int))


def _vlm_extraction_prompt(
    page: RawPageModel,
    route: PageComplexityRoute,
) -> str:
    return (
        f"Extract PDF form page {page.page_number} for downstream structured pipeline.\n"
        f"Deterministic route hint: {json.dumps(route.to_dict())}\n"
    )


def vlm_artifact_summary(artifacts: list[PageVlmArtifact]) -> dict[str, int]:
    return {
        "pages": len(artifacts),
        "hints": sum(len(artifact.semantic_page.hints) for artifact in artifacts),
        "chunk_seeds": sum(len(artifact.chunk_seeds) for artifact in artifacts),
    }
