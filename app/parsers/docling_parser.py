from __future__ import annotations

from pathlib import Path

from app.parsers.base import BasePDFParser


class DoclingPDFParser(BasePDFParser):
    def parse_to_markdown(self, pdf_path: Path) -> str:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as e:
            raise RuntimeError(
                "Docling is not installed. Install it to use PDF_PARSER=docling."
            ) from e

        converter = DocumentConverter()
        result = converter.convert(str(pdf_path))
        document = getattr(result, "document", None)
        if document is None:
            raise RuntimeError("Docling did not return a document object.")

        if hasattr(document, "export_to_markdown"):
            return document.export_to_markdown()
        if hasattr(document, "export_to_markdown_str"):
            return document.export_to_markdown_str()

        raise RuntimeError("Docling document does not support Markdown export.")
