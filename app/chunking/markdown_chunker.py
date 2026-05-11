from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


PAGE_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
UNDERLINE_RE = re.compile(r"^[_\s]{8,}$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\(?[a-zA-Z0-9]+\)|[oO])\s+")

MAJOR_SECTION_RE = re.compile(
    r"^(?:"
    r"current living arrangements|"
    r"justification for safety determination request|"
    r"additional required documentation|"
    r"submitting entity attestation|"
    r"recent .* admissions|"
    r"recent .* visits|"
    r"individual service plan\s*-\s*signature page|"
    r"fall form|"
    r"service coordinator verification"
    r")\s*:?\s*$",
    re.IGNORECASE,
)
SAFETY_CRITERION_RE = re.compile(
    r"^(?:"
    r"the applicant has|"
    r"applicant has|"
    r"applicant['’]s primary caregiver has|"
    r"none of the criteria above|"
    r"the applicant['’]s mco has|"
    r"the applicant is a current|"
    r"the applicant requires"
    r")\b",
    re.IGNORECASE,
)
SIGNATURE_CONTEXT_RE = re.compile(
    r"^(?:"
    r"applicant,\s*member or authorized representative|"
    r"applicant/member or authorized representative|"
    r"witness,\s*if applicable|"
    r"service coordinator"
    r")\s*:?\s*$",
    re.IGNORECASE,
)
SIGNATURE_FIELD_RE = re.compile(r"^(?:printed name|signature|date|credentials)\s*:?\s*$", re.IGNORECASE)
OPTION_RE = re.compile(
    r"^(?:"
    r"lives in\b|"
    r"assisted living\b|"
    r"other(?:\b|[-—])|"
    r"i do not believe\b|"
    r"i believe\b|"
    r"yes\b|"
    r"no\b|"
    r"am\b|"
    r"pm\b"
    r")",
    re.IGNORECASE,
)
FIELD_LINE_RE = re.compile(r"[:#]\s*_{2,}|_{4,}|(?:\bdate\b|\bdob\b|\bsignature\b|\bprinted name\b)$", re.IGNORECASE)
FOOTER_RE = re.compile(r"^(?:\d+\s+)?(?:safety determination request form|tc\d+|rda\s+\d+|form\s+h\d+-?\d*)\b", re.IGNORECASE)


class MarkdownChunk(BaseModel):
    chunk_id: str
    chunk_text: str
    source_pages: list[int] = Field(default_factory=list)
    heading_path: list[str] = Field(default_factory=list)
    previous_context_summary: str = ""


@dataclass
class _Unit:
    kind: str
    lines: list[str]
    pages: list[int] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def _pages_for_text(text: str, fallback_pages: list[int] | None = None) -> list[int]:
    pages = [int(m.group(1)) for m in PAGE_RE.finditer(text)]
    if pages:
        return sorted(set(pages))
    return sorted(set(fallback_pages or []))


def _is_image_only(text: str) -> bool:
    return bool(re.fullmatch(r"<!--\s*image\s*-->", text.strip(), re.IGNORECASE))


def _is_page_heading(text: str) -> bool:
    match = HEADING_RE.match(text.strip())
    return bool(match and match.group(2).lower().startswith("page "))


def _is_noise(text: str) -> bool:
    stripped = text.strip()
    return not stripped or _is_image_only(stripped) or _is_page_heading(stripped)


def _is_likely_section_title(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower().rstrip(":")
    if MAJOR_SECTION_RE.match(stripped):
        return True
    return False


def _is_table_line(line: str) -> bool:
    return bool(TABLE_RE.match(line))


def _is_field_or_prompt(text: str) -> bool:
    stripped = text.strip()
    return bool(
        FIELD_LINE_RE.search(stripped)
        or stripped.lower().startswith("description of documentation attached")
        or stripped.lower().startswith("label attachment")
        or stripped.lower().startswith("name of ")
        or stripped.lower().startswith("safety concerns leading")
        or stripped.lower().startswith("acute event")
        or stripped.lower().startswith("treatment required")
        or stripped.lower().startswith("duration of time needed")
    )


def _unit_kind(text: str) -> str:
    stripped = text.strip()
    if PAGE_RE.fullmatch(stripped):
        return "page_marker"
    if HEADING_RE.match(stripped):
        return "heading"
    if _is_image_only(stripped):
        return "noise"
    if FOOTER_RE.match(stripped):
        return "footer"
    if _is_likely_section_title(stripped):
        return "section_title"
    if SIGNATURE_CONTEXT_RE.match(stripped):
        return "signature_context"
    if SIGNATURE_FIELD_RE.match(stripped):
        return "signature_field"
    if SAFETY_CRITERION_RE.match(stripped):
        return "criterion"
    if BULLET_RE.match(stripped):
        return "followup"
    if OPTION_RE.match(stripped):
        return "option"
    if _is_field_or_prompt(stripped) or UNDERLINE_RE.match(stripped):
        return "field"
    return "paragraph"


def _parse_units(markdown: str) -> list[_Unit]:
    units: list[_Unit] = []
    paragraph: list[str] = []
    current_pages: list[int] = []
    heading_path: list[str] = []
    active_page: int | None = None

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        text = "\n".join(paragraph).strip()
        if text:
            units.append(_Unit(_unit_kind(text), paragraph, list(current_pages), list(heading_path)))
        paragraph = []

    lines = (markdown or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        page_match = PAGE_RE.fullmatch(stripped)
        if page_match:
            flush_paragraph()
            active_page = int(page_match.group(1))
            current_pages = [active_page]
            units.append(_Unit("page_marker", [line], [active_page], list(heading_path)))
            index += 1
            continue

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if _is_table_line(line):
            flush_paragraph()
            table_lines: list[str] = []
            while index < len(lines) and _is_table_line(lines[index]):
                table_lines.append(lines[index].rstrip())
                index += 1
            units.append(_Unit("table", table_lines, list(current_pages), list(heading_path)))
            continue

        heading_match = HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            if not heading_match.group(2).lower().startswith("page "):
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                heading_path = heading_path[: max(0, level - 1)] + [title]
            units.append(_Unit(_unit_kind(stripped), [line], list(current_pages), list(heading_path)))
            index += 1
            continue

        paragraph.append(line)
        if active_page is not None and active_page not in current_pages:
            current_pages.append(active_page)
        index += 1

    flush_paragraph()
    return units


def _unit_starts_chunk(unit: _Unit, current_units: list[_Unit]) -> bool:
    if not current_units:
        return False

    if unit.kind in {"page_marker", "footer", "noise"}:
        return False

    meaningful = [u for u in current_units if u.kind not in {"page_marker", "heading", "footer", "noise"}]
    if not meaningful:
        return False

    if unit.kind == "heading":
        heading = re.sub(r"^#{1,6}\s+", "", unit.text).strip().lower()
        if heading in {"applicant interview", "page"} or heading.startswith("page "):
            return False
        return True

    if unit.kind == "section_title":
        return True

    if unit.kind == "table":
        return len("\n\n".join(u.text for u in meaningful)) > 600

    if unit.kind == "criterion":
        last_meaningful = meaningful[-1]
        return last_meaningful.kind not in {"section_title", "criterion"}

    return False


def _should_keep_with_previous(unit: _Unit, current_units: list[_Unit]) -> bool:
    if not current_units:
        return False
    previous = next((u for u in reversed(current_units) if u.kind not in {"page_marker", "heading", "footer", "noise"}), None)
    if previous is None:
        return True

    if unit.kind in {"followup", "field"} and previous.kind in {"criterion", "followup", "field", "table"}:
        return True
    if unit.kind == "option" and previous.kind in {"section_title", "paragraph", "option"}:
        return True
    if unit.kind == "signature_field" and previous.kind in {"signature_context", "signature_field"}:
        return True
    if unit.kind == "signature_context" and previous.kind in {"paragraph", "section_title"}:
        return True
    return False


def _make_chunk(index: int, units: list[_Unit]) -> MarkdownChunk:
    text = "\n\n".join(unit.text for unit in units if unit.text and not _is_noise(unit.text)).strip()
    pages: list[int] = []
    heading_path: list[str] = []
    for unit in units:
        pages.extend(unit.pages)
        if unit.heading_path:
            heading_path = unit.heading_path

    return MarkdownChunk(
        chunk_id=f"chunk_{index:03d}",
        chunk_text=text,
        source_pages=_pages_for_text(text, pages),
        heading_path=heading_path,
    )


def _split_oversized_units(units: list[_Unit], max_chars: int) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []

    def current_len() -> int:
        return len("\n\n".join(unit.text for unit in current))

    for unit in units:
        unit_len = len(unit.text)
        if current and current_len() + unit_len + 2 > max_chars and not _should_keep_with_previous(unit, current):
            groups.append(current)
            current = []
        current.append(unit)

    if current:
        groups.append(current)
    return groups


def chunk_markdown(markdown: str, max_chars: int = 12000) -> list[MarkdownChunk]:
    units = [unit for unit in _parse_units(markdown) if unit.kind not in {"footer", "noise"}]
    chunk_unit_groups: list[list[_Unit]] = []
    current_units: list[_Unit] = []

    def flush() -> None:
        nonlocal current_units
        if any(unit.text and not _is_noise(unit.text) for unit in current_units):
            chunk_unit_groups.extend(_split_oversized_units(current_units, max_chars))
        current_units = []

    for unit in units:
        if _unit_starts_chunk(unit, current_units) and not _should_keep_with_previous(unit, current_units):
            flush()
        current_units.append(unit)
        if len("\n\n".join(u.text for u in current_units)) >= max_chars:
            flush()

    flush()

    chunks = [
        _make_chunk(index, group)
        for index, group in enumerate(chunk_unit_groups, start=1)
        if any(unit.text and not _is_noise(unit.text) for unit in group)
    ]

    if not chunks and markdown.strip():
        chunks.append(
            MarkdownChunk(
                chunk_id="chunk_001",
                chunk_text=markdown.strip(),
                source_pages=_pages_for_text(markdown),
                heading_path=[],
            )
        )

    for idx, chunk in enumerate(chunks):
        if idx == 0:
            continue
        previous = chunks[idx - 1].chunk_text.replace("\n", " ")
        chunks[idx] = chunk.model_copy(update={"previous_context_summary": previous[:500]})

    return chunks
