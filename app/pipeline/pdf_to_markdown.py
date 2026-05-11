from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.markdown.normalizer import normalize_markdown
from app.parsers.parser_factory import ParserFactory


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarkdownConversionResult:
    markdown_path: Path
    metadata_path: Path
    parser: str
    page_count: int
    status: str


def _safe_stem(path: Path) -> str:
    stem = path.stem.strip() or "document"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "document"


def _page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError:
        return 0

    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        logger.exception("Failed to read PDF page count: %s", pdf_path)
        return 0


def _write_metadata(path: Path, metadata: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def convert_pdf_to_markdown(
    pdf_path: Path,
    parser_name: str | None = None,
    output_dir: Path | None = None,
) -> MarkdownConversionResult:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Input file must be a PDF: {pdf_path}")

    selected_parser = (parser_name or settings.pdf_parser).strip().lower()
    parser = ParserFactory.create(selected_parser)

    base_output_dir = Path(output_dir) if output_dir is not None else settings.output_dir
    markdown_dir = base_output_dir if base_output_dir.name == "markdowns" else base_output_dir / "markdowns"
    markdown_dir.mkdir(parents=True, exist_ok=True)

    output_stem = _safe_stem(pdf_path)
    markdown_path = markdown_dir / f"{output_stem}.md"
    metadata_path = markdown_dir / f"{output_stem}.metadata.json"
    page_count = _page_count(pdf_path)

    logger.info("PDF to Markdown started: parser=%s input=%s", selected_parser, pdf_path)
    metadata: dict[str, object] = {
        "input_pdf": str(pdf_path),
        "parser": selected_parser,
        "output_markdown": str(markdown_path),
        "metadata_path": str(metadata_path),
        "status": "started",
        "page_count": page_count,
    }

    try:
        raw_markdown = parser.parse_to_markdown(pdf_path)
        markdown = normalize_markdown(raw_markdown)
        if not markdown.strip():
            metadata["status"] = "empty_output"
            _write_metadata(metadata_path, metadata)
            raise RuntimeError(f"Parser produced empty Markdown output: {selected_parser}")

        markdown_path.write_text(markdown, encoding="utf-8")
        metadata["status"] = "success"
        _write_metadata(metadata_path, metadata)
        logger.info(
            "PDF to Markdown completed: parser=%s output=%s page_count=%s",
            selected_parser,
            markdown_path,
            page_count,
        )
        return MarkdownConversionResult(
            markdown_path=markdown_path,
            metadata_path=metadata_path,
            parser=selected_parser,
            page_count=page_count,
            status="success",
        )
    except Exception as e:
        metadata["status"] = metadata.get("status") if metadata.get("status") == "empty_output" else "error"
        metadata["error"] = str(e)
        _write_metadata(metadata_path, metadata)
        logger.exception("PDF to Markdown failed: parser=%s input=%s", selected_parser, pdf_path)
        raise
