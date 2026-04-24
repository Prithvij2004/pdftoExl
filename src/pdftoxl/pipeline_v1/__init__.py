"""v1 of the PDF → EAB-Excel pipeline.

Stages (see sibling modules): extraction → classification → gate → llm →
merge → mapping → output. Each stage is a pure function consuming the
previous stage's artifact so they can be developed and tested in isolation.
"""
from .config import PipelineConfig, load_config
from .pipeline import PipelineV1, build_pipeline

__all__ = ["PipelineV1", "PipelineConfig", "build_pipeline", "load_config"]
