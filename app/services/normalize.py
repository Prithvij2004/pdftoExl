from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from app.models import ExtractedRow, QuestionType


_WS_RE = re.compile(r"[ \t]+")
_HEADER_PREFIX = "(header) "

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

# Do not auto-merge these short, commonly repeated field labels (unless the continuation
# is explicitly marked, e.g. "(continued)").
_STANDALONE_FIELD_LABELS = frozenset(
    {
        "name",
        "first name",
        "last name",
        "middle name",
        "date",
        "dob",
        "signature",
        "address",
        "phone",
        "email",
        "e-mail",
        "ssn",
        "zip",
        "zip code",
        "city",
        "state",
        "id number",
    }
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

_CONTINUED_Q_RE = re.compile(
    r"^\s*("
    r"\(continued\)|<continued>|\[continued\]|"
    r"continued\s*[:.]\s*|"  # "Continued:" body on next line or same
    r"cont\.?\s*[:.]\s*|"  # "Cont.:" 
    r"continuation(\s+of|)\s*[:.]?\s*"
    r")",
    re.IGNORECASE,
)

# Whole-line "continued" with no other meaningful title
_CONTINUED_STANDALONE_RE = re.compile(
    r"^\s*(\(continued\)|continued|cont\.?|continuation|…|\.\.\.)\s*$",
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


def _is_blocked_merge_label(q: str) -> bool:
    t = _norm_key(q)
    if not t:
        return True
    if t in _STANDALONE_FIELD_LABELS:
        return True
    if len(t) <= 2:
        return True
    return False


def _is_explicit_continued_question(q: str) -> bool:
    t = (q or "").strip()
    if not t:
        return False
    if _CONTINUED_STANDALONE_RE.match(t):
        return True
    if _CONTINUED_Q_RE.match(t):
        return True
    return "continued" in t.lower() and len(t) < 80


def _split_pipe_options(s: str) -> list[str]:
    s = (s or "").strip()
    if not s or "|" not in s:
        return []
    return [p.strip() for p in s.split("|") if p.strip()]


def _merge_pipe_options(a: str, b: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in _split_pipe_options(a) + _split_pipe_options(b):
        k = part.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(part)
    return " | ".join(out)


def _append_with_sep(a: str, b: str, sep: str = "\n\n") -> str:
    a = (a or "").strip()
    b = (b or "").strip()
    if not b:
        return a
    if not a:
        return b
    return f"{a}{sep}{b}"


def _meta_add_continued_from(meta: dict[str, Any], from_page: int) -> dict[str, Any]:
    m = deepcopy(meta) if meta else {}
    pages: list[int] = list(m.get("continuation_from_pages", []))  # type: ignore[assignment]
    if from_page not in pages:
        pages.append(from_page)
    m["continuation_from_pages"] = sorted(pages)
    return m


def _merge_option_rows(anchor: ExtractedRow, cont: ExtractedRow) -> ExtractedRow:
    merged_ans = _merge_pipe_options(anchor.answer_text, cont.answer_text)
    if not _split_pipe_options(merged_ans) and (anchor.answer_text or cont.answer_text):
        # Fallback: non-pipe; concatenate
        merged_ans = _append_with_sep(anchor.answer_text, cont.answer_text, " | ")
    return anchor.model_copy(
        update={
            "answer_text": merged_ans,
            "meta": _meta_add_continued_from(anchor.meta, cont.page_number),
        }
    )


def _merge_textual_continuation(anchor: ExtractedRow, cont: ExtractedRow) -> ExtractedRow:
    new_q = anchor.question_text
    # If the continuation had a "Continued: ..." title, keep body in answer
    c_q = (cont.question_text or "").strip()
    body_from_q = ""
    m = _CONTINUED_Q_RE.match(c_q)
    if m and len(c_q) > m.end():
        body_from_q = c_q[m.end() :].strip()
    new_a = _append_with_sep(anchor.answer_text, cont.answer_text)
    if body_from_q:
        new_a = _append_with_sep(new_a, body_from_q)
    return anchor.model_copy(
        update={
            "question_text": new_q,
            "answer_text": new_a,
            "meta": _meta_add_continued_from(anchor.meta, cont.page_number),
        }
    )


def _can_merge_option_group(anchor: ExtractedRow, cont: ExtractedRow) -> bool:
    if anchor.question_type not in (
        QuestionType.RADIO_BUTTON,
        QuestionType.CHECKBOX_GROUP,
        QuestionType.RADIO_BUTTON_WITH_TEXT_AREA,
        QuestionType.CHECKBOX_GROUP_WITH_TEXT_AREA,
    ):
        return False
    if cont.question_type != anchor.question_type:
        return False
    if _norm_key(anchor.question_text) != _norm_key(cont.question_text):
        return False
    if cont.page_number <= anchor.page_number:
        return False
    if not (_split_pipe_options(anchor.answer_text) or _split_pipe_options(cont.answer_text)):
        return False
    if _is_blocked_merge_label(anchor.question_text) and not _is_explicit_continued_question(
        cont.question_text
    ):
        return False
    return True


def _can_merge_textual_block(anchor: ExtractedRow, cont: ExtractedRow) -> bool:
    if not _is_explicit_continued_question(cont.question_text):
        return False
    if cont.page_number <= anchor.page_number:
        return False
    if anchor.question_type not in (
        QuestionType.TEXT_AREA,
        QuestionType.DISPLAY,
    ):
        return False
    if cont.question_type not in (anchor.question_type, QuestionType.DISPLAY, QuestionType.TEXT_AREA):
        return False
    if _is_blocked_merge_label(anchor.question_text) and not _is_explicit_continued_question(
        cont.question_text
    ):
        return False
    # If continuation has a full duplicate heading + body, only merge as continuation
    if _norm_key(anchor.question_text) == _norm_key(cont.question_text) and not _CONTINUED_STANDALONE_RE.match(
        (cont.question_text or "").strip()
    ):
        # Same heading on next page could be a repeated section, not a continuation
        if not _CONTINUED_Q_RE.match((cont.question_text or "").strip()):
            return False
    return True


def _can_merge_pair(anchor: ExtractedRow, cont: ExtractedRow) -> bool:
    if _can_merge_option_group(anchor, cont):
        return True
    if _can_merge_textual_block(anchor, cont):
        return True
    return False


def _do_merge(anchor: ExtractedRow, cont: ExtractedRow) -> ExtractedRow:
    if _can_merge_option_group(anchor, cont):
        return _merge_option_rows(anchor, cont)
    if _can_merge_textual_block(anchor, cont):
        return _merge_textual_continuation(anchor, cont)
    return anchor


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


def _merge_continuation_rows(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    if len(rows) < 2:
        return list(rows)
    out: list[ExtractedRow] = []
    for r in rows:
        merged = False
        for k in range(len(out)):
            if _can_merge_pair(out[k], r):
                out[k] = _do_merge(out[k], r)
                merged = True
                break
        if not merged:
            out.append(r)
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
    # Continuation and choice-type rules (deterministic, before header/footer heuristics)
    merged: list[ExtractedRow] = _merge_continuation_rows(cleaned)
    merged = [_apply_choice_type_rules(r) for r in merged]

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

    def with_header_prefix(r: ExtractedRow) -> ExtractedRow:
        qt = r.question_text or ""
        if qt.lower().startswith(_HEADER_PREFIX.strip().lower()):
            return r
        return r.model_copy(update={"question_text": f"{_HEADER_PREFIX}{qt}"})

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

    def is_valuable_footer_row(r: ExtractedRow) -> bool:
        # Conservative policy (with explicit exclusion): keep completion guidance and legal notices,
        # but do NOT keep ID/date metadata like OMB/form numbers, revisions, or expirations.
        blob = " ".join([r.question_text or "", r.answer_text or ""]).strip()
        if not blob:
            return False
        b = blob.lower()

        if is_generic_footer_noise(blob):
            return False

        if is_header_footer_id_or_date_metadata(blob):
            return False

        keywords = (
            "privacy act",
            "paperwork reduction act",
            "notice",
            "instructions",
            "submit",
            "return",
            "mail",
            "fax",
            "email",
            "retain",
            "keep a copy",
        )
        if any(k in b for k in keywords):
            return True

        return False

    # Repeated header detection: if identical rows appear in the header region on >=2 pages,
    # keep only the first occurrence, and mark it in the question_text.
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

        # Header: show only once (first occurrence), prefix `(header)` on the kept one.
        if is_header_region(r) and header_key_counts.get(k, 0) >= 2:
            if k in kept_header:
                continue
            kept_header.add(k)
            out.append(with_header_prefix(r))
            continue

        # Footer: keep only if valuable; for repeated valuable footers, show only once.
        if is_footer_region(r):
            if not is_valuable_footer_row(r):
                continue
            if footer_key_counts.get(k, 0) >= 2:
                if k in kept_footer:
                    continue
                kept_footer.add(k)
            out.append(r)
            continue

        out.append(r)

    return out
