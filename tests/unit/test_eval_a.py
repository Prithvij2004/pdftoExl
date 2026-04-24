"""Unit tests for Eval A metrics."""

from __future__ import annotations

import copy

import pytest

from pdftoxl.evals.metrics.eval_a import (
    SKIPPED_PLACEHOLDER,
    _match_blocks,
    block_coverage,
    branching_logic_exact_match,
    confidence_calibration,
    kendall_tau,
    parent_link_f1,
    run_eval_a,
    sequence_correctness,
    type_accuracy,
)


def _block(
    block_id: str,
    page: int = 1,
    x0: float = 0.0,
    y0: float = 0.0,
    x1: float = 10.0,
    y1: float = 10.0,
    text: str = "hello",
    block_type: str = "question_label",
    confidence: float = 0.9,
    parent_link=None,
    branching_logic=None,
    sequence=None,
    reading_order: int = 0,
) -> dict:
    return {
        "block_id": block_id,
        "page": page,
        "reading_order": reading_order,
        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "coord_origin": "top_left"},
        "text": text,
        "block_type": block_type,
        "confidence": confidence,
        "parent_link": parent_link,
        "branching_logic": branching_logic,
        "sequence": sequence,
        "question_type": None,
        "provenance": {"source": "rule"},
    }


def _doc(blocks):
    return {
        "schema_version": "1.0.0",
        "source": {"sha256": "x" * 64, "page_count": 1},
        "blocks": blocks,
    }


def test_block_coverage_missing_block():
    gold = [_block("g1", text="a"), _block("g2", text="b", x0=20, x1=30)]
    cand = [_block("c1", text="a")]
    matched, gc, _ = _match_blocks(cand, gold)
    assert gc == 2
    assert len(matched) == 1
    assert block_coverage(len(matched), gc) == pytest.approx(0.5)


def test_block_coverage_bbox_tolerance():
    gold = [_block("g1", x0=10.0, y0=10.0, x1=20.0, y1=20.0, text="same")]
    cand = [_block("c1", x0=10.8, y0=10.3, x1=20.4, y1=20.7, text="same")]
    matched, gc, _ = _match_blocks(cand, gold)
    assert len(matched) == 1 and gc == 1


def test_type_accuracy_wrong_type():
    g = _block("g1", block_type="question_label")
    c = _block("c1", block_type="checkbox_option")
    matched, _, _ = _match_blocks([c], [g])
    overall, per_type = type_accuracy(matched)
    assert overall == 0.0
    assert per_type == {"question_label": 0.0}


def test_type_accuracy_partial():
    g1 = _block("g1", text="a", block_type="question_label")
    g2 = _block("g2", text="b", x0=30, x1=40, block_type="checkbox_option")
    c1 = _block("c1", text="a", block_type="question_label")
    c2 = _block("c2", text="b", x0=30, x1=40, block_type="radio_option")
    matched, _, _ = _match_blocks([c1, c2], [g1, g2])
    overall, per_type = type_accuracy(matched)
    assert overall == pytest.approx(0.5)
    assert per_type["question_label"] == 1.0
    assert per_type["checkbox_option"] == 0.0


def test_parent_link_f1_broken_link():
    gold = [
        _block("gp", text="p"),
        _block(
            "gc",
            text="c",
            x0=30,
            x1=40,
            parent_link={"parent_block_id": "gp", "relation": "option_of"},
        ),
    ]
    cand = [
        _block("gp", text="p"),
        _block(
            "gc",
            text="c",
            x0=30,
            x1=40,
            parent_link={"parent_block_id": "wrong", "relation": "option_of"},
        ),
    ]
    f1, p, r = parent_link_f1(cand, gold)
    assert f1 == 0.0 and p == 0.0 and r == 0.0


def test_parent_link_f1_perfect():
    gold = [
        _block("gp", text="p"),
        _block(
            "gc",
            text="c",
            x0=30,
            x1=40,
            parent_link={"parent_block_id": "gp", "relation": "option_of"},
        ),
    ]
    f1, p, r = parent_link_f1(copy.deepcopy(gold), gold)
    assert f1 == 1.0 and p == 1.0 and r == 1.0


def test_parent_link_f1_mixed():
    gold = [
        _block(
            "a",
            parent_link={"parent_block_id": "P", "relation": "option_of"},
        ),
        _block(
            "b",
            x0=30,
            x1=40,
            parent_link={"parent_block_id": "Q", "relation": "option_of"},
        ),
    ]
    cand = [
        _block(
            "a",
            parent_link={"parent_block_id": "P", "relation": "option_of"},
        ),
        _block(
            "b",
            x0=30,
            x1=40,
            parent_link={"parent_block_id": "X", "relation": "option_of"},
        ),
    ]
    f1, p, r = parent_link_f1(cand, gold)
    assert p == pytest.approx(0.5)
    assert r == pytest.approx(0.5)
    assert f1 == pytest.approx(0.5)


def test_branching_logic_dsl_mismatch():
    g = _block("g1", branching_logic='Display if Q1 = "yes"')
    c_ok = _block("c1", branching_logic="Display if Q1 = 'yes'")
    c_bad = _block("c2", branching_logic='Display if Q1 = "no"')
    matched_ok, _, _ = _match_blocks([c_ok], [g])
    matched_bad, _, _ = _match_blocks([c_bad], [g])
    assert branching_logic_exact_match(matched_ok) == 1.0
    assert branching_logic_exact_match(matched_bad) == 0.0


def test_branching_logic_whitespace_normalized():
    g = _block("g1", branching_logic='Display if Q1 = "yes"')
    c = _block("c1", branching_logic='Display  if   Q1 =  "yes"')
    matched, _, _ = _match_blocks([c], [g])
    assert branching_logic_exact_match(matched) == 1.0


def test_branching_logic_no_golden_entries():
    g = _block("g1")
    c = _block("c1")
    matched, _, _ = _match_blocks([c], [g])
    assert branching_logic_exact_match(matched) == 1.0


def test_kendall_tau_reordered():
    # golden order 1,2,3; candidate order 3,2,1 → fully discordant
    pairs = [(1, 3), (2, 2), (3, 1)]
    assert kendall_tau(pairs) == pytest.approx(-1.0)


def test_kendall_tau_perfect():
    pairs = [(1, 1), (2, 2), (3, 3)]
    assert kendall_tau(pairs) == pytest.approx(1.0)


def test_sequence_correctness_reordered():
    g1 = _block("g1", text="a", sequence=1)
    g2 = _block("g2", text="b", x0=30, x1=40, sequence=2)
    g3 = _block("g3", text="c", x0=60, x1=70, sequence=3)
    c1 = _block("c1", text="a", sequence=3)
    c2 = _block("c2", text="b", x0=30, x1=40, sequence=2)
    c3 = _block("c3", text="c", x0=60, x1=70, sequence=1)
    matched, _, _ = _match_blocks([c1, c2, c3], [g1, g2, g3])
    assert sequence_correctness(matched) == pytest.approx(-1.0)


def test_confidence_calibration_miscalibration():
    # high confidence, wrong type → low bin accuracy
    g = _block("g1", block_type="question_label")
    c = _block("c1", block_type="checkbox_option", confidence=0.95)
    matched, _, _ = _match_blocks([c], [g])
    cal = confidence_calibration(matched)
    assert cal == {"0.8-1.0": 0.0}


def test_confidence_calibration_well_calibrated():
    g1 = _block("g1", text="a", block_type="question_label")
    g2 = _block("g2", text="b", x0=30, x1=40, block_type="checkbox_option")
    c1 = _block("c1", text="a", block_type="question_label", confidence=0.9)
    c2 = _block(
        "c2", text="b", x0=30, x1=40, block_type="checkbox_option", confidence=0.85
    )
    matched, _, _ = _match_blocks([c1, c2], [g1, g2])
    cal = confidence_calibration(matched)
    assert cal["0.8-1.0"] == 1.0


def test_run_eval_a_skipped_placeholder():
    golden = {"_placeholder": True, "fixture_id": "FX-X"}
    result = run_eval_a({}, golden, "FX-X")
    assert result.status == SKIPPED_PLACEHOLDER
    assert result.block_coverage is None


def test_run_eval_a_perfect_self_compare():
    blocks = [
        _block("a", text="a", sequence=1, block_type="question_label"),
        _block(
            "b",
            text="b",
            x0=30,
            x1=40,
            sequence=2,
            block_type="checkbox_option",
            parent_link={"parent_block_id": "a", "relation": "option_of"},
        ),
    ]
    doc = _doc(blocks)
    result = run_eval_a(copy.deepcopy(doc), doc, "FX-SELF")
    assert result.status == "OK"
    assert result.block_coverage == 1.0
    assert result.type_accuracy == 1.0
    assert result.parent_link_f1 == 1.0
    assert result.branching_logic_exact_match == 1.0
    assert result.sequence_correctness == 1.0


def test_contracts_a_validates():
    from pdftoxl.evals._contracts_a import EnrichedDocument

    doc = _doc([_block("a", text="a", sequence=1)])
    parsed = EnrichedDocument.model_validate(doc)
    assert parsed.blocks[0].block_id == "a"
