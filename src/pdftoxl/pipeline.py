from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Pipeline(Protocol):
    def __call__(self, pdf_path: Path) -> Path: ...


def make_null_pipeline(golden_xlsx: Path) -> Pipeline:
    golden = Path(golden_xlsx)

    def _null(pdf_path: Path) -> Path:
        return golden

    return _null
