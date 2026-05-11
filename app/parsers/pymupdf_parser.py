from __future__ import annotations

from pathlib import Path

from app.parsers.base import BasePDFParser


class PyMuPDFParser(BasePDFParser):
    def parse_to_markdown(self, pdf_path: Path) -> str:
        try:
            import fitz
        except ImportError as e:
            raise RuntimeError(
                "PyMuPDF is not installed. Install it to use PDF_PARSER=pymupdf."
            ) from e

        chunks: list[str] = []
        with fitz.open(str(pdf_path)) as doc:
            for page_index, page in enumerate(doc, start=1):
                chunks.append(f"<!-- page: {page_index} -->\n\n# Page {page_index}")
                blocks = page.get_text("blocks") or []
                blocks = sorted(blocks, key=lambda block: (round(block[1], 1), round(block[0], 1)))
                text_blocks = [str(block[4]).strip() for block in blocks if len(block) > 4 and str(block[4]).strip()]
                if text_blocks:
                    chunks.append("\n\n".join(text_blocks))

        return "\n\n".join(chunks)
