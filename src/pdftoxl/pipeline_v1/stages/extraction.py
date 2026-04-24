"""Stage 1: raw-block extraction from the PDF. No-op scaffold."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...adapters.logging import get_logger

log = get_logger("extraction")


@dataclass
class RawBlocks:
    pdf_path: Path
    blocks: list[dict]


def run(pdf_path: Path) -> RawBlocks:
    log.info("extract", pdf=str(pdf_path))
    return RawBlocks(pdf_path=pdf_path, blocks=[])
