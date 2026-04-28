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
from app.services.pdf_batches import PdfBatch, iter_pdf_page_batches


_DEFAULT_MAX_TOKENS = 10000
_REFINE_MAX_TOKENS = 10000
_PAGES_PER_BATCH = 5


class FieldCandidate(BaseModel):
    question_type: QuestionType
    question_text: str = Field(min_length=1)
    answer_text: str = ""
    branching_logic: str = ""
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


class RefinedBatch(BaseModel):
    overall_idea: str = Field(
        default="",
        description="Short summary of the form's purpose across these pages and how repeated/conditional sections work.",
    )
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
            "PURPOSE: the rows you produce are consumed as a CONFIG SCHEMA for an external system. "
            "Each row defines one field that the downstream system will render to collect answers. "
            "The same field must therefore appear in the output ONLY ONCE, even if the source PDF visually "
            "repeats the same field block multiple times to capture multiple answers (e.g. 'Fall #1', 'Fall #2', "
            "'Fall #3' all asking the same Date / Time / Location / etc. questions). The downstream system handles "
            "multiplicity at runtime; the config must describe the field set canonically.\n"
            "Analyze the document and identify ALL user-fillable fields and important static instructions.\n"
            "Classify fields by semantic intent, not by visual appearance alone. A blank box may be a Calendar, Number, Signature, Text Area, or another field type depending on the label and surrounding instructions.\n"
            "Within a single page, if the same group of fields is repeated to capture multiple instances of the same entity, emit each distinct field ONCE and drop the visual repetitions.\n"
            "Translate non-English to English.\n"
            "Return data in the required structured output schema."
        ),
        model_settings={"temperature": 0, "max_tokens": _DEFAULT_MAX_TOKENS},
    )


def _bedrock_refine_agent() -> Agent:
    cfg = Config(connect_timeout=3600, read_timeout=3600, retries={"max_attempts": 1})
    bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)
    provider = BedrockProvider(bedrock_client=bedrock_client)

    effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)
    model = BedrockConverseModel(model_name=effective_model_id, provider=provider)

    return Agent(
        model=model,
        output_type=RefinedBatch,
        retries=2,
        system_prompt=(
            "You are a SEMANTIC REVIEWER for a PDF-to-form-config pipeline.\n"
            "INPUT: a multi-page PDF batch AND a draft list of field candidates that another agent produced from "
            "those pages. The draft is often noisy: wrong field types, duplicates, fragments, missed branching.\n"
            "OUTPUT: a refined list of candidates that will be written into a CONFIG SCHEMA consumed by an "
            "external system. Each row defines one field the downstream system will render.\n"
            "\n"
            "STEP 1 — Build an OVERALL IDEA of these pages before touching the candidates: what is this form for, "
            "what entity is being captured, which sections are repeated for multiple instances (e.g. 'Fall #1/#2/#3', "
            "'Medication 1/2/3', 'Child A/B/C'), and which sections are conditional on earlier answers ('If yes…', "
            "'Complete only if…', 'Skip to…'). Put a short summary of this idea in `overall_idea`.\n"
            "\n"
            "STEP 2 — Walk every draft candidate against that overall idea and the actual PDF and revise it:\n"
            "- DROP noise: page numbers, form IDs, version/revision/expiry stamps, branding-only headers/footers, "
            "rows that are continuations adding no new field, and duplicates of an already-kept distinct field.\n"
            "- DROP visual repetitions of the same field block (e.g. the second/third copy of 'Fall #N: Date / Time / "
            "Location'). Keep the FIRST occurrence; the downstream system handles multiplicity.\n"
            "- FIX classification using semantics, not glyph shape:\n"
            "  * Radio Button: single-select from 2+ visible options under one stem (mutually exclusive by meaning, "
            "or wording like 'select one', 'choose one', 'which of the following'). Options pipe-separated in answer_text.\n"
            "  * Checkbox Group: multi-select from 2+ visible options ('select all that apply', or independent attributes "
            "that can co-occur). Options pipe-separated in answer_text.\n"
            "  * Checkbox (singular): a lone tickbox that IS the question ('I agree…'). answer_text empty.\n"
            "  * Dropdown: single-select rendered as a list-control (▼ / 'Select from list'), or a long enumeration.\n"
            "  * Calendar / Number / Signature / Text Area / Text Box / Display / Equation: pick by the answer the "
            "field is collecting, not by how blank looks.\n"
            "  * Radio Button with Text Area / Checkbox Group with Text Area: only when the option group itself "
            "includes an inline 'Other/specify/explain' free-text region; mark the option as 'Other: [Text area]'.\n"
            "- MERGE one-row-per-option mistakes back into ONE Radio Button / Checkbox Group / Dropdown row with all "
            "options pipe-separated in answer_text.\n"
            "- INLINE 'specify' BLANKS NEXT TO OPTIONS: if individual options end with a write-in blank "
            "('—specify ___', 'Other—specify ___'), keep the option group as ONE row with cleaned option labels in "
            "answer_text (strip the trailing '—specify ___'), then emit ONE Text Box row per blank, in option order, "
            "with question_text like 'Specify Relationship' and branching_logic 'Display if Q? = <option label>'.\n"
            "- BRANCHING LOGIC: any field that is conditional on another answer must have branching_logic populated. "
            "Use 'Display if Q? = <triggering value>' or 'Skip to Q? if <condition>'. Use the literal token 'Q?' for "
            "the parent reference — a downstream pass resolves it to the parent's Sequence number. For Checkbox / "
            "Checkbox Group parents prefer 'Display if Q? = checked(selected)'; for Radio / Dropdown parents fully "
            "name the option. Leave empty for unconditional rows. Use the overall idea to spot conditional "
            "sections the draft missed.\n"
            "- Fix question_text and answer_text wording to match what is actually visible on the page; translate "
            "non-English to English. Preserve bold using Markdown `**...**` only where the PDF is actually bold.\n"
            "- Treat each visually distinct paragraph as its own Display row; don't merge.\n"
            "\n"
            "STEP 3 — Output the refined candidates in correct top-to-bottom reading order across the WHOLE batch. "
            "Set page_number to the actual PDF page the field is on (within the batch range). Set source_order "
            "increasing strictly across the batch starting from 0; a downstream pass renumbers it per page.\n"
            "rationale must be at most 60 chars; name the single semantic clue used.\n"
            "\n"
            "WHEN IN DOUBT, KEEP. Losing real fields is worse than keeping a small duplicate."
        ),
        model_settings={"temperature": 0, "max_tokens": _REFINE_MAX_TOKENS},
    )


def _format_draft_candidates(candidates: list[FieldCandidate]) -> str:
    lines: list[str] = []
    for c in candidates:
        q = (c.question_text or "").replace("\n", " ").strip()
        a = (c.answer_text or "").replace("\n", " ").strip()
        b = (c.branching_logic or "").replace("\n", " ").strip()
        lines.append(
            f"- page={c.page_number} source_order={c.source_order} type={c.question_type.value}\n"
            f"  question_text: {q}\n"
            f"  answer_text: {a}\n"
            f"  branching_logic: {b}"
        )
    return "\n".join(lines) if lines else "(no draft candidates)"


def _refine_prompt(batch: PdfBatch, draft: BatchAnalysis) -> list[object]:
    return [
        (
            "Refine the draft candidates below for this PDF batch.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "First, form an OVERALL IDEA of what this form is doing across these pages "
            "(purpose, repeated instance blocks, conditional sections). Then revise every "
            "draft candidate against the actual PDF: drop noise/duplicates, fix the field "
            "type using semantics (Radio Button vs Checkbox vs Checkbox Group vs Dropdown "
            "etc.), merge per-option splits back into one row, and populate branching_logic "
            "for any conditional row using the 'Q?' token. Return the refined list in "
            "reading order with page_number set to the actual page within the batch range "
            "and source_order increasing across the whole batch.\n\n"
            "Draft candidates:\n"
            f"{_format_draft_candidates(draft.candidates)}"
        ),
        BinaryContent(data=batch.pdf_bytes, media_type="application/pdf"),
    ]


def _analysis_prompt(batch: PdfBatch) -> list[object]:
    return [
        (
            "Analyze this PDF batch and identify every form element and important display/instructional text.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "Rules:\n"
            "- The output is a CONFIG SCHEMA consumed by an external system that will render each row as a field. "
            "Emit each distinct field exactly ONCE. If the page repeats the same field block (e.g. 'Fall #1', 'Fall #2', ... "
            "each asking Date / Time / Location / ...), keep the FIRST occurrence only and drop the visual duplicates. "
            "The downstream system handles multiplicity at runtime.\n"
            "- One candidate per input element or meaningful static instruction.\n"
            "- question_type must be one of: "
            + ", ".join([qt.value for qt in QuestionType])
            + ".\n"
            "- Do not default blank-looking fields to Text Box. First infer the best type from the label, expected answer, nearby instructions, options, and field constraints.\n"
            "- Choice fields (Radio Button vs Checkbox vs Checkbox Group vs Dropdown) — read carefully:\n"
            "  * Radio Button: single-select from 2+ visible options under one question stem (e.g. Yes/No, Male/Female/Other, age bands, frequency scales). Use this when wording says 'select one', 'choose one', 'only one', 'which of the following', or when the option set is mutually exclusive by meaning. answer_text = ALL options joined with ' | '. ALWAYS emit ONE row per question, never one row per option.\n"
            "  * Checkbox Group: multi-select from 2+ visible options under one question stem. Use this when wording says 'select all that apply', 'check all that apply', 'one or more', 'mark all', or when options are independent attributes that can co-occur (e.g. symptoms, languages spoken, accommodations needed). answer_text = ALL options joined with ' | '. ALWAYS emit ONE row per question, never one row per option.\n"
            "  * Checkbox (singular): a single standalone checkbox that IS the question itself — there is no separate option list, just one box to tick (e.g. 'I agree to the terms', 'Check if applicable', a lone consent box). answer_text = EMPTY string for Checkbox.\n"
            "  * Dropdown: single-select where the form renders a list-control (▼ arrow, combo box, or 'Select from list' affordance) instead of visible bubbles/squares per option. Also use when the option set is a long enumeration (countries, states, ICD codes) presented as a single field. answer_text = options joined with ' | ' if visible; empty if the list isn't enumerated on the page.\n"
            "- Glyph shape is NOT decisive: radios are sometimes drawn with square glyphs and checkbox-groups sometimes with circles. Decide by question wording + option semantics (mutually exclusive vs co-occurring) FIRST; use glyph shape only as a tiebreaker.\n"
            "- Options split across multiple lines, columns, or pages still belong to ONE row. Never emit one row per option line. If you see a stem followed by a vertical list of choices, that is one Radio Button / Checkbox Group / Dropdown row with all choices pipe-separated in answer_text.\n"
            "- Ambiguity tiebreaker for an options list with no clear wording cue: prefer Radio Button (single-select) over Checkbox Group. Splitting a radio into independent checkboxes loses the option list and is harder to recover than the reverse.\n"
            "- Use Text Area for free-text answers with multiple lines displayed, such as comments, explanations, notes, narratives, descriptions, or address blocks.\n"
            "- Use Text Box for free-text answers with a single line displayed.\n"
            "- Use Calendar when the label asks for a single date response, DOB, effective date, signature date, review date, or any date-formatted value even if the field looks like a plain blank.\n"
            "- Use Display for static instructions or descriptive text with no answer choices.\n"
            "- Use Equation for fields used to calculate a score or derived value. Put the visible calculation/formula in answer_text, or '[Calculated score]' if no formula is shown.\n"
            "- Use Number for numeric-only values such as age, quantity, amount, count, percentage, or score. For phone numbers, IDs, and case numbers, use Text Box unless the form clearly restricts the value to numeric input only.\n"
            "- Use Radio Button with Text Area when a single-select option group includes an available free-text area, such as 'Other/specify/explain'. Keep options pipe-separated in answer_text and mark the text-entry option as shown, e.g. 'Other: [Text area]'.\n"
            "- Use Checkbox Group with Text Area when a multi-select option group includes an available free-text area, such as 'Other/specify/explain'. Keep options pipe-separated in answer_text and mark the text-entry option as shown, e.g. 'Other: [Text area]'.\n"
            "- INLINE 'specify' BLANKS NEXT TO OPTIONS: when individual options in a Radio Button / Checkbox Group end with a write-in blank such as '—specify relationship ____', '—specify ____', 'Other—specify ____', '(explain) ____', emit the question as ONE Radio Button or Checkbox Group row with ALL options pipe-separated in answer_text (strip the trailing '—specify ___' phrase from each option label so the option text is clean, e.g. 'Lives in own home/apt (with others)' not 'Lives in own home/apt (with others)—specify relationship'). THEN, immediately AFTER that row, emit ONE ADDITIONAL Text Box row PER write-in blank, in the same top-to-bottom order as the options that have blanks. The Text Box's question_text should be the prompt for the blank in title case, e.g. 'Specify Relationship', 'Specify', 'Specify Other'. answer_text for these Text Box rows is empty. Each such Text Box row's branching_logic must be set to 'Display if Q? = <option label>' where <option label> is the cleaned option that triggers the blank (e.g. 'Display if Q? = Lives in own home/apt (with others)'). Use the literal token 'Q?' — a downstream step replaces it with the parent question's number. DO NOT split the option list itself into one row per option — the option group is always a SINGLE Radio Button / Checkbox Group row, with the per-option write-in blanks emitted as follow-up Text Box rows.\n"
            "- BRANCHING LOGIC (general): if a field, sub-question, or section is only shown/answered conditionally based on the answer to another question (phrases like 'If yes, …', 'If no, skip to …', 'If applicable …', 'Complete only if …', 'Skip to question N if …'), populate branching_logic on the dependent row(s) with a string of the form 'Display if Q? = <triggering value>' or 'Skip to Q? if <condition>'. Use the literal token 'Q?' for any reference to the parent question — a downstream pass resolves it to the parent row's Sequence number (the global reading-order index). Leave branching_logic empty for rows that are unconditional.\n"
            "- BRANCHING LOGIC FORMAT BY PARENT TYPE: when the parent question is Radio Button or Dropdown, the value on the right of '=' must fully name the option that triggers the dependent row (e.g. 'Display if Q? = Lives in own home/apt (with others)'). When the parent question is a Checkbox or Checkbox Group, the dependent row's branching_logic should be 'If Q? = checked(selected)' — do not name a specific option. A downstream pass enforces this format based on the resolved parent type, so prefer the option-named form when in doubt; the system rewrites the value side to 'checked(selected)' automatically when the parent turns out to be checkbox-based.\n"
            "- Use Signature when the label asks for a signature, initials, signer name in a signature block, or signature capture area.\n"
            "- question_text should be the label/question/instruction in English.\n"
            "- answer_text describes what the user is expected to provide; options should be pipe-separated.\n"
            "- Treat every visually distinct paragraph as its OWN candidate row, even when adjacent paragraphs share the same surrounding heading or context. Never merge two paragraphs into a single question_text or answer_text.\n"
            "- For Display rows with a short heading/title and a larger paragraph or description, use the heading/title as question_text and the larger paragraph/description as answer_text. If there is no separate body text, keep answer_text empty. If there are multiple body paragraphs under one heading, emit one Display row per paragraph (heading may be repeated).\n"
            "- Page headers/footers: avoid emitting generic repeated noise like page numbers (e.g. \"Page 1 of 3\"), timestamps, or branding-only lines.\n"
            "- Do NOT emit header/footer-only metadata like form identifiers, control numbers, revision/version, or expiry/effective dates.\n"
            "- If a header/footer contains completion-critical instructions or legal notices needed to fill/submit the form, emit it as a Display row.\n"
            "- Preserve bold formatting in question_text using Markdown bold markers `**...**` around any words/phrases that are visibly bold in the PDF. Do not bold text that is not actually bold. Do not use any other markdown.\n"
            "- This batch may contain multiple pages. Set page_number to the ACTUAL page number of the PDF (within the range above) where each candidate appears.\n"
            "- source_order increases strictly top-to-bottom in reading order across the WHOLE batch (do not reset per page); a downstream pass renumbers it per page.\n"
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
                branching_logic=c.branching_logic,
                page_number=c.page_number,
                source_order=c.source_order,
                confidence=c.confidence,
                meta={"rationale": c.rationale} if c.rationale else {},
            )
        )
    return rows


def _renumber_rows(rows: list[ExtractedRow], batch: PdfBatch) -> list[ExtractedRow]:
    """Clamp page_number to the batch range and renumber source_order per page.

    Why: the model can drift on absolute page numbers and on resetting source_order
    per page. We trust the model's relative ordering within its returned list,
    clamp page_number to the batch's page range, and renumber source_order from 0
    within each page in the order the model returned them.
    """
    out: list[ExtractedRow] = []
    per_page_counter: dict[int, int] = {}
    for r in rows:
        page = r.page_number
        if page < batch.start_page_number or page > batch.end_page_number:
            page = batch.start_page_number
        idx = per_page_counter.get(page, 0)
        per_page_counter[page] = idx + 1
        out.append(r.model_copy(update={"page_number": page, "source_order": idx}))
    return out


async def _process_batch(
    agent: Agent, refine_agent: Agent, batch: PdfBatch
) -> list[ExtractedRow]:
    prompt = _analysis_prompt(batch)
    try:
        analysis = (await agent.run(prompt)).output
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

    refined = await _refine_batch(refine_agent, batch, analysis)
    return _candidates_to_rows(refined)


async def _refine_batch(
    agent: Agent, batch: PdfBatch, draft: BatchAnalysis
) -> BatchAnalysis:
    """Second pass: form an overall idea of the batch and revise the draft candidates.

    Best-effort: if the refinement call fails on transport errors, fall back to the draft.
    """
    if not draft.candidates:
        return draft
    prompt = _refine_prompt(batch, draft)
    try:
        result = (await agent.run(prompt)).output
    except (ClientError, BotoCoreError):
        return draft
    if not result.candidates:
        return draft
    return BatchAnalysis(candidates=result.candidates)


async def extract_rows_from_pdf_agentic(pdf_path: Path) -> list[ExtractedRow]:
    agent = _bedrock_analysis_agent()
    refine_agent = _bedrock_refine_agent()
    all_rows: list[ExtractedRow] = []

    for batch in iter_pdf_page_batches(pdf_path, _PAGES_PER_BATCH):
        rows = await _process_batch(agent, refine_agent, batch)
        all_rows.extend(_renumber_rows(rows, batch))

    return all_rows

