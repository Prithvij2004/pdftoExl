from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.config import PipelineConfig
from app.services.pipeline import CurrentExtractionPipeline


def _config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=None,
        bedrock_vision_model_id="vision",
        bedrock_text_model_id="text",
        page_render_dpi=72,
        complexity_uncertain_threshold=0.15,
        max_chunk_input_tokens=3500,
        max_concurrent_bedrock_calls=4,
        extraction_cache_dir=tmp_path / "cache",
    )


def test_current_pipeline_runs_preparation_parser_and_semantic_hints(tmp_path: Path) -> None:
    result = CurrentExtractionPipeline(_config(tmp_path)).run(Path("docs/sample-input-1.pdf"))

    assert result.prepared_pdf.pages
    assert result.raw_pdf.pages
    assert len(result.semantic_pages) == len(result.raw_pdf.pages)
    assert len(result.semantic_pages) == len(result.prepared_pdf.pages)
    assert len(result.complexity_routes) == len(result.raw_pdf.pages)

    payload = result.to_dict()
    decoded = json.loads(json.dumps(payload))

    assert decoded["prepared_pdf"]["pages"][0]["image_path"].endswith(".png")
    assert decoded["raw_pdf"]["pages"][0]["blocks"]
    assert decoded["semantic_pages"][0]["hints"]
    assert decoded["complexity_routes"][0]["recommended_path"] in {"parser", "vlm"}
    assert "bbox" not in decoded["semantic_pages"][0]["hints"][0]

    parser_pages = {artifact["page_number"] for artifact in decoded["parser_artifacts"]}
    parser_routed_pages = {
        route["page"]
        for route in decoded["complexity_routes"]
        if route["recommended_path"] == "parser"
    }
    assert parser_pages == parser_routed_pages
    if decoded["parser_artifacts"]:
        first_artifact = decoded["parser_artifacts"][0]
        assert first_artifact["markdown"].startswith("# Page ")
        assert first_artifact["chunk_seeds"]


def test_current_pipeline_writes_prompt_text(tmp_path: Path) -> None:
    result = CurrentExtractionPipeline(_config(tmp_path)).run(Path("docs/sample-input-1.pdf"))

    prompt_text = result.to_semantic_prompt_text()

    assert "# Page 1" in prompt_text
    assert "role=" in prompt_text
    assert "[" in prompt_text


def test_run_pipeline_cli_writes_testable_outputs(tmp_path: Path) -> None:
    output = tmp_path / "pipeline.json"
    semantic_output = tmp_path / "semantic.txt"
    cache_dir = tmp_path / "cache"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.run_pipeline",
            "docs/sample-input-1.pdf",
            "--output",
            str(output),
            "--semantic-output",
            str(semantic_output),
            "--pretty",
        ],
        check=True,
        env={"EXTRACTION_CACHE_DIR": str(cache_dir), "PAGE_RENDER_DPI": "72"},
        text=True,
        capture_output=True,
    )

    assert completed.stdout == ""
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["prepared_pdf"]["pages"]
    assert payload["semantic_pages"][0]["hints"]
    assert payload["complexity_routes"][0]["complexity"] in {
        "simple",
        "medium",
        "complex",
    }
    assert semantic_output.read_text(encoding="utf-8").startswith("# Page 1")
