"""Unit tests for Eval D metrics."""
from __future__ import annotations

import pytest

from pdftoxl.evals.metrics.eval_d import (
    CachingJudge,
    DisabledJudge,
    StubJudge,
    answer_text_equivalence,
    branching_logic_equivalence,
    equivalence_uplift_over_eval_B,
    question_text_equivalence,
    resolve_judge,
)


def test_branching_logic_equivalence_all_match_via_ast():
    candidates = ['Display if Q5 = "b" AND Q3 = "a"', 'Display if Q1 != "x"']
    references = ['Display if Q3 = "a" AND Q5 = "b"', 'Display if Q1 != "x"']
    result = branching_logic_equivalence(candidates, references)
    assert result.score == 1.0
    assert result.numerator == 2
    assert result.denominator == 2


def test_branching_logic_equivalence_mismatch():
    candidates = ['Display if Q1 = "a"', 'Display if Q2 = "b"']
    references = ['Display if Q1 = "a"', 'Display if Q2 = "different"']
    result = branching_logic_equivalence(candidates, references)
    assert result.score == pytest.approx(0.5)
    assert result.numerator == 1
    assert result.denominator == 2


def test_branching_logic_skips_both_empty_rows():
    candidates = [None, 'Display if Q1 = "a"']
    references = ["", 'Display if Q1 = "a"']
    result = branching_logic_equivalence(candidates, references)
    assert result.denominator == 1
    assert result.score == 1.0


def test_branching_logic_parse_error_counts_as_mismatch():
    candidates = ["garbage"]
    references = ['Display if Q1 = "a"']
    result = branching_logic_equivalence(candidates, references)
    assert result.score == 0.0


def test_question_text_exact_match_does_not_call_judge():
    judge = StubJudge()
    candidates = ["Date of Birth", "Gender"]
    references = ["Date of Birth", "Gender"]
    result = question_text_equivalence(candidates, references, judge)
    assert result.score == 1.0
    assert judge.calls == []


def test_question_text_judge_invoked_only_on_residual():
    judge = StubJudge(responses={("DOB", "Date of Birth"): "yes"})
    candidates = ["Gender", "DOB"]
    references = ["Gender", "Date of Birth"]
    result = question_text_equivalence(candidates, references, judge)
    assert result.score == 1.0
    assert judge.calls == [("DOB", "Date of Birth")]


def test_question_text_judge_says_no():
    judge = StubJudge(responses={("A", "B"): "no"})
    result = question_text_equivalence(["A"], ["B"], judge)
    assert result.score == 0.0


def test_question_text_judge_unknown_flags_unresolved():
    judge = StubJudge(default="unknown")
    result = question_text_equivalence(["A"], ["B"], judge)
    assert result.score == 0.0
    assert result.unresolved == 1


def test_answer_text_checkbox_set_equality():
    judge = StubJudge()
    candidates = ["red|blue|green"]
    references = ["green|red|blue"]
    result = answer_text_equivalence(candidates, references, judge, is_checkbox=[True])
    assert result.score == 1.0
    assert judge.calls == []


def test_answer_text_checkbox_difference_triggers_judge():
    judge = StubJudge(responses={("red|blue", "red|blue|green"): "no"})
    result = answer_text_equivalence(
        ["red|blue"], ["red|blue|green"], judge, is_checkbox=[True]
    )
    assert result.score == 0.0
    assert len(judge.calls) == 1


def test_answer_text_non_checkbox_exact_match():
    judge = StubJudge()
    result = answer_text_equivalence(["Yes"], ["Yes"], judge, is_checkbox=[False])
    assert result.score == 1.0


def test_caching_judge_memoizes_by_hash():
    inner = StubJudge(responses={("a", "b"): "yes"})
    caching = CachingJudge(inner=inner)
    caching.judge("a", "b")
    caching.judge("a", "b")
    caching.judge("a", "b")
    assert len(inner.calls) == 1


def test_caching_judge_does_not_cache_unknown():
    inner = StubJudge(default="unknown")
    caching = CachingJudge(inner=inner)
    caching.judge("a", "b")
    caching.judge("a", "b")
    assert len(inner.calls) == 2


def test_resolve_judge_default_is_disabled(monkeypatch):
    monkeypatch.delenv("PDFTOXL_ENABLE_D", raising=False)
    judge = resolve_judge()
    # Default judge returns "unknown" for all inputs.
    assert judge.judge("x", "y") == "unknown"


def test_resolve_judge_env_var_enables(monkeypatch):
    monkeypatch.setenv("PDFTOXL_ENABLE_D", "1")
    # We pass an explicit stub to avoid constructing the real Bedrock client
    # even though enable_d is on.
    stub = StubJudge(responses={("a", "b"): "yes"})
    judge = resolve_judge(judge=stub)
    assert judge.judge("a", "b") == "yes"


def test_resolve_judge_with_stub_wraps_in_cache():
    stub = StubJudge(responses={("a", "b"): "yes"})
    judge = resolve_judge(judge=stub)
    assert judge.judge("a", "b") == "yes"
    assert judge.judge("a", "b") == "yes"
    assert len(stub.calls) == 1


def test_equivalence_uplift_over_eval_b():
    equiv = {"Question Text": 1.0, "Branching Logic": 0.95}
    eval_b = {"Question Text": 0.98, "Branching Logic": 0.80}
    uplift = equivalence_uplift_over_eval_B(equiv, eval_b)
    assert uplift["Question Text"] == pytest.approx(0.02)
    assert uplift["Branching Logic"] == pytest.approx(0.15)


def test_equivalence_uplift_missing_eval_b_column():
    equiv = {"Answer Text": 0.9}
    uplift = equivalence_uplift_over_eval_B(equiv, {})
    assert uplift["Answer Text"] == pytest.approx(0.9)


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        branching_logic_equivalence(["a"], ["a", "b"])
    with pytest.raises(ValueError):
        question_text_equivalence(["a"], ["a", "b"], StubJudge())
    with pytest.raises(ValueError):
        answer_text_equivalence(["a"], ["a"], StubJudge(), is_checkbox=[True, False])
