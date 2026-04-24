from datetime import date, datetime

from pdftoxl.evals.normalize import (
    cells_equal,
    coerce_date,
    coerce_numeric,
    controlled_vocab_contains,
    dates_equal,
    is_blank,
    lookup_controlled_vocab,
    normalize_case_insensitive,
    normalize_string,
    numbers_equal,
    strings_equal,
)


def test_normalize_string_none_is_empty():
    assert normalize_string(None) == ""


def test_normalize_string_trim_and_nfc():
    s = "caf\u0065\u0301  "
    out = normalize_string(s)
    assert out == "caf\u00e9"


def test_is_blank_variants():
    assert is_blank(None)
    assert is_blank("")
    assert is_blank("   ")
    assert not is_blank("x")


def test_strings_equal_nfc_and_case():
    assert strings_equal("caf\u00e9", "caf\u0065\u0301")
    assert not strings_equal("Yes", "yes")
    assert strings_equal("Yes", "yes", case_insensitive=True)


def test_normalize_case_insensitive():
    assert normalize_case_insensitive("Hello") == "hello"


def test_coerce_numeric_and_equal():
    assert coerce_numeric("1,234.5") == 1234.5
    assert coerce_numeric(None) is None
    assert coerce_numeric("abc") is None
    assert numbers_equal("1.0", 1)
    assert not numbers_equal("x", 1)


def test_coerce_date_and_equal():
    assert coerce_date("2024-01-15") == date(2024, 1, 15)
    assert coerce_date(datetime(2024, 1, 15, 12, 0)) == date(2024, 1, 15)
    assert dates_equal("2024-01-15", "Jan 15, 2024")
    assert not dates_equal("not a date", "2024-01-15")


def test_cells_equal_none_and_empty():
    assert cells_equal(None, "")
    assert cells_equal("  ", None)
    assert cells_equal("abc", "abc")
    assert not cells_equal("abc", "abd")


def test_cells_equal_date_type():
    assert cells_equal(date(2024, 1, 15), "2024-01-15")


def test_cells_equal_numeric():
    assert cells_equal(1.0, "1")
    assert cells_equal(5, 5.0)


def test_controlled_vocab():
    vocab = ["Text Box", "Radio Button", "Checkbox Group"]
    assert controlled_vocab_contains(vocab, "text box")
    assert controlled_vocab_contains(vocab, None)
    assert not controlled_vocab_contains(vocab, "Textbox")
    assert lookup_controlled_vocab(vocab, "RADIO button") == "Radio Button"
    assert lookup_controlled_vocab(vocab, "nope") is None
