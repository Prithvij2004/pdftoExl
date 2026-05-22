"""Generic normalization for the canonical section-aware pipeline."""
from __future__ import annotations

import re
from collections import Counter

from .schema import Row

_QUESTION_TYPE_MAP = {
    "radio": "Radio Button",
    "radio button": "Radio Button",
    "radio buttons": "Radio Button",
    "drop down": "Dropdown",
    "dropdown": "Dropdown",
    "select": "Dropdown",
    "checkbox": "Checkbox",
    "check box": "Checkbox",
    "checkbox group": "Checkbox Group",
    "check box group": "Checkbox Group",
    "text": "Text Box",
    "text box": "Text Box",
    "textbox": "Text Box",
    "text area": "Text Area",
    "textarea": "Text Area",
    "date": "Date",
    "number": "Number",
    "numeric": "Number",
    "signature": "Signature",
    "display": "Display",
    "instruction": "Display",
    "group table": "Group Table",
    "table": "Group Table",
    "section header": "Section Header",
}

_VALIDATION_DEFAULTS = {
    "text box": "default characters = 100",
    "text area": "default characters = 600",
    "date": "Format is mm/dd/yyyy",
    "number": "only allow numeric characters",
    "signature": "",
}

_CHOICE_TYPES = {"radio button", "dropdown", "checkbox group"}
_Q_REF_RE = re.compile(r"\bq(\d+)\b", re.IGNORECASE)


def normalize_question_type(value: str) -> str:
    key = re.sub(r"\s+", " ", (value or "").strip().lower())
    return _QUESTION_TYPE_MAP.get(key, value or "")


def concatenate_sections(section_rows: list[list[Row]]) -> list[Row]:
    out: list[Row] = []
    for rows in section_rows:
        out.extend(rows)
    return out


def assign_global_sequence(rows: list[Row]) -> list[Row]:
    for index, row in enumerate(rows, start=1):
        row.sequence = index
    return rows


def ensure_dense_sections(rows: list[Row]) -> list[Row]:
    current = ""
    for row in rows:
        if row.question_type.strip().lower() in {"section header"}:
            row.question_type = "Display"
            title = (row.question_text or row.answer_text or "").strip().rstrip(":")
            row.question_text = "New Section"
            row.answer_text = title
            row.section = title

        if row.question_type.strip().lower() == "display" and row.question_text.strip().lower() == "new section":
            current = (row.answer_text or row.section or "").strip().rstrip(":")
            row.section = current
            continue
        if row.section:
            current = row.section.strip().rstrip(":")
        elif current:
            row.section = current
    return rows


def build_external_id_index(rows: list[Row]) -> dict[str, int]:
    index: dict[str, int] = {}
    for row in rows:
        external_id = getattr(row, "external_id", "") or ""
        if external_id and row.sequence is not None:
            index[external_id.strip()] = row.sequence
    return index


def _replace_external_ids(value: str, external_id_index: dict[str, int]) -> str:
    out = value or ""
    for external_id, sequence in sorted(external_id_index.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(external_id)}(?![A-Za-z0-9])")
        out = pattern.sub(f"Q{sequence}", out)
    return out


def resolve_item_code_branches(rows: list[Row]) -> list[Row]:
    external_id_index = build_external_id_index(rows)
    for row in rows:
        source = (getattr(row, "branching_source", "") or "").strip()
        rule = (row.question_rule or "").strip()
        model_branching = (row.branching_logic or "").strip()

        branch_candidate = model_branching or source
        if branch_candidate:
            row.branching_logic = _replace_external_ids(branch_candidate, external_id_index)
        if rule:
            row.question_rule = _replace_external_ids(rule, external_id_index)
    return rows


def apply_validation_defaults(rows: list[Row]) -> list[Row]:
    for row in rows:
        if row.answer_validation:
            continue
        default = _VALIDATION_DEFAULTS.get(row.question_type.strip().lower())
        if default:
            row.answer_validation = default
    return rows


def validate_choice_options(rows: list[Row]) -> list[Row]:
    for row in rows:
        if row.question_type.strip().lower() not in _CHOICE_TYPES:
            continue
        value = (row.answer_text or "").replace("\r\n", "\n").replace("\r", "\n")
        parts = [part.strip() for part in re.split(r"\n{1,}", value) if part.strip()]
        if len(parts) < 2:
            row.review_reasons.append("choice_options_missing_or_single")
            continue
        row.answer_text = "\n\n".join(parts)
    return rows


def validate_branching_refs(rows: list[Row]) -> list[Row]:
    seq_set = {row.sequence for row in rows if row.sequence is not None}
    for row in rows:
        for value in (row.question_rule, row.branching_logic):
            for match in _Q_REF_RE.finditer(value or ""):
                ref = int(match.group(1))
                if ref not in seq_set:
                    row.review_reasons.append(f"branching_ref_missing:Q{ref}")
        # Forward references are valid for skip logic and are not flagged.
    return rows


def emit_warnings(rows: list[Row]) -> list[str]:
    counts = Counter(reason for row in rows for reason in row.review_reasons)
    return [f"{reason}: {count}" for reason, count in sorted(counts.items())]


def normalize_rows(rows: list[Row]) -> tuple[list[Row], list[str]]:
    for row in rows:
        row.question_type = normalize_question_type(row.question_type)
        if row.required not in {"Yes", "No", ""}:
            row.review_reasons.append("required_not_yes_no_or_blank")
        if not row.question_text.strip():
            row.review_reasons.append("question_text_missing")

    rows = ensure_dense_sections(rows)
    rows = assign_global_sequence(rows)
    rows = resolve_item_code_branches(rows)
    rows = apply_validation_defaults(rows)
    rows = validate_choice_options(rows)
    rows = validate_branching_refs(rows)
    return rows, emit_warnings(rows)
