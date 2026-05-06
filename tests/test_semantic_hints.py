from __future__ import annotations

import json
from pathlib import Path

from app.services.layout_parser import (
    BoundingBox,
    CheapLayoutParser,
    ChoiceGlyphCandidate,
    FormWidget,
    RawPageModel,
    TableCandidate,
    TextBlock,
    TextLine,
    TextSpan,
)
from app.services.semantic_hints import (
    SEMANTIC_ROLES,
    SemanticHintBuilder,
    build_semantic_page_model,
)


def test_semantic_hint_builder_classifies_synthetic_form_roles() -> None:
    page = _raw_page(
        blocks=[
            _block("p3_b0001", "Medical Documentation", 72, 50, bold=True),
            _block("p3_b0002", "Complete this section for attached documentation.", 72, 82),
            _block("p3_b0003", "☐ Hospital records attached", 72, 112),
            _block("p3_b0004", "Description of documentation attached: ______", 96, 138),
            _block("p3_b0005", "Applicant Signature __________________", 72, 180),
        ],
        choice_candidates=[
            ChoiceGlyphCandidate(
                id="p3_d0001",
                glyph="box",
                bbox=BoundingBox(72, 112, 82, 122),
                source="drawing_shape",
                nearest_text="Hospital records attached",
                confidence=0.55,
            )
        ],
    )

    semantic = SemanticHintBuilder().build_page(page)

    roles_by_text = {hint.text: hint.role for hint in semantic.hints}
    assert roles_by_text["Medical Documentation"] == "section_heading"
    assert roles_by_text["Complete this section for attached documentation."] == "instruction"
    assert roles_by_text["☐ Hospital records attached"] == "choice_option"
    assert roles_by_text["Description of documentation attached: ______"] == "blank_field"
    assert roles_by_text["Applicant Signature __________________"] == "signature_block"

    followup = next(
        hint for hint in semantic.hints if hint.text == "Description of documentation attached: ______"
    )
    assert followup.nearby_heading == "Medical Documentation"
    assert followup.field_shape == "single_line_blank"

    choice = next(hint for hint in semantic.hints if hint.role == "choice_option")
    assert choice.group_candidate == "p3_g01"
    assert choice.id.startswith("p3_b")


def test_semantic_hint_builder_emits_table_columns_and_widget_hints() -> None:
    page = _raw_page(
        blocks=[_block("p1_b0001", "Services", 72, 50, bold=True)],
        widgets=[
            FormWidget(
                id="p1_w0001",
                field_name="member_name",
                field_label="Member Name",
                field_type="text",
                value=None,
                bbox=BoundingBox(180, 90, 360, 108),
            )
        ],
        table_candidates=[
            TableCandidate(
                id="p1_t0001",
                bbox=BoundingBox(72, 130, 500, 230),
                row_count=4,
                column_count=3,
                source="test",
                header_text=["Service", "Frequency", "Provider"],
            )
        ],
    )

    semantic = build_semantic_page_model(page)

    table = next(hint for hint in semantic.hints if hint.role == "table")
    columns = [hint for hint in semantic.hints if hint.role == "table_column"]
    widget = next(hint for hint in semantic.hints if "p1_w" in hint.id)

    assert table.text == "Service | Frequency | Provider"
    assert [column.text for column in columns] == ["Service", "Frequency", "Provider"]
    assert all(column.possible_parent == table.id for column in columns)
    assert widget.role == "blank_field"
    assert widget.field_shape == "single_line_widget"


def test_semantic_hint_model_is_json_serializable_and_prompt_friendly() -> None:
    raw_model = CheapLayoutParser().parse(Path("docs/sample-input-1.pdf"))

    semantic = build_semantic_page_model(raw_model.pages[0])
    payload = semantic.to_dict()
    decoded = json.loads(json.dumps(payload))
    prompt_text = semantic.to_prompt_text()

    assert decoded["page_number"] == 1
    assert decoded["hints"]
    assert all(hint["role"] in SEMANTIC_ROLES for hint in decoded["hints"])
    assert "[" in prompt_text
    assert "role=" in prompt_text
    assert not any("bbox" in hint for hint in decoded["hints"])


def _raw_page(
    *,
    blocks: list[TextBlock],
    widgets: list[FormWidget] | None = None,
    choice_candidates: list[ChoiceGlyphCandidate] | None = None,
    table_candidates: list[TableCandidate] | None = None,
) -> RawPageModel:
    page_number = _page_number_from_id(blocks[0].id) if blocks else 1
    lines: list[TextLine] = []
    spans: list[TextSpan] = []
    for block_index, block in enumerate(blocks, start=1):
        line_id = block.line_ids[0]
        span_id = f"p1_s{block_index:04d}"
        lines.append(TextLine(id=line_id, text=block.text, bbox=block.bbox, span_ids=[span_id]))
        spans.append(
            TextSpan(
                id=span_id,
                text=block.text,
                bbox=block.bbox,
                font="Helvetica-Bold" if block.id.endswith("bold") else "Helvetica",
                size=14 if block.id.endswith("bold") else 10,
                flags=16 if block.id.endswith("bold") else 0,
                color=None,
                is_bold=block.id.endswith("bold"),
                is_italic=False,
            )
        )
    return RawPageModel(
        page_number=page_number,
        width=612,
        height=792,
        parser_version="test",
        spans=spans,
        lines=lines,
        blocks=blocks,
        widgets=widgets or [],
        choice_glyph_candidates=choice_candidates or [],
        table_candidates=table_candidates or [],
    )


def _block(block_id: str, text: str, x0: float, y0: float, *, bold: bool = False) -> TextBlock:
    suffix = "bold" if bold else ""
    return TextBlock(
        id=f"{block_id}{suffix}",
        text=text,
        bbox=BoundingBox(x0, y0, x0 + 260, y0 + 14),
        line_ids=[block_id.replace("_b", "_l")],
        block_type="text",
    )


def _page_number_from_id(source_id: str) -> int:
    return int(source_id.split("_", maxsplit=1)[0].removeprefix("p"))
