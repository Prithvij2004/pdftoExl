from __future__ import annotations

from pathlib import Path

from app.parsers.base import BasePDFParser


def _escape_table_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "\\|").strip()


def _table_to_markdown(table: list[list[object]]) -> str:
    rows = [[_escape_table_cell(cell) for cell in row] for row in table if row]
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    body = rows[1:]

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


class PdfPlumberPDFParser(BasePDFParser):
    def parse_to_markdown(self, pdf_path: Path) -> str:
        try:
            import pdfplumber
        except ImportError as e:
            raise RuntimeError(
                "pdfplumber is not installed. Install it to use PDF_PARSER=pdfplumber."
            ) from e

        chunks: list[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                chunks.append(f"<!-- page: {page_number} -->\n\n# Page {page_number}")

                text = page.extract_text() or ""
                if text.strip():
                    chunks.append(text.strip())

                for table in page.extract_tables() or []:
                    markdown_table = _table_to_markdown(table)
                    if markdown_table:
                        chunks.append(markdown_table)

        return "\n\n".join(chunks)
