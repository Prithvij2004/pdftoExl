from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import fitz
from pydantic import BaseModel, ConfigDict, Field

from app.config import PipelineConfig


RENDERER_VERSION = f"pymupdf-{fitz.VersionBind}-png-v1"


class PreparedPage(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    page_number: int = Field(description="1-based page number within the source PDF.")
    width: float = Field(description="Page width in user-space units.")
    height: float = Field(description="Page height in user-space units.")
    image_path: Path = Field(description="Filesystem path to the rendered page image.")
    page_hash: str = Field(description="Stable hash of the page's textual and content-stream contents.")
    image_format: str = Field(description="Encoding of the rendered image (currently always 'png').")
    render_dpi: int = Field(description="DPI used when rasterising the page.")
    cache_key: str = Field(description="Deterministic cache key for the rendered image.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "image_path": str(self.image_path),
            "page_hash": self.page_hash,
            "image_format": self.image_format,
            "render_dpi": self.render_dpi,
            "cache_key": self.cache_key,
        }


class PreparedPdf(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pdf_path: Path = Field(description="Filesystem path to the source PDF.")
    pdf_hash: str = Field(description="SHA-256 hash of the source PDF bytes.")
    render_dpi: int = Field(description="DPI used to render every page in this PDF.")
    renderer_version: str = Field(description="Identifier of the renderer that produced the page images.")
    pages: list[PreparedPage] = Field(description="Rendered page artefacts in document order.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": str(self.pdf_path),
            "pdf_hash": self.pdf_hash,
            "render_dpi": self.render_dpi,
            "renderer_version": self.renderer_version,
            "pages": [page.to_dict() for page in self.pages],
        }


class PagePreparer:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def prepare(self, pdf_path: Path | str) -> PreparedPdf:
        source = Path(pdf_path)
        if not source.exists():
            raise FileNotFoundError(f"PDF not found: {source}")
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a PDF file, got: {source}")

        pdf_hash = hash_file(source)
        pages: list[PreparedPage] = []
        self.config.extraction_cache_dir.mkdir(parents=True, exist_ok=True)

        with fitz.open(source) as document:
            for index, page in enumerate(document, start=1):
                page_hash = hash_page(page)
                cache_key = render_cache_key(
                    pdf_hash=pdf_hash,
                    page_number=index,
                    render_dpi=self.config.page_render_dpi,
                    renderer_version=RENDERER_VERSION,
                )
                image_path = (
                    self.config.extraction_cache_dir
                    / pdf_hash[:16]
                    / f"{cache_key}.png"
                )
                image_path.parent.mkdir(parents=True, exist_ok=True)

                if not image_path.exists():
                    render_page(page, image_path, self.config.page_render_dpi)

                rect = page.rect
                pages.append(
                    PreparedPage(
                        page_number=index,
                        width=rect.width,
                        height=rect.height,
                        image_path=image_path,
                        page_hash=page_hash,
                        image_format="png",
                        render_dpi=self.config.page_render_dpi,
                        cache_key=cache_key,
                    )
                )

        return PreparedPdf(
            pdf_path=source,
            pdf_hash=pdf_hash,
            render_dpi=self.config.page_render_dpi,
            renderer_version=RENDERER_VERSION,
            pages=pages,
        )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_page(page: fitz.Page) -> str:
    digest = hashlib.sha256()
    digest.update(page.get_text("text").encode("utf-8", errors="replace"))
    digest.update(str(page.rect).encode("ascii"))
    for xref in page.get_contents() or []:
        digest.update(page.parent.xref_stream(xref) or b"")
    return digest.hexdigest()


def render_cache_key(
    *,
    pdf_hash: str,
    page_number: int,
    render_dpi: int,
    renderer_version: str,
) -> str:
    raw = f"{pdf_hash}:page={page_number}:dpi={render_dpi}:renderer={renderer_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_page(page: fitz.Page, image_path: Path, dpi: int) -> None:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    pixmap.save(image_path)
