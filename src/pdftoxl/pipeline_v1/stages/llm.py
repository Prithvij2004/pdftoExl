"""Stage 4: LLM enrichment for low-confidence blocks via Bedrock."""
from __future__ import annotations

from dataclasses import dataclass

from ...adapters.bedrock import LLMClient
from ...adapters.logging import get_logger
from .gate import GateOutput

log = get_logger("llm")


@dataclass
class LLMOutput:
    enriched: list[dict]


def run(gate_out: GateOutput, client: LLMClient | None) -> LLMOutput:
    if client is None or not gate_out.deferred:
        log.info("llm.skip", deferred=len(gate_out.deferred), has_client=client is not None)
        return LLMOutput(enriched=gate_out.deferred)

    log.info("llm.invoke", n=len(gate_out.deferred))
    # Real prompts land in a later issue; scaffold keeps blocks unchanged.
    return LLMOutput(enriched=gate_out.deferred)
