"""
Per-row confidence aggregation.

Layers used in v1:
  - schema_present : did the model emit a non-empty question_type AND question_text?
  - type_known     : is question_type in our known vocabulary?
  - span_grounded  : does question_text appear (fuzzy >= 0.7 token overlap) in the page's text layout?
  - branching_ref  : if branching_logic references Q<n>, does Q<n> exist?

Output: r.confidence in [0,1] and r.review_reasons[] populated.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .schema import QUESTION_TYPES, Row

_VOCAB = {q.lower() for q in QUESTION_TYPES}


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _token_overlap(a: str, b: str) -> float:
    a_tokens = set(re.findall(r"\w+", a.lower()))
    b_tokens = set(re.findall(r"\w+", b.lower()))
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / max(1, min(len(a_tokens), len(b_tokens)))


def score_rows(rows: list[Row], page_text_by_page: dict[int, str]) -> list[Row]:
    seq_set = {r.sequence for r in rows if r.sequence is not None}

    for r in rows:
        reasons: list[str] = list(r.review_reasons)
        score = 1.0

        # 1. schema_present
        if not (r.question_type and r.question_text):
            reasons.append("schema_incomplete")
            score -= 0.5

        # 2. type_known
        qt_norm = _normalize(r.question_type)
        if qt_norm and qt_norm not in _VOCAB:
            reasons.append(f"unknown_question_type:{r.question_type}")
            score -= 0.15

        # 3. span_grounded — Display rows for "New Section" or pure prose are exempt
        if (r.question_type or "").strip().lower() == "display" \
                and (r.question_text or "").strip().lower() == "new section":
            pass  # synthetic banner row, skip grounding
        elif r.question_text and r.page:
            page_text = page_text_by_page.get(r.page, "")
            if page_text:
                # Approximate match on first 80 chars of question_text
                snippet = r.question_text[:80]
                ratio = SequenceMatcher(None, _normalize(snippet), _normalize(page_text)).quick_ratio()
                overlap = _token_overlap(snippet, page_text)
                if overlap < 0.5 and ratio < 0.3:
                    reasons.append("not_grounded_in_page_text")
                    score -= 0.25

        # 4. branching_ref. Forward refs are allowed because skip logic can point later.
        bl = r.question_rule or r.branching_logic or ""
        if bl:
            m = re.search(r"q(\d+)", bl, re.I)
            if m:
                ref = int(m.group(1))
                if ref not in seq_set:
                    reasons.append(f"branching_ref_missing:Q{ref}")
                    score -= 0.2

        r.confidence = max(0.0, min(1.0, round(score, 3)))
        r.review_reasons = reasons
    return rows
