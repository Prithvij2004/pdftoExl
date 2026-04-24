"""Branching-logic DSL parser and canonicalizer for Eval D.

Grammar (per eval-D-semantic-equivalence.md / metrics-glossary.md):

    branching := "Display if " term (" AND " term)*
    term      := qref op value
    qref      := "Q" digit+
    op        := "=" | "!=" | "in"
    value     := quoted-string | "(" value ("," value)* ")"

Canonicalization rules:
  - Unicode NFC on all text
  - Collapse internal whitespace
  - Quote style normalized to double quotes
  - Escaped quotes (\\' or '') reduced to a literal character
  - Conjuncts (AND terms) sorted for order-independent equality
  - Tuple values (for the `in` operator) sorted as sets
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Union


class DSLParseError(ValueError):
    pass


@dataclass(frozen=True)
class QRef:
    name: str  # e.g. "Q7"

    def canonical(self) -> str:
        return self.name


@dataclass(frozen=True)
class Literal:
    value: str

    def canonical(self) -> str:
        escaped = self.value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'


@dataclass(frozen=True)
class Tuple_:
    items: tuple["Value", ...]

    def canonical(self) -> str:
        # Sort by canonical form for set semantics.
        sorted_items = sorted(self.items, key=lambda v: v.canonical())
        return "(" + ", ".join(v.canonical() for v in sorted_items) + ")"


Value = Union[Literal, Tuple_]


@dataclass(frozen=True)
class Term:
    qref: QRef
    op: str  # "=", "!=", "in"
    value: Value

    def canonical(self) -> str:
        return f"{self.qref.canonical()} {self.op} {self.value.canonical()}"


@dataclass(frozen=True)
class Branching:
    terms: tuple[Term, ...] = field(default_factory=tuple)

    def canonical(self) -> str:
        sorted_terms = sorted(self.terms, key=lambda t: t.canonical())
        joined = " AND ".join(t.canonical() for t in sorted_terms)
        return f"Display if {joined}"


_OP_RE = re.compile(r"!=|=|\bin\b")
_QREF_RE = re.compile(r"Q\d+")
_AND_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^\s*display\s+if\s+", re.IGNORECASE)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


class _Cursor:
    __slots__ = ("s", "i")

    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def eat_ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def eof(self) -> bool:
        return self.i >= len(self.s)


def _parse_quoted(cur: _Cursor) -> Literal:
    # Accept either "..." or '...'. Handle backslash escapes and doubled-quote escape.
    cur.eat_ws()
    q = cur.peek()
    if q not in ('"', "'"):
        raise DSLParseError(f"expected quote at pos {cur.i}, got {q!r}")
    cur.i += 1
    out: list[str] = []
    while cur.i < len(cur.s):
        c = cur.s[cur.i]
        if c == "\\" and cur.i + 1 < len(cur.s):
            nxt = cur.s[cur.i + 1]
            out.append(nxt)
            cur.i += 2
            continue
        if c == q:
            # Doubled-quote escape: ''  or ""  -> literal quote
            if cur.i + 1 < len(cur.s) and cur.s[cur.i + 1] == q:
                out.append(q)
                cur.i += 2
                continue
            cur.i += 1
            return Literal("".join(out))
        out.append(c)
        cur.i += 1
    raise DSLParseError("unterminated string literal")


def _parse_value(cur: _Cursor) -> Value:
    cur.eat_ws()
    if cur.peek() == "(":
        cur.i += 1
        items: list[Value] = []
        cur.eat_ws()
        if cur.peek() == ")":
            cur.i += 1
            return Tuple_(tuple(items))
        while True:
            items.append(_parse_value(cur))
            cur.eat_ws()
            c = cur.peek()
            if c == ",":
                cur.i += 1
                continue
            if c == ")":
                cur.i += 1
                return Tuple_(tuple(items))
            raise DSLParseError(f"expected ',' or ')' at pos {cur.i}")
    return _parse_quoted(cur)


def _parse_qref(cur: _Cursor) -> QRef:
    cur.eat_ws()
    m = _QREF_RE.match(cur.s, cur.i)
    if not m:
        raise DSLParseError(f"expected question reference (Q<digits>) at pos {cur.i}")
    cur.i = m.end()
    return QRef(m.group(0))


def _parse_op(cur: _Cursor) -> str:
    cur.eat_ws()
    remainder = cur.s[cur.i:]
    # Check longest first.
    if remainder.startswith("!="):
        cur.i += 2
        return "!="
    if remainder.startswith("="):
        cur.i += 1
        return "="
    # "in" may be surrounded by whitespace; word boundary check.
    m = re.match(r"in\b", remainder, re.IGNORECASE)
    if m:
        cur.i += m.end()
        return "in"
    raise DSLParseError(f"expected operator at pos {cur.i}")


def _parse_term(cur: _Cursor) -> Term:
    qref = _parse_qref(cur)
    op = _parse_op(cur)
    value = _parse_value(cur)
    if op == "in" and not isinstance(value, Tuple_):
        # A scalar on the right of `in` is accepted but normalized to a singleton tuple.
        value = Tuple_((value,))
    return Term(qref=qref, op=op, value=value)


def parse(text: str) -> Branching:
    """Parse a branching-logic expression into an AST.

    Leading "Display if" is optional — some sources emit only the predicate.
    """
    if text is None:
        raise DSLParseError("cannot parse None")
    s = _nfc(text).strip()
    if not s:
        raise DSLParseError("empty expression")
    m = _PREFIX_RE.match(s)
    if m:
        s = s[m.end():]
    # Split on AND (case-insensitive, whitespace-tolerant).
    parts = _AND_RE.split(s)
    terms: list[Term] = []
    for part in parts:
        if not part.strip():
            raise DSLParseError("empty conjunct")
        cur = _Cursor(part.strip())
        term = _parse_term(cur)
        cur.eat_ws()
        if not cur.eof():
            raise DSLParseError(f"trailing input in conjunct: {cur.s[cur.i:]!r}")
        terms.append(term)
    return Branching(terms=tuple(terms))


def canonicalize(text: str) -> str:
    """Return the canonical string form of a branching-logic expression."""
    return parse(text).canonical()


def equivalent(a: str, b: str) -> bool:
    """AST-level equality: True iff both parse and their canonical forms match."""
    try:
        return canonicalize(a) == canonicalize(b)
    except DSLParseError:
        return False
