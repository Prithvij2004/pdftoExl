from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any, Protocol

import fitz
from pydantic import BaseModel, ConfigDict, Field


PYMUPDF_PARSER_VERSION = f"pymupdf-{fitz.VersionBind}-layout-v1"
DEFAULT_LAYOUT_PARSER_BACKEND = "pymupdf"
CHECKBOX_RADIO_GLYPHS = frozenset("☐☑☒□■✓✔○●◯◉")


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)

    x0: float = Field(description="Left edge of the bounding box in PDF user-space units.")
    y0: float = Field(description="Top edge of the bounding box in PDF user-space units.")
    x1: float = Field(description="Right edge of the bounding box in PDF user-space units.")
    y1: float = Field(description="Bottom edge of the bounding box in PDF user-space units.")

    def __init__(self, *args: float, **kwargs: float) -> None:
        if args:
            if len(args) != 4 or kwargs:
                raise TypeError("BoundingBox accepts either 4 positional floats or keyword args.")
            kwargs = {"x0": args[0], "y0": args[1], "x1": args[2], "y1": args[3]}
        super().__init__(**kwargs)

    @classmethod
    def from_rect(cls, rect: fitz.Rect | tuple[float, float, float, float]) -> "BoundingBox":
        if isinstance(rect, fitz.Rect):
            return cls(x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)
        x0, y0, x1, y1 = rect
        return cls(x0=x0, y0=y0, x1=x1, y1=y1)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def to_dict(self) -> dict[str, float]:
        return {
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
        }


class TextSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the span, e.g. p1_s0001.")
    text: str = Field(description="Verbatim glyph text for this span.")
    bbox: BoundingBox = Field(description="Bounding box of the span on the rendered page.")
    font: str = Field(description="Font name reported by PyMuPDF.")
    size: float = Field(description="Font size in points.")
    flags: int = Field(description="Raw font flag bitmask from PyMuPDF.")
    color: int | None = Field(default=None, description="Encoded RGB colour or None when not set.")
    is_bold: bool = Field(description="True when the span is rendered bold.")
    is_italic: bool = Field(description="True when the span is rendered italic or oblique.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "font": self.font,
            "size": round(self.size, 2),
            "flags": self.flags,
            "color": self.color,
            "is_bold": self.is_bold,
            "is_italic": self.is_italic,
        }


class TextLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the line, e.g. p1_l0001.")
    text: str = Field(description="Concatenated text of the spans on this line.")
    bbox: BoundingBox = Field(description="Bounding box of the line.")
    span_ids: list[str] = Field(description="Identifiers of the spans that compose this line in reading order.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "span_ids": list(self.span_ids),
        }


class TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the block, e.g. p1_b0001.")
    text: str = Field(description="Newline-joined block text.")
    bbox: BoundingBox = Field(description="Bounding box of the block.")
    line_ids: list[str] = Field(description="Identifiers of the lines that compose this block in reading order.")
    block_type: str = Field(description="'text' or 'image' as reported by the layout parser.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "line_ids": list(self.line_ids),
            "block_type": self.block_type,
        }


class FormWidget(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the widget, e.g. p1_w0001.")
    field_name: str | None = Field(description="Internal PDF field name when present.")
    field_label: str | None = Field(description="Human-readable label PyMuPDF associates with the widget.")
    field_type: str = Field(description="Normalised widget kind: text, checkbox, radio, combobox, listbox, signature, or button.")
    value: str | None = Field(description="Current widget value if any.")
    bbox: BoundingBox = Field(description="Bounding box of the widget.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "field_name": self.field_name,
            "field_label": self.field_label,
            "field_type": self.field_type,
            "value": self.value,
            "bbox": self.bbox.to_dict(),
        }


class ChoiceGlyphCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the candidate.")
    glyph: str = Field(description="Glyph character or shape token associated with the choice candidate.")
    bbox: BoundingBox = Field(description="Bounding box of the glyph or shape.")
    source: str = Field(description="Origin of the candidate: text_glyph or drawing_shape.")
    nearest_text: str | None = Field(default=None, description="Best-effort nearest line text for prompt context.")
    confidence: float = Field(default=0.0, description="Heuristic confidence score in [0, 1].")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "glyph": self.glyph,
            "bbox": self.bbox.to_dict(),
            "source": self.source,
            "nearest_text": self.nearest_text,
            "confidence": round(self.confidence, 2),
        }


class TableCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier for the table candidate.")
    bbox: BoundingBox = Field(description="Bounding box of the candidate table.")
    row_count: int | None = Field(description="Detected row count or None when unknown.")
    column_count: int | None = Field(description="Detected column count or None when unknown.")
    source: str = Field(description="Source of the candidate (e.g. pymupdf_find_tables).")
    header_text: list[str] = Field(default_factory=list, description="Cell text from the first detected row, when extractable.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bbox": self.bbox.to_dict(),
            "row_count": self.row_count,
            "column_count": self.column_count,
            "source": self.source,
            "header_text": list(self.header_text),
        }


class RawPageModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int = Field(description="1-based page number within the PDF.")
    width: float = Field(description="Page width in user-space units.")
    height: float = Field(description="Page height in user-space units.")
    parser_version: str = Field(description="Identifier of the layout parser version that produced this page.")
    spans: list[TextSpan] = Field(description="All text spans extracted from the page in reading order.")
    lines: list[TextLine] = Field(description="Lines composed from spans.")
    blocks: list[TextBlock] = Field(description="Layout blocks composed from lines.")
    widgets: list[FormWidget] = Field(description="AcroForm widgets reported on this page.")
    choice_glyph_candidates: list[ChoiceGlyphCandidate] = Field(description="Heuristic checkbox/radio glyph candidates.")
    table_candidates: list[TableCandidate] = Field(description="Heuristic table candidates.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "parser_version": self.parser_version,
            "spans": [span.to_dict() for span in self.spans],
            "lines": [line.to_dict() for line in self.lines],
            "blocks": [block.to_dict() for block in self.blocks],
            "widgets": [widget.to_dict() for widget in self.widgets],
            "choice_glyph_candidates": [
                candidate.to_dict() for candidate in self.choice_glyph_candidates
            ],
            "table_candidates": [candidate.to_dict() for candidate in self.table_candidates],
        }


class RawPdfModel(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pdf_path: Path = Field(description="Filesystem path to the parsed PDF.")
    parser_version: str = Field(description="Identifier of the layout parser version that produced this model.")
    pages: list[RawPageModel] = Field(description="Pages in document order.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": str(self.pdf_path),
            "parser_version": self.parser_version,
            "pages": [page.to_dict() for page in self.pages],
        }


class LayoutParserBackend(Protocol):
    name: str
    version: str

    def parse(self, pdf_path: Path) -> RawPdfModel:
        """Return the stable raw layout contract for one PDF."""


class CheapLayoutParser:
    def __init__(self, backend: str | LayoutParserBackend | None = None) -> None:
        self.backend = resolve_layout_parser_backend(backend)

    def parse(self, pdf_path: Path | str) -> RawPdfModel:
        source = Path(pdf_path)
        if not source.exists():
            raise FileNotFoundError(f"PDF not found: {source}")
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a PDF file, got: {source}")

        return self.backend.parse(source)


class PyMuPDFLayoutParserBackend:
    name = "pymupdf"
    version = PYMUPDF_PARSER_VERSION

    def parse(self, pdf_path: Path) -> RawPdfModel:
        pages: list[RawPageModel] = []
        with fitz.open(pdf_path) as document:
            for index, page in enumerate(document, start=1):
                pages.append(_parse_pymupdf_page(page, index))

        return RawPdfModel(pdf_path=pdf_path, parser_version=self.version, pages=pages)


LAYOUT_PARSER_BACKENDS: dict[str, type[LayoutParserBackend]] = {
    PyMuPDFLayoutParserBackend.name: PyMuPDFLayoutParserBackend,
}


def available_layout_parser_backends() -> tuple[str, ...]:
    return tuple(sorted(LAYOUT_PARSER_BACKENDS))


def resolve_layout_parser_backend(
    backend: str | LayoutParserBackend | None,
) -> LayoutParserBackend:
    if backend is None:
        backend = DEFAULT_LAYOUT_PARSER_BACKEND
    if isinstance(backend, str):
        try:
            return LAYOUT_PARSER_BACKENDS[backend]()
        except KeyError as exc:
            valid = ", ".join(available_layout_parser_backends())
            raise ValueError(f"Unknown layout parser backend {backend!r}. Valid: {valid}") from exc
    return backend


def _parse_pymupdf_page(page: fitz.Page, page_number: int) -> RawPageModel:
    text_dict = page.get_text("dict", sort=True)
    spans: list[TextSpan] = []
    lines: list[TextLine] = []
    blocks: list[TextBlock] = []
    choice_candidates: list[ChoiceGlyphCandidate] = []

    for block_index, block in enumerate(text_dict.get("blocks", []), start=1):
        line_ids: list[str] = []
        block_line_texts: list[str] = []
        for line_index, line in enumerate(block.get("lines", []), start=1):
            span_ids: list[str] = []
            line_text_parts: list[str] = []
            for span_index, span in enumerate(line.get("spans", []), start=1):
                text = span.get("text", "")
                if not text:
                    continue
                span_id = f"p{page_number}_s{len(spans) + 1:04d}"
                font = span.get("font", "")
                flags = int(span.get("flags", 0))
                text_span = TextSpan(
                    id=span_id,
                    text=text,
                    bbox=BoundingBox.from_rect(span["bbox"]),
                    font=font,
                    size=float(span.get("size", 0.0)),
                    flags=flags,
                    color=span.get("color"),
                    is_bold="bold" in font.lower() or bool(flags & 16),
                    is_italic="italic" in font.lower() or "oblique" in font.lower() or bool(flags & 2),
                )
                spans.append(text_span)
                span_ids.append(span_id)
                line_text_parts.append(text)
                choice_candidates.extend(_glyph_candidates_from_span(page_number, text_span))

            if not span_ids:
                continue
            line_id = f"p{page_number}_l{len(lines) + 1:04d}"
            line_text = "".join(line_text_parts).strip()
            lines.append(
                TextLine(
                    id=line_id,
                    text=line_text,
                    bbox=BoundingBox.from_rect(line["bbox"]),
                    span_ids=span_ids,
                )
            )
            line_ids.append(line_id)
            if line_text:
                block_line_texts.append(line_text)

        block_id = f"p{page_number}_b{block_index:04d}"
        block_type = "text" if block.get("type") == 0 else "image"
        blocks.append(
            TextBlock(
                id=block_id,
                text="\n".join(block_line_texts),
                bbox=BoundingBox.from_rect(block["bbox"]),
                line_ids=line_ids,
                block_type=block_type,
            )
        )

    widgets = _widgets_from_page(page, page_number)
    choice_candidates.extend(_choice_candidates_from_drawings(page, page_number, lines))
    tables = _table_candidates_from_page(page, page_number)

    rect = page.rect
    return RawPageModel(
        page_number=page_number,
        width=rect.width,
        height=rect.height,
        parser_version=PYMUPDF_PARSER_VERSION,
        spans=spans,
        lines=lines,
        blocks=blocks,
        widgets=widgets,
        choice_glyph_candidates=choice_candidates,
        table_candidates=tables,
    )


def _glyph_candidates_from_span(
    page_number: int, span: TextSpan
) -> list[ChoiceGlyphCandidate]:
    candidates: list[ChoiceGlyphCandidate] = []
    for char in span.text:
        if char not in CHECKBOX_RADIO_GLYPHS:
            continue
        candidates.append(
            ChoiceGlyphCandidate(
                id=f"{span.id}_glyph_{len(candidates) + 1}",
                glyph=char,
                bbox=span.bbox,
                source="text_glyph",
                nearest_text=span.text.strip() or None,
                confidence=0.8,
            )
        )
    return candidates


def _widgets_from_page(page: fitz.Page, page_number: int) -> list[FormWidget]:
    widgets: list[FormWidget] = []
    for widget_index, widget in enumerate(page.widgets() or [], start=1):
        widgets.append(
            FormWidget(
                id=f"p{page_number}_w{widget_index:04d}",
                field_name=widget.field_name,
                field_label=widget.field_label,
                field_type=_widget_type_name(widget.field_type),
                value=None if widget.field_value is None else str(widget.field_value),
                bbox=BoundingBox.from_rect(widget.rect),
            )
        )
    return widgets


def _widget_type_name(field_type: int) -> str:
    return {
        fitz.PDF_WIDGET_TYPE_BUTTON: "button",
        fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
        fitz.PDF_WIDGET_TYPE_COMBOBOX: "combobox",
        fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
        fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
        fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
        fitz.PDF_WIDGET_TYPE_TEXT: "text",
    }.get(field_type, str(field_type))


def _choice_candidates_from_drawings(
    page: fitz.Page, page_number: int, lines: list[TextLine]
) -> list[ChoiceGlyphCandidate]:
    candidates: list[ChoiceGlyphCandidate] = []
    for drawing_index, drawing in enumerate(page.get_drawings(), start=1):
        bbox = BoundingBox.from_rect(drawing["rect"])
        if bbox.width < 5 or bbox.height < 5 or bbox.width > 28 or bbox.height > 28:
            continue
        ratio = bbox.width / bbox.height if bbox.height else 0
        if ratio < 0.65 or ratio > 1.35:
            continue
        candidates.append(
            ChoiceGlyphCandidate(
                id=f"p{page_number}_d{drawing_index:04d}",
                glyph="box",
                bbox=bbox,
                source="drawing_shape",
                nearest_text=_nearest_line_text(bbox, lines),
                confidence=0.55,
            )
        )
    return candidates


def _nearest_line_text(bbox: BoundingBox, lines: list[TextLine]) -> str | None:
    right_side = [
        line
        for line in lines
        if abs(_mid_y(line.bbox) - _mid_y(bbox)) <= max(8, bbox.height)
        and line.bbox.x0 >= bbox.x0
    ]
    if not right_side:
        return None
    nearest = min(right_side, key=lambda line: abs(line.bbox.x0 - bbox.x1))
    return nearest.text or None


def _mid_y(bbox: BoundingBox) -> float:
    return (bbox.y0 + bbox.y1) / 2


def _table_candidates_from_page(page: fitz.Page, page_number: int) -> list[TableCandidate]:
    if not hasattr(page, "find_tables"):
        return []

    candidates: list[TableCandidate] = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            table_finder = page.find_tables()
    except Exception:
        return []

    for table_index, table in enumerate(getattr(table_finder, "tables", []), start=1):
        rows = getattr(table, "rows", []) or []
        columns = getattr(table, "columns", []) or []
        header_text: list[str] = []
        try:
            extracted = table.extract()
            if extracted:
                header_text = [str(cell).strip() for cell in extracted[0] if cell]
        except Exception:
            header_text = []

        candidates.append(
            TableCandidate(
                id=f"p{page_number}_t{table_index:04d}",
                bbox=BoundingBox.from_rect(table.bbox),
                row_count=len(rows) or None,
                column_count=len(columns) or (len(header_text) or None),
                source="pymupdf_find_tables",
                header_text=header_text,
            )
        )
    return candidates
