from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter


@dataclass(frozen=True)
class PdfBatch:
    batch_index: int
    start_page_number: int  # 1-indexed
    end_page_number: int  # 1-indexed inclusive
    pdf_bytes: bytes


def iter_pdf_page_batches(pdf_path: Path, batch_size: int) -> Iterable[PdfBatch]:
    if batch_size <= 0:
        raise ValueError("batch_size must be >= 1")

    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)

    batch_index = 0
    for start_idx in range(0, total_pages, batch_size):
        end_idx = min(start_idx + batch_size, total_pages)
        writer = PdfWriter()
        for i in range(start_idx, end_idx):
            writer.add_page(reader.pages[i])

        buf = BytesIO()
        writer.write(buf)
        yield PdfBatch(
            batch_index=batch_index,
            start_page_number=start_idx + 1,
            end_page_number=end_idx,
            pdf_bytes=buf.getvalue(),
        )
        batch_index += 1

