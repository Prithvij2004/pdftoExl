from __future__ import annotations

from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider

from app.config import settings
from app.models import ExtractedRow, QuestionType
from app.services.extractor import _ensure_inference_profile_id
from app.services.pdf_batches import PdfBatch, iter_pdf_page_batches, split_batch_into_single_pages


class FieldCandidate(BaseModel):
    question_type: QuestionType
    question_text: str = Field(min_length=1)
    answer_text: str = ""
    page_number: int = Field(ge=1)
    source_order: int = Field(ge=0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: str = Field(
        default="",
        description="Brief justification for the classification (target <=60 chars; longer values are truncated downstream).",
    )

    @field_validator("rationale", mode="before")
    @classmethod
    def _truncate_rationale(cls, v: object) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s[:60]


class BatchAnalysis(BaseModel):
    candidates: list[FieldCandidate]


def _bedrock_analysis_agent() -> Agent:
    cfg = Config(
        connect_timeout=3600,
        read_timeout=3600,
        retries={"max_attempts": 1},
    )
    bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)
    provider = BedrockProvider(bedrock_client=bedrock_client)

    effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)
    model = BedrockConverseModel(model_name=effective_model_id, provider=provider)

    return Agent(
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
        model_settings={"temperature": 0.2, "max_tokens": 6000},
    )


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
            "- Treat every visually distinct paragraph as its OWN candidate row, even when adjacent paragraphs share the same surrounding heading or context. Never merge two paragraphs into a single question_text or answer_text.\n"
            "- For Display rows with a short heading/title and a larger paragraph or description, use the heading/title as question_text and the larger paragraph/description as answer_text. If there is no separate body text, keep answer_text empty. If there are multiple body paragraphs under one heading, emit one Display row per paragraph (heading may be repeated).\n"
            "- Preserve bold formatting in question_text using Markdown bold markers `**...**` around any words/phrases that are visibly bold in the PDF. Do not bold text that is not actually bold. Do not use any other markdown.\n"
            "- This batch contains exactly ONE page; set page_number to that page number for every candidate.\n"
            "- source_order starts at 0 and increases strictly top-to-bottom in reading order.\n"
            "- rationale must be at most 60 characters; name the single semantic clue used to choose the field type.\n"
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


def _is_truncated_tooluse_error(err: BaseException) -> bool:
    """Detect Nova's 424 ModelErrorException for malformed/truncated tool-use output."""
    msg = str(err)
    return (
        "ModelErrorException" in msg
        or "invalid sequence as part of ToolUse" in msg
        or "status_code: 424" in msg
    )


def _renumber_rows(rows: list[ExtractedRow], page_number: int) -> list[ExtractedRow]:
    """Override model-provided page_number/source_order with deterministic values.

    Why: the model is unreliable at assigning correct page_number across multi-page
    batches and at resetting source_order per page. Since each batch here is a single
    page, we authoritatively set page_number from the batch and renumber source_order
    from the returned reading order.
    """
    out: list[ExtractedRow] = []
    for idx, r in enumerate(rows):
        out.append(r.model_copy(update={"page_number": page_number, "source_order": idx}))
    return out


async def _process_batch(agent: Agent, batch: PdfBatch) -> list[ExtractedRow]:
    try:
        analysis = (await agent.run(_analysis_prompt(batch))).output
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

    return _candidates_to_rows(analysis)


async def extract_rows_from_pdf_agentic(pdf_path: Path) -> list[ExtractedRow]:
    agent = _bedrock_analysis_agent()
    all_rows: list[ExtractedRow] = []

    # Process one page at a time. The model is unreliable at attributing candidates
    # to the correct page within a multi-page batch and at resetting source_order
    # per page, which produced shuffled / mis-paged rows across consecutive runs.
    for batch in iter_pdf_page_batches(pdf_path, settings.pdf_batch_size):
        for sub in split_batch_into_single_pages(batch):
            rows = await _process_batch(agent, sub)
            all_rows.extend(_renumber_rows(rows, sub.start_page_number))

    return all_rows

