"""Eval E — cost and latency metrics.

Reads pipeline-emitted log records; never introspects pipeline internals.
A `CostTracker` context manager wraps a pipeline call and yields a result
object with wall-clock latency and (once fed log records) computed cost.

Expected log record shape (duck-typed):
    {
        "cost_usd":     float,    # optional
        "cache_hit":    bool,     # optional, per LLM call
        "llm_call":     bool,     # optional, marks a log row as an LLM call
        "routed":       bool,     # optional, marks a block as routed (v1)
        "block":        bool,     # optional, marks a block-extract event
    }
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional


LogRecord = Mapping[str, object]


def latency_s(start: float, end: float) -> float:
    return max(0.0, end - start)


def cost_usd(records: Iterable[LogRecord]) -> float:
    total = 0.0
    for r in records:
        v = r.get("cost_usd")
        if isinstance(v, (int, float)):
            total += float(v)
    return total


def cost_per_block_usd(total_cost: float, blocks_extracted: int) -> float:
    if blocks_extracted <= 0:
        return 0.0
    return total_cost / blocks_extracted


def route_rate(blocks_routed: int, blocks_extracted: int) -> float:
    if blocks_extracted <= 0:
        return 0.0
    return blocks_routed / blocks_extracted


def cache_hit_rate(records: Iterable[LogRecord]) -> float:
    total = 0
    hits = 0
    for r in records:
        if not r.get("llm_call"):
            continue
        total += 1
        if r.get("cache_hit"):
            hits += 1
    if total == 0:
        return 0.0
    return hits / total


def _count_true(records: Iterable[LogRecord], key: str) -> int:
    return sum(1 for r in records if bool(r.get(key)))


@dataclass
class EvalEResult:
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cost_per_block_usd: float = 0.0
    route_rate: float = 0.0
    cache_hit_rate: float = 0.0
    blocks_extracted: int = 0
    blocks_routed: int = 0
    llm_calls: int = 0


@dataclass
class CostTracker:
    """Context manager that captures wall-clock around a pipeline call.

    Usage:
        tracker = CostTracker()
        with tracker:
            xlsx_path = pipeline(pdf_path)
        tracker.ingest(log_records)
        result = tracker.result()
    """

    _start: Optional[float] = None
    _end: Optional[float] = None
    _records: list[LogRecord] = field(default_factory=list)

    def __enter__(self) -> "CostTracker":
        self._start = time.perf_counter()
        self._end = None
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._end = time.perf_counter()

    @property
    def elapsed_s(self) -> float:
        if self._start is None:
            return 0.0
        end = self._end if self._end is not None else time.perf_counter()
        return latency_s(self._start, end)

    def ingest(self, records: Iterable[LogRecord]) -> None:
        self._records.extend(records)

    def result(self) -> EvalEResult:
        recs = list(self._records)
        total_cost = cost_usd(recs)
        blocks = _count_true(recs, "block")
        routed = _count_true(recs, "routed")
        llm_calls = _count_true(recs, "llm_call")
        return EvalEResult(
            latency_s=self.elapsed_s,
            cost_usd=total_cost,
            cost_per_block_usd=cost_per_block_usd(total_cost, blocks),
            route_rate=route_rate(routed, blocks),
            cache_hit_rate=cache_hit_rate(recs),
            blocks_extracted=blocks,
            blocks_routed=routed,
            llm_calls=llm_calls,
        )


__all__ = [
    "CostTracker",
    "EvalEResult",
    "LogRecord",
    "cache_hit_rate",
    "cost_per_block_usd",
    "cost_usd",
    "latency_s",
    "route_rate",
]
