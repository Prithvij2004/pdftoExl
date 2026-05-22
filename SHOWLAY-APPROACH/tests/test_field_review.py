from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SHOWLAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHOWLAY_DIR))

from showlay.field_review import (  # noqa: E402
    build_review_manifest,
    review_row_fields,
    write_review_manifest,
)
from showlay.schema import Row  # noqa: E402


@dataclass
class FakePage:
    page_index: int = 0
    width: float = 612.0
    height: float = 792.0
    image_path: str = "/tmp/page_1.png"
    text_blocks: list[dict[str, Any]] = field(default_factory=list)
    widgets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeDoc:
    pages: list[FakePage]
    pdf_path: str = "/tmp/input.pdf"
    has_acroform: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _page(*texts: str, widgets: list[dict[str, Any]] | None = None) -> FakePage:
    return FakePage(
        text_blocks=[
            {"text": text, "rect": [10, 10 + i * 12, 300, 22 + i * 12], "size": 10}
            for i, text in enumerate(texts)
        ],
        widgets=widgets or [],
    )


def _row(**kwargs) -> Row:
    data = {
        "sequence": 1,
        "page": 1,
        "question_type": "Text Box",
        "question_text": "Applicant Name",
        "answer_text": "",
        "answer_validation": "",
        "section": "Applicant",
        "required": "",
        "confidence": 0.95,
    }
    data.update(kwargs)
    return Row(**data)


def _review(case: dict) -> dict:
    rows = case.get("rows") or [case["row"]]
    target_index = case.get("target_index", 0)
    row = rows[target_index]
    rows_by_sequence = {r.sequence: r for r in rows if r.sequence is not None}
    page = None if case.get("page") == "missing" else case.get("page") or _page(row.question_text)
    fields = review_row_fields(
        row,
        page=page,
        rows_by_sequence=rows_by_sequence,
        seen_sequences=case.get("seen_sequences", set()),
    )
    return {field["field"]: field for field in fields}[case["field"]]


FIELD_CASES = [
    {
        "id": "sequence_missing",
        "field": "sequence",
        "row": _row(sequence=None),
        "risk": "high",
        "reason": "sequence_missing",
    },
    {
        "id": "sequence_duplicate",
        "field": "sequence",
        "row": _row(sequence=1),
        "seen_sequences": {1},
        "risk": "high",
        "reason": "sequence_duplicate",
    },
    {
        "id": "sequence_negative",
        "field": "sequence",
        "row": _row(sequence=-1),
        "risk": "high",
        "reason": "sequence_not_positive",
    },
    {
        "id": "sequence_valid",
        "field": "sequence",
        "row": _row(sequence=9),
        "risk": "low",
    },
    {
        "id": "question_type_missing",
        "field": "question_type",
        "row": _row(question_type=""),
        "risk": "high",
        "reason": "question_type_missing",
    },
    {
        "id": "question_type_unknown",
        "field": "question_type",
        "row": _row(question_type="Magic Field"),
        "risk": "high",
        "reason": "unknown_question_type:Magic Field",
    },
    {
        "id": "radio_without_options",
        "field": "question_type",
        "row": _row(question_type="Radio Button", answer_text=""),
        "risk": "high",
        "reason": "choice_type_without_enough_options",
    },
    {
        "id": "dropdown_one_option",
        "field": "question_type",
        "row": _row(question_type="Dropdown", answer_text="Yes"),
        "risk": "high",
        "reason": "choice_type_without_enough_options",
    },
    {
        "id": "checkbox_group_without_options",
        "field": "question_type",
        "row": _row(question_type="Checkbox Group", answer_text=""),
        "risk": "high",
        "reason": "choice_type_without_enough_options",
    },
    {
        "id": "checkbox_with_multiple_options",
        "field": "question_type",
        "row": _row(question_type="Checkbox", answer_text="A\n\nB"),
        "risk": "medium",
        "reason": "checkbox_with_multiple_options_maybe_group",
    },
    {
        "id": "group_table_no_evidence",
        "field": "question_type",
        "row": _row(question_type="Group Table", question_text="Falls"),
        "page": _page("Falls"),
        "risk": "medium",
        "reason": "group_table_not_supported_by_evidence",
    },
    {
        "id": "group_table_with_table_evidence",
        "field": "question_type",
        "row": _row(question_type="Group Table", question_text="Medication table"),
        "page": _page("Medication table columns rows"),
        "risk": "low",
    },
    {
        "id": "date_without_date_hint",
        "field": "question_type",
        "row": _row(question_type="Date", question_text="When completed"),
        "risk": "medium",
        "reason": "date_type_needs_visual_check",
    },
    {
        "id": "date_with_date_hint",
        "field": "question_type",
        "row": _row(question_type="Date", question_text="Date completed"),
        "risk": "low",
    },
    {
        "id": "number_without_number_hint",
        "field": "question_type",
        "row": _row(question_type="Number", question_text="Describe result"),
        "risk": "medium",
        "reason": "number_type_needs_visual_check",
    },
    {
        "id": "number_with_score_hint",
        "field": "question_type",
        "row": _row(question_type="Number", question_text="Total Score"),
        "risk": "low",
    },
    {
        "id": "signature_without_signature_hint",
        "field": "question_type",
        "row": _row(question_type="Signature", question_text="Representative"),
        "risk": "medium",
        "reason": "signature_type_needs_visual_check",
    },
    {
        "id": "signature_with_hint",
        "field": "question_type",
        "row": _row(question_type="Signature", question_text="Applicant Signature"),
        "risk": "low",
    },
    {
        "id": "question_text_missing",
        "field": "question_text",
        "row": _row(question_text=""),
        "risk": "high",
        "reason": "question_text_missing",
    },
    {
        "id": "question_text_page_missing",
        "field": "question_text",
        "row": _row(question_text="Applicant Name"),
        "page": "missing",
        "risk": "medium",
        "reason": "page_missing",
    },
    {
        "id": "question_text_page_text_missing",
        "field": "question_text",
        "row": _row(question_text="Applicant Name"),
        "page": _page(),
        "risk": "low",
        "reason": "page_text_missing",
    },
    {
        "id": "question_text_not_grounded",
        "field": "question_text",
        "row": _row(question_text="Applicant Name"),
        "page": _page("Completely different benefit details"),
        "risk": "high",
        "reason": "not_grounded_in_page_text",
    },
    {
        "id": "question_text_weak_grounding",
        "field": "question_text",
        "row": _row(question_text="Applicant full legal name"),
        "page": _page("Applicant legal address"),
        "risk": "low",
        "reason": "weak_text_grounding",
    },
    {
        "id": "question_text_generic_label",
        "field": "question_text",
        "row": _row(question_text="Date"),
        "page": _page("Date"),
        "risk": "low",
        "reason": "generic_label_needs_context",
    },
    {
        "id": "question_text_formulaic_label",
        "field": "question_text",
        "row": _row(question_text="default characters = 100"),
        "page": _page("Name"),
        "risk": "high",
        "reason": "formulaic_text_in_question_label",
    },
    {
        "id": "question_text_grounded",
        "field": "question_text",
        "row": _row(question_text="Applicant Name"),
        "page": _page("Applicant Name"),
        "risk": "low",
    },
    {
        "id": "branching_blank_ok",
        "field": "branching_logic",
        "row": _row(branching_logic=""),
        "risk": "low",
    },
    {
        "id": "branching_possible_missing",
        "field": "branching_logic",
        "row": _row(sequence=2, question_text="If yes, describe", branching_logic=""),
        "risk": "medium",
        "reason": "possible_missing_branching_logic",
    },
    {
        "id": "branching_syntax_unusual",
        "field": "branching_logic",
        "row": _row(sequence=2, branching_logic="When Q1 is Yes"),
        "risk": "high",
        "reason": "branching_syntax_unusual",
    },
    {
        "id": "branching_missing_ref",
        "field": "branching_logic",
        "row": _row(sequence=2, branching_logic="If selected"),
        "risk": "high",
        "reason": "branching_ref_missing",
    },
    {
        "id": "branching_ref_not_found",
        "field": "branching_logic",
        "row": _row(sequence=2, branching_logic="If Q9 = Yes"),
        "risk": "high",
        "reason": "branching_ref_not_found:Q9",
    },
    {
        "id": "branching_forward_ref",
        "field": "branching_logic",
        "rows": [_row(sequence=2, branching_logic="If Q2 = Yes")],
        "risk": "medium",
        "reason": "branching_forward_ref:Q2",
    },
    {
        "id": "branching_literal_not_parent_option",
        "field": "branching_logic",
        "rows": [
            _row(sequence=1, question_type="Radio Button", answer_text="Yes\n\nNo"),
            _row(sequence=2, branching_logic="Display if Q1 = Maybe"),
        ],
        "target_index": 1,
        "risk": "medium",
        "reason": "branching_literal_not_in_parent_options",
    },
    {
        "id": "branching_valid_checked",
        "field": "branching_logic",
        "rows": [
            _row(sequence=1, question_type="Checkbox", question_text="Has attachment?"),
            _row(sequence=2, branching_logic="If Q1 = checked(selected)"),
        ],
        "target_index": 1,
        "risk": "low",
    },
    {
        "id": "branching_valid_literal",
        "field": "branching_logic",
        "rows": [
            _row(sequence=1, question_type="Radio Button", answer_text="Yes\n\nNo"),
            _row(sequence=2, branching_logic="Display if Q1 = Yes"),
        ],
        "target_index": 1,
        "risk": "low",
    },
    {
        "id": "answer_choice_missing",
        "field": "answer_text",
        "row": _row(question_type="Radio Button", answer_text=""),
        "risk": "high",
        "reason": "missing_or_single_choice_option",
    },
    {
        "id": "answer_choice_single",
        "field": "answer_text",
        "row": _row(question_type="Dropdown", answer_text="Yes"),
        "risk": "high",
        "reason": "missing_or_single_choice_option",
    },
    {
        "id": "answer_choice_long",
        "field": "answer_text",
        "row": _row(question_type="Radio Button", answer_text=("A" * 190) + "\n\nNo"),
        "risk": "medium",
        "reason": "choice_option_unusually_long",
    },
    {
        "id": "answer_choice_valid",
        "field": "answer_text",
        "row": _row(question_type="Radio Button", answer_text="Yes\n\nNo"),
        "risk": "low",
    },
    {
        "id": "answer_input_unexpected",
        "field": "answer_text",
        "row": _row(question_type="Text Box", answer_text="Blue"),
        "risk": "medium",
        "reason": "unexpected_answer_text_for_input",
    },
    {
        "id": "answer_input_formulaic",
        "field": "answer_text",
        "row": _row(question_type="Text Box", answer_text="default characters = 100"),
        "risk": "low",
        "reason": "formulaic_default_generated",
    },
    {
        "id": "answer_new_section_missing_title",
        "field": "answer_text",
        "row": _row(question_type="Display", question_text="New Section", answer_text=""),
        "risk": "high",
        "reason": "section_display_missing_answer_text",
    },
    {
        "id": "answer_group_table_blank",
        "field": "answer_text",
        "row": _row(question_type="Group Table", answer_text=""),
        "risk": "medium",
        "reason": "group_table_parent_without_answer_text",
    },
    {
        "id": "validation_blank_date",
        "field": "answer_validation",
        "row": _row(question_type="Date", answer_validation=""),
        "risk": "medium",
        "reason": "validation_blank_for_typed_input",
    },
    {
        "id": "validation_date_unusual",
        "field": "answer_validation",
        "row": _row(question_type="Date", answer_validation="100 characters"),
        "risk": "medium",
        "reason": "date_validation_unusual",
    },
    {
        "id": "validation_number_unusual",
        "field": "answer_validation",
        "row": _row(question_type="Number", answer_validation="Free text"),
        "risk": "medium",
        "reason": "number_validation_unusual",
    },
    {
        "id": "validation_choice_row",
        "field": "answer_validation",
        "row": _row(question_type="Radio Button", answer_text="Yes\n\nNo", answer_validation="Date"),
        "risk": "medium",
        "reason": "choice_row_has_answer_validation",
    },
    {
        "id": "validation_number_valid",
        "field": "answer_validation",
        "row": _row(question_type="Number", answer_validation="Numeric"),
        "risk": "low",
    },
    {
        "id": "section_new_missing",
        "field": "section",
        "row": _row(question_type="Display", question_text="New Section", section=""),
        "risk": "high",
        "reason": "new_section_row_missing_section",
    },
    {
        "id": "section_not_grounded",
        "field": "section",
        "row": _row(section="Safety Review"),
        "page": _page("Applicant Name"),
        "risk": "medium",
        "reason": "section_not_grounded_on_page",
    },
    {
        "id": "section_grounded",
        "field": "section",
        "row": _row(section="Applicant"),
        "page": _page("Applicant", "Applicant Name"),
        "risk": "low",
    },
    {
        "id": "required_invalid",
        "field": "required",
        "row": _row(required="Maybe"),
        "risk": "high",
        "reason": "required_not_yes_no_or_blank",
    },
    {
        "id": "required_yes",
        "field": "required",
        "row": _row(required="Yes"),
        "risk": "low",
    },
    {
        "id": "required_no",
        "field": "required",
        "row": _row(required="No"),
        "risk": "low",
    },
    {
        "id": "required_blank",
        "field": "required",
        "row": _row(required=""),
        "risk": "low",
    },
]


@pytest.mark.parametrize("case", FIELD_CASES, ids=[case["id"] for case in FIELD_CASES])
def test_field_review_cases(case):
    result = _review(case)

    assert result["risk_level"] == case["risk"]
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["suggested_action"]
    if "reason" in case:
        assert case["reason"] in result["review_reasons"]
    else:
        assert result["review_reasons"] == []


def test_build_review_manifest_includes_page_cache_and_field_summary():
    rows = [
        _row(sequence=1, question_text="Applicant Name", confidence=0.95),
        _row(
            sequence=2,
            question_type="Radio Button",
            question_text="Does applicant live alone?",
            answer_text="",
            confidence=0.6,
        ),
    ]
    doc = FakeDoc(pages=[_page("Applicant Name", "Does applicant live alone? Yes No")])

    manifest = build_review_manifest(
        rows,
        doc_struct=doc,
        raw_vlm=[
            {"_page": 1, "question_type": "Text Box", "question_text": "Applicant Name"},
            {"_page": 1, "question_type": "Radio Button", "question_text": "Does applicant live alone?"},
        ],
        telemetry=[{"page": 1, "elapsed_s": 1.2}],
        run_id="case-1",
        source_pdf="/tmp/input.pdf",
        template_path="/tmp/template.xlsx",
    )

    assert manifest["schema_version"] == "showlay.field_review.v1"
    assert manifest["summary"]["row_count"] == 2
    assert manifest["summary"]["rows_needing_review"] >= 1
    assert manifest["summary"]["field_risk_counts"]["high"] >= 1
    assert manifest["pages"][0]["text_blocks"]
    assert manifest["rows"][1]["fields"]["question_type"]["risk_level"] == "high"
    assert manifest["rows"][0]["source"]["raw_vlm_index"] == 0


def test_compound_signature_labels_use_local_repeated_field_bbox():
    page = _page(
        "Applicant, Member or Authorized Representative:",
        "Printed Name",
        "Signature",
        "Date",
        "Witness, if applicable:",
        "Printed Name",
        "Signature",
        "Date",
        "Service Coordinator:",
        "Printed Name",
        "Signature",
        "Date",
    )
    page.text_blocks[0]["rect"] = [20, 300, 240, 314]
    page.text_blocks[1]["rect"] = [20, 360, 85, 374]
    page.text_blocks[2]["rect"] = [236, 360, 283, 374]
    page.text_blocks[3]["rect"] = [452, 360, 474, 374]
    page.text_blocks[4]["rect"] = [20, 390, 112, 404]
    page.text_blocks[5]["rect"] = [20, 430, 85, 444]
    page.text_blocks[6]["rect"] = [236, 430, 283, 444]
    page.text_blocks[7]["rect"] = [452, 430, 474, 444]
    page.text_blocks[8]["rect"] = [20, 530, 112, 544]
    page.text_blocks[9]["rect"] = [20, 582, 85, 596]
    page.text_blocks[10]["rect"] = [236, 582, 283, 596]
    page.text_blocks[11]["rect"] = [452, 582, 474, 596]
    doc = FakeDoc(pages=[page])
    rows = [
        _row(sequence=1, question_type="Signature", question_text="Witness Signature:"),
        _row(sequence=2, question_type="Date", question_text="Witness Date:"),
        _row(sequence=3, question_type="Date", question_text="Service Coordinator Date:"),
    ]

    manifest = build_review_manifest(rows, doc_struct=doc)

    assert manifest["rows"][0]["source"]["nearest_text_block"]["rect"] == [236, 430, 283, 444]
    assert manifest["rows"][1]["source"]["nearest_text_block"]["rect"] == [452, 430, 474, 444]
    assert manifest["rows"][2]["source"]["nearest_text_block"]["rect"] == [452, 582, 474, 596]
    assert (
        manifest["rows"][2]["source"]["nearest_text_block"]["evidence_source"]
        == "compound_role_field"
    )


def test_inline_text_block_labels_get_distinct_ordered_bboxes():
    page = _page("Applicant Name: __________________   SSN: __________   DOB: __________")
    page.text_blocks[0]["rect"] = [36, 116, 578, 128]
    doc = FakeDoc(pages=[page])
    rows = [
        _row(sequence=1, question_type="Text Box", question_text="Applicant Name:"),
        _row(sequence=2, question_type="Text Box", question_text="SSN:"),
        _row(sequence=3, question_type="Date", question_text="DOB:"),
    ]

    manifest = build_review_manifest(rows, doc_struct=doc)
    rects = [row["source"]["nearest_text_block"]["rect"] for row in manifest["rows"]]

    assert rects[0] != rects[1] != rects[2]
    assert rects[0][0] < rects[1][0] < rects[2][0]
    assert all(
        row["source"]["nearest_text_block"]["evidence_source"] == "text_span"
        for row in manifest["rows"]
    )


def test_repeated_same_label_uses_next_matching_bbox_in_row_order():
    page = _page(
        "Lives in own home/apt (with others)—specify relationship __________________",
        "Lives in other’s home—specify relationship __________________",
    )
    page.text_blocks[0]["rect"] = [64, 318, 528, 331]
    page.text_blocks[1]["rect"] = [64, 332, 526, 345]
    doc = FakeDoc(pages=[page])
    rows = [
        _row(sequence=1, question_type="Text Box", question_text="Specify Relationship"),
        _row(sequence=2, question_type="Text Box", question_text="Specify Relationship"),
    ]

    manifest = build_review_manifest(rows, doc_struct=doc)

    assert manifest["rows"][0]["source"]["nearest_text_block"]["rect"][1] == 318
    assert manifest["rows"][1]["source"]["nearest_text_block"]["rect"][1] == 332


def test_reverse_specify_label_prefers_matching_line_not_same_tokens_elsewhere():
    page = _page(
        "Lives in other’s home—specify relationship __________________",
        "Other—specify______________________________________________",
    )
    page.text_blocks[0]["rect"] = [64, 332, 526, 345]
    page.text_blocks[1]["rect"] = [64, 372, 525, 385]
    doc = FakeDoc(pages=[page])

    manifest = build_review_manifest(
        [_row(sequence=1, question_type="Text Box", question_text="Specify Other")],
        doc_struct=doc,
    )

    assert manifest["rows"][0]["source"]["nearest_text_block"]["rect"][1] == 372


def test_write_review_manifest_round_trips_json(tmp_path):
    doc = FakeDoc(pages=[_page("Applicant Name")])
    manifest = build_review_manifest([_row()], doc_struct=doc, run_id="round-trip")
    path = tmp_path / "review_manifest.json"

    written = write_review_manifest(path, manifest)
    loaded = json.loads(Path(written).read_text(encoding="utf-8"))

    assert loaded["run_id"] == "round-trip"
    assert loaded["rows"][0]["fields"]["question_text"]["field"] == "question_text"
