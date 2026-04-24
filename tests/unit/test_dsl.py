"""Unit tests for the branching-logic DSL canonicalizer."""
from __future__ import annotations

import pytest

from pdftoxl.evals.dsl import (
    Branching,
    DSLParseError,
    Literal,
    QRef,
    Term,
    Tuple_,
    canonicalize,
    equivalent,
    parse,
)


def test_parse_simple_equality():
    ast = parse('Display if Q7 = "yes"')
    assert ast == Branching(terms=(Term(QRef("Q7"), "=", Literal("yes")),))


def test_canonical_form_uses_double_quotes():
    # Single quotes in source become double quotes in canonical form.
    assert canonicalize("Display if Q7 = 'yes'") == 'Display if Q7 = "yes"'


def test_whitespace_normalization():
    a = 'Display if  Q7  =  "yes"  AND   Q3 != "no"'
    b = 'Display if Q7="yes" AND Q3!="no"'
    assert canonicalize(a) == canonicalize(b)


def test_conjunct_ordering_is_sorted():
    a = 'Display if Q3 = "yes" AND Q5 = "no"'
    b = 'Display if Q5 = "no" AND Q3 = "yes"'
    assert canonicalize(a) == canonicalize(b)


def test_escaped_single_quote_backslash():
    # "Lives in other\'s home" -> Lives in other's home
    canon = canonicalize(r"""Display if Q7 = 'Lives in other\'s home'""")
    assert canon == 'Display if Q7 = "Lives in other\'s home"'


def test_escaped_single_quote_doubled():
    # SQL-style doubled quote: 'Lives in other''s home' -> Lives in other's home
    canon = canonicalize("""Display if Q7 = 'Lives in other''s home'""")
    assert canon == 'Display if Q7 = "Lives in other\'s home"'


def test_equivalence_of_quote_styles():
    a = 'Display if Q7 = "Lives in other\'s home"'
    b = r"""Display if Q7 = 'Lives in other\'s home'"""
    assert equivalent(a, b)


def test_in_operator_with_tuple_values_set_semantics():
    a = 'Display if Q1 in ("a", "b", "c")'
    b = 'Display if Q1 in ("c", "a", "b")'
    assert equivalent(a, b)


def test_in_operator_singleton_normalized_to_tuple():
    # Scalar RHS of `in` is accepted and normalized to a singleton tuple.
    a = 'Display if Q1 in "a"'
    b = 'Display if Q1 in ("a")'
    assert equivalent(a, b)


def test_ne_operator():
    ast = parse('Display if Q7 != "x"')
    assert ast.terms[0].op == "!="


def test_display_if_prefix_optional():
    a = 'Q3 = "yes" AND Q5 = "no"'
    b = 'Display if Q5 = "no" AND Q3 = "yes"'
    assert equivalent(a, b)


def test_case_insensitive_and():
    a = 'Display if Q3 = "yes" and Q5 = "no"'
    b = 'Display if Q3 = "yes" AND Q5 = "no"'
    assert equivalent(a, b)


def test_nfc_normalization():
    # Composed vs decomposed é -> equivalent after NFC
    composed = 'Display if Q1 = "\u00e9"'       # é as single code point
    decomposed = 'Display if Q1 = "e\u0301"'    # e + combining acute
    assert equivalent(composed, decomposed)


def test_non_equivalence_different_values():
    assert not equivalent('Display if Q1 = "a"', 'Display if Q1 = "b"')


def test_non_equivalence_different_qrefs():
    assert not equivalent('Display if Q1 = "a"', 'Display if Q2 = "a"')


def test_non_equivalence_different_ops():
    assert not equivalent('Display if Q1 = "a"', 'Display if Q1 != "a"')


def test_parse_error_empty():
    with pytest.raises(DSLParseError):
        parse("")


def test_parse_error_garbage():
    with pytest.raises(DSLParseError):
        parse("this is not a branching expression")


def test_parse_error_trailing_input():
    with pytest.raises(DSLParseError):
        parse('Display if Q1 = "a" extra')


def test_equivalent_returns_false_on_parse_error():
    assert not equivalent("garbage", 'Display if Q1 = "a"')


def test_tuple_with_internal_whitespace():
    a = 'Display if Q1 in ("a","b")'
    b = 'Display if Q1 in ( "a" , "b" )'
    assert canonicalize(a) == canonicalize(b)


def test_canonical_form_is_stable():
    # Canonical of canonical is itself.
    c1 = canonicalize('Display if Q5 = "b" AND Q3 = "a"')
    c2 = canonicalize(c1)
    assert c1 == c2
