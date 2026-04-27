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
        "Text Box",
        "Text Area",
        "Date",
        "Number",
        "Radio Button",
        "Checkbox",
        "Checkbox Group",
        "Dropdown",
        "Display",
        "Signature",
        "Group Table",
        "Group",
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
- Text Box: single-line blank/underscores; answer_text can be "" or "[Single-line text input field]".
- Text Area: multi-line blank region; answer_text can be "" or "[Multi-line text input area – adjustable height]".
- Calendar: date blank; answer_text "" or "[Date input – mm/dd/yyyy format]".
- Number: numeric blank; answer_text "" or "[Numeric input only]".
- Radio Button: one row per radio group; answer_text is options pipe-separated.
- Checkbox: standalone checkbox statement; answer_text "".
- Checkbox Group: one row per group instruction; answer_text is statements pipe-separated.
- Dropdown: one row; answer_text is options pipe-separated.
- Display: static instructional or descriptive text. If the display content has a short
  heading/title followed by a longer paragraph or description, put only the heading/title
  in question_text and put the larger paragraph/description in answer_text. If there is no
  separate body text, keep the display text in question_text and leave answer_text "".
  Treat every visually distinct paragraph as its OWN row, even when adjacent paragraphs
  share the same surrounding heading. Never merge two paragraphs into a single cell; emit
  one row per paragraph (the heading may be repeated across rows).
- Signature: signature line/box; answer_text "[Signature capture area]".
- Group: section header/container; answer_text "".
- Group Table: table data-entry block; answer_text describes columns and available empty rows.

Output requirements:
- Include EVERY input element and instructional/display text in reading order.
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

