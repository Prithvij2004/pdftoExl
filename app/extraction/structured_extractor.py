from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.chunking.markdown_chunker import MarkdownChunk, chunk_markdown
from app.config import settings
from app.extraction.bedrock_client import BedrockLLMClient
from app.extraction.llm_client import BaseLLMClient
from app.extraction.prompts import extraction_prompt
from app.schemas.form_schema import DocumentExtraction, FormItem, FormSection


def _artifact_dir() -> Path:
    path = settings.output_dir / "extraction"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _chunk_dir() -> Path:
    path = settings.output_dir / "chunks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_json(path: Path, payload: object) -> None:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")  # type: ignore[attr-defined]
    elif isinstance(payload, list):
        data = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in payload]
    else:
        data = payload
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fallback_document_for_chunk(chunk: MarkdownChunk, error: Exception) -> DocumentExtraction:
    section_title = " / ".join(chunk.heading_path) or "Review Required"
    section = FormSection(
        section_id=f"{chunk.chunk_id}_review",
        section_title=section_title,
        source_pages=chunk.source_pages,
        items=[
            FormItem(
                item_id=f"{chunk.chunk_id}_review_item",
                item_type="review_required",
                question_text="LLM extraction failed for this chunk",
                display_text=chunk.chunk_text[:4000],
                source_text=chunk.chunk_text[:4000],
                source_pages=chunk.source_pages,
                confidence=0.0,
                needs_review=True,
                review_reason=f"LLM failed to return valid structured JSON: {error}",
            )
        ],
    )
    return DocumentExtraction(document_title=section_title, sections=[section])


def _merge_documents(docs: list[DocumentExtraction]) -> DocumentExtraction:
    sections: list[FormSection] = []
    by_title: dict[str, int] = {}

    for doc in docs:
        for section in doc.sections:
            key = section.section_title.strip().lower() or section.section_id
            if key in by_title:
                idx = by_title[key]
                existing = sections[idx]
                pages = sorted(set(existing.source_pages + section.source_pages))
                sections[idx] = existing.model_copy(
                    update={"source_pages": pages, "items": existing.items + section.items}
                )
            else:
                by_title[key] = len(sections)
                sections.append(section)

    first = docs[0] if docs else DocumentExtraction()
    return DocumentExtraction(
        document_title=first.document_title,
        form_name=first.form_name,
        form_number=first.form_number,
        revision_date=first.revision_date,
        sections=sections,
    )


def extract_document_from_markdown(
    markdown_path: Path,
    llm_client: BaseLLMClient | None = None,
) -> DocumentExtraction:
    markdown = Path(markdown_path).read_text(encoding="utf-8")
    chunks = chunk_markdown(markdown)
    stem = Path(markdown_path).stem
    _save_json(_chunk_dir() / f"{stem}_chunks.json", chunks)

    client = llm_client or BedrockLLMClient()
    schema = DocumentExtraction.model_json_schema()
    docs: list[DocumentExtraction] = []

    for chunk in chunks:
        try:
            data = client.invoke_json(extraction_prompt(chunk), schema)
        except Exception as e:
            _save_json(
                _artifact_dir() / f"{stem}_{chunk.chunk_id}_extraction_error.json",
                {
                    "chunk_id": chunk.chunk_id,
                    "heading_path": chunk.heading_path,
                    "source_pages": chunk.source_pages,
                    "error": str(e),
                    "chunk_text_preview": chunk.chunk_text[:4000],
                },
            )
            docs.append(_fallback_document_for_chunk(chunk, e))
            continue
        try:
            docs.append(DocumentExtraction.model_validate(data))
        except ValidationError as e:
            _save_json(
                _artifact_dir() / f"{stem}_{chunk.chunk_id}_schema_error.json",
                {
                    "chunk_id": chunk.chunk_id,
                    "heading_path": chunk.heading_path,
                    "source_pages": chunk.source_pages,
                    "error": str(e),
                    "raw_data": data,
                },
            )
            docs.append(_fallback_document_for_chunk(chunk, e))

    merged = _merge_documents(docs)
    _save_json(_artifact_dir() / f"{stem}_extracted.json", merged)
    return merged
