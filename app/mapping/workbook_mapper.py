from __future__ import annotations

import re

from app.schemas.form_schema import DocumentExtraction, Followup, FormItem, Option
from app.schemas.workbook_schema import WorkbookRow, WorkbookQuestionType


DATE_LABEL_RE = re.compile(r"\b(date|dob|birth|begin date|end date|revision date|signed)\b", re.IGNORECASE)
NUMBER_LABEL_RE = re.compile(r"\b(score|count|number|#|fall #)\b", re.IGNORECASE)
SIGNATURE_LABEL_RE = re.compile(r"\b(signature|signed)\b", re.IGNORECASE)


ITEM_TYPE_MAP: dict[str, WorkbookQuestionType] = {
    "display": "Display",
    "section_marker": "Display",
    "review_required": "Display",
    "text_box": "Text Box",
    "text_area": "Text Area",
    "date": "Date",
    "number": "Number",
    "signature": "Signature",
    "radio_group": "Radio Button",
    "checkbox_item": "Checkbox",
    "dropdown": "Dropdown",
    "table": "Group Table",
}

INLINE_TYPE_MAP: dict[str, WorkbookQuestionType] = {
    "Text Box": "Text Box",
    "Text Area": "Text Area",
    "Date": "Date",
    "Number": "Number",
    "Signature": "Signature",
    "Dropdown": "Dropdown",
    "None": "Display",
}

FOLLOWUP_TYPE_MAP: dict[str, WorkbookQuestionType] = {
    "Text Box": "Text Box",
    "Text Area": "Text Area",
    "Date": "Date",
    "Number": "Number",
    "Signature": "Signature",
    "Dropdown": "Dropdown",
    "Display": "Display",
    "Review Required": "Display",
}


def _item_text(item: FormItem) -> str:
    if item.item_type == "display":
        return item.display_text or item.question_text or item.source_text
    if item.item_type == "table" and item.table:
        return item.table.table_name or item.question_text or "Table"
    return item.question_text or item.display_text or item.source_text or "Review required"


def _display_label_and_answer(item: FormItem) -> tuple[str, str]:
    text = item.display_text or item.question_text or item.source_text
    text = (text or "").strip()
    if not text:
        return "Display", ""

    # Turn "Freedom of Choice: body..." into label + retained full display text.
    label, sep, _body = text.partition(":")
    if sep and 3 <= len(label.strip()) <= 90:
        return label.strip(), text

    return text, "" if text == item.question_text else text


def _answer_text(item: FormItem) -> str:
    if item.item_type in {"radio_group", "dropdown"}:
        return "\n".join(option.option_text for option in item.options if option.option_text)
    if item.item_type == "checkbox_item":
        return ""
    if item.item_type == "display":
        return _display_label_and_answer(item)[1]
    return ""


def _mapped_question_type(item: FormItem, question_text: str) -> WorkbookQuestionType:
    if item.item_type in {"text_box", "review_required"}:
        if DATE_LABEL_RE.search(question_text):
            return "Date"
        if SIGNATURE_LABEL_RE.search(question_text):
            return "Signature"
        if NUMBER_LABEL_RE.search(question_text):
            return "Number"
    return ITEM_TYPE_MAP.get(item.item_type, "Display")


def _is_noise_item(item: FormItem) -> bool:
    text = _item_text(item).strip().lower()
    return text in {"<!-- image -->", "image", "<!-- image -->".lower()}


def _signature_context(group_index: int, last_display_label: str) -> str:
    if "service coordinator" in last_display_label.lower():
        return "Service Coordinator"
    if group_index == 1:
        return "Applicant/Member or Authorized Representative"
    if group_index == 2:
        return "Witness"
    if group_index == 3:
        return "Service Coordinator"
    return "Signer"


def _contextual_signature_label(label: str, context: str) -> str:
    clean = label.strip().rstrip(":")
    if clean.lower() == "printed name":
        if context == "Witness":
            return "Witness Name:"
        if context == "Service Coordinator":
            return "Service Coordinator Name:"
        return f"{context} Printed Name:"
    if clean.lower() == "signature":
        return f"{context} Signature:"
    if clean.lower() == "date":
        return f"{context} Date Signed:"
    return label


def _branch_for_option(parent_sequence: int, option: Option) -> str:
    label = option.option_text or option.option_id
    return f'Display if Q{parent_sequence} = "{label}"'


def _branch_for_followup(parent_sequence: int, followup: Followup) -> str:
    trigger = followup.trigger.strip()
    option_match = re.search(r"option ['\"](.+?)['\"] is selected", trigger, re.IGNORECASE)
    if option_match:
        return f'Display if Q{parent_sequence} = "{option_match.group(1)}"'
    if re.search(r"checked|selected", trigger, re.IGNORECASE):
        return f"If Q{parent_sequence} = checked(selected)"
    return f"Q{parent_sequence}: {trigger}" if trigger else f"If Q{parent_sequence} = checked(selected)"


class _Builder:
    def __init__(self) -> None:
        self.rows: list[WorkbookRow] = []

    def add(
        self,
        *,
        section: str,
        question_type: WorkbookQuestionType,
        question_text: str,
        question_rule: str = "",
        branching_logic: str = "",
        answer_text: str = "",
        needs_review: bool = False,
        confidence_score: float = 0.0,
        review_reason: str = "",
    ) -> WorkbookRow:
        row = WorkbookRow(
            section=section,
            sequence=len(self.rows) + 1,
            question_rule=question_rule,
            question_type=question_type,
            question_text=question_text,
            branching_logic=branching_logic,
            answer_text=answer_text,
            needs_review=needs_review,
            confidence_score=confidence_score,
            review_reason=review_reason,
        )
        self.rows.append(row)
        return row


def map_document_to_workbook_rows(doc: DocumentExtraction) -> list[WorkbookRow]:
    builder = _Builder()

    for section in doc.sections:
        section_title = section.section_title
        signer_group_index = 0
        active_signer_context = ""
        last_display_label = ""
        if section_title:
            builder.add(
                section=section_title,
                question_type="Display",
                question_text="New Section",
                answer_text=section_title,
            )

        for item in section.items:
            if item.item_type == "section_marker":
                continue
            if _is_noise_item(item):
                continue

            question_text = _item_text(item)
            answer_text = _answer_text(item)

            if item.item_type == "display":
                question_text, answer_text = _display_label_and_answer(item)
                last_display_label = question_text

            if question_text.strip().lower() == "printed name":
                signer_group_index += 1
                active_signer_context = _signature_context(signer_group_index, last_display_label)

            if question_text.strip().lower() in {"printed name", "signature", "date"} and active_signer_context:
                question_text = _contextual_signature_label(question_text, active_signer_context)

            parent = builder.add(
                section=section_title,
                question_rule=item.question_rule,
                question_type=_mapped_question_type(item, question_text),
                question_text=question_text,
                answer_text=answer_text,
                needs_review=item.needs_review,
                confidence_score=item.confidence,
                review_reason=item.review_reason,
            )

            if item.item_type in {"radio_group", "dropdown"}:
                for option in item.options:
                    inline = option.inline_input
                    if not inline or inline.question_type == "None":
                        continue
                    builder.add(
                        section=section_title,
                        question_type=INLINE_TYPE_MAP[inline.question_type],
                        question_text=inline.label or f"{option.option_text} - specify",
                        branching_logic=_branch_for_option(parent.sequence, option),
                        needs_review=inline.needs_review or option.needs_review,
                        confidence_score=item.confidence,
                        review_reason=inline.review_reason or option.review_reason,
                    )

            for followup in item.followups:
                builder.add(
                    section=section_title,
                    question_type=FOLLOWUP_TYPE_MAP.get(followup.question_type, "Display"),
                    question_text=followup.question_text or "Review required",
                    branching_logic=_branch_for_followup(parent.sequence, followup),
                    needs_review=followup.needs_review,
                    confidence_score=item.confidence,
                    review_reason=followup.review_reason,
                )

            if item.item_type == "table" and item.table:
                for column in item.table.columns:
                    builder.add(
                        section=section_title,
                        question_type=column.question_type if column.question_type != "Unknown" else "Text Box",  # type: ignore[arg-type]
                        question_text=column.column_name,
                        answer_text="\n".join(column.answer_options),
                        needs_review=column.question_type == "Unknown",
                        confidence_score=item.confidence,
                        review_reason="Unknown table column type" if column.question_type == "Unknown" else "",
                    )

    return builder.rows
