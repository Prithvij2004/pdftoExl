"""Stage 3: confidence gate splitting high-confidence from LLM-bound blocks."""
from __future__ import annotations

from dataclasses import dataclass, field

from ...adapters.logging import get_logger
from .classification import ClassifiedBlocks

log = get_logger("gate")


@dataclass
class GateOutput:
    accepted: list[dict] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)


def run(classified: ClassifiedBlocks, *, confidence_threshold: float) -> GateOutput:
    accepted: list[dict] = []
    deferred: list[dict] = []
    for b in classified.blocks:
        if b.get("confidence", 0.0) >= confidence_threshold:
            accepted.append(b)
        else:
            deferred.append(b)
    log.info("gate", accepted=len(accepted), deferred=len(deferred))
    return GateOutput(accepted=accepted, deferred=deferred)
