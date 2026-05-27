"""Generic AcroForm enrichment for extracted rows.

The VLM sees the page image and receives widget hints, but fillable PDFs often
encode stronger field metadata than what is printed on the page. This module
uses that metadata conservatively: enrich existing rows when a widget label
matches the row label, without relying on file names or form-specific IDs.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .schema import Row


_DATE_HINT_RE = re.compile(r"\b(date|dob|birthdate|birth\s+date)\b", re.IGNORECASE)
_NUMBER_HINT_RE = re.compile(
    r"\b(age|score|count|amount|quantity|number\s+of|height|weight)\b",
    re.IGNORECASE,
)
_NON_NUMBER_HINT_RE = re.compile(
    r"\b(ssn|social\s+security|medicaid|phone|fax|zip|postal|id|identifier|code)\b",
    re.IGNORECASE,
)


def _text_key(value: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", value or "")
    s = re.sub(r"\b\d+$", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _tokens(value: str) -> set[str]:
    return {t for t in _text_key(value).split() if len(t) > 2}


def _token_overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _rect_center(rect: list[float] | None) -> tuple[float, float] | None:
    if not rect or len(rect) != 4:
        return None
    return ((float(rect[0]) + float(rect[2])) / 2.0, (float(rect[1]) + float(rect[3])) / 2.0)


def _contains(rect: list[float] | None, point: tuple[float, float] | None) -> bool:
    if not rect or len(rect) != 4 or point is None:
        return False
    x, y = point
    return float(rect[0]) <= x <= float(rect[2]) and float(rect[1]) <= y <= float(rect[3])


def _widget_label(widget: dict[str, Any]) -> str:
    return str(widget.get("label") or widget.get("name") or "").strip()


def _widget_question_type(widget: dict[str, Any]) -> str:
    field_type = str(widget.get("type") or "").strip().lower()
    label = _widget_label(widget)
    if field_type == "signature":
        return "Signature"
    if field_type == "text":
        if _DATE_HINT_RE.search(label):
            return "Date"
        if _NUMBER_HINT_RE.search(label) and not _NON_NUMBER_HINT_RE.search(label):
            return "Number"
        return "Text Box"
    if field_type == "radiobutton":
        return "Radio Button"
    if field_type == "checkbox":
        return "Checkbox"
    if field_type in {"choice", "combobox", "listbox"}:
        return "Dropdown"
    return ""


def _row_can_use_widget_type(row: Row, widget_type: str) -> bool:
    current = (row.question_type or "").strip().lower()
    target = widget_type.lower()
    if not target:
        return False
    if not current:
        return target in {"text box", "text area", "date", "number", "signature"}
    if current == "text box":
        return target in {"text box", "text area", "date", "number", "signature"}
    return False


def _best_widget_for_row(row: Row, widgets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not widgets:
        return None

    row_widget_ids = {sid for sid in row.source_ids if sid.upper().startswith("W")}
    if row_widget_ids:
        for widget in widgets:
            if str(widget.get("id") or "").upper() in row_widget_ids:
                return widget

    row_center = _rect_center(row.bbox)
    for widget in widgets:
        if _contains(widget.get("rect"), row_center):
            return widget

    label = row.question_text or ""
    best = None
    best_score = 0.0
    for widget in widgets:
        score = _token_overlap(label, _widget_label(widget))
        if score > best_score:
            best = widget
            best_score = score
    return best if best_score >= 0.6 else None


def enrich_rows_from_acroform(rows: list[Row], doc_struct=None) -> list[Row]:
    """Use AcroForm widget metadata to refine existing extracted rows.

    This pass is intentionally conservative: it does not create rows, does not
    overwrite non-empty answer values, and only changes a row type when the row
    already represents an input/control compatible with the widget type.
    """
    if doc_struct is None or not getattr(doc_struct, "has_acroform", False):
        return rows

    widgets_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for page in getattr(doc_struct, "pages", []) or []:
        page_no = int(getattr(page, "page_index", 0)) + 1
        widgets_by_page[page_no].extend(list(getattr(page, "widgets", []) or []))

    if not widgets_by_page:
        return rows

    for row in rows:
        page_widgets = widgets_by_page.get(int(row.page or 0), [])
        if not page_widgets:
            continue
        widget = _best_widget_for_row(row, page_widgets)
        if not widget:
            continue

        widget_type = _widget_question_type(widget)
        if _row_can_use_widget_type(row, widget_type):
            row.question_type = widget_type

        widget_id = str(widget.get("id") or "").strip()
        if widget_id and widget_id not in row.source_ids:
            row.source_ids.append(widget_id)
    return rows
