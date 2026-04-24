"""Stage 5: merge gate-accepted + LLM-enriched blocks into a single stream."""
from __future__ import annotations

from dataclasses import dataclass

from ...adapters.logging import get_logger
from .gate import GateOutput
from .llm import LLMOutput

log = get_logger("merge")


@dataclass
class MergedBlocks:
    blocks: list[dict]


def run(gate_out: GateOutput, llm_out: LLMOutput, *, min_confidence: float) -> MergedBlocks:
    combined = [*gate_out.accepted, *llm_out.enriched]
    filtered = [b for b in combined if b.get("confidence", 1.0) >= min_confidence]
    log.info("merge", total=len(combined), kept=len(filtered))
    return MergedBlocks(blocks=filtered)
