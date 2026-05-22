"""Section-aware Bedrock extraction pipeline.

Textract is intentionally not used here. Stage 1 relies on PyMuPDF text/layout,
widgets, and rasterized page images; stages 2-3 use Bedrock tool-use.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError

from .canonical import (
    CANONICAL_EXCEL_SCHEMA_VERSION,
    DocumentProfile,
    ExtractedRow,
    SectionExtraction,
    SectionProfile,
)
from .extract import DocStructure, PageStructure, _bedrock_runtime
from .normalizer import normalize_rows
from .schema import Row

ProgressCallback = Callable[[str, str, int | None, int | None], None]
BEDROCK_MAX_OUTPUT_TOKENS = 9000
UNSECTIONED_CONTENT_NAME = "Unsectioned Content"
_SECTION_EXTRACTOR_PROMPT = Template(
    (Path(__file__).with_name("prompts") / "section_extractor_prompt.txt").read_text(
        encoding="utf-8"
    ).strip()
)

_SECTION_PROFILE_GUIDANCE = """
Section guidance:
- Sections are broad logical content areas with real visible section headings, not every question and not the whole PDF by default.
- Create a section for a major visible heading, topic area, repeated table, review/certification area, signature area, or a large instruction area that has its own visible heading.
- Some extractable content can appear before any section heading, especially at the beginning of page 1. Do not force that content into the next section.
- If extractable content has no visible section heading, cover only that no-heading content with a neutral extraction chunk named exactly "Unsectioned Content".
- Use real visible content headings as sections whenever they exist. Do not replace real headings with "Unsectioned Content".
- Some PDFs have no sections at all. Only in that case, return one neutral "Unsectioned Content" chunk covering the extractable pages.
- For a one-page form where the only prominent heading is the form title, return "Unsectioned Content", not the form title, as the section/chunk name.
- Every page with extractable form content must belong to at least one real section or to "Unsectioned Content".
- If a section heading starts near the bottom of one page and continues onto the next page, include both pages in that section.
- The form title belongs only in form_title. Never use the document title as a section name.
- Do NOT create sections for page chrome: repeated document titles, applicant/SSN/DOB header fields, page numbers, revision codes, footers, cover/title blocks, or generic front matter.
- Do NOT invent generic sections like "Header and Introduction", "Header and Identification", "Title", or "Cover Page" unless those exact words are a real content heading in the form.
- Introductory boilerplate before the first real content heading is not its own titled section. Use "Unsectioned Content" when it contains extractable instructions or fields.
- Each section should be large enough to preserve context, but small enough that the extractor can focus on one topic at a time.
""".strip()

@dataclass(frozen=True)
class AgentConfig:
    profiler_model_id: str = field(
        default_factory=lambda: os.environ.get(
            "BEDROCK_PROFILER_MODEL_ID",
            "us.amazon.nova-2-lite-v1:0",
        )
    )
    extractor_default_model_id: str = field(
        default_factory=lambda: os.environ.get(
            "BEDROCK_EXTRACTOR_DEFAULT_MODEL_ID",
            "us.amazon.nova-pro-v1:0",
        )
    )
    extractor_complex_model_id: str = field(
        default_factory=lambda: os.environ.get(
            "BEDROCK_EXTRACTOR_COMPLEX_MODEL_ID",
            "us.amazon.nova-pro-v1:0",
        )
    )
    max_section_images: int = field(
        default_factory=lambda: int(os.environ.get("SHOWLAY_MAX_SECTION_IMAGES", "5"))
    )
    max_tokens: int = field(default_factory=lambda: int(os.environ.get("SHOWLAY_MAX_TOKENS", "9000")))
    temperature: float = 0.0


@dataclass
class AgenticExtractionResult:
    profile: DocumentProfile
    raw_rows: list[dict[str, Any]]
    rows: list[Row]
    telemetry: list[dict[str, Any]]
    warnings: list[str]

    def canonical_meta(self, doc: DocStructure) -> dict[str, Any]:
        return {
            "form_title": self.profile.form_title,
            "form_version": self.profile.form_version,
            "page_count": doc.page_count,
            "detected_sections": [section.model_dump() for section in self.profile.sections],
            "excel_schema_version": CANONICAL_EXCEL_SCHEMA_VERSION,
            "extraction_strategy": self.profile.extraction_strategy,
            "complexity": self.profile.complexity,
        }


class ExtractionGraphState(TypedDict, total=False):
    doc: DocStructure
    profile: DocumentProfile
    sections: list[SectionProfile]
    section_index: int
    raw_rows: list[dict[str, Any]]
    rows: list[Row]
    telemetry: list[dict[str, Any]]
    warnings: list[str]


class ExtractionGraphContext(TypedDict):
    config: AgentConfig
    client: Any
    progress: ProgressCallback | None


def _strip_codefence(value: str) -> str:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _json_from_text(value: str) -> Any:
    cleaned = _strip_codefence(value)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char not in "{[":
                continue
            try:
                parsed, _ = decoder.raw_decode(cleaned[index:])
                return parsed
            except json.JSONDecodeError:
                continue
        raise


def _tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(mode="validation")


def _message_text(resp: dict[str, Any]) -> str:
    parts = resp.get("output", {}).get("message", {}).get("content", [])
    return "\n".join(str(part.get("text", "")) for part in parts if "text" in part).strip()


def _adapt_tool_data(data: Any, response_model: type[BaseModel]) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if response_model is SectionExtraction and isinstance(data, list):
        return {"rows": data}
    raise ValueError(f"Expected object for {response_model.__name__}, got {type(data).__name__}")


def _response_debug(resp: dict[str, Any]) -> str:
    stop_reason = resp.get("stopReason", "")
    text = _message_text(resp)
    content = resp.get("output", {}).get("message", {}).get("content", [])
    content_keys = [sorted(part.keys()) for part in content if isinstance(part, dict)]
    preview = re.sub(r"\s+", " ", text or "")[:500]
    return f"stopReason={stop_reason!r}; content_keys={content_keys}; text_preview={preview!r}"


def _tool_input(resp: dict[str, Any], tool_name: str, response_model: type[BaseModel]) -> dict[str, Any]:
    for part in resp.get("output", {}).get("message", {}).get("content", []):
        tool_use = part.get("toolUse")
        if tool_use and tool_use.get("name") == tool_name:
            return _adapt_tool_data(tool_use.get("input") or {}, response_model)
        if tool_use and tool_use.get("input"):
            return _adapt_tool_data(tool_use.get("input"), response_model)
    text = _message_text(resp)
    if text:
        return _adapt_tool_data(_json_from_text(text), response_model)
    raise ValueError(
        f"Bedrock response did not contain tool output for {tool_name}. {_response_debug(resp)}"
    )


def _image_block(path: str) -> dict[str, Any]:
    return {"image": {"format": "png", "source": {"bytes": Path(path).read_bytes()}}}


def _is_tool_use_model_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        exc.__class__.__name__ == "ModelErrorException"
        and "tooluse" in text
        and "invalid sequence" in text
    )


def _converse_tool(
    client: Any,
    *,
    model_id: str,
    tool_name: str,
    tool_description: str,
    response_model: type[BaseModel],
    content: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
) -> tuple[BaseModel, dict[str, Any]]:
    t0 = time.time()
    schema = _tool_schema(response_model)
    requested_max_tokens = max_tokens
    max_tokens = min(max_tokens, BEDROCK_MAX_OUTPUT_TOKENS)
    fallback_used = False
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            toolConfig={
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool_name,
                            "description": tool_description,
                            "inputSchema": {"json": schema},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": tool_name}},
            },
        )
    except Exception as exc:
        if not _is_tool_use_model_error(exc):
            raise
        fallback_used = True
        resp = _converse_json_fallback(
            client,
            model_id=model_id,
            content=content,
            response_model=response_model,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    try:
        data = _tool_input(resp, tool_name, response_model)
    except (ValueError, json.JSONDecodeError):
        fallback_used = True
        fallback_resp = _converse_json_fallback(
            client,
            model_id=model_id,
            content=content,
            response_model=response_model,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        data = _adapt_tool_data(_json_from_text(_message_text(fallback_resp)), response_model)
        resp = fallback_resp
    try:
        parsed = response_model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Bedrock tool output failed {response_model.__name__} validation: {exc}") from exc
    telemetry = {
        "model_id": model_id,
        "elapsed_s": round(time.time() - t0, 2),
        "input_tokens": resp.get("usage", {}).get("inputTokens"),
        "output_tokens": resp.get("usage", {}).get("outputTokens"),
        "fallback_used": fallback_used,
        "requested_max_tokens": requested_max_tokens,
        "max_tokens": max_tokens,
    }
    return parsed, telemetry


def _converse_json_fallback(
    client: Any,
    *,
    model_id: str,
    content: list[dict[str, Any]],
    response_model: type[BaseModel],
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    fallback_content = list(content)
    instruction = (
        "\n\nTool use failed or did not return valid tool output. "
        "Return ONLY valid JSON matching this schema. No markdown. No explanation.\n"
        f"Schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    if fallback_content and "text" in fallback_content[-1]:
        fallback_content[-1] = {"text": str(fallback_content[-1]["text"]) + instruction}
    else:
        fallback_content.append({"text": instruction})
    return client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": fallback_content}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )


def _page_excerpt(page: PageStructure, limit: int = 900) -> str:
    text = " ".join(str(block.get("text") or "") for block in page.text_blocks)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _page_profile_excerpt(page: PageStructure, limit: int = 1000) -> str:
    text = _page_excerpt(page, 100_000)
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]} ... {text[-half:]}"


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _repeated_page_lines(doc: DocStructure) -> set[str]:
    counts: dict[str, int] = {}
    for page in doc.pages:
        seen_on_page: set[str] = set()
        for block in page.text_blocks:
            text = _compact_text(str(block.get("text") or ""))
            if text:
                seen_on_page.add(text.lower())
        for text in seen_on_page:
            counts[text] = counts.get(text, 0) + 1
    threshold = 2 if doc.page_count <= 4 else max(2, doc.page_count // 3)
    return {text for text, count in counts.items() if count >= threshold}


def _looks_like_section_heading(text: str, repeated_lines: set[str]) -> bool:
    normalized = text.lower()
    if normalized in repeated_lines:
        return False
    if len(text) < 4 or len(text) > 120:
        return False
    if "____" in text:
        return False
    if re.search(r"\b(ssn|dob|rda|rev\.?|tc\d+)\b", normalized):
        return False
    if re.match(r"^\d+\s+\S+", text):
        return False
    field_label_prefixes = (
        "date of ",
        "time of ",
        "location of ",
        "if yes",
        "printed name",
        "signature of ",
        "credentials",
    )
    if normalized.startswith(field_label_prefixes):
        return False
    if text.endswith(":"):
        return True
    return bool(re.match(r"^[A-Z][A-Za-z0-9()'’&/ -]+$", text) and len(text.split()) <= 7)


def _heading_candidates(page: PageStructure, repeated_lines: set[str], limit: int = 10) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for block in page.text_blocks:
        text = _compact_text(str(block.get("text") or ""))
        key = text.lower()
        if key in seen:
            continue
        if _looks_like_section_heading(text, repeated_lines):
            candidates.append(text)
            seen.add(key)
        if len(candidates) >= limit:
            break
    return candidates


def _profile_prompt(doc: DocStructure) -> str:
    repeated_lines = _repeated_page_lines(doc)
    excerpts = [
        {
            "page": page.page_index + 1,
            "text_excerpt": _page_profile_excerpt(page),
            "candidate_section_headings": _heading_candidates(page, repeated_lines),
            "widget_count": len(page.widgets),
            "text_block_count": len(page.text_blocks),
        }
        for page in doc.pages
    ]
    return (
        "You are profiling a healthcare PDF form for structured HIP workbook extraction.\n"
        "Use visible rendered text and page layout as the primary signal. Widgets are only supporting evidence.\n"
        "Identify the form title, version, sections with 1-based pages, shared coding legends, complexity, "
        "and whether extraction should be single_call or section_by_section.\n"
        "Use candidate_section_headings as hints for real section boundaries; ignore them if they are only field labels. "
        f"Do not return only {UNSECTIONED_CONTENT_NAME!r} for a multi-page form when candidate_section_headings "
        "contains real content headings.\n"
        "Return only via the provided tool schema.\n\n"
        f"{_SECTION_PROFILE_GUIDANCE}\n\n"
        f"PDF path: {doc.pdf_path}\n"
        f"Page count: {doc.page_count}\n"
        f"Has AcroForm widgets: {doc.has_acroform}\n"
        f"Per-page excerpts:\n{json.dumps(excerpts, ensure_ascii=False, indent=2)}"
    )


def _is_generic_front_matter_section(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    if not normalized:
        return False
    exact = {
        "header",
        "header and introduction",
        "header and identification",
        "header and applicant information",
        "document header",
        "page header",
        "title",
        "title and introduction",
        "cover page",
        "front matter",
        "introductory text",
        "introductory information",
        "introductory instructions",
    }
    if normalized in exact:
        return True
    return normalized.startswith("header and ") or normalized.startswith("title and ")


def _is_unsectioned_section(name: str) -> bool:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip() == "unsectioned content"


def _is_form_title_section(name: str, form_title: str) -> bool:
    normalized_name = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", form_title.lower()).strip()
    return bool(normalized_name and normalized_title and normalized_name == normalized_title)


def _section_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _block_matches_section_name(block_text: str, section_name: str) -> bool:
    block_key = _section_key(block_text)
    section_key = _section_key(section_name)
    if not block_key or not section_key:
        return False
    return block_key == section_key or block_key.startswith(f"{section_key} ")


def _is_meaningful_unsectioned_text(text: str, form_title: str, repeated_lines: set[str]) -> bool:
    normalized = text.lower()
    if not text or normalized in repeated_lines:
        return False
    if _is_form_title_section(text, form_title):
        return False
    if "____" in text:
        return False
    if re.search(r"\b(ssn|dob|rda|rev\.?|tc\d+)\b", normalized):
        return False
    if re.match(r"^\d+\s+\S+", text):
        return False
    return len(text) >= 8


def _pages_with_leading_unsectioned_content(
    sections: list[SectionProfile],
    doc: DocStructure,
    form_title: str,
) -> set[int]:
    repeated_lines = _repeated_page_lines(doc)
    pages: set[int] = set()
    for page in doc.pages:
        page_no = page.page_index + 1
        section_names = [
            section.name
            for section in sections
            if page_no in section.pages and not _is_unsectioned_section(section.name)
        ]
        if not section_names:
            continue
        first_section_index: int | None = None
        for index, block in enumerate(page.text_blocks):
            text = _compact_text(str(block.get("text") or ""))
            if any(_block_matches_section_name(text, section_name) for section_name in section_names):
                first_section_index = index
                break
        if first_section_index is None:
            continue
        leading_blocks = page.text_blocks[:first_section_index]
        if any(
            _is_meaningful_unsectioned_text(
                _compact_text(str(block.get("text") or "")),
                form_title,
                repeated_lines,
            )
            for block in leading_blocks
        ):
            pages.add(page_no)
    return pages


def _add_unsectioned_pages(sections: list[SectionProfile], pages: set[int]) -> list[SectionProfile]:
    if not pages:
        return sections
    for section in sections:
        if _is_unsectioned_section(section.name):
            section.pages = sorted(set(section.pages) | pages)
            return sections
    return [SectionProfile(name=UNSECTIONED_CONTENT_NAME, pages=sorted(pages)), *sections]


def _ensure_extraction_page_coverage(sections: list[SectionProfile], all_pages: set[int]) -> list[SectionProfile]:
    if not sections:
        return [SectionProfile(name=UNSECTIONED_CONTENT_NAME, pages=sorted(all_pages))]
    covered = {page for section in sections for page in section.pages}
    return _add_unsectioned_pages(sections, all_pages - covered)


def _normalize_profile(profile: DocumentProfile, doc: DocStructure) -> DocumentProfile:
    sections: list[SectionProfile] = []
    all_pages = set(range(1, doc.page_count + 1))
    unsectioned_pages: set[int] = set()
    for section in profile.sections:
        pages = [int(page) for page in section.pages if int(page) in all_pages]
        if not pages:
            continue
        if (
            _is_generic_front_matter_section(section.name)
            or _is_form_title_section(section.name, profile.form_title)
        ):
            unsectioned_pages.update(pages)
            continue
        sections.append(SectionProfile(name=section.name.strip() or f"Pages {pages[0]}-{pages[-1]}", pages=pages))

    unsectioned_pages.update(_pages_with_leading_unsectioned_content(sections, doc, profile.form_title))
    sections = _add_unsectioned_pages(sections, unsectioned_pages)
    sections = _ensure_extraction_page_coverage(sections, all_pages)
    profile.sections = sections
    if doc.page_count <= 6 and profile.complexity != "complex":
        profile.extraction_strategy = "single_call"
        profile.recommended_model_tier = "low_cost"
    elif profile.extraction_strategy not in {"single_call", "section_by_section"}:
        profile.extraction_strategy = "section_by_section"
    return profile


def profile_document(client: Any, doc: DocStructure, config: AgentConfig) -> tuple[DocumentProfile, dict[str, Any]]:
    pages = [doc.pages[0]]
    if doc.page_count > 1:
        pages.append(doc.pages[-1])
    content = [_image_block(page.image_path) for page in pages]
    content.append({"text": _profile_prompt(doc)})
    parsed, telemetry = _converse_tool(
        client,
        model_id=config.profiler_model_id,
        tool_name="record_document_profile",
        tool_description="Return the section profile and extraction strategy for this PDF form.",
        response_model=DocumentProfile,
        content=content,
        max_tokens=min(config.max_tokens, 6000),
        temperature=config.temperature,
    )
    profile = _normalize_profile(parsed, doc)
    telemetry["stage"] = "profile"
    telemetry["section_count"] = len(profile.sections)
    return profile, telemetry


def _format_blocks(pages: list[PageStructure], key: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in pages:
        for item in getattr(page, key):
            compact = dict(item)
            compact["page"] = page.page_index + 1
            out.append(compact)
            if len(out) >= limit:
                return out
    return out


def _extractor_prompt(
    doc: DocStructure,
    profile: DocumentProfile,
    section: SectionProfile,
    pages: list[PageStructure],
    chunk_index: int,
    chunk_count: int,
) -> str:
    page_numbers = [page.page_index + 1 for page in pages]
    return _SECTION_EXTRACTOR_PROMPT.substitute(
        unsectioned_content=repr(UNSECTIONED_CONTENT_NAME),
        document_title=profile.form_title,
        document_version=profile.form_version,
        all_sections=json.dumps([s.model_dump() for s in profile.sections], ensure_ascii=False),
        shared_legends=json.dumps(
            [legend.model_dump() for legend in profile.shared_legends],
            ensure_ascii=False,
        ),
        section_name=section.name,
        section_pages=section.pages,
        chunk_number=chunk_index + 1,
        chunk_count=chunk_count,
        page_numbers=page_numbers,
        text_blocks=json.dumps(_format_blocks(pages, "text_blocks", 180), ensure_ascii=False),
        widgets=json.dumps(_format_blocks(pages, "widgets", 140), ensure_ascii=False),
    )


def _page_chunks(section: SectionProfile, doc: DocStructure, max_images: int) -> list[list[PageStructure]]:
    by_no = {page.page_index + 1: page for page in doc.pages}
    pages = [by_no[page_no] for page_no in section.pages if page_no in by_no]
    if not pages:
        return []
    max_images = max(1, max_images)
    return [pages[index : index + max_images] for index in range(0, len(pages), max_images)]


def extract_section(
    client: Any,
    doc: DocStructure,
    profile: DocumentProfile,
    section: SectionProfile,
    config: AgentConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    raw_rows: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []
    warnings: list[str] = []
    chunks = _page_chunks(section, doc, config.max_section_images)
    model_id = (
        config.extractor_complex_model_id
        if profile.complexity == "complex" or profile.recommended_model_tier == "mid_tier"
        else config.extractor_default_model_id
    )

    for chunk_index, pages in enumerate(chunks):
        content = [_image_block(page.image_path) for page in pages]
        content.append({"text": _extractor_prompt(doc, profile, section, pages, chunk_index, len(chunks))})
        parsed, tele = _converse_tool(
            client,
            model_id=model_id,
            tool_name="record_section_extraction",
            tool_description="Return extracted canonical rows for this section or section page chunk.",
            response_model=SectionExtraction,
            content=content,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
        for raw in parsed.rows:
            row = raw.model_dump()
            if not row.get("section"):
                row["section"] = parsed.section_name or section.name
            if not row.get("page"):
                row["page"] = pages[0].page_index + 1
            raw_rows.append(row)
        warnings.extend(parsed.warnings)
        tele.update(
            {
                "stage": "extract_section",
                "section": section.name,
                "pages": [page.page_index + 1 for page in pages],
                "row_count": len(parsed.rows),
            }
        )
        telemetry.append(tele)
    return raw_rows, telemetry, warnings


def _raw_rows_to_rows(raw_rows: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = []
    for raw in raw_rows:
        extracted = ExtractedRow.model_validate(raw)
        row = Row(
            section=extracted.section,
            question_type=str(extracted.question_type),
            question_text=extracted.question_text,
            answer_text=extracted.answer_text,
            answer_validation=extracted.answer_validation,
            branching_logic=extracted.branching_logic,
            question_rule=extracted.question_rule,
            required=extracted.required,
            page=extracted.page,
            bbox=extracted.bbox,
            confidence=max(0.0, min(1.0, float(extracted.confidence_hint or 1.0))),
            review_reasons=list(extracted.warnings),
        )
        row.external_id = extracted.external_id
        row.branching_source = extracted.branching_source
        row.source_ids = list(extracted.source_ids)
        rows.append(row)
    return rows


def _graph_profile_document(
    state: ExtractionGraphState,
    runtime: Runtime[ExtractionGraphContext],
) -> dict[str, Any]:
    progress = runtime.context.get("progress")
    if progress:
        progress("profile", "Profiling document sections...", None, None)
    profile, profile_telemetry = profile_document(
        runtime.context["client"],
        state["doc"],
        runtime.context["config"],
    )
    return {
        "profile": profile,
        "telemetry": [profile_telemetry],
        "raw_rows": [],
        "warnings": [],
        "section_index": 0,
    }


def _graph_plan_sections(state: ExtractionGraphState) -> dict[str, Any]:
    doc = state["doc"]
    profile = state["profile"]
    if profile.extraction_strategy == "single_call":
        sections = [
            SectionProfile(
                name=profile.form_title or "Document",
                pages=list(range(1, doc.page_count + 1)),
            )
        ]
    else:
        sections = profile.sections
    return {"sections": sections, "section_index": 0}


def _route_after_section_planning(
    state: ExtractionGraphState,
) -> Literal["extract_section", "normalize_rows"]:
    if state.get("section_index", 0) < len(state.get("sections", [])):
        return "extract_section"
    return "normalize_rows"


def _graph_extract_next_section(
    state: ExtractionGraphState,
    runtime: Runtime[ExtractionGraphContext],
) -> dict[str, Any]:
    sections = state.get("sections", [])
    section_index = state.get("section_index", 0)
    section = sections[section_index]
    total = len(sections)
    progress = runtime.context.get("progress")
    if progress:
        progress(
            "extract",
            f"Extracting section {section_index + 1} of {total}: {section.name}",
            section_index,
            total,
        )

    section_rows, section_telemetry, section_warnings = extract_section(
        runtime.context["client"],
        state["doc"],
        state["profile"],
        section,
        runtime.context["config"],
    )

    next_index = section_index + 1
    if progress:
        progress(
            "extract",
            f"Finished section {next_index} of {total}: {section.name}",
            next_index,
            total,
        )
    return {
        "raw_rows": [*state.get("raw_rows", []), *section_rows],
        "telemetry": [*state.get("telemetry", []), *section_telemetry],
        "warnings": [*state.get("warnings", []), *section_warnings],
        "section_index": next_index,
    }


def _graph_normalize_rows(
    state: ExtractionGraphState,
    runtime: Runtime[ExtractionGraphContext],
) -> dict[str, Any]:
    progress = runtime.context.get("progress")
    if progress:
        progress("postprocess", "Normalizing canonical rows...", None, None)
    rows, normalizer_warnings = normalize_rows(_raw_rows_to_rows(state.get("raw_rows", [])))
    return {
        "rows": rows,
        "warnings": [*state.get("warnings", []), *normalizer_warnings],
    }


def build_document_extraction_graph():
    graph = StateGraph(ExtractionGraphState, context_schema=ExtractionGraphContext)
    graph.add_node("profile_document", _graph_profile_document)
    graph.add_node("plan_sections", _graph_plan_sections)
    graph.add_node("extract_section", _graph_extract_next_section)
    graph.add_node("normalize_rows", _graph_normalize_rows)
    graph.add_edge(START, "profile_document")
    graph.add_edge("profile_document", "plan_sections")
    graph.add_conditional_edges(
        "plan_sections",
        _route_after_section_planning,
        {"extract_section": "extract_section", "normalize_rows": "normalize_rows"},
    )
    graph.add_conditional_edges(
        "extract_section",
        _route_after_section_planning,
        {"extract_section": "extract_section", "normalize_rows": "normalize_rows"},
    )
    graph.add_edge("normalize_rows", END)
    return graph.compile()


document_extraction_graph = build_document_extraction_graph()


def extract_document_agentic(
    doc: DocStructure,
    *,
    config: AgentConfig | None = None,
    progress: ProgressCallback | None = None,
) -> AgenticExtractionResult:
    config = config or AgentConfig()
    client = _bedrock_runtime()
    final_state = document_extraction_graph.invoke(
        {"doc": doc},
        context={
            "config": config,
            "client": client,
            "progress": progress,
        },
    )
    return AgenticExtractionResult(
        profile=final_state["profile"],
        raw_rows=final_state.get("raw_rows", []),
        rows=final_state.get("rows", []),
        telemetry=final_state.get("telemetry", []),
        warnings=final_state.get("warnings", []),
    )
