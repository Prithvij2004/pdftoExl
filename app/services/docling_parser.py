from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


DOCLING_PARSER_VERSION = "docling-no-ocr-v1"


class DoclingPageMarkdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int = Field(description="1-based page number within the source PDF.")
    markdown: str = Field(description="Markdown rendering of the page produced by Docling.")


class DoclingParser(Protocol):
    version: str

    def parse(self, pdf_path: Path) -> list[DoclingPageMarkdown]:
        """Return per-page Markdown for the PDF."""


class DoclingMarkdownParser:
    version = DOCLING_PARSER_VERSION

    def __init__(self) -> None:
        self._converter = None

    def _build_converter(self):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        pipeline_options.do_table_structure = True
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )

    def parse(self, pdf_path: Path) -> list[DoclingPageMarkdown]:
        if self._converter is None:
            self._converter = self._build_converter()
        result = self._converter.convert(Path(pdf_path))
        document = result.document
        page_count = len(document.pages) if getattr(document, "pages", None) else 0
        if page_count == 0:
            return [DoclingPageMarkdown(page_number=1, markdown=document.export_to_markdown())]
        pages: list[DoclingPageMarkdown] = []
        for page_no in range(1, page_count + 1):
            markdown = document.export_to_markdown(page_no=page_no)
            pages.append(DoclingPageMarkdown(page_number=page_no, markdown=markdown))
        return pages
