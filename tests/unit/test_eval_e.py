"""Unit tests for Eval E metrics."""
from __future__ import annotations

import time

import pytest

from pdftoxl.evals.metrics.eval_e import (
    CostTracker,
    cache_hit_rate,
    cost_per_block_usd,
    cost_usd,
    latency_s,
    route_rate,
)


def test_latency_s_basic():
    assert latency_s(100.0, 101.5) == pytest.approx(1.5)


def test_latency_s_clamped_non_negative():
    assert latency_s(200.0, 100.0) == 0.0


def test_cost_usd_sums_records():
    records = [
        {"cost_usd": 0.01},
        {"cost_usd": 0.02},
        {"cost_usd": 0.005},
        {"other": "ignored"},
    ]
    assert cost_usd(records) == pytest.approx(0.035)


def test_cost_usd_empty():
    assert cost_usd([]) == 0.0


def test_cost_per_block_usd():
    assert cost_per_block_usd(0.10, 10) == pytest.approx(0.01)


def test_cost_per_block_usd_zero_blocks():
    assert cost_per_block_usd(0.10, 0) == 0.0


def test_route_rate():
    assert route_rate(5, 20) == pytest.approx(0.25)


def test_route_rate_zero_blocks_is_zero():
    assert route_rate(0, 0) == 0.0


def test_cache_hit_rate():
    records = [
        {"llm_call": True, "cache_hit": True},
        {"llm_call": True, "cache_hit": False},
        {"llm_call": True, "cache_hit": True},
        {"llm_call": False, "cache_hit": True},  # non-LLM — ignored
    ]
    assert cache_hit_rate(records) == pytest.approx(2 / 3)


def test_cache_hit_rate_no_llm_calls():
    assert cache_hit_rate([{"block": True}]) == 0.0


def test_cost_tracker_measures_wall_clock():
    tracker = CostTracker()
    with tracker:
        time.sleep(0.01)
    assert tracker.elapsed_s >= 0.01


def test_cost_tracker_ingests_and_computes_result():
    tracker = CostTracker()
    with tracker:
        pass
    tracker.ingest(
        [
            {"block": True},
            {"block": True},
            {"block": True, "routed": True},
            {"block": True, "routed": True},
            {"llm_call": True, "cache_hit": False, "cost_usd": 0.02},
            {"llm_call": True, "cache_hit": True, "cost_usd": 0.0},
        ]
    )
    result = tracker.result()
    assert result.blocks_extracted == 4
    assert result.blocks_routed == 2
    assert result.cost_usd == pytest.approx(0.02)
    assert result.cost_per_block_usd == pytest.approx(0.005)
    assert result.route_rate == pytest.approx(0.5)
    assert result.cache_hit_rate == pytest.approx(0.5)
    assert result.llm_calls == 2


def test_cost_tracker_ingest_can_be_called_multiple_times():
    tracker = CostTracker()
    with tracker:
        pass
    tracker.ingest([{"cost_usd": 0.01}])
    tracker.ingest([{"cost_usd": 0.02}])
    assert tracker.result().cost_usd == pytest.approx(0.03)


def test_cost_tracker_elapsed_before_exit_is_nonzero():
    tracker = CostTracker()
    with tracker:
        time.sleep(0.005)
        mid = tracker.elapsed_s
    assert mid >= 0.0
    assert tracker.elapsed_s >= mid


def test_cost_tracker_no_enter_returns_zero():
    tracker = CostTracker()
    assert tracker.elapsed_s == 0.0
