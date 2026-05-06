from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.layout_parser import (
    CheapLayoutParser,
    RawPageModel,
    RawPdfModel,
    available_layout_parser_backends,
)


def test_parse_layout_extracts_raw_page_model_from_sample_pdf() -> None:
    raw_model = CheapLayoutParser().parse(Path("docs/sample-input-1.pdf"))

    assert raw_model.pages
    assert raw_model.parser_version
    page = raw_model.pages[0]

    assert page.page_number == 1
    assert page.width > 0
    assert page.height > 0
    assert page.spans
    assert page.lines
    assert page.blocks
    assert page.widgets
    assert page.table_candidates

    page_text = "\n".join(line.text for line in page.lines)
    assert "Individual Service Plan" in page_text


def test_parse_layout_detects_choice_glyph_candidates() -> None:
    raw_model = CheapLayoutParser().parse(Path("docs/example-input.pdf"))

    assert sum(len(page.choice_glyph_candidates) for page in raw_model.pages) > 0


def test_parse_layout_output_is_json_serializable() -> None:
    raw_model = CheapLayoutParser().parse(Path("docs/sample-input-1.pdf"))

    payload = raw_model.to_dict()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert decoded["pages"][0]["spans"][0]["bbox"].keys() == {"x0", "y0", "x1", "y1"}
    assert isinstance(decoded["pages"][0]["widgets"][0]["bbox"]["x0"], float)


def test_parse_layout_rejects_non_pdf(tmp_path: Path) -> None:
    text_file = tmp_path / "not-a-pdf.txt"
    text_file.write_text("not a pdf")

    with pytest.raises(ValueError, match="Expected a PDF file"):
        CheapLayoutParser().parse(text_file)


def test_parse_layout_can_use_injected_backend() -> None:
    class StubBackend:
        name = "stub"
        version = "stub-v1"

        def parse(self, pdf_path: Path) -> RawPdfModel:
            return RawPdfModel(
                pdf_path=pdf_path,
                parser_version=self.version,
                pages=[
                    RawPageModel(
                        page_number=1,
                        width=100,
                        height=200,
                        parser_version=self.version,
                        spans=[],
                        lines=[],
                        blocks=[],
                        widgets=[],
                        choice_glyph_candidates=[],
                        table_candidates=[],
                    )
                ],
            )

    raw_model = CheapLayoutParser(backend=StubBackend()).parse(Path("docs/sample-input-1.pdf"))

    assert raw_model.parser_version == "stub-v1"
    assert raw_model.pages[0].width == 100


def test_parse_layout_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unknown layout parser backend"):
        CheapLayoutParser(backend="missing")


def test_layout_parser_backend_registry_lists_pymupdf() -> None:
    assert available_layout_parser_backends() == ("pymupdf",)
