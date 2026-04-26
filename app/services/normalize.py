from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.models import ExtractedRow, QuestionType


_WS_RE = re.compile(r"[ \t]+")


def _clean_text(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = s.strip()
    s = _WS_RE.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


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

    # Merge fragmented Display rows (common when paragraphs are split line-by-line)
    merged: list[ExtractedRow] = []
    for r in cleaned:
        if (
            merged
            and r.question_type == QuestionType.DISPLAY
            and merged[-1].question_type == QuestionType.DISPLAY
            and r.page_number == merged[-1].page_number
            and r.source_order == merged[-1].source_order + 1
        ):
            prev = merged[-1]
            merged[-1] = prev.model_copy(
                update={"question_text": _clean_text(prev.question_text + "\n" + r.question_text)}
            )
            continue
        merged.append(r)

    # Deduplicate obvious repeated headers/footers for Display/Group-like rows
    key_counts = Counter(
        (r.question_type, r.question_text)
        for r in merged
        if r.question_type in {QuestionType.DISPLAY, QuestionType.GROUP} and r.question_text
    )

    seen_pages: dict[tuple[QuestionType, str], set[int]] = defaultdict(set)
    for r in merged:
        if r.question_type in {QuestionType.DISPLAY, QuestionType.GROUP} and r.question_text:
            seen_pages[(r.question_type, r.question_text)].add(r.page_number)

    def is_probably_header_footer(r: ExtractedRow) -> bool:
        if r.question_type not in {QuestionType.DISPLAY, QuestionType.GROUP}:
            return False
        if not r.question_text or len(r.question_text) > 120:
            return False
        # Most headers appear at the very top; footers often near end.
        return r.source_order <= 1 or r.source_order >= 25

    deduped: list[ExtractedRow] = []
    kept_first_page: dict[tuple[QuestionType, str], int] = {}
    for r in merged:
        k = (r.question_type, r.question_text)
        if k in key_counts and key_counts[k] >= 2 and is_probably_header_footer(r):
            first_page = kept_first_page.get(k)
            if first_page is None:
                kept_first_page[k] = r.page_number
                deduped.append(r)
            else:
                # skip repeats on later pages
                continue
        else:
            deduped.append(r)

    # Drop any empty question_text rows that slipped through
    final = [r for r in deduped if r.question_text.strip()]

    return final

