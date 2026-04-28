from __future__ import annotations

from typing import Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider

from app.config import settings
from app.models import ExtractedRow
from app.services.extractor import _ensure_inference_profile_id


_DEFAULT_MAX_TOKENS = 8000


class RowDecision(BaseModel):
    page_number: int = Field(ge=1)
    source_order: int = Field(ge=0)
    action: Literal["keep", "drop"]
    reason: str = Field(default="", description="Brief justification (<=80 chars).")


class SemanticDecisions(BaseModel):
    decisions: list[RowDecision]


def _bedrock_semantic_agent() -> Agent:
    cfg = Config(connect_timeout=3600, read_timeout=3600, retries={"max_attempts": 1})
    bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)
    provider = BedrockProvider(bedrock_client=bedrock_client)
    effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)
    model = BedrockConverseModel(model_name=effective_model_id, provider=provider)

    return Agent(
        model=model,
        output_type=SemanticDecisions,
        retries=2,
        system_prompt=(
            "You perform a SEMANTIC PASS over rows already extracted from a PDF form.\n"
            "PURPOSE OF THE OUTPUT: the rows are consumed as a CONFIG SCHEMA for an external system. "
            "Each row defines one field that the downstream system will render to collect answers. "
            "Each distinct field must therefore appear EXACTLY ONCE in the final config, even if the source PDF "
            "visually repeats the same field block multiple times to capture multiple answers. The downstream "
            "system handles multiplicity at runtime; the config describes the field set canonically.\n"
            "\n"
            "YOUR JOB: review the full ordered list of rows across ALL pages, then for every row decide keep or drop.\n"
            "\n"
            "DROP a row when it is a SEMANTIC DUPLICATE of an already-kept row:\n"
            "- Repeated field groups: the PDF lists the same questions under 'Fall #1', 'Fall #2', 'Fall #3' (or "
            "'Medication 1/2/3', 'Hospitalization 1/2/3', 'Child 1/2/3', 'Witness 1/2/3', etc.). Keep the FIRST "
            "occurrence of each distinct question and drop every subsequent visual repetition that asks the same "
            "thing for the next instance.\n"
            "- Repeated section headers / instructions that appear once per page or once per repeated block.\n"
            "- A row that is a continuation of the immediately previous row split across a page break and adds no "
            "new field.\n"
            "- Visually duplicated signature/date blocks that ask the same thing twice (keep one).\n"
            "\n"
            "KEEP a row when it represents a distinct field, even if the wording is similar to another row:\n"
            "- 'Applicant signature' and 'Witness signature' are DIFFERENT fields — keep both.\n"
            "- 'Date of birth' and 'Date of fall' are DIFFERENT fields — keep both.\n"
            "- A field that looks repeated but actually targets a different entity (applicant vs spouse vs child) is NOT a duplicate.\n"
            "- When in doubt, KEEP. Losing real fields is worse than keeping a small duplicate.\n"
            "\n"
            "Identify each row by its (page_number, source_order). Return one decision per input row. "
            "Every (page_number, source_order) pair you receive MUST appear in the decisions list exactly once."
        ),
        model_settings={"temperature": 0, "max_tokens": _DEFAULT_MAX_TOKENS},
    )


def _format_rows(rows: list[ExtractedRow]) -> str:
    lines: list[str] = []
    for r in rows:
        q = (r.question_text or "").replace("\n", " ").strip()
        a = (r.answer_text or "").replace("\n", " ").strip()
        lines.append(
            f"- page={r.page_number} source_order={r.source_order} type={r.question_type.value}\n"
            f"  question_text: {q}\n"
            f"  answer_text: {a}"
        )
    return "\n".join(lines)


async def llm_semantic_pass(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """Global semantic pass: drop rows that are semantic duplicates of earlier rows.

    The output is consumed as a config schema by an external system, so each distinct
    field must appear exactly once even when the PDF visually repeats field blocks.
    """
    if not rows:
        return rows

    agent = _bedrock_semantic_agent()
    prompt = (
        "Review the full ordered list of extracted rows below and return a keep/drop decision for "
        "EVERY row. Drop semantic duplicates produced by visually-repeated field blocks; keep the "
        "first occurrence of each distinct field.\n\n"
        f"{_format_rows(rows)}"
    )

    try:
        result = (await agent.run(prompt)).output
    except NoCredentialsError as e:
        raise RuntimeError(
            "Bedrock invoke failed: AWS credentials not found for semantic pass."
        ) from e
    except (ClientError, BotoCoreError):
        # Best-effort: if Bedrock fails, fall back to the un-deduped output.
        return rows

    drop_set: set[tuple[int, int]] = {
        (d.page_number, d.source_order) for d in result.decisions if d.action == "drop"
    }
    if not drop_set:
        return rows

    return [r for r in rows if (r.page_number, r.source_order) not in drop_set]
