"""
Field-level review manifest for the SHOWLAY human-review phase.

This is the second pass after row confidence. It does not re-parse the PDF.
It uses cached page structure from the first pass: page images, text blocks,
widgets, row page numbers, and raw VLM rows.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .schema import QUESTION_TYPES, Row

SCHEMA_VERSION = "showlay.field_review.v1"

REVIEW_FIELDS = [
    "sequence",
    "question_type",
    "question_text",
    "branching_logic",
    "question_rule",
    "external_id",
    "branching_source",
    "answer_text",
    "answer_validation",
    "section",
    "required",
]

CHOICE_TYPES = {
    "radio button",
    "dropdown",
    "drop down",
    "checkbox group",
}
INPUT_TYPES = {"text box", "text area", "date", "number", "signature"}
YES_NO = {"yes", "no", ""}
KNOWN_QTYPES = {" ".join(q.split()).strip().lower() for q in QUESTION_TYPES}
COMPOUND_FIELD_SUFFIXES = ("printed name", "signature", "date")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _token_overlap(a: str, b: str) -> float:
    a_tokens = _tokens(a)
    b_tokens = _tokens(b)
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / max(1, min(len(a_tokens), len(b_tokens)))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _word_spans(value: str) -> list[tuple[str, int, int]]:
    return [(match.group(0).lower(), match.start(), match.end()) for match in re.finditer(r"[a-z0-9]+", value.lower())]


def _query_sequences(query: str) -> list[list[str]]:
    tokens = [token for token, _, _ in _word_spans(query)]
    if not tokens:
        return []
    sequences = [tokens]
    if tokens[0] == "specify" and len(tokens) > 1:
        sequences.insert(0, tokens[1:] + ["specify"])
    return sequences


def _find_token_span(query: str, text: str) -> tuple[int, int] | None:
    words = _word_spans(text)
    if not words:
        return None
    for sequence in _query_sequences(query):
        if len(sequence) > len(words):
            continue
        for start_idx in range(0, len(words) - len(sequence) + 1):
            window = words[start_idx : start_idx + len(sequence)]
            if [word for word, _, _ in window] == sequence:
                return window[0][1], window[-1][2]
    return None


def _rect_for_text_span(rect: list[Any], text: str, span: tuple[int, int]) -> list[float] | None:
    if len(rect or []) != 4 or not text:
        return None
    x0, y0, x1, y1 = [float(value) for value in rect]
    width = max(1.0, x1 - x0)
    start, end = span
    text_len = max(1, len(text))
    span_x0 = x0 + (width * start / text_len)
    span_x1 = x0 + (width * end / text_len)
    if span_x1 - span_x0 < 14:
        span_x1 = min(x1, span_x0 + 14)
    return [round(span_x0, 2), round(y0, 2), round(span_x1, 2), round(y1, 2)]


def _clip(score: float) -> float:
    return round(max(0.0, min(1.0, score)), 3)


def _risk(score: float) -> str:
    if score < 0.65:
        return "high"
    if score < 0.85:
        return "medium"
    return "low"


def _page_map(doc_struct: Any) -> dict[int, Any]:
    pages = getattr(doc_struct, "pages", None) or []
    return {int(getattr(page, "page_index", 0)) + 1: page for page in pages}


def _text_blocks(page: Any) -> list[dict]:
    if page is None:
        return []
    return list(getattr(page, "text_blocks", None) or [])


def _widgets(page: Any) -> list[dict]:
    if page is None:
        return []
    return list(getattr(page, "widgets", None) or [])


def _page_text(page: Any) -> str:
    return " ".join(str(block.get("text") or "") for block in _text_blocks(page))


def _compound_field_evidence(query: str, page: Any) -> dict[str, Any] | None:
    """Resolve labels like "Witness Date" to the local repeated "Date" block.

    Signature sections repeat generic labels: Printed Name, Signature, Date.
    A full generated label usually includes a role prefix, so generic string
    matching can pick the role label or the first repeated Date on the page.
    """
    query_norm = _norm(query).rstrip(":")
    suffix = next(
        (candidate for candidate in COMPOUND_FIELD_SUFFIXES if query_norm.endswith(candidate)),
        None,
    )
    if not suffix:
        return None

    role = query_norm[: -len(suffix)].strip(" :-,")
    if not role:
        return None
    if role.startswith("individual service plan") or role == "revision":
        return None

    blocks = _text_blocks(page)
    role_candidates: list[tuple[float, dict]] = []
    for block in blocks:
        text = str(block.get("text") or "")
        text_norm = _norm(text).strip(" :-,")
        if not text_norm:
            continue
        overlap = _token_overlap(role, text_norm)
        sim = _similarity(role, text_norm)
        if overlap >= 0.5 or sim >= 0.55:
            role_candidates.append(((0.65 * overlap) + (0.35 * sim), block))
    if not role_candidates:
        return None

    role_candidates.sort(
        key=lambda item: (
            item[0],
            -float((item[1].get("rect") or [0, 0, 0, 0])[1]),
        ),
        reverse=True,
    )

    for _, role_block in role_candidates[:3]:
        role_rect = role_block.get("rect") or []
        if len(role_rect) != 4:
            continue
        role_y = float(role_rect[1])
        targets: list[tuple[float, dict]] = []
        for block in blocks:
            text_norm = _norm(block.get("text")).strip(" :-,")
            rect = block.get("rect") or []
            if len(rect) != 4:
                continue
            if text_norm != suffix:
                continue
            y = float(rect[1])
            delta = y - role_y
            if 0 < delta <= 90:
                targets.append((delta, block))
        if targets:
            targets.sort(key=lambda item: item[0])
            target = targets[0][1]
            return {
                "score": 0.98,
                "token_overlap": 1.0,
                "similarity": 0.95,
                "text": str(target.get("text") or "")[:220],
                "rect": target.get("rect"),
                "evidence_source": "compound_role_field",
                "role_text": str(role_block.get("text") or "")[:220],
                "role_rect": role_rect,
            }

    return None


def _best_text_evidence(query: str, page: Any) -> dict[str, Any]:
    candidates = _text_evidence_candidates(query, page)
    if candidates:
        return candidates[0]
    return {
        "score": 0.0,
        "token_overlap": 0.0,
        "similarity": 0.0,
        "text": "",
        "rect": None,
    }


def _text_evidence_candidates(query: str, page: Any) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    compound = _compound_field_evidence(query, page)
    if compound is not None:
        return [compound]

    candidates: list[dict[str, Any]] = []
    for block in _text_blocks(page):
        text = str(block.get("text") or "")
        rect = block.get("rect")
        overlap = _token_overlap(query, text)
        sim = _similarity(query[:140], text[:180])
        score = (0.65 * overlap) + (0.35 * sim)
        span = _find_token_span(query, text)
        refined_rect = _rect_for_text_span(rect, text, span) if span else None
        if span:
            score = max(score, 0.92)
        candidates.append(
            {
                "score": round(score, 3),
                "token_overlap": round(overlap, 3),
                "similarity": round(sim, 3),
                "text": text[:220],
                "rect": refined_rect or rect,
                "block_rect": rect,
                "evidence_source": "text_span" if refined_rect else "text_block",
                "span": list(span) if span else None,
            }
        )

    whole_page_overlap = _token_overlap(query, _page_text(page))
    for candidate in candidates:
        if whole_page_overlap > candidate["token_overlap"]:
            candidate["token_overlap"] = round(whole_page_overlap, 3)
            candidate["score"] = round(max(candidate["score"], whole_page_overlap * 0.9), 3)

    candidates.sort(
        key=lambda item: (
            item.get("span") is not None,
            item["score"],
            -float((item.get("rect") or [0, 0, 0, 0])[1]),
        ),
        reverse=True,
    )
    return candidates


def _ordered_text_evidence(
    query: str,
    page: Any,
    evidence_usage: Counter[tuple[int, str]],
) -> dict[str, Any]:
    candidates = _text_evidence_candidates(query, page)
    if not candidates:
        return _best_text_evidence(query, page)

    page_no = int(getattr(page, "page_index", 0)) + 1 if page is not None else 0
    key = (page_no, _norm(query))
    span_candidates = [candidate for candidate in candidates if candidate.get("span") is not None]
    if span_candidates:
        span_candidates.sort(
            key=lambda item: (
                float((item.get("rect") or [0, 0, 0, 0])[1]),
                float((item.get("rect") or [0, 0, 0, 0])[0]),
            )
        )
        index = min(evidence_usage[key], len(span_candidates) - 1)
        evidence_usage[key] += 1
        return span_candidates[index]

    evidence_usage[key] += 1
    return candidates[0]


def _split_options(value: str) -> list[str]:
    if not value:
        return []
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\n\n" in value:
        parts = re.split(r"\n{2,}", value)
    else:
        parts = value.split("\n")
    return [part.strip() for part in parts if part.strip()]


def _looks_formulaic(value: str) -> bool:
    return bool(
        re.match(
            r"^\s*(default characters?\s*=\s*\d+|format is |signature area|"
            r"only allow numeric characters|numeric)\b",
            value or "",
            re.IGNORECASE,
        )
    )


def _page_for_row(row: Row, pages: dict[int, Any]) -> Any:
    return pages.get(int(row.page or 0))


def _page_no(page: Any) -> int:
    return int(getattr(page, "page_index", 0)) + 1


def _resolve_row_page(row: Row, pages: dict[int, Any]) -> tuple[int | None, Any]:
    """Use text evidence to recover from rows assigned to the first page of a chunk."""
    current_page_no = int(row.page or 0) or None
    current_page = pages.get(current_page_no or 0)
    if not pages or not (row.question_text or "").strip():
        return current_page_no, current_page

    scored_pages: list[tuple[float, int, Any, dict[str, Any]]] = []
    for page_no, page in pages.items():
        evidence = _best_text_evidence(row.question_text or "", page)
        score = float(evidence.get("score") or 0)
        if evidence.get("span") is not None:
            score += 0.08
        scored_pages.append((score, page_no, page, evidence))

    if not scored_pages:
        return current_page_no, current_page

    scored_pages.sort(key=lambda item: item[0], reverse=True)
    best_score, best_page_no, best_page, best_evidence = scored_pages[0]
    if best_score < 0.45:
        return current_page_no, current_page
    if current_page is None:
        return best_page_no, best_page
    if best_page_no == current_page_no:
        return current_page_no, current_page

    current_evidence = _best_text_evidence(row.question_text or "", current_page)
    current_score = float(current_evidence.get("score") or 0)
    current_overlap = float(current_evidence.get("token_overlap") or 0)
    best_overlap = float(best_evidence.get("token_overlap") or 0)
    best_has_span = best_evidence.get("span") is not None
    current_has_span = current_evidence.get("span") is not None

    if current_score < 0.35:
        return best_page_no, best_page
    if best_has_span and not current_has_span and best_score >= current_score + 0.05:
        return best_page_no, best_page
    if best_score >= current_score + 0.18 and best_overlap >= current_overlap:
        return best_page_no, best_page
    return current_page_no, current_page


def _qref(branching_logic: str) -> int | None:
    match = re.search(r"\bq\s*(\d+)\b", branching_logic or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def _parent_for_ref(rows_by_sequence: dict[int, Row], ref: int | None) -> Row | None:
    if ref is None:
        return None
    return rows_by_sequence.get(ref)


def _base_result(field: str, value: Any, score: float, reasons: list[str], evidence=None) -> dict:
    return {
        "field": field,
        "value": value if value is not None else "",
        "confidence": _clip(score),
        "risk_level": _risk(score),
        "review_reasons": reasons,
        "evidence": evidence or {},
        "suggested_action": _suggest_action(field, reasons),
    }


def _suggest_action(field: str, reasons: list[str]) -> str:
    if not reasons:
        return "No review needed"
    if field == "question_text":
        return "Compare the extracted label with the highlighted PDF text"
    if field == "question_type":
        return "Verify the field type against the visual control"
    if field == "branching_logic":
        return "Check the parent question and conditional option"
    if field == "question_rule":
        return "Check the machine-readable conditional rule"
    if field == "external_id":
        return "Check the source item code"
    if field == "branching_source":
        return "Check the source skip or display wording"
    if field == "answer_text":
        return "Check selectable options or table child values"
    if field == "answer_validation":
        return "Check the expected format or length rule"
    if field == "section":
        return "Check the section banner near this row"
    if field == "required":
        return "Use only Yes, No, or blank"
    if field == "sequence":
        return "Check row order around this item"
    return "Review this field"


def _review_sequence(row: Row, seen_sequences: set[int]) -> dict:
    reasons: list[str] = []
    score = 0.95
    if row.sequence is None:
        return _base_result("sequence", "", 0.2, ["sequence_missing"])
    if row.sequence in seen_sequences:
        score -= 0.5
        reasons.append("sequence_duplicate")
    if row.sequence <= 0:
        score -= 0.4
        reasons.append("sequence_not_positive")
    return _base_result("sequence", row.sequence, score, reasons)


def _review_question_text(row: Row, page: Any) -> dict:
    text = row.question_text or ""
    reasons: list[str] = []
    score = 0.97
    evidence = _best_text_evidence(text, page)

    if not text.strip():
        return _base_result("question_text", text, 0.1, ["question_text_missing"], evidence)
    if page is None:
        score -= 0.15
        reasons.append("page_missing")
    elif not _text_blocks(page):
        score -= 0.1
        reasons.append("page_text_missing")
    elif evidence["token_overlap"] < 0.45 and evidence["similarity"] < 0.35:
        score -= 0.38
        reasons.append("not_grounded_in_page_text")
    elif evidence["token_overlap"] < 0.7:
        score -= 0.12
        reasons.append("weak_text_grounding")

    if _norm(text) in {"name", "date", "signature", "printed name", "title"}:
        score -= 0.12
        reasons.append("generic_label_needs_context")
    if _looks_formulaic(text):
        score -= 0.25
        reasons.append("formulaic_text_in_question_label")
    return _base_result("question_text", text, score, reasons, evidence)


def _review_question_type(row: Row, page: Any) -> dict:
    qtype = row.question_type or ""
    qtype_norm = _norm(qtype)
    text_norm = _norm(row.question_text)
    answer = row.answer_text or ""
    page_text = _norm(_page_text(page))
    reasons: list[str] = []
    score = 0.96

    if not qtype_norm:
        return _base_result("question_type", qtype, 0.1, ["question_type_missing"])
    if qtype_norm not in KNOWN_QTYPES:
        return _base_result("question_type", qtype, 0.35, [f"unknown_question_type:{qtype}"])

    options = _split_options(answer)
    if qtype_norm in CHOICE_TYPES and len(options) < 2:
        score -= 0.34
        reasons.append("choice_type_without_enough_options")
    if qtype_norm == "checkbox" and len(options) > 1:
        score -= 0.18
        reasons.append("checkbox_with_multiple_options_maybe_group")
    if qtype_norm == "group table":
        table_signal = any(
            token in f"{text_norm} {page_text}" for token in ("table", "column", "row", "grid")
        )
        if len(options) < 2 and not table_signal:
            score -= 0.28
            reasons.append("group_table_not_supported_by_evidence")
    if qtype_norm == "date" and not re.search(r"\b(date|dob|birth|mm/dd/yyyy)\b", text_norm):
        score -= 0.12
        reasons.append("date_type_needs_visual_check")
    if qtype_norm == "number" and not re.search(r"\b(number|score|amount|total|age|count)\b", text_norm):
        score -= 0.12
        reasons.append("number_type_needs_visual_check")
    if qtype_norm == "signature" and "signature" not in text_norm:
        score -= 0.15
        reasons.append("signature_type_needs_visual_check")

    return _base_result("question_type", qtype, score, reasons)


def _review_branching_logic(row: Row, rows_by_sequence: dict[int, Row]) -> dict:
    value = row.branching_logic or ""
    validation_value = row.question_rule or value
    reasons: list[str] = []
    score = 0.95
    text_norm = _norm(row.question_text)

    if not value.strip():
        if re.match(r"^(if|when)\b", text_norm) and row.sequence and row.sequence > 1:
            score -= 0.2
            reasons.append("possible_missing_branching_logic")
        return _base_result("branching_logic", value, score, reasons)

    if not re.match(r"^\s*(if|display if)\s+q\d+\s*=", validation_value, re.IGNORECASE):
        if row.question_rule or re.search(r"\b[A-Z]{1,4}\d{2,5}[A-Z]?\b", value):
            score -= 0.1
            reasons.append("branching_source_without_standard_q_rule")
        else:
            score -= 0.22
            reasons.append("branching_syntax_unusual")

    ref = _qref(validation_value)
    parent = _parent_for_ref(rows_by_sequence, ref)
    if ref is None:
        score -= 0.22
        reasons.append("branching_ref_missing")
    elif parent is None:
        score -= 0.42
        reasons.append(f"branching_ref_not_found:Q{ref}")
    # Forward references are valid for skip logic. Only unresolved refs are risky.

    literal_match = re.search(r"=\s*(.+?)\s*$", validation_value)
    literal = literal_match.group(1).strip(" '\"") if literal_match else ""
    if parent is not None and literal and "checked(selected)" not in literal.lower():
        parent_options = [_norm(option) for option in _split_options(parent.answer_text or "")]
        if parent_options and _norm(literal) not in parent_options:
            score -= 0.16
            reasons.append("branching_literal_not_in_parent_options")

    evidence = {"referenced_sequence": ref}
    if parent is not None:
        evidence["parent_question_text"] = parent.question_text
        evidence["parent_question_type"] = parent.question_type
    return _base_result("branching_logic", value, score, reasons, evidence)


def _review_answer_text(row: Row) -> dict:
    value = row.answer_text or ""
    qtype = _norm(row.question_type)
    reasons: list[str] = []
    score = 0.94
    options = _split_options(value)

    if qtype in CHOICE_TYPES and len(options) < 2:
        score -= 0.38
        reasons.append("missing_or_single_choice_option")
    elif qtype in CHOICE_TYPES and any(len(option) > 180 for option in options):
        score -= 0.12
        reasons.append("choice_option_unusually_long")

    if qtype in INPUT_TYPES:
        if value and not _looks_formulaic(value):
            score -= 0.22
            reasons.append("unexpected_answer_text_for_input")
        elif value and _looks_formulaic(value):
            score -= 0.05
            reasons.append("formulaic_default_generated")

    if qtype == "display" and _norm(row.question_text) == "new section" and not value.strip():
        score -= 0.4
        reasons.append("section_display_missing_answer_text")

    if qtype == "group table" and not value.strip():
        score -= 0.12
        reasons.append("group_table_parent_without_answer_text")

    return _base_result(
        "answer_text",
        value,
        score,
        reasons,
        {"option_count": len(options)},
    )


def _review_answer_validation(row: Row) -> dict:
    value = row.answer_validation or ""
    qtype = _norm(row.question_type)
    value_norm = _norm(value)
    reasons: list[str] = []
    score = 0.92

    if not value_norm:
        if qtype in {"date", "number"}:
            score -= 0.14
            reasons.append("validation_blank_for_typed_input")
        return _base_result("answer_validation", value, score, reasons)

    if qtype == "date" and not re.search(r"\b(date|mm/dd/yyyy|format)\b", value_norm):
        score -= 0.18
        reasons.append("date_validation_unusual")
    if qtype == "number" and not re.search(r"\b(numeric|number|digit|integer)\b", value_norm):
        score -= 0.18
        reasons.append("number_validation_unusual")
    if qtype in {"radio button", "dropdown", "checkbox group"} and value_norm:
        score -= 0.1
        reasons.append("choice_row_has_answer_validation")
    return _base_result("answer_validation", value, score, reasons)


def _review_section(row: Row, page: Any) -> dict:
    value = row.section or ""
    reasons: list[str] = []
    score = 0.9
    if _norm(row.question_text) == "new section" and not value.strip():
        score -= 0.35
        reasons.append("new_section_row_missing_section")
    if value.strip() and page is not None:
        evidence = _best_text_evidence(value, page)
        if evidence["token_overlap"] < 0.45 and evidence["similarity"] < 0.35:
            score -= 0.12
            reasons.append("section_not_grounded_on_page")
            return _base_result("section", value, score, reasons, evidence)
    return _base_result("section", value, score, reasons)


def _review_required(row: Row) -> dict:
    value = row.required or ""
    reasons: list[str] = []
    score = 0.93
    if _norm(value) not in YES_NO:
        score -= 0.55
        reasons.append("required_not_yes_no_or_blank")
    return _base_result("required", value, score, reasons)


def _review_optional_text_field(row: Row, field_name: str) -> dict:
    value = getattr(row, field_name, "") or ""
    score = 0.95
    reasons: list[str] = []
    if field_name == "question_rule":
        for ref in re.findall(r"\bq(\d+)\b", value, flags=re.IGNORECASE):
            if int(ref) <= 0:
                score -= 0.35
                reasons.append(f"question_rule_invalid_ref:Q{ref}")
    return _base_result(field_name, value, score, reasons)


def review_row_fields(
    row: Row,
    *,
    page: Any,
    rows_by_sequence: dict[int, Row],
    seen_sequences: set[int] | None = None,
) -> list[dict]:
    """Return field-level review results for one row."""
    seen_sequences = seen_sequences or set()
    return [
        _review_sequence(row, seen_sequences),
        _review_question_type(row, page),
        _review_question_text(row, page),
        _review_branching_logic(row, rows_by_sequence),
        _review_optional_text_field(row, "question_rule"),
        _review_optional_text_field(row, "external_id"),
        _review_optional_text_field(row, "branching_source"),
        _review_answer_text(row),
        _review_answer_validation(row),
        _review_section(row, page),
        _review_required(row),
    ]


def _raw_match(row: Row, raw_vlm: list[dict] | None) -> dict[str, Any]:
    if not raw_vlm:
        return {}
    best_idx = None
    best_score = 0.0
    for idx, raw in enumerate(raw_vlm):
        raw_page = raw.get("_page") or raw.get("page")
        if row.page and raw_page and int(raw_page) != int(row.page):
            continue
        raw_text = str(raw.get("question_text") or "")
        score = _token_overlap(row.question_text or "", raw_text) + _similarity(
            row.question_text or "", raw_text
        )
        if score > best_score:
            best_score = score
            best_idx = idx
    if best_idx is None:
        return {}
    raw = raw_vlm[best_idx]
    return {
        "raw_vlm_index": best_idx,
        "raw_vlm_question_type": raw.get("question_type", ""),
        "raw_vlm_question_text": raw.get("question_text", ""),
    }


def _serialize_pages(doc_struct: Any) -> list[dict[str, Any]]:
    pages = []
    for page_no, page in sorted(_page_map(doc_struct).items()):
        pages.append(
            {
                "page": page_no,
                "width": getattr(page, "width", None),
                "height": getattr(page, "height", None),
                "image_path": getattr(page, "image_path", ""),
                "text_blocks": _text_blocks(page),
                "widgets": _widgets(page),
            }
        )
    return pages


def _row_snapshot(row: Row) -> dict[str, Any]:
    return {
        "sequence": row.sequence,
        "page": row.page,
        "bbox": row.bbox,
        "confidence": row.confidence,
        "review_reasons": list(row.review_reasons),
        "section": row.section,
        "question_type": row.question_type,
        "question_text": row.question_text,
        "branching_logic": row.branching_logic,
        "question_rule": row.question_rule,
        "external_id": getattr(row, "external_id", ""),
        "branching_source": getattr(row, "branching_source", ""),
        "answer_text": row.answer_text,
        "answer_validation": row.answer_validation,
        "required": row.required,
    }


def build_review_manifest(
    rows: list[Row],
    *,
    doc_struct: Any,
    raw_vlm: list[dict] | None = None,
    telemetry: list[dict] | None = None,
    run_id: str | None = None,
    source_pdf: str | None = None,
    template_path: str | None = None,
) -> dict[str, Any]:
    """Build the JSON artifact consumed by a human review workbench."""
    pages = _page_map(doc_struct)
    rows_by_sequence = {int(r.sequence): r for r in rows if r.sequence is not None}
    seen_sequences: set[int] = set()
    evidence_usage: Counter[tuple[int, str]] = Counter()
    manifest_rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    field_risk_counts: Counter[str] = Counter()
    rows_needing_review = 0

    for index, row in enumerate(rows):
        resolved_page_no, page = _resolve_row_page(row, pages)
        if resolved_page_no is not None:
            row.page = resolved_page_no
        field_reviews = review_row_fields(
            row,
            page=page,
            rows_by_sequence=rows_by_sequence,
            seen_sequences=seen_sequences,
        )
        if row.sequence is not None:
            seen_sequences.add(row.sequence)

        for field in field_reviews:
            field_risk_counts[field["risk_level"]] += 1
            reason_counts.update(field["review_reasons"])

        high_or_medium = [f for f in field_reviews if f["risk_level"] != "low"]
        if high_or_medium or row.confidence < 0.85:
            rows_needing_review += 1

        row_risk_score = min([row.confidence or 1.0] + [f["confidence"] for f in field_reviews])
        page_evidence = _ordered_text_evidence(row.question_text or "", page, evidence_usage)
        if page is not None:
            page_evidence = {**page_evidence, "page": _page_no(page)}
        manifest_rows.append(
            {
                "row_id": f"row_{index + 1:04d}",
                "row_index": index,
                "sequence": row.sequence,
                "page": row.page,
                "bbox": row.bbox,
                "row_confidence": row.confidence,
                "risk_level": _risk(row_risk_score),
                "fields": {field["field"]: field for field in field_reviews},
                "row": _row_snapshot(row),
                "source": {
                    "page_image": getattr(page, "image_path", "") if page is not None else "",
                    "nearest_text_block": page_evidence,
                    **_raw_match(row, raw_vlm),
                },
                "suggested_action": _row_action(field_reviews, row.confidence),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or "",
        "source_pdf": source_pdf or getattr(doc_struct, "pdf_path", ""),
        "template_path": template_path or "",
        "page_count": getattr(doc_struct, "page_count", len(pages)),
        "has_acroform": bool(getattr(doc_struct, "has_acroform", False)),
        "summary": {
            "row_count": len(rows),
            "rows_needing_review": rows_needing_review,
            "field_count": len(rows) * len(REVIEW_FIELDS),
            "field_risk_counts": dict(field_risk_counts),
            "review_reason_counts": dict(reason_counts),
            "high_risk_rows": sum(1 for row in manifest_rows if row["risk_level"] == "high"),
            "medium_risk_rows": sum(1 for row in manifest_rows if row["risk_level"] == "medium"),
            "low_risk_rows": sum(1 for row in manifest_rows if row["risk_level"] == "low"),
        },
        "telemetry": telemetry or [],
        "pages": _serialize_pages(doc_struct),
        "rows": manifest_rows,
    }


def _row_action(field_reviews: list[dict], row_confidence: float) -> str:
    high_fields = [field["field"] for field in field_reviews if field["risk_level"] == "high"]
    medium_fields = [field["field"] for field in field_reviews if field["risk_level"] == "medium"]
    if high_fields:
        return "Review high-risk fields: " + ", ".join(high_fields)
    if row_confidence < 0.85:
        return "Review row because aggregate row confidence is below 0.85"
    if medium_fields:
        return "Spot-check medium-risk fields: " + ", ".join(medium_fields)
    return "Can be accepted unless reviewer sees a visual mismatch"


def repair_manifest_page_evidence(manifest: dict[str, Any]) -> dict[str, Any]:
    """Repair loaded manifests whose rows were assigned to the wrong page."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("rows"), list):
        return manifest

    pages: dict[int, Any] = {}
    for page_data in manifest.get("pages") or []:
        try:
            page_no = int(page_data.get("page") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if page_no <= 0:
            continue
        pages[page_no] = SimpleNamespace(
            page_index=page_no - 1,
            width=page_data.get("width"),
            height=page_data.get("height"),
            image_path=page_data.get("image_path", ""),
            text_blocks=page_data.get("text_blocks") or [],
            widgets=page_data.get("widgets") or [],
        )

    if not pages:
        return manifest

    evidence_usage: Counter[tuple[int, str]] = Counter()
    for row_data in manifest["rows"]:
        if not isinstance(row_data, dict):
            continue
        snapshot = {**(row_data.get("row") or {})}
        snapshot.setdefault("sequence", row_data.get("sequence"))
        snapshot.setdefault("page", row_data.get("page"))
        snapshot.setdefault("bbox", row_data.get("bbox"))
        snapshot.setdefault("confidence", row_data.get("row_confidence", 1.0))
        try:
            row = Row(**snapshot)
        except Exception:
            continue

        resolved_page_no, page = _resolve_row_page(row, pages)
        if resolved_page_no is not None:
            row_data["page"] = resolved_page_no
            row_data.setdefault("row", {})["page"] = resolved_page_no
        if page is None:
            continue

        page_evidence = _ordered_text_evidence(row.question_text or "", page, evidence_usage)
        row_data.setdefault("source", {})["nearest_text_block"] = {
            **page_evidence,
            "page": _page_no(page),
        }
        row_data["source"]["page_image"] = getattr(page, "image_path", "")

    return manifest


def write_review_manifest(path: str | Path, manifest: dict[str, Any]) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
