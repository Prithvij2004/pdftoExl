"""PipelineV1: stitches stages together. Satisfies the `Pipeline` protocol."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..adapters.bedrock import BedrockSettings, LLMClient, build_bedrock_client
from ..adapters.logging import get_logger
from .config import PipelineConfig
from .stages import classification, extraction, gate, llm, mapping, merge, output

log = get_logger("pipeline_v1")


@dataclass
class PipelineContext:
    """Per-run inputs the stages need beyond the PDF itself.

    The golden workbook stands in as the template until the mapping stage
    is implemented; any caller can override with a neutral template.
    """

    out_path: Path
    template_xlsx: Path
    question_sheet: str
    header_row: int


class PipelineV1:
    """Concrete `Pipeline` implementation. Currently a no-op end-to-end."""

    def __init__(
        self,
        config: PipelineConfig,
        context: PipelineContext,
        *,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.config = config
        self.context = context
        self._llm_client = llm_client

    def __call__(self, pdf_path: Path) -> Path:
        log.info("run.start", pdf=str(pdf_path), out=str(self.context.out_path))
        cfg = self.config

        raw = extraction.run(pdf_path) if cfg.stages.extraction else extraction.RawBlocks(pdf_path, [])
        classified = (
            classification.run(raw)
            if cfg.stages.classification
            else classification.ClassifiedBlocks(raw.blocks)
        )
        gated = (
            gate.run(classified, confidence_threshold=cfg.thresholds.gate_confidence)
            if cfg.stages.gate
            else gate.GateOutput(accepted=classified.blocks)
        )
        llm_out = (
            llm.run(gated, self._llm_client)
            if cfg.stages.llm
            else llm.LLMOutput(enriched=gated.deferred)
        )
        merged = (
            merge.run(gated, llm_out, min_confidence=cfg.thresholds.min_block_confidence)
            if cfg.stages.merge
            else merge.MergedBlocks(blocks=[*gated.accepted, *llm_out.enriched])
        )
        mapped = mapping.run(merged) if cfg.stages.mapping else mapping.MappedRows()
        out = output.run(
            mapped,
            out_path=self.context.out_path,
            template_xlsx=self.context.template_xlsx,
            question_sheet=self.context.question_sheet,
            header_row=self.context.header_row,
        )
        log.info("run.done", out=str(out))
        return out


def build_pipeline(
    config: PipelineConfig,
    context: PipelineContext,
    *,
    with_llm: bool = False,
) -> PipelineV1:
    """Construct a PipelineV1. Only instantiates the Bedrock client if asked,
    so tests and offline runs don't need AWS credentials."""
    client: LLMClient | None = None
    if with_llm and config.stages.llm:
        client = build_bedrock_client(
            BedrockSettings(
                model_id=config.bedrock.model_id,
                region=config.bedrock.region,
                max_tokens=config.bedrock.max_tokens,
                temperature=config.bedrock.temperature,
                timeout_seconds=config.bedrock.timeout_seconds,
            )
        )
    return PipelineV1(config, context, llm_client=client)
