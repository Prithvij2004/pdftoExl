from __future__ import annotations

import unicodedata
from datetime import date, datetime
from typing import Any

from dateutil import parser as date_parser


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFC", s)
    s = s.strip()
    return s


def normalize_case_insensitive(value: Any) -> str:
    return normalize_string(value).casefold()


def is_blank(value: Any) -> bool:
    return normalize_string(value) == ""


def strings_equal(a: Any, b: Any, *, case_insensitive: bool = False) -> bool:
    na = normalize_string(a)
    nb = normalize_string(b)
    if case_insensitive:
        return na.casefold() == nb.casefold()
    return na == nb


def coerce_numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    s = normalize_string(value)
    if s == "":
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def numbers_equal(a: Any, b: Any, *, tol: float = 1e-9) -> bool:
    na = coerce_numeric(a)
    nb = coerce_numeric(b)
    if na is None or nb is None:
        return False
    return abs(na - nb) <= tol


def coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = normalize_string(value)
    if s == "":
        return None
    try:
        return date_parser.parse(s, dayfirst=False, yearfirst=False).date()
    except (ValueError, OverflowError, TypeError):
        return None


def dates_equal(a: Any, b: Any) -> bool:
    da = coerce_date(a)
    db = coerce_date(b)
    if da is None or db is None:
        return False
    return da == db


def cells_equal(a: Any, b: Any) -> bool:
    if is_blank(a) and is_blank(b):
        return True
    if isinstance(a, (date, datetime)) or isinstance(b, (date, datetime)):
        if dates_equal(a, b):
            return True
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        if numbers_equal(a, b):
            return True
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        if numbers_equal(a, b):
            return True
    return strings_equal(a, b)


def controlled_vocab_contains(vocab: list[str], value: Any) -> bool:
    if is_blank(value):
        return True
    norm = {normalize_string(v).casefold() for v in vocab}
    return normalize_string(value).casefold() in norm


def lookup_controlled_vocab(vocab: list[str], value: Any) -> str | None:
    if is_blank(value):
        return None
    target = normalize_string(value).casefold()
    for v in vocab:
        if normalize_string(v).casefold() == target:
            return v
    return None
