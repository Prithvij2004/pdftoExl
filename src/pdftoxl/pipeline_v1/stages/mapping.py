"""Stage 6: map merged blocks to EAB-Excel rows (28-column contract)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ...adapters.logging import get_logger
from .merge import MergedBlocks

log = get_logger("mapping")


@dataclass
class MappedRows:
    rows: list[dict] = field(default_factory=list)


def run(merged: MergedBlocks) -> MappedRows:
    log.info("map", n=len(merged.blocks))
    return MappedRows(rows=[])
