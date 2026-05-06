from __future__ import annotations

import time
from pathlib import Path

from app.config import PipelineConfig
from app.services.page_preparation import PagePreparer, render_cache_key


def test_prepare_pages_renders_images_and_metadata(tmp_path: Path) -> None:
    config = PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=72,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=tmp_path / "cache",
    )

    prepared = PagePreparer(config).prepare(Path("docs/sample-input-1.pdf"))

    assert len(prepared.pdf_hash) == 64
    assert prepared.render_dpi == 72
    assert prepared.pages
    assert [page.page_number for page in prepared.pages] == list(
        range(1, len(prepared.pages) + 1)
    )
    for page in prepared.pages:
        assert page.width > 0
        assert page.height > 0
        assert len(page.page_hash) == 64
        assert page.image_format == "png"
        assert page.image_path.exists()
        assert page.image_path.stat().st_size > 0
        assert page.cache_key == render_cache_key(
            pdf_hash=prepared.pdf_hash,
            page_number=page.page_number,
            render_dpi=72,
            renderer_version=prepared.renderer_version,
        )


def test_prepare_pages_reuses_render_cache(tmp_path: Path) -> None:
    config = PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=72,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=tmp_path / "cache",
    )
    preparer = PagePreparer(config)

    first = preparer.prepare(Path("docs/sample-input-1.pdf"))
    mtimes = {page.page_number: page.image_path.stat().st_mtime_ns for page in first.pages}
    time.sleep(0.01)
    second = preparer.prepare(Path("docs/sample-input-1.pdf"))

    assert [page.image_path for page in second.pages] == [
        page.image_path for page in first.pages
    ]
    assert {
        page.page_number: page.image_path.stat().st_mtime_ns for page in second.pages
    } == mtimes
