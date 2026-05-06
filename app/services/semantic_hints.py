from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.layout_parser import (
    BoundingBox,
    ChoiceGlyphCandidate,
    FormWidget,
    RawPageModel,
    TableCandidate,
    TextBlock,
)


SEMANTIC_ROLES = frozenset(
    {
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
    }
)

QUESTION_WORDS = frozenset(
    {
        "name",
        "date",
        "dob",
        "address",
        "phone",
        "email",
        "number",
        "id",
        "signature",
        "describe",
        "explain",
        "select",
        "check",
        "indicate",
    }
)

INSTRUCTION_STARTS = (
    "complete ",
    "enter ",
    "provide ",
    "attach ",
    "please ",
    "use ",
    "include ",
    "do not ",
    "for ",
)

BLANK_PATTERN = re.compile(r"_{3,}|\.{4,}|-{4,}")


class SemanticHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the hint, propagated downstream.")
    role: str = Field(description=f"Semantic role of the block. One of: {', '.join(sorted(SEMANTIC_ROLES))}.")
    text: str = Field(description="Visible text associated with this hint.")
    source_ids: list[str] = Field(description="Identifiers of the raw layout artefacts (blocks, widgets, tables) backing this hint.")
    indent_level: int = Field(default=0, description="Heuristic indent level used to preserve hierarchy in prompt rendering.")
    column: int = Field(default=0, description="0-based column index for multi-column page layouts.")
    nearby_heading: str | None = Field(default=None, description="Most-recent section heading text the hint is associated with.")
    field_shape: str | None = Field(default=None, description="Visual shape descriptor (single_line_blank, checkbox_or_radio, etc.) when applicable.")
    possible_parent: str | None = Field(default=None, description="Identifier of a parent hint such as a table or choice option, when known.")
    group_candidate: str | None = Field(default=None, description="Synthetic group identifier shared across related choice options.")
    facts: dict[str, Any] = Field(default_factory=dict, description="Auxiliary structural facts attached for downstream prompts.")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in SEMANTIC_ROLES:
            raise ValueError(f"Unknown semantic role: {value}")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "source_ids": list(self.source_ids),
            "indent_level": self.indent_level,
            "column": self.column,
            "nearby_heading": self.nearby_heading,
            "field_shape": self.field_shape,
            "possible_parent": self.possible_parent,
            "group_candidate": self.group_candidate,
            "facts": dict(self.facts),
        }

    def to_prompt_line(self) -> str:
        facts = [
            f"role={self.role}",
            f"indent={self.indent_level}",
            f"column={self.column}",
        ]
        if self.nearby_heading:
            facts.append(f"nearby_heading={self.nearby_heading!r}")
        if self.field_shape:
            facts.append(f"field_shape={self.field_shape}")
        if self.possible_parent:
            facts.append(f"possible_parent={self.possible_parent}")
        if self.group_candidate:
            facts.append(f"group={self.group_candidate}")
        return f"[{self.id}] {', '.join(facts)}:\n{self.text}"


class SemanticPageModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int = Field(description="1-based page number within the source PDF.")
    width: float = Field(description="Page width in user-space units.")
    height: float = Field(description="Page height in user-space units.")
    parser_version: str = Field(description="Version of the parser that produced this semantic page.")
    hints: list[SemanticHint] = Field(description="Ordered semantic hints derived from the page.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "parser_version": self.parser_version,
            "hints": [hint.to_dict() for hint in self.hints],
        }

    def to_prompt_text(self) -> str:
        return "\n\n".join(hint.to_prompt_line() for hint in self.hints)


class SemanticHintBuilder:
    def build_page(self, page: RawPageModel) -> SemanticPageModel:
        hints: list[SemanticHint] = []
        active_heading: str | None = None
        choice_group: str | None = None
        last_choice_id: str | None = None

        sorted_blocks = sorted(page.blocks, key=lambda block: (block.bbox.y0, block.bbox.x0))
        indent_unit = _indent_unit(sorted_blocks)
        suppressed_block_ids = _blocks_redundant_with_widgets(sorted_blocks, page)

        for order, block in enumerate(sorted_blocks, start=1):
            if not block.text.strip():
                continue
            if block.id in suppressed_block_ids:
                continue
            if _is_micro_block(block):
                continue

            role = _role_for_block(block, page, order, len(sorted_blocks))
            if role == "section_heading":
                active_heading = _single_line(block.text)
                choice_group = None
                last_choice_id = None

            if role == "choice_option":
                if choice_group is None:
                    choice_group = f"p{page.page_number}_g{len([h for h in hints if h.role == 'choice_option']) + 1:02d}"
                possible_parent = last_choice_id if _is_followup_text(block.text) else None
            elif role == "followup_field":
                possible_parent = last_choice_id
            else:
                possible_parent = None
                if role not in {"instruction", "blank_field"}:
                    choice_group = None

            hint = SemanticHint(
                id=_semantic_id(block.id),
                role=role,
                text=_normalize_text(block.text),
                source_ids=[block.id],
                indent_level=_indent_level(block.bbox, indent_unit),
                column=_column_for_bbox(block.bbox, page.width),
                nearby_heading=active_heading if role != "section_heading" else None,
                field_shape=_field_shape_for_block(block, page),
                possible_parent=possible_parent,
                group_candidate=choice_group if role in {"choice_option", "followup_field"} else None,
                facts=_facts_for_block(block, page),
            )
            hints.append(hint)

            if role == "choice_option":
                last_choice_id = hint.id

        hints.extend(_table_hints(page, active_heading))
        hints.extend(_widget_hints(page, active_heading))

        return SemanticPageModel(
            page_number=page.page_number,
            width=page.width,
            height=page.height,
            parser_version=page.parser_version,
            hints=sorted(hints, key=lambda hint: (_source_order_key(hint.source_ids[0]), hint.id)),
        )


def build_semantic_page_model(page: RawPageModel) -> SemanticPageModel:
    return SemanticHintBuilder().build_page(page)


def _role_for_block(
    block: TextBlock,
    page: RawPageModel,
    order: int,
    block_count: int,
) -> str:
    text = _normalize_text(block.text)
    lower = text.lower()
    if order <= 2 and _looks_like_repeated_band(block.bbox, page.height, top=True):
        return "repeated_header"
    if order >= max(1, block_count - 1) and _looks_like_repeated_band(block.bbox, page.height, top=False):
        return "repeated_footer"
    if _contains_signature(lower) and _looks_like_signature_line(block, text, page):
        return "signature_block"
    if _choice_near_block(block, page.choice_glyph_candidates):
        return "choice_option"
    if _looks_like_blank_field(text):
        return "blank_field"
    if _looks_like_heading(block, text, page):
        return "section_heading"
    if _looks_like_instruction(lower):
        return "instruction"
    if _looks_like_question(text):
        return "question_stem"
    return "unknown"


def _table_hints(page: RawPageModel, active_heading: str | None) -> list[SemanticHint]:
    hints: list[SemanticHint] = []
    for table in page.table_candidates:
        if _table_covered_by_widgets(table, page.widgets):
            continue
        hints.append(
            SemanticHint(
                id=_semantic_id(table.id),
                role="table",
                text=" | ".join(table.header_text) if table.header_text else "Table",
                source_ids=[table.id],
                indent_level=_indent_level(table.bbox, 48),
                column=_column_for_bbox(table.bbox, page.width),
                nearby_heading=active_heading,
                facts=_facts_for_table(table),
            )
        )
        for index, header in enumerate(table.header_text, start=1):
            hints.append(
                SemanticHint(
                    id=f"{_semantic_id(table.id)}_c{index:02d}",
                    role="table_column",
                    text=header,
                    source_ids=[table.id],
                    indent_level=_indent_level(table.bbox, 48) + 1,
                    column=index,
                    nearby_heading=active_heading,
                    possible_parent=_semantic_id(table.id),
                )
            )
    return hints


def _widget_hints(page: RawPageModel, active_heading: str | None) -> list[SemanticHint]:
    hints: list[SemanticHint] = []
    for widget in page.widgets:
        role = _role_for_widget(widget)
        text = widget.field_label or widget.field_name or widget.field_type
        hints.append(
            SemanticHint(
                id=_semantic_id(widget.id),
                role=role,
                text=text,
                source_ids=[widget.id],
                indent_level=_indent_level(widget.bbox, 48),
                column=_column_for_bbox(widget.bbox, page.width),
                nearby_heading=active_heading,
                field_shape=_field_shape_for_widget(widget),
                facts={"widget_type": widget.field_type},
            )
        )
    return hints


def _role_for_widget(widget: FormWidget) -> str:
    if widget.field_type == "signature":
        return "signature_block"
    if widget.field_type in {"checkbox", "radio", "button"}:
        return "choice_option"
    if widget.field_type in {"combobox", "listbox"}:
        return "choice_group"
    return "blank_field"


def _semantic_id(raw_id: str) -> str:
    return raw_id.replace("_b0", "_b").replace("_w0", "_w").replace("_t0", "_t")


def _normalize_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\r", "\n")).strip()


def _single_line(text: str) -> str:
    return " ".join(_normalize_text(text).splitlines())


def _indent_unit(blocks: list[TextBlock]) -> float:
    x_positions = sorted({round(block.bbox.x0, 0) for block in blocks if block.text.strip()})
    if len(x_positions) < 2:
        return 48
    gaps = [right - left for left, right in zip(x_positions, x_positions[1:]) if right - left >= 8]
    return min(gaps) if gaps else 48


def _indent_level(bbox: BoundingBox, indent_unit: float) -> int:
    return max(0, round(bbox.x0 / max(indent_unit, 1)))


def _column_for_bbox(bbox: BoundingBox, page_width: float) -> int:
    if page_width <= 0:
        return 0
    midpoint = (bbox.x0 + bbox.x1) / 2
    if midpoint < page_width / 3:
        return 0
    if midpoint < page_width * 2 / 3:
        return 1
    return 2


def _looks_like_repeated_band(bbox: BoundingBox, page_height: float, *, top: bool) -> bool:
    if page_height <= 0:
        return False
    if top:
        return bbox.y0 <= page_height * 0.04
    return bbox.y1 >= page_height * 0.96


def _contains_signature(lower: str) -> bool:
    return "signature" in lower or "sign here" in lower or "signed by" in lower


SIGNATURE_SUBHEAD_TOKENS = frozenset({"printed name", "signature", "date", "initials"})


def _looks_like_signature_line(block: TextBlock, text: str, page: RawPageModel) -> bool:
    stripped = text.strip()
    if len(stripped) > 80:
        return False
    has_blank_run = bool(BLANK_PATTERN.search(stripped))
    if has_blank_run:
        return True
    near_signature_widget = any(
        widget.field_type == "signature"
        and abs(_mid_y(widget.bbox) - _mid_y(block.bbox)) <= max(40, block.bbox.height * 3)
        for widget in page.widgets
    )
    if near_signature_widget:
        return True
    line_count = len(block.line_ids) or 1
    tokens = {part.strip().lower() for part in stripped.replace("\n", "/").split("/")}
    return line_count <= 1 and bool(tokens & SIGNATURE_SUBHEAD_TOKENS) and len(tokens) <= 4


def _table_covered_by_widgets(table: TableCandidate, widgets: list[FormWidget]) -> bool:
    contained = sum(1 for widget in widgets if _bbox_contains(table.bbox, widget.bbox))
    threshold = max(2, ((table.column_count or 1) * (table.row_count or 1)) // 2)
    return contained >= threshold


def _blocks_redundant_with_widgets(
    blocks: list[TextBlock], page: RawPageModel
) -> set[str]:
    if not page.widgets:
        return set()
    widget_labels: list[tuple[str, FormWidget]] = []
    for widget in page.widgets:
        label = (widget.field_label or "").strip()
        if not label:
            continue
        widget_labels.append((_norm_label(label), widget))
    if not widget_labels:
        return set()

    suppressed: set[str] = set()
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        parts = [_norm_label(part) for part in text.replace("\n", "/").split("/")]
        parts = [part for part in parts if part]
        if len(parts) < 2:
            continue
        matched = 0
        for part in parts:
            for label, widget in widget_labels:
                if not _label_matches(part, label):
                    continue
                if not _bbox_near(widget.bbox, block.bbox, slack=200):
                    continue
                matched += 1
                break
        if matched >= max(2, len(parts) - 1):
            suppressed.add(block.id)
    return suppressed


def _is_micro_block(block: TextBlock) -> bool:
    return block.bbox.width < 12 and block.bbox.height < 12


def _norm_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _label_matches(part: str, label: str) -> bool:
    if not part or not label:
        return False
    if part == label or part in label or label in part:
        return True
    part_tokens = {token for token in part.split() if len(token) >= 3}
    label_tokens = {token for token in label.split() if len(token) >= 3}
    if not part_tokens or not label_tokens:
        return False
    overlap = len(part_tokens & label_tokens)
    smaller = min(len(part_tokens), len(label_tokens))
    return overlap / smaller >= 0.7


def _bbox_contains(outer: BoundingBox, inner: BoundingBox) -> bool:
    return (
        outer.x0 - 2 <= inner.x0
        and outer.y0 - 2 <= inner.y0
        and outer.x1 + 2 >= inner.x1
        and outer.y1 + 2 >= inner.y1
    )


def _bbox_near(left: BoundingBox, right: BoundingBox, *, slack: float) -> bool:
    return (
        abs(_mid_y(left) - _mid_y(right)) <= slack
        and abs(_mid_x(left) - _mid_x(right)) <= slack * 2
    )


def _mid_x(bbox: BoundingBox) -> float:
    return (bbox.x0 + bbox.x1) / 2


def _choice_near_block(block: TextBlock, candidates: list[ChoiceGlyphCandidate]) -> bool:
    return any(
        abs(_mid_y(candidate.bbox) - _mid_y(block.bbox)) <= max(10, block.bbox.height)
        and candidate.bbox.x0 <= block.bbox.x0 + 18
        for candidate in candidates
    )


def _looks_like_blank_field(text: str) -> bool:
    if BLANK_PATTERN.search(text):
        return True
    stripped = text.rstrip()
    if not stripped.endswith(":"):
        return False
    if _looks_like_instruction(stripped.lower()):
        return False
    last_line = stripped.splitlines()[-1] if stripped.splitlines() else stripped
    return len(stripped) <= 120 and len(last_line) <= 80


def _looks_like_heading(block: TextBlock, text: str, page: RawPageModel) -> bool:
    if len(text) > 100 or "?" in text:
        return False
    spans = [span for span in page.spans if span.id in _span_ids_for_block(block, page)]
    has_large_or_bold = any(span.is_bold or span.size >= 12 for span in spans)
    word_count = len(text.split())
    return has_large_or_bold and word_count <= 10


def _looks_like_instruction(lower: str) -> bool:
    if lower.startswith(INSTRUCTION_STARTS) or "must be completed" in lower:
        return True
    if len(lower) >= 200 and ("." in lower or ":" in lower):
        return True
    stripped = lower.strip()
    if stripped.endswith(":") and 6 <= len(stripped) <= 80 and not BLANK_PATTERN.search(stripped):
        return True
    return False


def _looks_like_question(text: str) -> bool:
    lower_words = set(re.findall(r"[a-zA-Z#]+", text.lower()))
    return "?" in text or bool(lower_words & QUESTION_WORDS)


def _is_followup_text(text: str) -> bool:
    lower = text.lower()
    return _looks_like_blank_field(text) or lower.startswith(("if ", "describe", "explain"))


def _field_shape_for_block(block: TextBlock, page: RawPageModel) -> str | None:
    text = block.text
    if BLANK_PATTERN.search(text):
        return "single_line_blank"
    if block.bbox.height > 40 and _looks_like_blank_field(text):
        return "multi_line_blank"
    if _choice_near_block(block, page.choice_glyph_candidates):
        return "checkbox_or_radio"
    return None


def _field_shape_for_widget(widget: FormWidget) -> str:
    if widget.field_type in {"checkbox", "radio", "button"}:
        return "checkbox_or_radio"
    if widget.field_type in {"combobox", "listbox"}:
        return "select_box"
    if widget.bbox.height > 28:
        return "multi_line_widget"
    return "single_line_widget"


def _facts_for_block(block: TextBlock, page: RawPageModel) -> dict[str, Any]:
    return {
        "line_count": len(block.line_ids),
        "relative_width": round(block.bbox.width / page.width, 3) if page.width else 0,
    }


def _facts_for_table(table: TableCandidate) -> dict[str, Any]:
    return {
        "row_count": table.row_count,
        "column_count": table.column_count,
        "source": table.source,
    }


def _span_ids_for_block(block: TextBlock, page: RawPageModel) -> set[str]:
    line_ids = set(block.line_ids)
    span_ids: set[str] = set()
    for line in page.lines:
        if line.id in line_ids:
            span_ids.update(line.span_ids)
    return span_ids


def _mid_y(bbox: BoundingBox) -> float:
    return (bbox.y0 + bbox.y1) / 2


def _source_order_key(source_id: str) -> tuple[int, str]:
    match = re.search(r"_(?:b|w|t)(\d+)", source_id)
    if not match:
        return (10_000, source_id)
    return (int(match.group(1)), source_id)
