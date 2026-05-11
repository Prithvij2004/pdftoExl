from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.pipeline.markdown_to_excel import ExcelConversionResult, convert_markdown_to_excel
from app.pipeline.pdf_to_markdown import MarkdownConversionResult, convert_pdf_to_markdown


@dataclass(frozen=True)
class PdfToExcelResult:
    markdown: MarkdownConversionResult
    excel: ExcelConversionResult


def convert_pdf_to_excel(pdf_path: Path, parser_name: str | None = None) -> PdfToExcelResult:
    markdown_result = convert_pdf_to_markdown(pdf_path, parser_name=parser_name)
    excel_result = convert_markdown_to_excel(markdown_result.markdown_path)
    return PdfToExcelResult(markdown=markdown_result, excel=excel_result)
