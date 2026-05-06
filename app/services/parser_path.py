from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.complexity_router import PageComplexityRoute
from app.services.docling_parser import DoclingParser
from app.services.layout_parser import RawPageModel, RawPdfModel, TextBlock
from app.services.semantic_hints import SemanticHint, SemanticPageModel


PARSER_PATH_VERSION = "parser-path-v1"


class RepeatedBandHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(description="Normalised text of the repeated band.")
    band: Literal["header", "footer"] = Field(description="Whether this band sits at the top (header) or bottom (footer) of the page.")
    page_numbers: list[int] = Field(description="Pages on which the band was detected.")
    source_ids: list[str] = Field(description="Layout block IDs that compose this band across pages.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "band": self.band,
            "page_numbers": list(self.page_numbers),
            "source_ids": list(self.source_ids),
        }


class ContinuationHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    direction: Literal["from_previous", "to_next"] = Field(description="Whether the hint signals continuation from the previous page or to the next page.")
    page_number: int = Field(description="Page on which the hint applies.")
    neighbor_page: int | None = Field(description="The neighbouring page, or None when no eligible neighbour exists.")
    text: str = Field(description="Leading or trailing text used to anchor the continuation.")
    source_ids: list[str] = Field(description="Source block IDs backing the continuation hint.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "page_number": self.page_number,
            "neighbor_page": self.neighbor_page,
            "text": self.text,
            "source_ids": list(self.source_ids),
        }


class ChunkSeed(BaseModel):
    model_config = ConfigDict(frozen=True)

    seed_id: str = Field(description="Stable identifier for the chunk seed.")
    chunk_type_hint: str = Field(description="High-level kind of chunk this seed will produce (page, section, table, etc.).")
    section_hint: str | None = Field(description="Section heading the seed sits under, if known.")
    source_block_ids: list[str] = Field(description="Layout block IDs assigned to this seed.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_id": self.seed_id,
            "chunk_type_hint": self.chunk_type_hint,
            "section_hint": self.section_hint,
            "source_block_ids": list(self.source_block_ids),
        }


class PageParserArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int = Field(description="1-based page number.")
    parser_version: str = Field(description="Identifier of the parser path that produced the artefact.")
    markdown: str = Field(description="Per-page Markdown rendered for downstream chunking.")
    continuation_hints: list[ContinuationHint] = Field(description="Continuation hints linking this page to its neighbours.")
    repeated_header_hints: list[RepeatedBandHint] = Field(description="Detected repeated header bands.")
    repeated_footer_hints: list[RepeatedBandHint] = Field(description="Detected repeated footer bands.")
    chunk_seeds: list[ChunkSeed] = Field(description="Chunk seeds produced for the chunker.")
    excluded_source_ids: list[str] = Field(default_factory=list, description="Block IDs excluded from chunk seeds (e.g. repeated bands).")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "parser_version": self.parser_version,
            "markdown": self.markdown,
            "continuation_hints": [hint.to_dict() for hint in self.continuation_hints],
            "repeated_header_hints": [hint.to_dict() for hint in self.repeated_header_hints],
            "repeated_footer_hints": [hint.to_dict() for hint in self.repeated_footer_hints],
            "chunk_seeds": [seed.to_dict() for seed in self.chunk_seeds],
            "excluded_source_ids": list(self.excluded_source_ids),
        }


class ParserPath:
    def __init__(
        self,
        *,
        parser_version: str = PARSER_PATH_VERSION,
        docling_parser: DoclingParser | None = None,
    ) -> None:
        self.parser_version = parser_version
        self.docling_parser = docling_parser

    def build_pages(
        self,
        raw_pdf: RawPdfModel,
        semantic_pages: list[SemanticPageModel],
        complexity_routes: list[PageComplexityRoute],
    ) -> list[PageParserArtifact]:
        semantic_by_number = {page.page_number: page for page in semantic_pages}
        raw_by_number = {page.page_number: page for page in raw_pdf.pages}
        routes_by_number = {route.page: route for route in complexity_routes}

        repeated_bands = _detect_repeated_bands(raw_pdf.pages)

        docling_markdown_by_page: dict[int, str] = {}
        parser_version = self.parser_version
        if self.docling_parser is not None:
            needs_parser = any(
                route.recommended_path == "parser" for route in complexity_routes
            )
            if needs_parser:
                pages = self.docling_parser.parse(raw_pdf.pdf_path)
                docling_markdown_by_page = {
                    page.page_number: page.markdown for page in pages
                }
                parser_version = self.docling_parser.version

        artifacts: list[PageParserArtifact] = []
        for route in complexity_routes:
            if route.recommended_path != "parser":
                continue
            page_number = route.page
            raw_page = raw_by_number[page_number]
            semantic_page = semantic_by_number[page_number]

            header_hints = repeated_bands.headers_for_page(page_number)
            footer_hints = repeated_bands.footers_for_page(page_number)
            excluded_ids = repeated_bands.excluded_source_ids_for_page(page_number)

            continuation = _continuation_hints(
                route,
                semantic_page,
                excluded_ids,
                routes_by_number,
            )
            if page_number in docling_markdown_by_page:
                body = docling_markdown_by_page[page_number].rstrip()
                markdown = f"# Page {page_number}\n\n{body}\n" if body else f"# Page {page_number}\n"
            else:
                markdown = _page_markdown(
                    raw_page,
                    semantic_page,
                    excluded_ids,
                )
            chunk_seeds = _chunk_seeds(semantic_page, excluded_ids, route.complexity)

            artifacts.append(
                PageParserArtifact(
                    page_number=page_number,
                    parser_version=parser_version,
                    markdown=markdown,
                    continuation_hints=continuation,
                    repeated_header_hints=header_hints,
                    repeated_footer_hints=footer_hints,
                    chunk_seeds=chunk_seeds,
                    excluded_source_ids=sorted(excluded_ids),
                )
            )
        return artifacts


class _RepeatedBandIndex:
    __slots__ = ("headers_by_page", "footers_by_page", "excluded_by_page")

    def __init__(
        self,
        headers_by_page: dict[int, list[RepeatedBandHint]],
        footers_by_page: dict[int, list[RepeatedBandHint]],
        excluded_by_page: dict[int, set[str]],
    ) -> None:
        self.headers_by_page = headers_by_page
        self.footers_by_page = footers_by_page
        self.excluded_by_page = excluded_by_page

    def headers_for_page(self, page_number: int) -> list[RepeatedBandHint]:
        return list(self.headers_by_page.get(page_number, []))

    def footers_for_page(self, page_number: int) -> list[RepeatedBandHint]:
        return list(self.footers_by_page.get(page_number, []))

    def excluded_source_ids_for_page(self, page_number: int) -> set[str]:
        return set(self.excluded_by_page.get(page_number, set()))


def _detect_repeated_bands(pages: list[RawPageModel]) -> _RepeatedBandIndex:
    headers_by_page: dict[int, list[RepeatedBandHint]] = defaultdict(list)
    footers_by_page: dict[int, list[RepeatedBandHint]] = defaultdict(list)
    excluded_by_page: dict[int, set[str]] = defaultdict(set)

    if len(pages) < 2:
        return _RepeatedBandIndex(headers_by_page, footers_by_page, excluded_by_page)

    header_occurrences: dict[str, list[tuple[int, str]]] = defaultdict(list)
    footer_occurrences: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for page in pages:
        for block in _band_blocks(page, top=True):
            key = _normalize_band_text(block.text)
            if not key:
                continue
            header_occurrences[key].append((page.page_number, block.id))
        for block in _band_blocks(page, top=False):
            key = _normalize_band_text(block.text)
            if not key:
                continue
            footer_occurrences[key].append((page.page_number, block.id))

    repetition_threshold = max(2, len(pages) // 2)

    for key, entries in header_occurrences.items():
        unique_pages = sorted({page_number for page_number, _ in entries})
        if len(unique_pages) < repetition_threshold:
            continue
        page_numbers = unique_pages
        source_ids = [block_id for _, block_id in entries]
        hint = RepeatedBandHint(
            text=key,
            band="header",
            page_numbers=page_numbers,
            source_ids=source_ids,
        )
        for page_number, block_id in entries:
            headers_by_page[page_number].append(hint)
            excluded_by_page[page_number].add(block_id)

    for key, entries in footer_occurrences.items():
        unique_pages = sorted({page_number for page_number, _ in entries})
        if len(unique_pages) < repetition_threshold:
            continue
        page_numbers = unique_pages
        source_ids = [block_id for _, block_id in entries]
        hint = RepeatedBandHint(
            text=key,
            band="footer",
            page_numbers=page_numbers,
            source_ids=source_ids,
        )
        for page_number, block_id in entries:
            footers_by_page[page_number].append(hint)
            excluded_by_page[page_number].add(block_id)

    return _RepeatedBandIndex(
        headers_by_page=dict(headers_by_page),
        footers_by_page=dict(footers_by_page),
        excluded_by_page=dict(excluded_by_page),
    )


def _band_blocks(page: RawPageModel, *, top: bool) -> list[TextBlock]:
    if page.height <= 0:
        return []
    matched: list[TextBlock] = []
    for block in page.blocks:
        if not block.text.strip():
            continue
        if top and block.bbox.y0 <= page.height * 0.08:
            matched.append(block)
        elif not top and block.bbox.y1 >= page.height * 0.92:
            matched.append(block)
    return matched


def _normalize_band_text(text: str) -> str:
    flattened = re.sub(r"\s+", " ", text).strip()
    flattened = re.sub(r"\bpage\s+\d+(\s+of\s+\d+)?\b", "page", flattened, flags=re.IGNORECASE)
    flattened = re.sub(r"\b\d+\b", "#", flattened)
    return flattened


def _continuation_hints(
    route: PageComplexityRoute,
    semantic_page: SemanticPageModel,
    excluded_ids: set[str],
    routes_by_number: dict[int, PageComplexityRoute],
) -> list[ContinuationHint]:
    hints: list[ContinuationHint] = []
    eligible = [hint for hint in semantic_page.hints if not _is_excluded(hint, excluded_ids)]
    if not eligible:
        return hints

    if route.continuation.from_previous:
        leader = eligible[0]
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

    if route.continuation.to_next:
        trailer = eligible[-1]
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


def _is_excluded(hint: SemanticHint, excluded_ids: set[str]) -> bool:
    return any(source_id in excluded_ids for source_id in hint.source_ids)


def _page_markdown(
    raw_page: RawPageModel,
    semantic_page: SemanticPageModel,
    excluded_ids: set[str],
) -> str:
    lines: list[str] = [f"# Page {raw_page.page_number}"]
    seen_table_ids: set[str] = set()
    for hint in semantic_page.hints:
        if _is_excluded(hint, excluded_ids):
            continue
        rendered = _render_hint_markdown(hint, seen_table_ids)
        if rendered is None:
            continue
        lines.append(rendered)
    return "\n\n".join(lines).rstrip() + "\n"


def _render_hint_markdown(hint: SemanticHint, seen_table_ids: set[str]) -> str | None:
    text = hint.text.strip()
    if not text:
        return None
    indent = "  " * max(0, hint.indent_level)

    if hint.role == "section_heading":
        return f"## {text}"
    if hint.role == "instruction":
        return f"{indent}> {text}"
    if hint.role == "question_stem":
        return f"{indent}**{text}**"
    if hint.role == "blank_field":
        return f"{indent}- [field] {text}"
    if hint.role == "choice_option":
        return f"{indent}- [ ] {text}"
    if hint.role == "choice_group":
        return f"{indent}- [select] {text}"
    if hint.role == "followup_field":
        return f"{indent}  - [followup] {text}"
    if hint.role == "signature_block":
        return f"{indent}- [signature] {text}"
    if hint.role == "table":
        seen_table_ids.add(hint.id)
        return f"{indent}### Table: {text}"
    if hint.role == "table_column":
        if hint.possible_parent and hint.possible_parent not in seen_table_ids:
            return f"{indent}- [column] {text}"
        return f"{indent}- [column] {text}"
    if hint.role in {"repeated_header", "repeated_footer"}:
        return None
    return f"{indent}{text}"


def _chunk_seeds(
    semantic_page: SemanticPageModel,
    excluded_ids: set[str],
    complexity: str,
) -> list[ChunkSeed]:
    eligible = [hint for hint in semantic_page.hints if not _is_excluded(hint, excluded_ids)]
    if not eligible:
        return []

    if complexity == "simple":
        seed_id = f"p{semantic_page.page_number:02d}_seed01"
        return [
            ChunkSeed(
                seed_id=seed_id,
                chunk_type_hint="page",
                section_hint=_first_section(eligible),
                source_block_ids=[hint.source_ids[0] for hint in eligible if hint.source_ids],
            )
        ]

    seeds: list[ChunkSeed] = []
    current_section: str | None = None
    current_block_ids: list[str] = []
    current_type = "section"
    seed_index = 0

    def flush() -> None:
        nonlocal seed_index, current_block_ids
        if not current_block_ids:
            return
        seed_index += 1
        seed_id = f"p{semantic_page.page_number:02d}_seed{seed_index:02d}"
        seeds.append(
            ChunkSeed(
                seed_id=seed_id,
                chunk_type_hint=current_type,
                section_hint=current_section,
                source_block_ids=list(current_block_ids),
            )
        )
        current_block_ids = []

    for hint in eligible:
        if hint.role == "section_heading":
            flush()
            current_section = hint.text
            current_type = "section"
            current_block_ids.extend(hint.source_ids)
            continue
        if hint.role == "table":
            flush()
            current_type = "table"
            current_block_ids.extend(hint.source_ids)
            continue
        if hint.role == "table_column" and current_type == "table":
            current_block_ids.extend(hint.source_ids)
            continue
        if hint.role == "table_column":
            flush()
            current_type = "table"
            current_block_ids.extend(hint.source_ids)
            continue
        if current_type == "table" and hint.role not in {"table", "table_column"}:
            flush()
            current_type = "section"
        current_block_ids.extend(hint.source_ids)

    flush()

    if not seeds:
        seed_id = f"p{semantic_page.page_number:02d}_seed01"
        seeds.append(
            ChunkSeed(
                seed_id=seed_id,
                chunk_type_hint="page",
                section_hint=_first_section(eligible),
                source_block_ids=[hint.source_ids[0] for hint in eligible if hint.source_ids],
            )
        )
    return seeds


def _first_section(hints: list[SemanticHint]) -> str | None:
    for hint in hints:
        if hint.role == "section_heading":
            return hint.text
        if hint.nearby_heading:
            return hint.nearby_heading
    return None


def repeated_band_summary(artifacts: list[PageParserArtifact]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for artifact in artifacts:
        counter["headers"] += len(artifact.repeated_header_hints)
        counter["footers"] += len(artifact.repeated_footer_hints)
    return dict(counter)
