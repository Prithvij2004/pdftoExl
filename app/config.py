from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PipelineConfig(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    bedrock_region: str | None = Field(description="AWS region hosting Bedrock; when None, Bedrock-dependent stages are skipped.")
    bedrock_vision_model_id: str = Field(description="Bedrock model ID used by the VLM page extractor and complexity classifier.")
    bedrock_text_model_id: str = Field(description="Bedrock model ID used for chunk extraction and semantic merging.")
    page_render_dpi: int = Field(description="DPI used when rasterising PDF pages for VLM stages.")
    complexity_uncertain_threshold: float = Field(description="Score above which the deterministic complexity router escalates a page to the VLM classifier.")
    max_chunk_input_tokens: int = Field(description="Approximate token budget per semantic chunk fed to the chunk extractor.")
    max_concurrent_bedrock_calls: int = Field(description="Maximum concurrent Bedrock invocations for chunk extraction.")
    extraction_cache_dir: Path = Field(description="Directory holding rendered page images and other intermediate artefacts.")


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float, got {raw!r}") from exc


def load_pipeline_config() -> PipelineConfig:
    return PipelineConfig(
        bedrock_region=(
            os.environ.get("BEDROCK_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        ),
        bedrock_vision_model_id=os.environ.get(
            "BEDROCK_VISION_MODEL_ID",
            "qwen.qwen3-vl-235b-a22b",
        ),
        bedrock_text_model_id=os.environ.get(
            "BEDROCK_TEXT_MODEL_ID",
            "qwen.qwen3-235b-a22b-2507-v1:0",
        ),
        page_render_dpi=_get_int("PAGE_RENDER_DPI", 220),
        complexity_uncertain_threshold=_get_float(
            "COMPLEXITY_UNCERTAIN_THRESHOLD",
            0.15,
        ),
        max_chunk_input_tokens=_get_int("MAX_CHUNK_INPUT_TOKENS", 3500),
        max_concurrent_bedrock_calls=_get_int("MAX_CONCURRENT_BEDROCK_CALLS", 4),
        extraction_cache_dir=Path(
            os.environ.get("EXTRACTION_CACHE_DIR", "runtime/cache")
        ),
    )
