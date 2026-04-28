from __future__ import annotations

import re
from collections import Counter

from app.models import ExtractedRow, QuestionType


_WS_RE = re.compile(r"[ \t]+")

_PAGE_RE = re.compile(r"^\s*page\s+\d+(\s*(/|of)\s*\d+)?\s*$", re.IGNORECASE)
_URL_ONLY_RE = re.compile(r"^\s*(https?://\S+|www\.\S+)\s*$", re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"^\s*\d+\s*$")
_DATEISH_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b"
)
_MONTH_NAME_RE = re.compile(
    r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|"
    r"sep|sept|september|oct|october|nov|november|dec|december)\b",
    re.IGNORECASE,
)

# Strong signals to choose one answer (mutually exclusive) vs many (checkboxes).
_EXCLUSIVE_WORDING_RE = re.compile(
    r"\b("
    r"select one|select only one|choose one|check one|mark one|"
    r"one choice only|one answer only|only one|single choice|"
    r"may not select|cannot select|may select only|"
    r"one of the following|which one|"
    r"mutually exclusive|not more than one"
    r")\b",
    re.IGNORECASE,
)
_MULTI_SELECT_WORDING_RE = re.compile(
    r"\b("
    r"select all that apply|select any that apply|select all|check all|"
    r"mark all|choose all|check each|one or more|all that apply|"
    r"check every|you may select more|select multiple|check multiple"
    r")\b",
    re.IGNORECASE,
)

_YN_PROMPT_RE = re.compile(
    r"\b(yes|no|y/n)\b.*\b(yes|no|y/n)\b",
    re.IGNORECASE,
)


def _clean_text(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = s.strip()
    s = _WS_RE.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _split_pipe_options(s: str) -> list[str]:
    s = (s or "").strip()
    if not s or "|" not in s:
        return []
    return [p.strip() for p in s.split("|") if p.strip()]


def _apply_choice_type_rules(r: ExtractedRow) -> ExtractedRow:
    """Re-tag Radio Button vs Checkbox Group vs Checkbox from question/answer semantics."""
    blob = f"{r.question_text} {r.answer_text}".strip()
    opts = _split_pipe_options(r.answer_text)
    num_opts = len(opts)

    qt = r.question_type
    new_qt = qt
    if qt not in (QuestionType.CHECKBOX, QuestionType.CHECKBOX_GROUP, QuestionType.RADIO_BUTTON):
        return r

    # Multiple explicit options: use wording to pick single- vs multi-select
    if num_opts >= 2:
        ex = _EXCLUSIVE_WORDING_RE.search(blob)
        multi = _MULTI_SELECT_WORDING_RE.search(blob)
        yn = _YN_PROMPT_RE.search(blob)
        if multi and not ex:
            new_qt = QuestionType.CHECKBOX_GROUP
        elif (ex or yn) and not multi:
            new_qt = QuestionType.RADIO_BUTTON
        # If both or neither, keep the model’s type
    else:
        # No pipe-separated list: usually a single statement/checkbox (not a group/radio)
        ex = _EXCLUSIVE_WORDING_RE.search(blob)
        multi = _MULTI_SELECT_WORDING_RE.search(blob)
        yn = _YN_PROMPT_RE.search(r.question_text)
        if not ex and not multi and not yn:
            new_qt = QuestionType.CHECKBOX

    if new_qt == qt:
        return r
    return r.model_copy(update={"question_type": new_qt})


_PARENT_QUESTION_TYPES = {
    QuestionType.RADIO_BUTTON,
    QuestionType.CHECKBOX_GROUP,
    QuestionType.DROPDOWN,
    QuestionType.RADIO_BUTTON_WITH_TEXT_AREA,
    QuestionType.CHECKBOX_GROUP_WITH_TEXT_AREA,
    QuestionType.CHECKBOX,
}

_CHECKBOX_PARENT_TYPES = {
    QuestionType.CHECKBOX,
    QuestionType.CHECKBOX_GROUP,
    QuestionType.CHECKBOX_GROUP_WITH_TEXT_AREA,
}


def _rewrite_for_checkbox_parent(bl: str, parent_seq: int) -> str:
    """For checkbox-based parents, the value side is replaced with 'checked(selected)'.

    Preserve the original prefix verb ('Display if', 'If', 'Skip to', etc.) when reasonable;
    default to 'If' when the agent didn't supply one.
    """
    parts = bl.split("=", 1)
    prefix = parts[0].strip() if len(parts) == 2 else ""
    if not prefix or "Q?" not in prefix:
        prefix = f"If Q{parent_seq}"
    else:
        prefix = prefix.replace("Q?", f"Q{parent_seq}")
    return f"{prefix} = checked(selected)"


def _resolve_branching_logic(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """Replace the literal 'Q?' placeholder in branching_logic with the parent's sequence number.

    The parent of a row is the most recent preceding row whose type can host a branching
    condition (option-bearing or single checkbox). For checkbox-based parents, the value
    side of the condition is rewritten to 'checked(selected)'.

    Requires sequence numbers to be assigned beforehand.
    """
    out: list[ExtractedRow] = []
    for i, r in enumerate(rows):
        bl = (r.branching_logic or "").strip()
        if not bl or "Q?" not in bl:
            out.append(r)
            continue

        parent: ExtractedRow | None = None
        for j in range(i - 1, -1, -1):
            prev = rows[j]
            if prev.question_type in _PARENT_QUESTION_TYPES:
                parent = prev
                break

        if parent is None or parent.sequence <= 0:
            out.append(r.model_copy(update={"branching_logic": ""}))
            continue

        if parent.question_type in _CHECKBOX_PARENT_TYPES:
            new_bl = _rewrite_for_checkbox_parent(bl, parent.sequence)
        else:
            new_bl = bl.replace("Q?", f"Q{parent.sequence}")

        out.append(r.model_copy(update={"branching_logic": new_bl}))
    return out


def assign_sequence(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """Assign 1-based global reading-order sequence numbers across all rows."""
    out: list[ExtractedRow] = []
    for i, r in enumerate(rows, start=1):
        out.append(r.model_copy(update={"sequence": i}))
    return out


def normalize_rows(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    if not rows:
        return []

    # Normalize whitespace and ordering first
    cleaned: list[ExtractedRow] = []
    for r in rows:
        cleaned.append(
            r.model_copy(
                update={
                    "question_text": _clean_text(r.question_text),
                    "answer_text": _clean_text(r.answer_text),
                }
            )
        )

    cleaned.sort(key=lambda r: (r.page_number, r.source_order))
    # Choice-type rules (deterministic, before header/footer heuristics)
    merged: list[ExtractedRow] = [_apply_choice_type_rules(r) for r in cleaned]

    # Compute per-page bounds so header/footer detection works for small pages too.
    page_max_order: dict[int, int] = {}
    for r in merged:
        page_max_order[r.page_number] = max(page_max_order.get(r.page_number, 0), r.source_order)

    def is_header_region(r: ExtractedRow) -> bool:
        # Top-of-page (very first elements).
        return r.source_order <= 1

    def is_footer_region(r: ExtractedRow) -> bool:
        # Bottom-of-page (last 1-2 elements).
        max_order = page_max_order.get(r.page_number, r.source_order)
        # Avoid misclassifying short pages as having a "footer region".
        # Heuristic: only treat as footer if the page has enough extracted elements.
        if max_order < 6:
            return False
        return r.source_order >= max(0, max_order - 1)

    def row_key(r: ExtractedRow) -> tuple[QuestionType, str, str]:
        # Include answer_text to avoid collapsing distinct Display paragraphs that share the same heading.
        return (r.question_type, r.question_text, r.answer_text)

    def is_generic_footer_noise(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        if _PAGE_RE.match(t):
            return True
        if _URL_ONLY_RE.match(t):
            return True
        if _BARE_NUM_RE.match(t):
            # Often just a running page number.
            return True
        tl = t.lower()
        # Common low-value footer boilerplate
        if "page" in tl and (" of " in tl or "/" in tl) and any(ch.isdigit() for ch in tl):
            return True
        if tl in {"confidential", "internal use only"}:
            return True
        return False

    def is_header_footer_id_or_date_metadata(text: str) -> bool:
        """Detect low-value header/footer metadata (IDs / revision / expiry dates).

        Per product requirement: do not include ID/date metadata from headers/footers.
        """
        t = (text or "").strip()
        if not t:
            return False
        tl = t.lower()

        # Common form/footer metadata phrases
        metadata_keywords = (
            "omb",
            "control no",
            "control number",
            "form no",
            "form number",
            "revision",
            "rev.",
            "version",
            "edition",
            "expires",
            "expiration",
            "effective date",
            "issued",
        )
        if any(k in tl for k in metadata_keywords):
            # If this line is primarily metadata, treat as metadata.
            if len(t) <= 120:
                return True

        # Date-heavy short lines like "Rev. 01/2024" or "Expires 12/31/2026"
        if len(t) <= 80 and (_DATEISH_RE.search(t) or _MONTH_NAME_RE.search(tl)):
            if any(k in tl for k in ("rev", "revision", "expire", "effective", "edition", "version")):
                return True

        return False

    def is_droppable_footer_row(r: ExtractedRow) -> bool:
        # Drop only rows that look like true page-footer noise or ID/date metadata.
        # Real form content at the bottom of a page (signature blocks, dates, etc.) must be kept.
        blob = " ".join([r.question_text or "", r.answer_text or ""]).strip()
        if not blob:
            return True
        if is_generic_footer_noise(blob):
            return True
        if is_header_footer_id_or_date_metadata(blob):
            return True
        return False

    # Repeated header detection: if identical rows appear in the header region on >=2 pages,
    # keep only the first occurrence.
    header_key_counts = Counter(row_key(r) for r in merged if r.question_text and is_header_region(r))
    footer_key_counts = Counter(row_key(r) for r in merged if r.question_text and is_footer_region(r))

    out: list[ExtractedRow] = []
    kept_header: set[tuple[QuestionType, str, str]] = set()
    kept_footer: set[tuple[QuestionType, str, str]] = set()

    for r in merged:
        if not r.question_text.strip():
            continue

        k = row_key(r)

        # Explicitly drop ID/date-like metadata in header/footer regions.
        if (is_header_region(r) or is_footer_region(r)) and is_header_footer_id_or_date_metadata(
            " ".join([r.question_text or "", r.answer_text or ""]).strip()
        ):
            continue

        # Header: show only once (first occurrence).
        if is_header_region(r) and header_key_counts.get(k, 0) >= 2:
            if k in kept_header:
                continue
            kept_header.add(k)
            out.append(r)
            continue

        # Footer: drop only true page-footer noise/metadata; for repeated footers, show only once.
        if is_footer_region(r):
            if is_droppable_footer_row(r):
                continue
            if footer_key_counts.get(k, 0) >= 2:
                if k in kept_footer:
                    continue
                kept_footer.add(k)
            out.append(r)
            continue

        out.append(r)

    return out


def resolve_branching_logic(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """Public entry point: resolve 'Q?' placeholders to parent question numbers.

    Run AFTER any row-dropping passes (e.g. semantic pass), so question numbering
    matches the final output order.
    """
    return _resolve_branching_logic(rows)
