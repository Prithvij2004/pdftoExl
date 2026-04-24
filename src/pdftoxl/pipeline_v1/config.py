"""Pipeline configuration model and loader.

Precedence (highest first): explicit CLI `--config` file → PDFTOXL_CONFIG env
→ `./config.yaml` → built-in defaults. Environment variables prefixed with
`PDFTOXL_` override any nested field (use `__` as the delimiter, e.g.
`PDFTOXL_BEDROCK__MODEL_ID`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BedrockConfig(BaseModel):
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    region: str = "us-east-1"
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout_seconds: int = 60


class StageToggles(BaseModel):
    extraction: bool = True
    classification: bool = True
    gate: bool = True
    llm: bool = True
    merge: bool = True
    mapping: bool = True
    output: bool = True


class Thresholds(BaseModel):
    gate_confidence: float = 0.75
    min_block_confidence: float = 0.05


class Paths(BaseModel):
    fixtures_yaml: Path = Path("evals/fixtures.yaml")
    default_template_xlsx: Path | None = None


class LoggingConfig(BaseModel):
    level: str = "INFO"
    renderer: str = "console"


class PipelineConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDFTOXL_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    pipeline_version: str = "v1"
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)
    stages: StageToggles = Field(default_factory=StageToggles)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    paths: Paths = Field(default_factory=Paths)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def _default_config_path() -> Path | None:
    env = os.environ.get("PDFTOXL_CONFIG")
    if env:
        return Path(env)
    local = Path("config.yaml")
    return local if local.exists() else None


def load_config(path: Path | None = None) -> PipelineConfig:
    cfg_path = path or _default_config_path()
    data: dict[str, Any] = {}
    if cfg_path and Path(cfg_path).exists():
        with open(cfg_path) as fh:
            data = yaml.safe_load(fh) or {}
    # Env vars still override file values (BaseSettings will merge).
    return PipelineConfig(**data)
