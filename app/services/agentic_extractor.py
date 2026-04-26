from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, Field
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider

from app.config import settings
from app.models import ExtractedRow, ExtractionResult, QuestionType
from app.services.extractor import _ensure_inference_profile_id
from app.services.pdf_batches import PdfBatch, iter_pdf_page_batches


class FieldCandidate(BaseModel):
    question_type: QuestionType
    question_text: str = Field(min_length=1)
    answer_text: str = ""
    page_number: int = Field(ge=1)
    source_order: int = Field(ge=0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: str = Field(default="", description="Brief justification for classification and text choices.")


class BatchAnalysis(BaseModel):
    candidates: list[FieldCandidate]


@dataclass(frozen=True)
class _AgentBundle:
    analysis_agent: Agent
    finalize_agent: Agent


def _bedrock_agent_bundle() -> _AgentBundle:
    cfg = Config(
        connect_timeout=3600,
        read_timeout=3600,
        retries={"max_attempts": 1},
    )
    bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)
    provider = BedrockProvider(bedrock_client=bedrock_client)

    effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)
    model = BedrockConverseModel(model_name=effective_model_id, provider=provider)

    analysis_agent = Agent(
        model=model,
        output_type=BatchAnalysis,
        retries=2,
        system_prompt=(
            "You are an expert at reading PDF forms and converting them into structured form-field rows.\n"
            "Analyze the document and identify ALL user-fillable fields and important static instructions.\n"
            "Classify fields by semantic intent, not by visual appearance alone. A blank box may be a Date, Number, Signature, Text Area, or another field type depending on the label and surrounding instructions.\n"
            "Translate non-English to English.\n"
            "Return data in the required structured output schema."
        ),
        model_settings={"temperature": 0.2, "top_p": 0.1, "max_tokens": 4000},
    )

    finalize_agent = Agent(
        model=model,
        output_type=ExtractionResult,
        retries=2,
        system_prompt=(
            "You are producing the FINAL extraction rows that will be written to Excel.\n"
            "Output must conform exactly to the schema.\n"
            "Do not include reasoning text in the final fields; reasoning may be added only in row.meta."
        ),
        model_settings={"temperature": 0.1, "top_p": 0.1, "max_tokens": 6000},
    )

    return _AgentBundle(analysis_agent=analysis_agent, finalize_agent=finalize_agent)


def _analysis_prompt(batch: PdfBatch) -> list[object]:
    return [
        (
            "Analyze this PDF batch and identify every form element and important display/instructional text.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "Rules:\n"
            "- One candidate per input element or meaningful static instruction.\n"
            "- question_type must be one of: "
            + ", ".join([qt.value for qt in QuestionType])
            + ".\n"
            "- Do not default blank-looking fields to Text Box. First infer the best type from the label, expected answer, nearby instructions, options, and field constraints.\n"
            "- Use Date when the label asks for a date, DOB, effective date, signature date, review date, or any date-formatted value even if the field looks like a plain blank.\n"
            "- Use Number for numeric-only values such as age, quantity, amount, count, percentage, or score. For phone numbers, IDs, and case numbers, use Text Box unless the form clearly restricts the value to numeric input only.\n"
            "- Use Signature when the label asks for a signature, initials, signer name in a signature block, or signature capture area.\n"
            "- Use Text Area for comments, explanation, notes, narrative, description, address blocks, or any answer that expects multiple words/lines.\n"
            "- Use Radio Button or Checkbox Group for option sets where the user selects one or many options; put options in answer_text separated by pipes.\n"
            "- Use Checkbox for a single standalone checkbox statement.\n"
            "- Use Dropdown only when the field is clearly a select/list choice rather than visible radio/checkbox options.\n"
            "- Use Group Table for repeated row/column data-entry regions and Group for section headers/containers.\n"
            "- question_text should be the label/question/instruction in English.\n"
            "- answer_text describes what the user is expected to provide; options should be pipe-separated.\n"
            "- For Display rows with a short heading/title and a larger paragraph or description, use the heading/title as question_text and the larger paragraph/description as answer_text. If there is no separate body text, keep answer_text empty.\n"
            "- page_number must be the absolute page number in the original PDF.\n"
            "- source_order starts at 0 for each page and increases top-to-bottom.\n"
            "- rationale must be brief and specific, and should mention the semantic clue used to choose the field type.\n"
        ),
        BinaryContent(data=batch.pdf_bytes, media_type="application/pdf"),
    ]


def _finalize_prompt(batch: PdfBatch, analysis: BatchAnalysis) -> list[object]:
    payload = analysis.model_dump(mode="json")
    return [
        (
            "Convert the analyzed candidates into the FINAL rows for Excel.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "Constraints:\n"
            "- Output ONLY rows (no extra commentary).\n"
            "- question_type must be exactly one of the allowed values.\n"
            "- Preserve the best semantic type from the analysis. Do not change fields to Text Box just because they look like blanks.\n"
            "- Re-check Date, Number, Signature, Text Area, option groups, tables, and section/display rows using the PDF context before finalizing.\n"
            "- question_text must be non-empty English.\n"
            "- For Display rows, preserve a heading-plus-description split when present: heading/title in question_text, larger paragraph/description in answer_text.\n"
            "- Keep ordering stable by (page_number, source_order).\n"
            "- Store rationale in row.meta.rationale if helpful.\n\n"
            "Analyzed candidates JSON:\n"
            + json.dumps(payload, ensure_ascii=False)
        ),
        BinaryContent(data=batch.pdf_bytes, media_type="application/pdf"),
    ]


def _candidates_to_rows(analysis: BatchAnalysis) -> list[ExtractedRow]:
    rows: list[ExtractedRow] = []
    for c in analysis.candidates:
        rows.append(
            ExtractedRow(
                question_type=c.question_type,
                question_text=c.question_text,
                answer_text=c.answer_text,
                page_number=c.page_number,
                source_order=c.source_order,
                confidence=c.confidence,
                meta={"rationale": c.rationale} if c.rationale else {},
            )
        )
    return rows


async def extract_rows_from_pdf_agentic(pdf_path: Path) -> list[ExtractedRow]:
    bundle = _bedrock_agent_bundle()
    all_rows: list[ExtractedRow] = []

    for batch in iter_pdf_page_batches(pdf_path, settings.pdf_batch_size):
        try:
            analysis = (await bundle.analysis_agent.run(_analysis_prompt(batch))).output
        except NoCredentialsError as e:
            raise RuntimeError(
                "Bedrock invoke failed: AWS credentials not found. "
                "Configure credentials (e.g., `aws configure` or env vars) and ensure Bedrock access."
            ) from e
        except (ClientError, BotoCoreError) as e:
            raise RuntimeError(
                "Bedrock invoke failed during analysis. If you see an on-demand throughput error, "
                "set BEDROCK_MODEL_ID to an inference profile ID like "
                "`us.amazon.nova-pro-v1:0` (or `eu.amazon.nova-pro-v1:0`). "
                f"Underlying error: {e}"
            ) from e

        try:
            final_rows = (await bundle.finalize_agent.run(_finalize_prompt(batch, analysis))).output.rows
        except NoCredentialsError as e:
            raise RuntimeError(
                "Bedrock invoke failed: AWS credentials not found. "
                "Configure credentials (e.g., `aws configure` or env vars) and ensure Bedrock access."
            ) from e
        except (ClientError, BotoCoreError) as e:
            raise RuntimeError(
                "Bedrock invoke failed during finalize. If you see an on-demand throughput error, "
                "set BEDROCK_MODEL_ID to an inference profile ID like "
                "`us.amazon.nova-pro-v1:0` (or `eu.amazon.nova-pro-v1:0`). "
                f"Underlying error: {e}"
            ) from e
        except Exception:
            final_rows = _candidates_to_rows(analysis)

        all_rows.extend(final_rows)

    return all_rows

