from __future__ import annotations

import json
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider

from app.config import settings
from app.models import ExtractedRow
from app.services.extractor import _ensure_inference_profile_id


class SectionAssignment(BaseModel):
    sequence: int = Field(ge=1)
    section: str = ""
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: str = Field(default="")

    @field_validator("section", "rationale", mode="before")
    @classmethod
    def _clean_short_text(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()[:160]


class SectionRefinement(BaseModel):
    assignments: list[SectionAssignment]


def _bedrock_section_agent() -> Agent:
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
        output_type=SectionRefinement,
        retries=2,
        system_prompt=(
            "You refine only the Section column for rows extracted from PDF forms. "
            "Never change, rewrite, merge, remove, or re-order rows. "
            "Return one section assignment for every input sequence."
        ),
        model_settings={"temperature": 0, "max_tokens": 10000},
    )


def _row_payload(rows: list[ExtractedRow]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for idx, r in enumerate(rows, start=1):
        payload.append(
            {
                "sequence": r.sequence or idx,
                "page_number": r.page_number,
                "source_order": r.source_order,
                "current_section": r.section,
                "question_type": r.question_type.value,
                "question_text": r.question_text,
                "answer_text": r.answer_text,
            }
        )
    return payload


def _section_prompt(rows: list[ExtractedRow]) -> str:
    return (
        "Assign the correct Section value for each extracted workbook row.\n\n"
        "Section definition:\n"
        "- A section is a visually presented major form heading that organizes related rows. It is usually "
        "a standalone heading/title row, often larger, prominent, boxed, centered, or otherwise visually set "
        "apart from ordinary questions/instructions.\n"
        "- Use only the exact visible major heading text as the section value. Never summarize, shorten, "
        "rename, or invent a category from question text, answer text, instructions, or nearby context.\n"
        "- Most ordinary rows should inherit the active major section until a new major section starts. "
        "Do not leave section blank just because the section heading is not repeated on the same page.\n"
        "- Leave section empty only when the row is outside any active major section, is a page/header/footer "
        "item, or is a form title/page number/repeated metadata artifact.\n"
        "- Do not use repeated form titles, page numbers, field labels, table captions, attachment labels, "
        "row labels, standalone instructions, or answer option text as section.\n"
        "- Do not use supporting-document labels or short labels derived from instructions as sections. "
        "For example, do not turn text about a plan of care, recent events, safety explanation, documentation, "
        "admissions, visits, falls, or risk into a new Section value unless that exact text is visibly presented "
        "as a major standalone form section heading.\n"
        "- Bold text alone does not make a section. If bold text is just an emphasized phrase inside an "
        "instruction, question, option, note, or paragraph, keep the active section instead of creating a new one.\n"
        "- If an explicit major section starts at a row, assign that section to that row and following rows "
        "that belong to the same major grouping.\n"
        "- If a row is a continuation of a previous major section, keep the same section.\n"
        "- If the form clearly leaves an area outside the main sections, return an empty section.\n"
        "- Prefer exact visible major-section wording, including punctuation such as a trailing colon when shown.\n"
        "- If uncertain whether text is a true visual section heading, do not create a new section from it; "
        "use the active previous section if the row still belongs there, otherwise return an empty section.\n\n"
        "Good section examples when visibly presented as standalone major headings: Applicant Information; "
        "Member Details; Current Living Arrangements; Clinical Information; Additional Required Documentation:; "
        "Provider Attestation; Submitting Entity Attestation; Signature.\n"
        "Bad section examples: Page 1 of 5; Form title; DOB; SSN; Date; Admit Date; "
        "Description of documentation attached:; Label attachment(s) as ...; Recent hospital admissions; "
        "Reason for admission; Plan of Care; Recent Events; Safety Explanation; Behavioral Risk; "
        "Safety Determination Request Form.\n\n"
        "Return exactly one assignment per input row sequence. Only set sequence, section, confidence, and rationale.\n\n"
        f"Rows:\n{json.dumps(_row_payload(rows), ensure_ascii=False)}"
    )


def _merge_section_assignments(
    rows: list[ExtractedRow], refinement: SectionRefinement
) -> list[ExtractedRow]:
    by_sequence = {a.sequence: a for a in refinement.assignments}
    out: list[ExtractedRow] = []

    for idx, r in enumerate(rows, start=1):
        seq = r.sequence or idx
        assignment = by_sequence.get(seq)
        if assignment is None:
            out.append(r)
            continue

        meta = dict(r.meta or {})
        if assignment.confidence is not None:
            meta["section_refinement_confidence"] = assignment.confidence
        if assignment.rationale:
            meta["section_refinement_rationale"] = assignment.rationale

        out.append(r.model_copy(update={"section": assignment.section, "meta": meta}))

    return out


async def refine_sections_with_ai(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    if not rows:
        return []

    agent = _bedrock_section_agent()
    try:
        refinement = (await agent.run(_section_prompt(rows))).output
    except NoCredentialsError as e:
        raise RuntimeError(
            "Section refinement failed: AWS credentials not found. "
            "Configure AWS credentials and ensure Bedrock access."
        ) from e
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Section refinement failed during Bedrock analysis: {e}") from e

    return _merge_section_assignments(rows, refinement)
