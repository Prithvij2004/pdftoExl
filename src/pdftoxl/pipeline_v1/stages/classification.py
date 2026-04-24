"""Stage 2: rule-based block classification. No-op scaffold."""
from __future__ import annotations

from dataclasses import dataclass

from ...adapters.logging import get_logger
from .extraction import RawBlocks

log = get_logger("classification")


@dataclass
class ClassifiedBlocks:
    blocks: list[dict]


def run(raw: RawBlocks) -> ClassifiedBlocks:
    log.info("classify", n=len(raw.blocks))
    return ClassifiedBlocks(blocks=raw.blocks)
