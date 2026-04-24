"""Tests for pdftoxl.pipeline_v1.config — defaults, YAML loading, env overrides."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pdftoxl.pipeline_v1.config import (
    BedrockConfig,
    PipelineConfig,
    StageToggles,
    Thresholds,
    load_config,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PDFTOXL_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_populate_nested_models():
    cfg = PipelineConfig()
    assert cfg.pipeline_version == "v1"
    assert isinstance(cfg.bedrock, BedrockConfig)
    assert isinstance(cfg.stages, StageToggles)
    assert isinstance(cfg.thresholds, Thresholds)
    assert cfg.stages.extraction is True
    assert cfg.stages.llm is True
    assert cfg.thresholds.gate_confidence == 0.75
    assert cfg.thresholds.min_block_confidence == 0.05
    assert cfg.bedrock.temperature == 0.0


def test_load_config_reads_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "pipeline_version: v1\n"
        "bedrock:\n"
        "  model_id: custom.model-1\n"
        "  region: us-west-2\n"
        "stages:\n"
        "  llm: false\n"
        "thresholds:\n"
        "  gate_confidence: 0.5\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.bedrock.model_id == "custom.model-1"
    assert cfg.bedrock.region == "us-west-2"
    assert cfg.stages.llm is False
    assert cfg.stages.extraction is True  # untouched default
    assert cfg.thresholds.gate_confidence == 0.5


def test_load_config_missing_path_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./config.yaml
    cfg = load_config(None)
    assert cfg.bedrock.model_id.startswith("anthropic.")
    assert cfg.stages.extraction is True


def test_load_config_respects_env_var_pointer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "alt.yaml"
    cfg_path.write_text("bedrock:\n  region: eu-west-1\n")
    monkeypatch.setenv("PDFTOXL_CONFIG", str(cfg_path))
    cfg = load_config(None)
    assert cfg.bedrock.region == "eu-west-1"


def test_env_nested_override_applies_when_field_absent_from_yaml(tmp_path, monkeypatch):
    """Env vars populate fields that the YAML file doesn't mention.

    Note: when a field is explicitly set in the YAML, the explicit kwarg beats
    the env-var source in pydantic-settings' current precedence — documented
    here so future refactors notice if that changes.
    """
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    # YAML sets model_id only; region is left to env/default.
    cfg_path.write_text("bedrock:\n  model_id: yaml-model\n")
    monkeypatch.setenv("PDFTOXL_BEDROCK__REGION", "ap-south-1")
    cfg = load_config(cfg_path)
    assert cfg.bedrock.model_id == "yaml-model"
    assert cfg.bedrock.region == "ap-south-1"


def test_empty_yaml_falls_back_to_defaults(tmp_path):
    cfg_path = tmp_path / "empty.yaml"
    cfg_path.write_text("")
    cfg = load_config(cfg_path)
    assert cfg.pipeline_version == "v1"


def test_paths_default_points_to_fixtures_yaml():
    cfg = PipelineConfig()
    assert cfg.paths.fixtures_yaml == Path("evals/fixtures.yaml")
