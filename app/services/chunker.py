from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import PipelineConfig
from app.services.parser_path import (
    ChunkSeed,
    ContinuationHint,
    PageParserArtifact,
    RepeatedBandHint,
    _render_hint_markdown,
)
from app.services.semantic_hints import SemanticHint, SemanticPageModel
from app.services.vlm_path import PageVlmArtifact


CHUNKER_VERSION = "chunker-v1"

_TINY_CHUNK_TOKENS = 400


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="Stable identifier for the chunk, e.g. p01_c02.")
    pages: list[int] = Field(description="Page numbers covered by the chunk.")
    order_key: tuple[int, int] = Field(description="Ordering key (page, sub-index) used to sort chunks deterministically.")
    section_hint: str | None = Field(description="Section heading the chunk falls under, when known.")
    source_block_ids: list[str] = Field(description="Layout block IDs that contributed to the chunk.")
    chunk_type_hint: str | None = Field(description="Coarse chunk kind: page, section, table, checkbox_followups, questions, etc.")
    markdown: str = Field(description="Markdown body fed to the chunk extractor.")
    structure_hints: list[str] = Field(default_factory=list, description="Compact one-line structural facts to help the LLM parse the chunk.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "pages": list(self.pages),
            "order_key": list(self.order_key),
            "section_hint": self.section_hint,
            "source_block_ids": list(self.source_block_ids),
            "chunk_type_hint": self.chunk_type_hint,
            "markdown": self.markdown,
            "structure_hints": list(self.structure_hints),
        }


class _PageBundle(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    page_number: int
    semantic_page: SemanticPageModel
    chunk_seeds: list[ChunkSeed]
    repeated_band_ids: set[str]
    continuation_hints: list[ContinuationHint]


class SemanticChunker:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def build(
        self,
        parser_artifacts: list[PageParserArtifact],
        vlm_artifacts: list[PageVlmArtifact],
        semantic_pages: list[SemanticPageModel] | None = None,
    ) -> list[Chunk]:
        bundles = _collect_bundles(parser_artifacts, vlm_artifacts, semantic_pages or [])
        budget = max(1, self.config.max_chunk_input_tokens)

        chunks_by_page: dict[int, list[Chunk]] = {}
        for bundle in bundles:
            chunks_by_page[bundle.page_number] = _build_page_chunks(bundle, budget)

        merged = _apply_cross_page_continuations(bundles, chunks_by_page, budget)
        merged.sort(key=lambda chunk: chunk.order_key)
        return merged


def _collect_bundles(
    parser_artifacts: list[PageParserArtifact],
    vlm_artifacts: list[PageVlmArtifact],
    semantic_pages: list[SemanticPageModel],
) -> list[_PageBundle]:
    semantic_by_page = {page.page_number: page for page in semantic_pages}
    vlm_by_page = {artifact.page_number: artifact for artifact in vlm_artifacts}
    parser_by_page = {artifact.page_number: artifact for artifact in parser_artifacts}

    page_numbers = sorted(set(vlm_by_page) | set(parser_by_page))
    bundles: list[_PageBundle] = []
    for page_number in page_numbers:
        if page_number in vlm_by_page:
            artifact = vlm_by_page[page_number]
            semantic = artifact.semantic_page
            band_ids = _band_source_ids(
                artifact.repeated_header_hints, artifact.repeated_footer_hints
            )
            bundles.append(
                _PageBundle(
                    page_number=page_number,
                    semantic_page=semantic,
                    chunk_seeds=list(artifact.chunk_seeds),
                    repeated_band_ids=band_ids,
                    continuation_hints=list(artifact.continuation_hints),
                )
            )
            continue

        artifact = parser_by_page[page_number]
        semantic = semantic_by_page.get(page_number)
        if semantic is None:
            continue
        band_ids = _band_source_ids(
            artifact.repeated_header_hints, artifact.repeated_footer_hints
        )
        bundles.append(
            _PageBundle(
                page_number=page_number,
                semantic_page=semantic,
                chunk_seeds=list(artifact.chunk_seeds),
                repeated_band_ids=band_ids,
                continuation_hints=list(artifact.continuation_hints),
            )
        )
    return bundles


def _band_source_ids(
    headers: list[RepeatedBandHint],
    footers: list[RepeatedBandHint],
) -> set[str]:
    ids: set[str] = set()
    for hint in list(headers) + list(footers):
        ids.update(hint.source_ids)
    return ids


def _build_page_chunks(bundle: _PageBundle, budget: int) -> list[Chunk]:
    eligible_hints = [
        hint
        for hint in bundle.semantic_page.hints
        if not _hint_excluded(hint, bundle.repeated_band_ids)
    ]
    if not eligible_hints:
        return []

    seeds = bundle.chunk_seeds or [_fallback_seed(bundle, eligible_hints)]
    raw_chunks = _chunks_from_seeds(bundle, seeds, eligible_hints)
    if not raw_chunks:
        raw_chunks = [_chunk_from_hints(bundle, eligible_hints, seeds[0] if seeds else None)]

    split_chunks: list[_DraftChunk] = []
    for chunk in raw_chunks:
        split_chunks.extend(_split_if_over_budget(chunk, budget))

    merged = _merge_tiny_adjacent(split_chunks, budget)
    return _finalize_chunks(bundle.page_number, merged)


def _hint_excluded(hint: SemanticHint, band_ids: set[str]) -> bool:
    if not band_ids:
        return False
    return any(source_id in band_ids for source_id in hint.source_ids)


def _fallback_seed(bundle: _PageBundle, hints: list[SemanticHint]) -> ChunkSeed:
    return ChunkSeed(
        seed_id=f"p{bundle.page_number:02d}_seed01",
        chunk_type_hint="page",
        section_hint=_section_for_hints(hints),
        source_block_ids=[hint.source_ids[0] for hint in hints if hint.source_ids],
    )


class _DraftChunk:
    __slots__ = ("page_number", "section_hint", "chunk_type_hint", "hints")

    def __init__(
        self,
        *,
        page_number: int,
        section_hint: str | None,
        chunk_type_hint: str | None,
        hints: list[SemanticHint],
    ) -> None:
        self.page_number = page_number
        self.section_hint = section_hint
        self.chunk_type_hint = chunk_type_hint
        self.hints = hints


def _chunks_from_seeds(
    bundle: _PageBundle,
    seeds: list[ChunkSeed],
    eligible_hints: list[SemanticHint],
) -> list[_DraftChunk]:
    drafts: list[_DraftChunk] = []
    for seed in seeds:
        seed_ids = set(seed.source_block_ids)
        seed_hints = [
            hint
            for hint in eligible_hints
            if seed_ids.intersection(hint.source_ids) or hint.id in seed_ids
        ]
        if not seed_hints:
            continue
        drafts.append(
            _DraftChunk(
                page_number=bundle.page_number,
                section_hint=seed.section_hint or _section_for_hints(seed_hints),
                chunk_type_hint=_resolve_chunk_type(seed.chunk_type_hint, seed_hints),
                hints=seed_hints,
            )
        )
    return drafts


def _chunk_from_hints(
    bundle: _PageBundle,
    hints: list[SemanticHint],
    seed: ChunkSeed | None,
) -> _DraftChunk:
    return _DraftChunk(
        page_number=bundle.page_number,
        section_hint=(seed.section_hint if seed else None) or _section_for_hints(hints),
        chunk_type_hint=_resolve_chunk_type(seed.chunk_type_hint if seed else None, hints),
        hints=list(hints),
    )


def _section_for_hints(hints: list[SemanticHint]) -> str | None:
    # A section_heading at the very top of the hint list is the page/form title,
    # not a real in-page sub-section — skip it. Only treat a section_heading as
    # a section when it's preceded by some other content on the page.
    seen_non_heading = False
    for hint in hints:
        if hint.role == "section_heading":
            if seen_non_heading:
                return hint.text
            continue
        if hint.nearby_heading:
            return hint.nearby_heading
        seen_non_heading = True
    return None


def _resolve_chunk_type(seed_kind: str | None, hints: list[SemanticHint]) -> str | None:
    if seed_kind:
        return seed_kind
    roles = [hint.role for hint in hints]
    if any(role == "table" for role in roles):
        return "table"
    checkbox_like = sum(
        1 for role in roles if role in {"choice_option", "followup_field"}
    )
    if checkbox_like and checkbox_like >= len(roles) // 2:
        return "checkbox_followups"
    return "questions"


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _render_chunk_markdown(page_number: int, hints: list[SemanticHint]) -> str:
    lines = [f"# Page {page_number}"]
    seen_table_ids: set[str] = set()
    for hint in hints:
        rendered = _render_hint_markdown(hint, seen_table_ids)
        if rendered is None:
            continue
        lines.append(rendered)
    return "\n\n".join(lines).rstrip() + "\n"


def _structure_hints(hints: list[SemanticHint]) -> list[str]:
    out: list[str] = []
    for hint in hints:
        text = hint.text.strip().splitlines()[0] if hint.text.strip() else ""
        out.append(
            f"[{hint.id}] role={hint.role}, indent={hint.indent_level}: {text}"
        )
    return out


def _split_if_over_budget(chunk: _DraftChunk, budget: int) -> list[_DraftChunk]:
    markdown = _render_chunk_markdown(chunk.page_number, chunk.hints)
    if _approx_tokens(markdown) <= budget or len(chunk.hints) <= 1:
        return [chunk]

    parts: list[_DraftChunk] = []
    current: list[SemanticHint] = []
    for hint in chunk.hints:
        candidate = current + [hint]
        candidate_md = _render_chunk_markdown(chunk.page_number, candidate)
        if current and _approx_tokens(candidate_md) > budget:
            parts.append(
                _DraftChunk(
                    page_number=chunk.page_number,
                    section_hint=chunk.section_hint,
                    chunk_type_hint=chunk.chunk_type_hint,
                    hints=list(current),
                )
            )
            current = [hint]
        else:
            current = candidate
    if current:
        parts.append(
            _DraftChunk(
                page_number=chunk.page_number,
                section_hint=chunk.section_hint,
                chunk_type_hint=chunk.chunk_type_hint,
                hints=list(current),
            )
        )
    return parts


def _merge_tiny_adjacent(drafts: list[_DraftChunk], budget: int) -> list[_DraftChunk]:
    if not drafts:
        return []
    merged: list[_DraftChunk] = [drafts[0]]
    for draft in drafts[1:]:
        previous = merged[-1]
        if previous.section_hint != draft.section_hint:
            merged.append(draft)
            continue
        previous_md = _render_chunk_markdown(previous.page_number, previous.hints)
        draft_md = _render_chunk_markdown(draft.page_number, draft.hints)
        if (
            _approx_tokens(previous_md) < _TINY_CHUNK_TOKENS
            and _approx_tokens(draft_md) < _TINY_CHUNK_TOKENS
        ):
            combined = previous.hints + draft.hints
            combined_md = _render_chunk_markdown(previous.page_number, combined)
            if _approx_tokens(combined_md) <= budget:
                merged[-1] = _DraftChunk(
                    page_number=previous.page_number,
                    section_hint=previous.section_hint,
                    chunk_type_hint=previous.chunk_type_hint or draft.chunk_type_hint,
                    hints=combined,
                )
                continue
        merged.append(draft)
    return merged


def _finalize_chunks(page_number: int, drafts: list[_DraftChunk]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for index, draft in enumerate(drafts, start=1):
        markdown = _render_chunk_markdown(draft.page_number, draft.hints)
        source_ids: list[str] = []
        seen: set[str] = set()
        for hint in draft.hints:
            for source_id in hint.source_ids:
                if source_id not in seen:
                    seen.add(source_id)
                    source_ids.append(source_id)
        chunks.append(
            Chunk(
                chunk_id=f"p{draft.page_number:02d}_c{index:02d}",
                pages=[draft.page_number],
                order_key=(draft.page_number, index),
                section_hint=draft.section_hint,
                source_block_ids=source_ids,
                chunk_type_hint=draft.chunk_type_hint,
                markdown=markdown,
                structure_hints=_structure_hints(draft.hints),
            )
        )
    return chunks


def _apply_cross_page_continuations(
    bundles: list[_PageBundle],
    chunks_by_page: dict[int, list[Chunk]],
    budget: int,
) -> list[Chunk]:
    bundle_by_page = {bundle.page_number: bundle for bundle in bundles}
    sorted_pages = sorted(chunks_by_page.keys())

    for index in range(len(sorted_pages) - 1):
        current_page = sorted_pages[index]
        next_page = sorted_pages[index + 1]
        current_bundle = bundle_by_page.get(current_page)
        next_bundle = bundle_by_page.get(next_page)
        if not current_bundle or not next_bundle:
            continue
        if not _has_direction(current_bundle.continuation_hints, "to_next"):
            continue
        if not _has_direction(next_bundle.continuation_hints, "from_previous"):
            continue
        current_chunks = chunks_by_page.get(current_page) or []
        next_chunks = chunks_by_page.get(next_page) or []
        if not current_chunks or not next_chunks:
            continue
        tail = current_chunks[-1]
        head = next_chunks[0]
        merged_markdown = (tail.markdown.rstrip() + "\n\n" + head.markdown.lstrip()).strip() + "\n"
        if _approx_tokens(merged_markdown) > budget:
            continue
        merged_chunk = Chunk(
            chunk_id=tail.chunk_id,
            pages=sorted({*tail.pages, *head.pages}),
            order_key=tail.order_key,
            section_hint=tail.section_hint or head.section_hint,
            source_block_ids=_merge_unique(tail.source_block_ids, head.source_block_ids),
            chunk_type_hint=tail.chunk_type_hint or head.chunk_type_hint,
            markdown=merged_markdown,
            structure_hints=list(tail.structure_hints) + list(head.structure_hints),
        )
        chunks_by_page[current_page] = current_chunks[:-1] + [merged_chunk]
        chunks_by_page[next_page] = next_chunks[1:]

    flattened: list[Chunk] = []
    for page_number in sorted_pages:
        flattened.extend(chunks_by_page.get(page_number) or [])
    return flattened


def _has_direction(hints: list[ContinuationHint], direction: str) -> bool:
    return any(hint.direction == direction for hint in hints)


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in list(left) + list(right):
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
