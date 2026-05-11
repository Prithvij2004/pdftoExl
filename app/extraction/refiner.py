from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.config import settings
from app.extraction.bedrock_client import BedrockLLMClient
from app.extraction.llm_client import BaseLLMClient
from app.extraction.prompts import refinement_prompt
from app.schemas.form_schema import DocumentExtraction


def _save_refined(stem: str, document: DocumentExtraction) -> Path:
    out_dir = settings.output_dir / "extraction"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}_refined.json"
    path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
    return path


def refine_document_extraction(
    doc: DocumentExtraction,
    *,
    file_stem: str = "document",
    reference_mode: bool = False,
    llm_client: BaseLLMClient | None = None,
) -> DocumentExtraction:
    client = llm_client or BedrockLLMClient()
    try:
        data = client.invoke_json(refinement_prompt(doc), DocumentExtraction.model_json_schema())
    except Exception as e:
        for section in doc.sections:
            for item in section.items:
                item.needs_review = True
                if not item.review_reason:
                    item.review_reason = f"LLM refinement failed; using extracted output without refinement: {e}"
        _save_refined(file_stem, doc)
        return doc
    try:
        refined = DocumentExtraction.model_validate(data)
    except ValidationError as e:
        for section in doc.sections:
            for item in section.items:
                item.needs_review = True
                if not item.review_reason:
                    item.review_reason = f"LLM refinement returned invalid schema; using extracted output: {e}"
        _save_refined(file_stem, doc)
        return doc

    _save_refined(file_stem, refined)
    return refined
