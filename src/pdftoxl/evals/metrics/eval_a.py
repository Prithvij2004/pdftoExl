"""Eval A — enriched JSON structure check.

All metrics are per-fixture. No aggregation across fixtures here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

BBOX_TOLERANCE = 2.0
SKIPPED_PLACEHOLDER = "SKIPPED_PLACEHOLDER"


@dataclass
class EvalAResult:
    fixture_id: str
    status: str  # "OK" | "SKIPPED_PLACEHOLDER"
    block_coverage: Optional[float] = None
    type_accuracy: Optional[float] = None
    type_accuracy_per_block_type: dict[str, float] = field(default_factory=dict)
    parent_link_f1: Optional[float] = None
    parent_link_precision: Optional[float] = None
    parent_link_recall: Optional[float] = None
    branching_logic_exact_match: Optional[float] = None
    sequence_correctness: Optional[float] = None
    confidence_calibration: dict[str, float] = field(default_factory=dict)


BlockLike = Mapping[str, Any]
DocLike = Mapping[str, Any]


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bbox_key(bbox: Mapping[str, Any]) -> tuple[int, int, int, int]:
    # Quantize by tolerance so near-equal bboxes collide.
    q = BBOX_TOLERANCE
    return (
        int(round(bbox["x0"] / q)),
        int(round(bbox["y0"] / q)),
        int(round(bbox["x1"] / q)),
        int(round(bbox["y1"] / q)),
    )


def _identity(block: BlockLike) -> tuple[int, tuple[int, int, int, int], str]:
    return (block["page"], _bbox_key(block["bbox"]), _text_sha(block["text"]))


_WS = re.compile(r"\s+")


def _canon_branching(expr: str) -> str:
    s = expr.replace("'", '"')
    s = _WS.sub(" ", s).strip()
    return s


def is_placeholder(golden: Mapping[str, Any]) -> bool:
    return bool(golden.get("_placeholder"))


def _match_blocks(
    candidate_blocks: Iterable[BlockLike],
    golden_blocks: Iterable[BlockLike],
) -> tuple[list[tuple[BlockLike, BlockLike]], int, int]:
    cand_by_id = {_identity(b): b for b in candidate_blocks}
    gold_list = list(golden_blocks)
    matched: list[tuple[BlockLike, BlockLike]] = []
    for gb in gold_list:
        key = _identity(gb)
        cb = cand_by_id.get(key)
        if cb is not None:
            matched.append((cb, gb))
    return matched, len(gold_list), len(cand_by_id)


def block_coverage(matched_count: int, golden_count: int) -> float:
    if golden_count == 0:
        return 1.0
    return matched_count / golden_count


def type_accuracy(matched: list[tuple[BlockLike, BlockLike]]) -> tuple[float, dict[str, float]]:
    if not matched:
        return 1.0, {}
    correct = 0
    per_type_total: dict[str, int] = {}
    per_type_correct: dict[str, int] = {}
    for cb, gb in matched:
        gtype = str(gb["block_type"])
        per_type_total[gtype] = per_type_total.get(gtype, 0) + 1
        if str(cb["block_type"]) == gtype:
            correct += 1
            per_type_correct[gtype] = per_type_correct.get(gtype, 0) + 1
    overall = correct / len(matched)
    per_type = {
        t: per_type_correct.get(t, 0) / per_type_total[t] for t in per_type_total
    }
    return overall, per_type


def _triples(blocks: Iterable[BlockLike]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for b in blocks:
        pl = b.get("parent_link")
        if pl:
            out.add((b["block_id"], pl["parent_block_id"], pl["relation"]))
    return out


def parent_link_f1(
    candidate_blocks: Iterable[BlockLike], golden_blocks: Iterable[BlockLike]
) -> tuple[float, float, float]:
    cand = _triples(candidate_blocks)
    gold = _triples(golden_blocks)
    tp = len(cand & gold)
    fp = len(cand - gold)
    fn = len(gold - cand)
    if tp == 0 and (fp > 0 or fn > 0):
        return 0.0, 0.0, 0.0
    if tp == 0 and fp == 0 and fn == 0:
        return 1.0, 1.0, 1.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall == 0:
        return 0.0, precision, recall
    f1 = 2 * precision * recall / (precision + recall)
    return f1, precision, recall


def branching_logic_exact_match(matched: list[tuple[BlockLike, BlockLike]]) -> float:
    gold_with_logic = [
        (cb, gb) for cb, gb in matched if gb.get("branching_logic") is not None
    ]
    if not gold_with_logic:
        return 1.0
    hits = 0
    for cb, gb in gold_with_logic:
        cand_expr = cb.get("branching_logic")
        if cand_expr is None:
            continue
        if _canon_branching(cand_expr) == _canon_branching(gb["branching_logic"]):
            hits += 1
    return hits / len(gold_with_logic)


def kendall_tau(pairs: list[tuple[int, int]]) -> float:
    n = len(pairs)
    if n < 2:
        return 1.0
    concordant = 0
    discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            a0, b0 = pairs[i]
            a1, b1 = pairs[j]
            da = a1 - a0
            db = b1 - b0
            if da == 0 or db == 0:
                continue
            if (da > 0) == (db > 0):
                concordant += 1
            else:
                discordant += 1
        # end inner
    total = concordant + discordant
    if total == 0:
        return 1.0
    return (concordant - discordant) / total


def sequence_correctness(matched: list[tuple[BlockLike, BlockLike]]) -> float:
    pairs: list[tuple[int, int]] = []
    for cb, gb in matched:
        cs = cb.get("sequence")
        gs = gb.get("sequence")
        if cs is None or gs is None:
            continue
        pairs.append((int(gs), int(cs)))
    return kendall_tau(pairs)


def confidence_calibration(matched: list[tuple[BlockLike, BlockLike]]) -> dict[str, float]:
    bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0 + 1e-9)]
    labels = ["0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"]
    out: dict[str, float] = {}
    for (lo, hi), label in zip(bins, labels):
        items = [
            (cb, gb)
            for cb, gb in matched
            if lo <= float(cb["confidence"]) < hi
        ]
        if not items:
            continue
        hits = sum(
            1 for cb, gb in items if str(cb["block_type"]) == str(gb["block_type"])
        )
        out[label] = hits / len(items)
    return out


def run_eval_a(candidate: DocLike, golden: DocLike, fixture_id: str) -> EvalAResult:
    if is_placeholder(golden):
        return EvalAResult(fixture_id=fixture_id, status=SKIPPED_PLACEHOLDER)

    cand_blocks = candidate.get("blocks", [])
    gold_blocks = golden.get("blocks", [])
    matched, gold_count, _ = _match_blocks(cand_blocks, gold_blocks)

    cov = block_coverage(len(matched), gold_count)
    ta, ta_per_type = type_accuracy(matched)
    f1, p, r = parent_link_f1(cand_blocks, gold_blocks)
    bl = branching_logic_exact_match(matched)
    seq = sequence_correctness(matched)
    cal = confidence_calibration(matched)

    return EvalAResult(
        fixture_id=fixture_id,
        status="OK",
        block_coverage=cov,
        type_accuracy=ta,
        type_accuracy_per_block_type=ta_per_type,
        parent_link_f1=f1,
        parent_link_precision=p,
        parent_link_recall=r,
        branching_logic_exact_match=bl,
        sequence_correctness=seq,
        confidence_calibration=cal,
    )
