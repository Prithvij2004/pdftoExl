from __future__ import annotations

from app.models import ExtractedRow, QuestionType
from app.services.section_refiner import (
    SectionAssignment,
    SectionRefinement,
    _merge_section_assignments,
    _section_prompt,
)


def _row(seq: int, section: str, question: str) -> ExtractedRow:
    return ExtractedRow(
        sequence=seq,
        section=section,
        question_type=QuestionType.TEXT_BOX,
        question_text=question,
        answer_text="",
        page_number=1,
        source_order=seq,
    )


def test_merge_section_assignments_updates_only_section_and_meta() -> None:
    rows = [
        _row(1, "Wrong Header", "Applicant Name"),
        _row(2, "Wrong Header", "Member ID"),
    ]
    refinement = SectionRefinement(
        assignments=[
            SectionAssignment(sequence=1, section="", confidence=0.9, rationale="Header row"),
            SectionAssignment(
                sequence=2,
                section="Member Information",
                confidence=0.95,
                rationale="Major grouping",
            ),
        ]
    )

    out = _merge_section_assignments(rows, refinement)

    assert [r.section for r in out] == ["", "Member Information"]
    assert [r.question_text for r in out] == ["Applicant Name", "Member ID"]
    assert [r.question_type for r in out] == [QuestionType.TEXT_BOX, QuestionType.TEXT_BOX]
    assert out[1].meta["section_refinement_confidence"] == 0.95
    assert out[1].meta["section_refinement_rationale"] == "Major grouping"


def test_section_prompt_balances_carry_forward_and_blank_sections() -> None:
    prompt = _section_prompt(
        [
            _row(1, "Clinical Information", "Recent visits"),
            _row(2, "", "Date"),
            _row(3, "", "Appendix notes"),
        ]
    )

    assert "Use only the exact visible major heading text" in prompt
    assert "Never summarize, shorten" in prompt
    assert "Most ordinary rows should inherit the active major section" in prompt
    assert "If a row is a continuation of a previous major section" in prompt
    assert "If the form clearly leaves an area outside the main sections" in prompt
    assert "If uncertain whether text is a true visual section heading" in prompt
    assert "Label attachment(s) as ..." in prompt
    assert "Bold text alone does not make a section" in prompt
    assert "Plan of Care" in prompt
    assert "Recent Events" in prompt
    assert "Behavioral Risk" in prompt
