from __future__ import annotations

from app.models import ExtractedRow, QuestionType
from app.services.normalize import normalize_rows


def _row(
    *,
    qt: QuestionType,
    q: str,
    a: str = "",
    section: str = "",
    page: int,
    order: int,
) -> ExtractedRow:
    return ExtractedRow(
        section=section,
        question_type=qt,
        question_text=q,
        answer_text=a,
        page_number=page,
        source_order=order,
    )


def test_repeated_page_header_display_is_kept_once_and_prefixed() -> None:
    rows = [
        _row(qt=QuestionType.DISPLAY, q="Form ABC", page=1, order=0),
        _row(qt=QuestionType.TEXT_BOX, q="Name", page=1, order=5),
        _row(qt=QuestionType.DISPLAY, q="Page 1 of 2", page=1, order=10),
        _row(qt=QuestionType.DISPLAY, q="Form ABC", page=2, order=0),
        _row(qt=QuestionType.TEXT_BOX, q="Name", page=2, order=5),
        _row(qt=QuestionType.DISPLAY, q="Page 2 of 2", page=2, order=10),
    ]

    out = normalize_rows(rows)

    # Header appears once and is marked.
    headers = [r for r in out if "Form ABC" in r.question_text]
    assert len(headers) == 1
    assert headers[0].question_text.lower().startswith("(header)")

    # Page-number footers are dropped.
    assert not any("page 1 of 2" in r.question_text.lower() for r in out)
    assert not any("page 2 of 2" in r.question_text.lower() for r in out)

    # Body fields are retained per page.
    assert [r.question_text for r in out if r.question_type == QuestionType.TEXT_BOX] == ["Name", "Name"]


def test_repeated_header_fillable_field_is_kept_once_and_prefixed() -> None:
    rows = [
        _row(qt=QuestionType.TEXT_BOX, q="Case Number", page=1, order=0),
        _row(qt=QuestionType.TEXT_AREA, q="Describe incident", page=1, order=5),
        _row(qt=QuestionType.DISPLAY, q="OMB No. 1234-5678", page=1, order=9),
        _row(qt=QuestionType.TEXT_BOX, q="Case Number", page=2, order=0),
        _row(qt=QuestionType.TEXT_AREA, q="Describe incident", page=2, order=5),
        _row(qt=QuestionType.DISPLAY, q="OMB No. 1234-5678", page=2, order=9),
    ]

    out = normalize_rows(rows)

    case = [r for r in out if "Case Number" in r.question_text]
    assert len(case) == 1
    assert case[0].question_text.lower().startswith("(header)")

    # Header/footer-only IDs/dates (like OMB/control/form metadata) are dropped.
    assert not any("omb" in r.question_text.lower() for r in out)

    # Non-header repeated body fields remain (not in header region).
    desc = [r for r in out if r.question_text == "Describe incident"]
    assert len(desc) == 2


def test_footer_instructions_are_kept_but_id_date_metadata_is_dropped() -> None:
    rows = [
        _row(qt=QuestionType.TEXT_BOX, q="Name", page=1, order=2),
        _row(qt=QuestionType.TEXT_BOX, q="Address", page=1, order=3),
        _row(qt=QuestionType.TEXT_BOX, q="City", page=1, order=4),
        _row(qt=QuestionType.TEXT_BOX, q="State", page=1, order=5),
        _row(qt=QuestionType.TEXT_BOX, q="Zip", page=1, order=6),
        _row(qt=QuestionType.DISPLAY, q="Submit completed form to your local office.", page=1, order=7),
        _row(qt=QuestionType.DISPLAY, q="Rev. 01/2024", page=1, order=8),
    ]
    out = normalize_rows(rows)
    assert any("submit completed form" in r.question_text.lower() for r in out)
    assert not any("rev." in r.question_text.lower() for r in out)


def test_non_repeated_footer_is_removed_if_not_valuable() -> None:
    rows = [
        _row(qt=QuestionType.TEXT_BOX, q="Name", page=1, order=2),
        _row(qt=QuestionType.TEXT_BOX, q="Address", page=1, order=3),
        _row(qt=QuestionType.TEXT_BOX, q="City", page=1, order=4),
        _row(qt=QuestionType.TEXT_BOX, q="State", page=1, order=5),
        _row(qt=QuestionType.TEXT_BOX, q="Zip", page=1, order=6),
        # Footer-like noise at the very end of the page.
        _row(qt=QuestionType.DISPLAY, q="www.example.com", page=1, order=7),
    ]
    out = normalize_rows(rows)
    assert "www.example.com" not in [r.question_text for r in out]
    assert "Name" in [r.question_text for r in out]


def test_merge_split_radio_options_across_pages() -> None:
    rows = [
        _row(
            qt=QuestionType.RADIO_BUTTON,
            q="What is your status?",
            a="A | B | C",
            page=1,
            order=0,
        ),
        _row(
            qt=QuestionType.RADIO_BUTTON,
            q="What is your status?",
            a="D | E",
            page=2,
            order=0,
        ),
    ]
    out = normalize_rows(rows)
    assert len(out) == 1
    assert out[0].question_type == QuestionType.RADIO_BUTTON
    assert set(p.strip() for p in out[0].answer_text.split("|")) == {
        "A",
        "B",
        "C",
        "D",
        "E",
    }
    assert out[0].page_number == 1
    assert 2 in (out[0].meta or {}).get("continuation_from_pages", [])


def test_merge_text_area_with_continued_marker_on_next_page() -> None:
    rows = [
        _row(
            qt=QuestionType.TEXT_AREA,
            q="Explain your history",
            a="Line one.",
            page=1,
            order=0,
        ),
        _row(
            qt=QuestionType.TEXT_AREA,
            q="(continued)",
            a="Line two.",
            page=2,
            order=0,
        ),
    ]
    out = normalize_rows(rows)
    assert len(out) == 1
    assert "Line one." in out[0].answer_text
    assert "Line two." in out[0].answer_text
    assert out[0].page_number == 1


def test_does_not_merge_repeated_name_across_pages() -> None:
    # Use source_order > 1 so these are not treated as page headers (see normalize header dedup).
    rows = [
        _row(qt=QuestionType.TEXT_BOX, q="Name", a="", page=1, order=3),
        _row(qt=QuestionType.TEXT_BOX, q="Name", a="", page=2, order=3),
    ]
    out = normalize_rows(rows)
    assert len([r for r in out if r.question_text == "Name"]) == 2


def test_multi_select_wording_makes_checkbox_group() -> None:
    r = _row(
        qt=QuestionType.CHECKBOX,
        q="Indicate all that apply (select all that apply)",
        a="X | Y | Z",
        page=1,
        order=0,
    )
    out = normalize_rows([r])
    assert len(out) == 1
    assert out[0].question_type == QuestionType.CHECKBOX_GROUP


def test_exclusive_wording_makes_radio_button() -> None:
    r = _row(
        qt=QuestionType.CHECKBOX_GROUP,
        q="Select only one option below",
        a="A | B | C",
        page=1,
        order=0,
    )
    out = normalize_rows([r])
    assert len(out) == 1
    assert out[0].question_type == QuestionType.RADIO_BUTTON


def test_section_is_cleaned_and_carried_forward() -> None:
    rows = [
        _row(qt=QuestionType.TEXT_BOX, section=" **Member Information:** ", q="First name", page=1, order=0),
        _row(qt=QuestionType.CALENDAR, q="Date of birth", page=1, order=1),
    ]

    out = normalize_rows(rows)

    assert [r.section for r in out] == ["Member Information:", "Member Information:"]


def test_section_is_not_inferred_from_display_heading() -> None:
    rows = [
        _row(qt=QuestionType.DISPLAY, q="Provider Details", page=1, order=0),
        _row(qt=QuestionType.TEXT_BOX, q="Provider name", page=1, order=1),
    ]

    out = normalize_rows(rows)

    assert [r.section for r in out] == ["", ""]


def test_field_label_section_is_removed() -> None:
    rows = [
        _row(qt=QuestionType.TEXT_BOX, section="DOB", q="Date of birth", page=1, order=0),
        _row(qt=QuestionType.TEXT_BOX, section="Member Information", q="Name", page=1, order=1),
    ]

    out = normalize_rows(rows)

    assert [r.section for r in out] == ["", "Member Information"]


def test_sequence_is_global_after_normalization() -> None:
    rows = [
        _row(qt=QuestionType.DISPLAY, q="Page 1 of 2", page=1, order=9),
        _row(qt=QuestionType.TEXT_BOX, q="Name", page=1, order=2),
        _row(qt=QuestionType.CALENDAR, q="Date of birth", page=1, order=3),
        _row(qt=QuestionType.TEXT_BOX, q="Address", page=2, order=1),
    ]

    out = normalize_rows(rows)

    assert [r.question_text for r in out] == ["Name", "Date of birth", "Address"]
    assert [r.sequence for r in out] == [1, 2, 3]

