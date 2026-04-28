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
_REPORT_MAX_TOKENS = 10000
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


class BatchReport(BaseModel):
    report: str = Field(
        default="",
        description=(
            "A detailed report of all pages in this batch combined. Walks through "
            "each section/part of each page in reading order and explains, in detail, "
            "what is being asked, what input type each element should map to "
            "(Radio Button / Checkbox Group / Checkbox / Dropdown / Calendar / "
            "Number / Signature / Text Area / Text Box / Display / Equation / "
            "Radio Button with Text Area / Checkbox Group with Text Area), "
            "with the wording or layout cue that justifies the choice. Must also "
            "call out repeated instance blocks, conditional sections, and inline "
            "'specify' write-ins. Not a summary — detailed knowledge."
        ),
    )


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


def _bedrock_report_agent() -> Agent:
    cfg = Config(connect_timeout=3600, read_timeout=3600, retries={"max_attempts": 1})
    bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)
    provider = BedrockProvider(bedrock_client=bedrock_client)

    effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)
    model = BedrockConverseModel(model_name=effective_model_id, provider=provider)

    return Agent(
        model=model,
        output_type=BatchReport,
        retries=2,
        system_prompt=(
            "You are a FORM ANALYST. Your single job is to read the multi-page PDF batch given to you and "
            "produce a DETAILED REPORT of all the pages combined. The report is consumed by a second agent that "
            "will use it to extract structured form-field rows. The accuracy of the second agent depends on the "
            "depth and correctness of your report.\n"
            "\n"
            "Walk through every page in this batch in page order, and within each page walk through every "
            "section / question / instruction in top-to-bottom reading order. For EACH part write a detailed "
            "paragraph that covers:\n"
            "  • The exact label / question / heading text on the page (in English; translate if needed).\n"
            "  • What input type this part should be classified as, picked from this fixed set: Radio Button, "
            "Checkbox, Checkbox Group, Dropdown, Calendar, Number, Signature, Text Area, Text Box, Display, "
            "Equation, Radio Button with Text Area, Checkbox Group with Text Area. State the type explicitly.\n"
            "  • The wording or layout cue that justifies that type. Use these rules to decide:\n"
            "      - Radio Button: single-select with 2+ visible options under one stem; mutually exclusive by "
            "meaning, or wording like 'select one', 'choose one', 'which of the following', Yes/No, "
            "Male/Female/Other, age bands, frequency scales.\n"
            "      - Checkbox Group: multi-select with 2+ visible options; 'select all that apply', 'check all', "
            "or independent attributes that can co-occur (symptoms, languages, accommodations).\n"
            "      - Checkbox (singular): a lone tickbox that IS the question itself ('I agree…', 'Check if "
            "applicable'). No option list.\n"
            "      - Dropdown: single-select rendered as a list-control (▼ / 'Select from list') or a long "
            "enumeration (countries, states, ICD codes).\n"
            "      - Calendar: any date answer (DOB, effective date, signature date) even if the field looks blank.\n"
            "      - Number: numeric-only values (age, count, percentage, score). Phone numbers / IDs / case "
            "numbers stay Text Box unless clearly numeric-restricted.\n"
            "      - Signature: signature, initials, signer-name in a signature block.\n"
            "      - Text Area: multi-line free text (comments, narrative, address block, explanation).\n"
            "      - Text Box: single-line free text. **TEXT BOX IS A LAST RESORT.** Even when the field "
            "visually looks like a plain blank line and the draft instinct is 'Text Box', evaluate every other "
            "option FIRST: is the label a date in any form (DOB, effective/expiry, signature date, review date, "
            "month/year) → Calendar; numeric-only (age, count, score, percentage) → Number; signature / initials → "
            "Signature; multi-line / paragraph / narrative / address block → Text Area; date or list with a ▼ → "
            "Calendar or Dropdown; calculated/derived value → Equation; static instruction → Display; an option "
            "group with an 'Other—specify ___' → Radio Button with Text Area / Checkbox Group with Text Area. "
            "Only choose Text Box after you have ruled all of the above out by reading the label and surrounding "
            "context. Do not let a blank-line look override the semantics of the label.\n"
            "      - Display: static instructions or descriptive text with no answer.\n"
            "      - Equation: a derived/calculated score field.\n"
            "      - Radio Button with Text Area / Checkbox Group with Text Area: a Radio / Checkbox Group whose "
            "option list includes an inline 'Other/specify/explain' free-text region.\n"
            "      - Glyph shape (circle vs square) is NOT decisive — decide by question wording + option semantics.\n"
            "  • For choice items, list the visible options in display order.\n"
            "  • Whether the part belongs to a repeated instance block (e.g. 'Fall #1 / Fall #2 / Fall #3', "
            "'Medication 1/2/3', 'Child A/B/C'). State which copy is the canonical first occurrence and which "
            "are visual repetitions, because the downstream config keeps each distinct field only once.\n"
            "  • Whether the part is conditional on another answer ('If yes…', 'Complete only if…', "
            "'Skip to question N if…'). Name the parent question and the triggering value.\n"
            "  • Inline 'specify' / 'Other—specify ___' write-in blanks attached to options.\n"
            "\n"
            "FORMAT: write the report as plain prose grouped by page, e.g.:\n"
            "  Page 3:\n"
            "    1. <label> — <type> — <reason>. Options: …. Notes: ….\n"
            "    2. …\n"
            "Be exhaustive. Do not skip parts. Do not produce a short summary.\n"
            "\n"
            "For every blank-looking field, walk through the alternatives to Text Box (Calendar / Number / "
            "Signature / Text Area / Dropdown / Equation / Display / the with-Text-Area variants) before "
            "committing to a type."
        ),
        model_settings={"temperature": 0, "max_tokens": _REPORT_MAX_TOKENS},
    )


def _report_prompt(batch: PdfBatch) -> list[object]:
    return [
        (
            "Produce a DETAILED REPORT of every page in this PDF batch combined.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "Walk through every page in order, and within each page walk through every "
            "section / question / instruction in top-to-bottom reading order. For EACH "
            "part state the exact label, the input type it should map to (Radio Button / "
            "Checkbox / Checkbox Group / Dropdown / Calendar / Number / Signature / "
            "Text Area / Text Box / Display / Equation / Radio Button with Text Area / "
            "Checkbox Group with Text Area), the wording or layout cue that justifies the "
            "type, the visible options for choice items, repeated-instance-block "
            "membership, conditional gating, and inline 'specify' write-ins.\n\n"
            "Be exhaustive. The next agent will rely entirely on this report to decide "
            "field types, so do not omit parts and do not condense into a short summary."
        ),
        BinaryContent(data=batch.pdf_bytes, media_type="application/pdf"),
    ]


def _analysis_prompt(batch: PdfBatch, report: str) -> list[object]:
    return [
        (
            "Analyze this PDF batch and identify every form element and important display/instructional text.\n"
            f"Pages in original PDF: {batch.start_page_number}..{batch.end_page_number}.\n\n"
            "A prior agent has already produced a DETAILED REPORT of these pages, included "
            "below. Use the report as the SOURCE OF TRUTH for what each part is and which "
            "field type it should be classified as. If the report says a part is a Radio "
            "Button / Checkbox / Checkbox Group / Dropdown / etc., use that classification "
            "for the corresponding row. Verify against the actual PDF and only deviate from "
            "the report when the PDF clearly contradicts it.\n\n"
            "===== REPORT START =====\n"
            f"{report.strip() or '(no report)'}\n"
            "===== REPORT END =====\n\n"
            "Rules:\n"
            "- The output is a CONFIG SCHEMA consumed by an external system that will render each row as a field. "
            "Emit each distinct field exactly ONCE. If the page repeats the same field block (e.g. 'Fall #1', 'Fall #2', ... "
            "each asking Date / Time / Location / ...), keep the FIRST occurrence only and drop the visual duplicates. "
            "The downstream system handles multiplicity at runtime.\n"
            "- One candidate per input element or meaningful static instruction.\n"
            "- question_type must be one of: "
            + ", ".join([qt.value for qt in QuestionType])
            + ".\n"
            "- TEXT BOX IS A LAST RESORT. Even when a field visually looks like a plain blank line, evaluate every "
            "other option FIRST before choosing Text Box: is the label a date (DOB, effective date, signature date, "
            "review date, month/year, any date-formatted answer) → Calendar; numeric-only (age, count, score, "
            "percentage) → Number; signature / initials → Signature; multi-line / narrative / address block → Text "
            "Area; list-control or long enumeration → Dropdown; option group with 'Other—specify' → Radio Button "
            "with Text Area / Checkbox Group with Text Area; calculated value → Equation; static instruction → "
            "Display. The report above already classified each part — match it. Only choose Text Box when none of "
            "the other types apply. Do not let a blank-line look override the semantics of the label.\n"
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


async def _generate_report(agent: Agent, batch: PdfBatch) -> str:
    """First pass: detailed report of all pages in the batch combined.

    Best-effort: on transport errors return an empty string and let the
    extraction agent fall back to working from the PDF alone.
    """
    try:
        result = (await agent.run(_report_prompt(batch))).output
    except (ClientError, BotoCoreError):
        return ""
    return result.report or ""


async def _process_batch(
    report_agent: Agent, extract_agent: Agent, batch: PdfBatch
) -> list[ExtractedRow]:
    report = await _generate_report(report_agent, batch)
    prompt = _analysis_prompt(batch, report)
    try:
        analysis = (await extract_agent.run(prompt)).output
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
    report_agent = _bedrock_report_agent()
    extract_agent = _bedrock_analysis_agent()
    all_rows: list[ExtractedRow] = []

    for batch in iter_pdf_page_batches(pdf_path, _PAGES_PER_BATCH):
        rows = await _process_batch(report_agent, extract_agent, batch)
        all_rows.extend(_renumber_rows(rows, batch))

    return all_rows

