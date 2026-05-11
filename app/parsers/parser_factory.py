from __future__ import annotations

from app.parsers.base import BasePDFParser
from app.parsers.docling_parser import DoclingPDFParser
from app.parsers.pdfplumber_parser import PdfPlumberPDFParser
from app.parsers.pymupdf_parser import PyMuPDFParser


class ParserFactory:
    _PARSERS = {
        "docling": DoclingPDFParser,
        "pdfplumber": PdfPlumberPDFParser,
        "pymupdf": PyMuPDFParser,
    }

    @classmethod
    def create(cls, parser_name: str) -> BasePDFParser:
        name = (parser_name or "").strip().lower()
        parser_cls = cls._PARSERS.get(name)
        if parser_cls is None:
            supported = ", ".join(sorted(cls._PARSERS))
            raise ValueError(f"Unsupported parser: {parser_name}. Supported parsers: {supported}")
        return parser_cls()

    @classmethod
    def supported_parsers(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._PARSERS))
