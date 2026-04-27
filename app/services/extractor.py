from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.exceptions import NoCredentialsError

from app.config import settings
from app.models import ExtractionResult, ExtractedRow
from app.services.pdf_batches import iter_pdf_page_batches


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)

def _preferred_inference_profile_prefix(aws_region: str) -> str:
    # Bedrock inference profile IDs commonly use geo prefixes like `us.` and `eu.`.
    # Keep this simple and predictable for v1.
    if aws_region.startswith("eu-"):
        return "eu"
    return "us"


def _ensure_inference_profile_id(model_id: str, aws_region: str) -> str:
    """
    Nova models may require invocation via an inference profile ID like:
      us.amazon.nova-pro-v1:0
    If user supplies the raw model ID:
      amazon.nova-pro-v1:0
    we map it to a region-appropriate inference profile ID.
    """
    model_id = (model_id or "").strip()
    if model_id.startswith(("us.", "eu.")):
        return model_id
    if model_id.startswith("amazon.nova-"):
        return f"{_preferred_inference_profile_prefix(aws_region)}.{model_id}"
    return model_id


def _build_prompt(start_page: int, end_page: int) -> str:
    allowed_types = [
        "Radio Button",
        "Checkbox",
        "Checkbox Group",
        "Text Area",
        "Text Box",
        "Calendar",
        "Display",
        "Dropdown",
        "Equation",
        "Number",
        "Radio Button with Text Area",
        "Checkbox Group with Text Area",
        "Signature",
    ]

    return f"""
You are extracting ALL form content from a PDF batch (pages {start_page} to {end_page} of the original document).

Return ONLY valid JSON (no markdown) with this shape:
{{
  "rows": [
    {{
      "question_type": "<one of the allowed values>",
      "question_text": "<English question / label / instruction / section title>",
      "answer_text": "<English expected answer text or options, pipe-separated where applicable>",
      "page_number": <absolute page number from the original PDF, integer>,
      "source_order": <integer increasing top-to-bottom within each page>,
      "confidence": <optional 0..1 number>
    }}
  ]
}}

Rules:
- question_type MUST be exactly one of: {", ".join(allowed_types)}.
- Preserve the exact wording as much as possible; if non-English is present, translate to English.
- Do not invent answers; answer_text describes what the respondent is expected to provide.
- Radio Button: single-select answer options displayed; one row per radio group. answer_text is options pipe-separated. Use for visible radio circles/bubbles, or exclusive wording such as "select/choose one", "only one", or Yes/No choice pairs.
- Checkbox: single checkbox at the question level; no separate option list; answer_text "".
- Checkbox Group: multi-select answer options displayed; one row per group when multiple options may be selected. answer_text is options or statements pipe-separated. Use for "select all that apply", "one or more", "check all", or similar.
- Text Area: free text with multiple lines displayed; answer_text can be "" or "[Multi-line text input area]".
- Text Box: free text with a single line displayed; answer_text can be "" or "[Single-line text input field]".
- Calendar: allows a single date response to the question; answer_text "" or "[Date input]".
- Display: static instructional or descriptive text with no answer choices. If the display content has a short
  heading/title followed by a longer paragraph or description, put only the heading/title
  in question_text and put the larger paragraph/description in answer_text. If there is no
  separate body text, keep the display text in question_text and leave answer_text "".
  Treat every visually distinct paragraph as its OWN row, even when adjacent paragraphs
  share the same surrounding heading. Never merge two paragraphs into a single cell; emit
  one row per paragraph (the heading may be repeated across rows).
- Dropdown: single-select answer options in a select/list control; answer_text is options pipe-separated.
- Equation: field used to calculate a score or derived value; answer_text describes the visible calculation/formula or "[Calculated score]" if no formula is shown.
- Number: only allows numeric characters; answer_text "" or "[Numeric input only]".
- Radio Button with Text Area: single-select answer options with an available free-text area, such as "Other/specify/explain". Keep the options pipe-separated in answer_text and mark the text-entry option as shown, e.g. "Other: [Text area]".
- Checkbox Group with Text Area: multi-select answer options with an available free-text area, such as "Other/specify/explain". Keep the options pipe-separated in answer_text and mark the text-entry option as shown, e.g. "Other: [Text area]".
- If a field or instruction continues on the next page, emit a matching continuation row (same question_type and the same or clearly continued question text) so a later merge can combine split options or text. Prefer marking continuation in question_text with "(continued)" or "Continued:" when the layout shows a continuation.
- Signature: signature line/box; answer_text "[Signature capture area]".

Output requirements:
- Include EVERY input element and instructional/display text in reading order.
- Page headers/footers: avoid emitting generic repeated noise like page numbers (e.g. "Page 1 of 3"), timestamps, or branding-only lines.
- Do NOT emit header/footer-only metadata like form identifiers, control numbers, revision/version, or expiry/effective dates.
- If a header/footer contains completion-critical instructions or legal notices needed to fill/submit the form, include it as a Display row.
- page_number MUST use absolute page numbers (not 1..N within the batch).
- source_order starts at 0 for each page and increases top-to-bottom.
- Preserve bold formatting in question_text by wrapping bold words/phrases in Markdown
  `**...**` markers exactly as they appear in the PDF. Do not mark non-bold text as bold.
  Do not use any other markdown.
- Each visually distinct paragraph must be its own row. Never concatenate two paragraphs
  into a single cell even if they share the same surrounding context.
""".strip()


def _parse_model_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty model response")

    m = _JSON_BLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()

    return json.loads(text)


def extract_rows_from_pdf(pdf_path: Path) -> list[ExtractedRow]:
    cfg = Config(
        connect_timeout=3600,
        read_timeout=3600,
        retries={"max_attempts": 1},
    )
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)

    all_rows: list[ExtractedRow] = []
    effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)

    for batch in iter_pdf_page_batches(pdf_path, settings.pdf_batch_size):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "format": "pdf",
                            "name": "Document",
                            "source": {"bytes": batch.pdf_bytes},
                        }
                    },
                    {"text": _build_prompt(batch.start_page_number, batch.end_page_number)},
                ],
            }
        ]

        try:
            resp = client.converse(
                modelId=effective_model_id,
                messages=messages,
                inferenceConfig={"maxTokens": 6000, "temperature": 0.1},
            )
        except NoCredentialsError as e:
            raise RuntimeError(
                "Bedrock invoke failed: AWS credentials not found. "
                "Configure credentials (e.g., `aws configure` or env vars) and ensure Bedrock access."
            ) from e
        except ClientError as e:
            raise RuntimeError(
                "Bedrock invoke failed. If you see an on-demand throughput error, "
                "set BEDROCK_MODEL_ID to an inference profile ID like "
                "`us.amazon.nova-pro-v1:0` (or `eu.amazon.nova-pro-v1:0`). "
                f"Underlying error: {e}"
            ) from e
        except BotoCoreError as e:
            raise RuntimeError(f"Bedrock invoke failed: {e}") from e

        content = resp.get("output", {}).get("message", {}).get("content", [])
        response_text = ""
        if content and isinstance(content, list) and isinstance(content[0], dict):
            response_text = content[0].get("text") or ""

        data = _parse_model_json(response_text)
        result = ExtractionResult.model_validate(data)
        all_rows.extend(result.rows)

    return all_rows

