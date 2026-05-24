"""Section-aware Bedrock extraction pipeline.

Textract is intentionally not used here. Stage 1 relies on PyMuPDF text/layout,
widgets, and rasterized page images; agent stages use ChatBedrockConverse JSON.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Literal, TypedDict

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError

from .canonical import (
    CANONICAL_EXCEL_COLUMNS,
    CANONICAL_EXCEL_SCHEMA_VERSION,
    CANONICAL_OUTPUT_FIELD_TAXONOMY,
    CANONICAL_QUESTION_TYPE_TAXONOMY,
    DocumentProfile,
    ExtractedRow,
    ExtractionPolicy,
    QuestionTypeExample,
    QuestionTypePolicy,
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
- Do NOT create sections for page chrome: repeated document titles, repeated identity/date header fields, page numbers, revision codes, footers, cover/title blocks, or generic front matter.
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
    policy_model_id: str = field(
        default_factory=lambda: os.environ.get(
            "BEDROCK_POLICY_MODEL_ID",
            "us.amazon.nova-pro-v1:0",
        )
    )
    max_section_images: int = field(
        default_factory=lambda: int(os.environ.get("SHOWLAY_MAX_SECTION_IMAGES", "5"))
    )
    max_policy_images: int = field(
        default_factory=lambda: int(os.environ.get("SHOWLAY_MAX_POLICY_IMAGES", "8"))
    )
    max_tokens: int = field(default_factory=lambda: int(os.environ.get("SHOWLAY_MAX_TOKENS", "9000")))
    temperature: float = 0.0


@dataclass
class AgenticExtractionResult:
    profile: DocumentProfile
    extraction_policy: ExtractionPolicy
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
            "extraction_policy_summary": self.extraction_policy.form_summary,
        }


class ExtractionGraphState(TypedDict, total=False):
    doc: DocStructure
    profile: DocumentProfile
    extraction_policy: ExtractionPolicy
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


def _json_from_text(value: str, *, prefer_object: bool = False) -> Any:
    return _json_candidates_from_text(value, prefer_object=prefer_object)[0]


def _json_candidates_from_text(value: str, *, prefer_object: bool = False) -> list[Any]:
    cleaned = _strip_codefence(value)
    try:
        return [json.loads(cleaned)]
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start_chars = ("{", "[") if prefer_object else ("[", "{")
        candidates: list[Any] = []
        seen: set[tuple[int, int]] = set()
        for start_char in start_chars:
            for index, char in enumerate(cleaned):
                if char != start_char:
                    continue
                try:
                    parsed, end = decoder.raw_decode(cleaned[index:])
                except json.JSONDecodeError:
                    continue
                key = (index, index + end)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(parsed)
        if candidates:
            return candidates
        raise


def _tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(mode="validation")


def _adapt_tool_data(data: Any, response_model: type[BaseModel]) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if response_model is SectionExtraction and isinstance(data, list):
        return {"rows": data}
    if response_model is ExtractionPolicy and isinstance(data, list):
        policy_items = [item for item in data if isinstance(item, dict) and "policy" in item]
        if len(policy_items) == 1:
            return policy_items[0]
        guidance_items = [item for item in data if isinstance(item, dict) and item.get("question_type")]
        if guidance_items:
            seen_types: set[str] = set()
            question_types: list[str] = []
            for item in guidance_items:
                question_type = str(item.get("question_type") or "").strip()
                if question_type and question_type not in seen_types:
                    seen_types.add(question_type)
                    question_types.append(question_type)
            return {
                "policy": {
                    "question_types": question_types,
                    "excel_columns": CANONICAL_EXCEL_COLUMNS,
                    "question_type_guidance": guidance_items,
                }
            }
    raise ValueError(f"Expected object for {response_model.__name__}, got {type(data).__name__}")


def _parse_model_json_output(raw_text: str, response_model: type[BaseModel]) -> BaseModel:
    errors: list[str] = []
    candidates = _json_candidates_from_text(
        raw_text,
        prefer_object=response_model is not SectionExtraction,
    )
    for candidate in candidates:
        try:
            data = _adapt_tool_data(candidate, response_model)
            return response_model.model_validate(data)
        except (ValidationError, ValueError) as exc:
            errors.append(f"{type(candidate).__name__}: {str(exc).splitlines()[0]}")
    preview = re.sub(r"\s+", " ", raw_text)[:500]
    raise ValueError(
        f"No JSON candidate matched {response_model.__name__}. "
        f"Candidate errors: {errors[:5]}; text_preview={preview!r}"
    )


def _langchain_image_block(path: str) -> dict[str, Any]:
    encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": encoded,
        },
    }


def _langchain_text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _langchain_content(image_paths: list[str], prompt: str) -> list[dict[str, Any]]:
    return [*[_langchain_image_block(path) for path in image_paths], _langchain_text_block(prompt)]


def _is_tool_use_model_error(exc: Exception) -> bool:
    text = str(exc).lower()
    response = getattr(exc, "response", {}) or {}
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code") or exc.__class__.__name__).lower()
    return (
        ("modelerrorexception" in code or exc.__class__.__name__ == "ModelErrorException")
        and (
            "tooluse" in text
            or "tool use" in text
            or "invalid sequence" in text
            or "tool use troubleshooting" in text
        )
    )


def _bedrock_content_from_langchain(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bedrock_content: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "text":
            bedrock_content.append({"text": str(block.get("text", ""))})
        elif block.get("type") == "image":
            source = block.get("source") or {}
            bedrock_content.append(
                {
                    "image": {
                        "format": "png",
                        "source": {
                            "bytes": base64.b64decode(str(source.get("data") or "")),
                        },
                    }
                }
            )
    return bedrock_content


def _bedrock_message_text(resp: dict[str, Any]) -> str:
    parts = resp.get("output", {}).get("message", {}).get("content", [])
    return "\n".join(str(part.get("text", "")) for part in parts if "text" in part).strip()


def _langchain_message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
        return "\n".join(parts).strip()
    return str(content).strip()


def _langchain_usage(message: AIMessage) -> dict[str, int | None]:
    usage = getattr(message, "usage_metadata", None) or {}
    response_metadata = getattr(message, "response_metadata", None) or {}
    response_usage = response_metadata.get("usage") if isinstance(response_metadata, dict) else {}
    response_usage = response_usage or {}
    return {
        "input_tokens": usage.get("input_tokens") or response_usage.get("inputTokens"),
        "output_tokens": usage.get("output_tokens") or response_usage.get("outputTokens"),
    }


def _chat_bedrock_model(
    client: Any,
    *,
    model_id: str,
    max_tokens: int,
    temperature: float,
) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        model=model_id,
        client=client,
        bedrock_client=client,
        max_tokens=max_tokens,
        temperature=temperature,
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )


def _invoke_langchain_json_model(
    client: Any,
    *,
    model_id: str,
    response_model: type[BaseModel],
    content: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    instruction: str,
) -> tuple[BaseModel, dict[str, Any]]:
    t0 = time.time()
    schema = _tool_schema(response_model)
    requested_max_tokens = max_tokens
    max_tokens = min(max_tokens, BEDROCK_MAX_OUTPUT_TOKENS)
    model = _chat_bedrock_model(
        client,
        model_id=model_id,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    prompt_suffix = (
        f"\n\n{instruction}\n"
        "Return ONLY valid JSON matching this schema. No markdown. No explanation.\n"
        f"Schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    lc_content = list(content)
    if lc_content and lc_content[-1].get("type") == "text":
        lc_content[-1] = _langchain_text_block(str(lc_content[-1].get("text", "")) + prompt_suffix)
    else:
        lc_content.append(_langchain_text_block(prompt_suffix))
    try:
        message = model.invoke([HumanMessage(content=lc_content)])
    except Exception as exc:
        if not _is_tool_use_model_error(exc):
            raise
        return _invoke_bedrock_converse_json_model(
            client,
            model_id=model_id,
            response_model=response_model,
            content=lc_content,
            max_tokens=max_tokens,
            temperature=temperature,
            requested_max_tokens=requested_max_tokens,
            started_at=t0,
            fallback_reason=str(exc),
        )
    try:
        parsed = _parse_model_json_output(_langchain_message_text(message), response_model)
    except ValueError as exc:
        raise ValueError(
            f"ChatBedrockConverse JSON output failed {response_model.__name__} validation: {exc}"
        ) from exc
    usage = _langchain_usage(message)
    telemetry = {
        "model_id": model_id,
        "elapsed_s": round(time.time() - t0, 2),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "fallback_used": False,
        "provider_wrapper": "ChatBedrockConverse",
        "json_schema_mode": True,
        "requested_max_tokens": requested_max_tokens,
        "max_tokens": max_tokens,
    }
    return parsed, telemetry


def _invoke_bedrock_converse_json_model(
    client: Any,
    *,
    model_id: str,
    response_model: type[BaseModel],
    content: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    requested_max_tokens: int,
    started_at: float,
    fallback_reason: str,
) -> tuple[BaseModel, dict[str, Any]]:
    fallback_content = list(content)
    no_tool_instruction = (
        "\n\nTool calling is disabled. Do not output or simulate toolUse/toolResult blocks. "
        "Return plain JSON text only."
    )
    if fallback_content and fallback_content[-1].get("type") == "text":
        fallback_content[-1] = _langchain_text_block(
            str(fallback_content[-1].get("text", "")) + no_tool_instruction
        )
    else:
        fallback_content.append(_langchain_text_block(no_tool_instruction))

    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": _bedrock_content_from_langchain(fallback_content)}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    try:
        parsed = _parse_model_json_output(_bedrock_message_text(resp), response_model)
    except ValueError as exc:
        raise ValueError(
            f"Bedrock Converse JSON output failed {response_model.__name__} validation: {exc}"
        ) from exc
    usage = resp.get("usage", {})
    telemetry = {
        "model_id": model_id,
        "elapsed_s": round(time.time() - started_at, 2),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "fallback_used": True,
        "fallback_reason": fallback_reason[:500],
        "provider_wrapper": "boto3.converse",
        "json_schema_mode": True,
        "requested_max_tokens": requested_max_tokens,
        "max_tokens": max_tokens,
    }
    return parsed, telemetry


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
        "Return only valid JSON matching the provided schema.\n\n"
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
    content = _langchain_content([page.image_path for page in pages], _profile_prompt(doc))
    parsed, telemetry = _invoke_langchain_json_model(
        client,
        model_id=config.profiler_model_id,
        response_model=DocumentProfile,
        content=content,
        max_tokens=min(config.max_tokens, 6000),
        temperature=config.temperature,
        instruction="Return the section profile and extraction strategy for this PDF form as JSON.",
    )
    profile = _normalize_profile(parsed, doc)
    telemetry["stage"] = "profile"
    telemetry["section_count"] = len(profile.sections)
    return profile, telemetry


def _policy_block_sample(page: PageStructure, key: str, limit: int, text_limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in getattr(page, key):
        compact = dict(item)
        compact["page"] = page.page_index + 1
        if "text" in compact:
            compact["text"] = str(compact.get("text") or "")[:text_limit]
        out.append(compact)
        if len(out) >= limit:
            break
    return out


def _rect(item: dict[str, Any]) -> list[float]:
    rect = item.get("rect") or [0, 0, 0, 0]
    return [float(value) for value in rect[:4]]


def _mid_y(item: dict[str, Any]) -> float:
    rect = _rect(item)
    return (rect[1] + rect[3]) / 2


def _strip_option_marker(text: str) -> str:
    text = re.sub(r"[\uf071☐□☑✓✔]", " ", text)
    return _compact_text(text).strip(" :-")


def _box_option_blocks(page: PageStructure) -> list[dict[str, Any]]:
    return [
        block
        for block in page.text_blocks
        if any(marker in str(block.get("text") or "") for marker in ("\uf071", "☐", "□", "☑"))
    ]


_OPTION_MARKER_RE = r"[\uf071☐□☑]"


def _canonical_option_label(value: str) -> str:
    return re.sub(r"[^A-Z0-9./-]+", "", _strip_option_marker(value).upper())


def _is_short_option_label(value: str) -> bool:
    canonical = _canonical_option_label(value)
    return bool(canonical) and len(canonical) <= 3


def _is_option_only_label(value: str) -> bool:
    canonical = _canonical_option_label(value)
    return _is_short_option_label(canonical) or canonical in {"TRUE", "FALSE"}


def _is_binary_option_pair(first: str, second: str) -> bool:
    first_key = _canonical_option_label(first)
    second_key = _canonical_option_label(second)
    if not first_key or not second_key or first_key == second_key:
        return False
    if first_key in {"YES", "Y", "TRUE"} and second_key in {"NO", "N", "FALSE"}:
        return True
    if first_key in {"NO", "N", "FALSE"} and second_key in {"YES", "Y", "TRUE"}:
        return True
    return _is_short_option_label(first_key) and _is_short_option_label(second_key)


def _normalized_label(value: str) -> str:
    value = _compact_text(value.replace("!", "A")).strip(" :")
    parts = value.split(maxsplit=1)
    if len(parts) == 2 and _is_option_only_label(parts[0]) and re.match(r"[A-Z]", parts[1]):
        value = parts[1]
    return value.strip(" :")


def _dedupe_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


_NUMERIC_LABEL_WORDS = {
    "age",
    "amount",
    "count",
    "quantity",
    "number",
    "numeric",
    "total",
    "percent",
    "percentage",
    "rate",
    "score",
    "measurement",
    "code",
    "identifier",
    "id",
    "zip",
}
_NUMERIC_LABEL_PHRASES = (
    "postal code",
    "identification number",
    "id number",
    "account number",
    "member number",
    "phone number",
    "social security number",
)
_NUMERIC_LABEL_ABBREVIATIONS = {"ssn", "tin", "npi"}


def _has_numeric_label_semantics(lowered_label: str) -> bool:
    label_words = set(lowered_label.split())
    return (
        bool(label_words & _NUMERIC_LABEL_WORDS)
        or any(phrase in lowered_label for phrase in _NUMERIC_LABEL_PHRASES)
        or lowered_label in _NUMERIC_LABEL_ABBREVIATIONS
    )


def _candidate_question_type(label: str, widget_type: str = "", rect: list[float] | None = None) -> str:
    lowered = _dedupe_key(label)
    widget_type_lower = widget_type.lower()
    label_words = set(lowered.split())
    if "date" in label_words or lowered.endswith(" date"):
        return "Date"
    if "signature" in lowered:
        return "Signature"
    if _has_numeric_label_semantics(lowered):
        return "Number"
    if widget_type_lower in {"checkbox", "radiobutton", "radio button"}:
        return "Checkbox"
    if rect:
        height = max(0.0, rect[3] - rect[1])
        if height >= 28:
            return "Text Area"
    return "Text Box"


def _label_before_inline_options(text: str, marker_start: int) -> str:
    prefix = text[:marker_start]
    prefix = re.split(r"_{2,}", prefix)[-1]
    matches = re.findall(r"[A-Za-z][A-Za-z0-9’' /().-]*", prefix)
    return _normalized_label(matches[-1] if matches else prefix)


def _inline_choice_pairs(text: str) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    option_re = re.compile(
        rf"(?P<m1>{_OPTION_MARKER_RE})\s*(?P<option1>[A-Za-z0-9./-]{{1,24}})"
        rf"\s+(?P<m2>{_OPTION_MARKER_RE})\s*(?P<option2>[A-Za-z0-9./-]{{1,24}})",
        flags=re.I,
    )
    for match in option_re.finditer(text):
        option1 = _normalized_label(match.group("option1"))
        option2 = _normalized_label(match.group("option2"))
        label = _label_before_inline_options(text, match.start("m1"))
        if not label or not option1 or not option2:
            continue
        pairs.append(
            {
                "question_text": label,
                "option_labels": [option1, option2],
            }
        )
    return pairs


def _text_blocks_near_rect(page: PageStructure, rect: list[float], *, y_tolerance: float = 18) -> list[dict[str, Any]]:
    y_mid = (rect[1] + rect[3]) / 2
    return [
        block
        for block in page.text_blocks
        if abs(_mid_y(block) - y_mid) <= y_tolerance
    ]


def _field_candidate(
    *,
    page: PageStructure,
    question_type: str,
    question_text: str,
    source_ids: list[str],
    visible_text: str,
    reason: str,
    nearby_widget_ids: list[str] | None = None,
    answer_text: str = "",
) -> dict[str, Any]:
    return {
        "question_type": question_type,
        "question_text": _normalized_label(question_text),
        "answer_text": answer_text,
        "page": page.page_index + 1,
        "source_ids": [source_id for source_id in source_ids if source_id],
        "nearby_widget_ids": nearby_widget_ids or [],
        "visible_text": _compact_text(visible_text),
        "reason": reason,
    }


def _line_field_candidates(page: PageStructure, block: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(block.get("text") or "")
    source_id = str(block.get("source_id") or "")
    if not text or not source_id:
        return []
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"([A-Za-z][A-Za-z0-9’' /().-]*?)(?::)?\s*_{3,}", text):
        label = _normalized_label(match.group(1))
        if not label or len(label) > 80:
            continue
        question_type = _candidate_question_type(label)
        candidates.append(
            _field_candidate(
                page=page,
                question_type=question_type,
                question_text=label,
                source_ids=[source_id],
                nearby_widget_ids=_nearby_widget_ids(page, _rect(block), x_tolerance=90),
                visible_text=text,
                reason="Printed label followed by an underline/blank on a compact same-line field.",
            )
        )
    for pair in _inline_choice_pairs(text):
        candidates.append(
            _field_candidate(
                page=page,
                question_type="Radio Button",
                question_text=str(pair["question_text"]),
                answer_text=_hint_answer_text([str(label) for label in pair["option_labels"]]),
                source_ids=[source_id],
                nearby_widget_ids=_nearby_widget_ids(page, _rect(block), x_tolerance=90),
                visible_text=text,
                reason="Two peer option markers on the same line indicate one single-select field.",
            )
        )
    return candidates


def _widget_field_candidate(page: PageStructure, widget: dict[str, Any]) -> dict[str, Any] | None:
    label = _normalized_label(str(widget.get("label") or widget.get("name") or ""))
    if not label or _dedupe_key(label).startswith("undefined"):
        return None
    if re.fullmatch(r"text\d+", _dedupe_key(label).replace(" ", "")):
        return None
    if _dedupe_key(label) == "date" and any(
        "signature" in _dedupe_key(str(block.get("text") or "")) for block in page.text_blocks
    ):
        return None
    if page.page_index > 0 and _dedupe_key(label) in {
        "potential applicants name",
        "potential applicant s name",
    }:
        return None
    rect = _rect(widget)
    question_type = _candidate_question_type(label, str(widget.get("type") or ""), rect)
    if question_type == "Checkbox":
        return None
    nearby_blocks = _text_blocks_near_rect(page, rect, y_tolerance=22)
    visible_text = " ".join(str(block.get("text") or "") for block in nearby_blocks) or label
    source_ids = [str(block.get("source_id") or "") for block in nearby_blocks]
    return _field_candidate(
        page=page,
        question_type=question_type,
        question_text=label,
        source_ids=source_ids,
        nearby_widget_ids=[str(widget.get("source_id") or "")],
        visible_text=visible_text,
        reason="AcroForm widget label/type identifies a fillable field; visible text nearby confirms context.",
    )


def _signature_date_candidates(page: PageStructure) -> list[dict[str, Any]]:
    date_blocks = [
        block
        for block in page.text_blocks
        if _dedupe_key(str(block.get("text") or "")) == "date"
    ]
    signature_blocks = [
        block
        for block in page.text_blocks
        if "signature" in _dedupe_key(str(block.get("text") or ""))
    ]
    candidates: list[dict[str, Any]] = []
    for date_block in date_blocks:
        date_rect = _rect(date_block)
        same_row_signatures = [
            block
            for block in signature_blocks
            if abs(_mid_y(block) - _mid_y(date_block)) <= 12 and _rect(block)[0] < date_rect[0]
        ]
        context = str(same_row_signatures[0].get("text") or "").strip() if same_row_signatures else ""
        question_text = f"{context} Date" if context else "Date"
        source_ids = [str(date_block.get("source_id") or "")]
        if same_row_signatures:
            source_ids.insert(0, str(same_row_signatures[0].get("source_id") or ""))
        candidates.append(
            _field_candidate(
                page=page,
                question_type="Date",
                question_text=question_text,
                source_ids=source_ids,
                nearby_widget_ids=_nearby_widget_ids(page, date_rect, x_tolerance=80),
                visible_text=" ".join(part for part in [context, "Date"] if part),
                reason="Repeated Date label is tied to the signature label on the same row.",
            )
        )
    return candidates


def _field_candidate_hints(doc: DocStructure) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page in doc.pages:
        for block in page.text_blocks:
            candidates.extend(_line_field_candidates(page, block))
        for widget in page.widgets:
            candidate = _widget_field_candidate(page, widget)
            if candidate:
                candidates.append(candidate)
        candidates.extend(_signature_date_candidates(page))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for candidate in candidates:
        key = (
            int(candidate.get("page") or 0),
            str(candidate.get("question_type") or ""),
            _dedupe_key(str(candidate.get("question_text") or "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped[:80]


def _nearby_widget_ids(
    page: PageStructure,
    rect: list[float],
    *,
    x_tolerance: float = 35,
    y_tolerance: float = 22,
) -> list[str]:
    x0, y0, x1, y1 = rect
    y_mid = (y0 + y1) / 2
    ids: list[str] = []
    for widget in page.widgets:
        wx0, wy0, wx1, wy1 = _rect(widget)
        wy_mid = (wy0 + wy1) / 2
        if abs(wy_mid - y_mid) <= y_tolerance and wx0 >= x0 - x_tolerance and wx1 <= x1 + x_tolerance:
            source_id = str(widget.get("source_id") or "")
            if source_id:
                ids.append(source_id)
    return ids[:8]


def _prompt_blocks_left_of(
    page: PageStructure,
    option_blocks: list[dict[str, Any]],
    *,
    bottom_limit: float | None = None,
) -> list[dict[str, Any]]:
    if not option_blocks:
        return []
    option_rects = [_rect(block) for block in option_blocks]
    min_x = min(rect[0] for rect in option_rects)
    min_y = min(rect[1] for rect in option_rects) - 4
    max_y = bottom_limit if bottom_limit is not None else max(rect[3] for rect in option_rects) + 4
    prompts: list[dict[str, Any]] = []
    for block in page.text_blocks:
        text = str(block.get("text") or "")
        if not text or block in option_blocks or "\uf071" in text:
            continue
        x0, y0, x1, y1 = _rect(block)
        block_mid_y = (y0 + y1) / 2
        if x1 < min_x - 20 and min_y <= block_mid_y <= max_y:
            prompts.append(block)
    return prompts


def _radio_pair_hints(page: PageStructure) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for block in page.text_blocks:
        text = str(block.get("text") or "")
        if not any(marker in text for marker in ("\uf071", "☐", "□", "☑")):
            continue
        for pair in _inline_choice_pairs(text):
            hints.append(
                {
                    "kind": "single_select_pair",
                    "page": page.page_index + 1,
                    "source_ids": [str(block.get("source_id"))],
                    "option_source_ids": [str(block.get("source_id"))],
                    "nearby_widget_ids": _nearby_widget_ids(page, _rect(block), x_tolerance=60),
                    "shared_prompt_text": pair["question_text"],
                    "option_labels": pair["option_labels"],
                    "why_it_matters": (
                        "The line has one prompt followed by peer option markers, so it should be "
                        "Radio Button, not separate standalone Checkbox rows."
                    ),
                }
            )

    option_blocks = _box_option_blocks(page)
    for idx, first in enumerate(option_blocks[:-1]):
        second = option_blocks[idx + 1]
        first_label = _strip_option_marker(str(first.get("text") or ""))
        second_label = _strip_option_marker(str(second.get("text") or ""))
        if not _is_binary_option_pair(first_label, second_label):
            continue
        first_rect = _rect(first)
        second_rect = _rect(second)
        if abs(first_rect[0] - second_rect[0]) > 8 or second_rect[1] - first_rect[1] > 24:
            continue
        next_pair_top: float | None = None
        for later in option_blocks[idx + 2 :]:
            if _canonical_option_label(str(later.get("text") or "")) != _canonical_option_label(first_label):
                continue
            later_rect = _rect(later)
            if abs(first_rect[0] - later_rect[0]) <= 8:
                next_pair_top = later_rect[1]
                break
        prompts = _prompt_blocks_left_of(
            page,
            [first, second],
            bottom_limit=(next_pair_top - 4 if next_pair_top is not None else None),
        )
        prompt_text = " ".join(str(block.get("text") or "") for block in prompts)
        hints.append(
            {
                "kind": "single_select_pair",
                "page": page.page_index + 1,
                "source_ids": [
                    *[str(block.get("source_id")) for block in prompts],
                    str(first.get("source_id")),
                    str(second.get("source_id")),
                ],
                "option_source_ids": [str(first.get("source_id")), str(second.get("source_id"))],
                "nearby_widget_ids": _nearby_widget_ids(
                    page,
                    [
                        min(first_rect[0], second_rect[0]),
                        min(first_rect[1], second_rect[1]),
                        max(first_rect[2], second_rect[2]),
                        max(first_rect[3], second_rect[3]),
                    ],
                ),
                "shared_prompt_text": _compact_text(prompt_text),
                "option_labels": [first_label, second_label],
                "why_it_matters": (
                    "The two adjacent option labels are peer options for one row prompt, so the row should be "
                    "Radio Button. Do not emit the option labels as separate Checkbox questions."
                ),
            }
        )
    return hints


def _checkbox_group_hints(page: PageStructure) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for idx, block in enumerate(page.text_blocks):
        prompt_text = str(block.get("text") or "")
        normalized = prompt_text.lower()
        if not (
            "check all" in normalized
            or "select all" in normalized
            or "choose all" in normalized
            or "mark all" in normalized
        ):
            continue
        prompt_rect = _rect(block)
        option_blocks: list[dict[str, Any]] = []
        for candidate in page.text_blocks[idx + 1 :]:
            candidate_text = str(candidate.get("text") or "")
            candidate_rect = _rect(candidate)
            if candidate_rect[1] < prompt_rect[3] or candidate_rect[1] - prompt_rect[3] > 240:
                continue
            if "\uf071" not in candidate_text:
                if option_blocks:
                    break
                continue
            option_blocks.append(candidate)
        if len(option_blocks) < 2:
            continue
        source_ids = [str(block.get("source_id")), *[str(item.get("source_id")) for item in option_blocks]]
        option_source_ids = [str(item.get("source_id")) for item in option_blocks]
        option_labels = [_strip_option_marker(str(item.get("text") or "")) for item in option_blocks]
        group_rect = [
            min(_rect(item)[0] for item in option_blocks),
            min(_rect(item)[1] for item in option_blocks),
            max(_rect(item)[2] for item in option_blocks),
            max(_rect(item)[3] for item in option_blocks),
        ]
        hints.append(
            {
                "kind": "multi_select_group",
                "page": page.page_index + 1,
                "source_ids": source_ids,
                "option_source_ids": option_source_ids,
                "nearby_widget_ids": _nearby_widget_ids(page, group_rect, x_tolerance=16, y_tolerance=150),
                "shared_prompt_text": prompt_text,
                "option_labels": option_labels,
                "why_it_matters": (
                    "The prompt asks the user to select multiple independent options. These are one "
                    "Checkbox Group with option labels in Answer Text, not separate Checkbox rows."
                ),
            }
        )
    return hints


def _standalone_checkbox_hints(page: PageStructure, used_source_ids: set[str]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for block in _box_option_blocks(page):
        source_id = str(block.get("source_id") or "")
        if source_id in used_source_ids:
            continue
        label = _strip_option_marker(str(block.get("text") or ""))
        if _is_option_only_label(label):
            continue
        hints.append(
            {
                "kind": "standalone_checkbox_candidate",
                "page": page.page_index + 1,
                "source_ids": [source_id],
                "nearby_widget_ids": _nearby_widget_ids(page, _rect(block)),
                "label_text": label,
                "why_it_matters": (
                    "Only use Checkbox when this label is a standalone declaration, not one option "
                    "inside a radio pair or checkbox group."
                ),
            }
        )
    return hints


def _policy_pattern_hints(doc: DocStructure) -> dict[str, Any]:
    radio_pairs: list[dict[str, Any]] = []
    checkbox_groups: list[dict[str, Any]] = []
    standalone_checkboxes: list[dict[str, Any]] = []
    for page in doc.pages:
        page_radio_pairs = _radio_pair_hints(page)
        page_checkbox_groups = _checkbox_group_hints(page)
        used_source_ids = {
            source_id
            for hint in [*page_radio_pairs, *page_checkbox_groups]
            for source_id in hint.get("source_ids", [])
        }
        radio_pairs.extend(page_radio_pairs)
        checkbox_groups.extend(page_checkbox_groups)
        standalone_checkboxes.extend(_standalone_checkbox_hints(page, used_source_ids))
    return {
        "single_select_pair_candidates": radio_pairs[:20],
        "multi_select_group_candidates": checkbox_groups[:12],
        "standalone_checkbox_candidates": standalone_checkboxes[:12],
        "field_candidates": _field_candidate_hints(doc),
        "standalone_checkbox_note": (
            "If standalone_checkbox_candidates is empty, do not include Checkbox unless the page images "
            "show a clear standalone binary declaration that the text/widget hints missed."
        ),
    }


def _policy_page_evidence(doc: DocStructure) -> list[dict[str, Any]]:
    repeated_lines = _repeated_page_lines(doc)
    return [
        {
            "page": page.page_index + 1,
            "text_excerpt": _page_profile_excerpt(page, 1800),
            "candidate_section_headings": _heading_candidates(page, repeated_lines),
            "text_blocks": _policy_block_sample(page, "text_blocks", 55, 280),
            "widgets": _policy_block_sample(page, "widgets", 40, 220),
            "text_block_count": len(page.text_blocks),
            "widget_count": len(page.widgets),
        }
        for page in doc.pages
    ]


def _policy_image_pages(
    doc: DocStructure,
    sections: list[SectionProfile],
    max_images: int,
) -> list[PageStructure]:
    if max_images <= 0:
        return []
    if doc.page_count <= max_images:
        return doc.pages

    page_numbers: list[int] = [1, doc.page_count]
    for section in sections:
        if section.pages:
            page_numbers.append(min(section.pages))
            page_numbers.append(max(section.pages))

    if len(set(page_numbers)) < max_images:
        step = max(1, doc.page_count // max_images)
        page_numbers.extend(range(1, doc.page_count + 1, step))

    selected: list[PageStructure] = []
    seen: set[int] = set()
    by_page = {page.page_index + 1: page for page in doc.pages}
    for page_no in page_numbers:
        if page_no in seen or page_no not in by_page:
            continue
        selected.append(by_page[page_no])
        seen.add(page_no)
        if len(selected) >= max_images:
            break
    return selected


def _policy_prompt(
    doc: DocStructure,
    profile: DocumentProfile,
    sections: list[SectionProfile],
    image_pages: list[PageStructure],
) -> str:
    computed_hints = _policy_pattern_hints(doc)
    evidence = {
        "pdf_path": doc.pdf_path,
        "page_count": doc.page_count,
        "has_acroform": doc.has_acroform,
        "profile": profile.model_dump(),
        "planned_sections": [section.model_dump() for section in sections],
        "image_pages": [page.page_index + 1 for page in image_pages],
        "pages": _policy_page_evidence(doc),
        "computed_pattern_hints": computed_hints,
        "computed_field_candidates": computed_hints.get("field_candidates", []),
    }
    return (
        "You are creating an extraction policy for a healthcare PDF form.\n"
        "Your job is NOT to extract final rows. Your job is to explain how this specific PDF "
        "represents the fields that a later section extractor must produce.\n"
        "The section extractor will receive page images, text_blocks with source_ids, widgets "
        "with bboxes/names/types, document context, and your policy. Write the policy so it "
        "teaches that agent how this PDF's fields look in those inputs.\n"
        "Read the whole document evidence below. The page evidence covers every page; selected "
        "page images are attached only to help with layout clues.\n\n"
        "Only describe how input fields and static content are presented in this PDF. Do not teach the "
        "taxonomy and do not extract workbook rows.\n"
        "Give the next agent concrete things to look for: label and blank positions, widget/name clues, "
        "underlines, boxes, circles, table/grid structure, repeated rows, section-local exceptions, and page chrome to ignore.\n"
        "For option controls, explain where the shared question/prompt appears, where printed answer options appear, "
        "how options are separated, and whether options sit inline, below, to the side, or inside a table.\n"
        "For conditional/skip/applicability text, describe where that wording appears relative to the field it controls.\n"
        "Use page numbers, source_ids, widget ids, bboxes, and visible text examples whenever they help locate the pattern.\n"
        "Use computed_pattern_hints and computed_field_candidates only as evidence about the PDF layout; the next "
        "agent owns final taxonomy and Excel-column decisions.\n"
        "Populate the policy object as compact PDF-observation notes. In question_type_guidance, use the closest "
        "schema question_type name as a loose bucket for each observed pattern, but keep instruction, "
        "question_text_instruction, answer_text_instruction, distinguishing_features, and examples focused on "
        "where fields/options appear in this PDF.\n\n"
        f"Document evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
    )


def _guidance_by_type(policy: ExtractionPolicy) -> dict[str, Any]:
    return {str(guidance.question_type): guidance for guidance in policy.policy.question_type_guidance}


def _example_source_ids(guidance: Any) -> set[str]:
    return {
        source_id
        for example in getattr(guidance, "examples", [])
        for source_id in getattr(example, "source_ids", [])
        if source_id
    }


def _example_visible_key(text: str) -> str:
    return _compact_text(_strip_option_marker(text)).lower()


def _checkbox_example_is_option_only(visible_text: str) -> bool:
    labels = [
        _strip_option_marker(line)
        for line in visible_text.splitlines()
        if _strip_option_marker(line)
    ]
    return bool(labels) and all(_is_option_only_label(label) for label in labels)


def _hint_sources(pattern_hints: dict[str, Any] | None, key: str, *, options_only: bool = False) -> set[str]:
    if not pattern_hints:
        return set()
    source_key = "option_source_ids" if options_only else "source_ids"
    return {
        source_id
        for hint in pattern_hints.get(key, [])
        for source_id in hint.get(source_key, [])
        if source_id
    }


def _field_candidate_types(pattern_hints: dict[str, Any] | None) -> set[str]:
    if not pattern_hints:
        return set()
    return {
        str(candidate.get("question_type") or "")
        for candidate in pattern_hints.get("field_candidates", [])
        if candidate.get("question_type")
    }


def _is_generic_instruction(value: str) -> bool:
    normalized = _compact_text(value).lower().rstrip(".")
    return normalized in {
        "use the visible label text",
        "use the shared prompt text",
        "collect all option labels into answer text separated by two newlines",
        "collect all option labels into answer text separated by exactly two newlines",
    }


def _policy_quality_issues(
    policy: ExtractionPolicy,
    pattern_hints: dict[str, Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    choice_types = {"Checkbox", "Checkbox Group", "Radio Button"}
    by_type = _guidance_by_type(policy)
    policy_types = set(policy.policy.question_types)
    for candidate_type in sorted(_field_candidate_types(pattern_hints)):
        if candidate_type == "Checkbox":
            continue
        if candidate_type not in policy_types:
            issues.append(
                f"{candidate_type} field candidates exist in computed_field_candidates but {candidate_type} "
                "is missing from policy.question_types."
            )
    single_select_sources = _hint_sources(pattern_hints, "single_select_pair_candidates")
    single_select_option_sources = _hint_sources(
        pattern_hints, "single_select_pair_candidates", options_only=True
    )
    checkbox_group_sources = _hint_sources(pattern_hints, "multi_select_group_candidates")
    checkbox_group_option_sources = _hint_sources(
        pattern_hints, "multi_select_group_candidates", options_only=True
    )
    seen_sources: dict[tuple[str, ...], str] = {}
    seen_visible_text: dict[str, str] = {}
    for guidance in policy.policy.question_type_guidance:
        question_type = str(guidance.question_type)
        if _is_generic_instruction(guidance.question_text_instruction):
            issues.append(
                f"{question_type} question_text_instruction is generic. It must reference this PDF's "
                "source_ids, labels, widgets, or layout clues."
            )
        if question_type in {"Radio Button", "Checkbox Group", "Checkbox"} and _is_generic_instruction(
            guidance.answer_text_instruction
        ):
            issues.append(
                f"{question_type} answer_text_instruction is generic. It must name this PDF's option pattern "
                "and examples."
            )
        for example in guidance.examples:
            source_key = tuple(sorted(source_id for source_id in example.source_ids if source_id))
            if source_key:
                previous_type = seen_sources.get(source_key)
                if (
                    previous_type
                    and previous_type != question_type
                    and previous_type in choice_types
                    and question_type in choice_types
                ):
                    issues.append(
                        f"{question_type} reuses example source_ids {list(source_key)} already used by {previous_type}."
                    )
                seen_sources[source_key] = question_type
            visible_key = _example_visible_key(example.visible_text)
            if visible_key:
                previous_type = seen_visible_text.get(visible_key)
                if (
                    previous_type
                    and previous_type != question_type
                    and previous_type in choice_types
                    and question_type in choice_types
                ):
                    issues.append(
                        f"{question_type} reuses visible example text '{visible_key[:80]}' already used by {previous_type}."
                    )
                seen_visible_text[visible_key] = question_type

    checkbox_guidance = by_type.get("Checkbox")
    if checkbox_guidance:
        checkbox_sources = _example_source_ids(checkbox_guidance)
        overlap = sorted(checkbox_sources & (single_select_sources | checkbox_group_sources))
        if overlap:
            issues.append(
                f"Checkbox examples use source_ids {overlap} that computed layout hints classified as "
                "single-select or checkbox-group controls. A Checkbox example must be a unique standalone "
                "declaration."
            )
        for choice_type in ("Radio Button", "Checkbox Group"):
            choice_guidance = by_type.get(choice_type)
            if not choice_guidance:
                continue
            overlap = sorted(checkbox_sources & _example_source_ids(choice_guidance))
            if overlap:
                issues.append(
                    f"Checkbox examples overlap {choice_type} examples at source_ids {overlap}. "
                    "A Checkbox example must be a unique standalone declaration."
                )
        for example in checkbox_guidance.examples:
            if _checkbox_example_is_option_only(example.visible_text):
                issues.append(
                    "Checkbox uses only standalone option labels as its example. "
                    "Those should be Radio Button peer options unless there is a full standalone declaration."
                )
    radio_guidance = by_type.get("Radio Button")
    if radio_guidance and single_select_option_sources:
        radio_sources = _example_source_ids(radio_guidance)
        if not radio_sources & single_select_option_sources:
            issues.append(
                "Radio Button examples do not include the option source_ids identified in computed "
                "single_select_pair_candidates."
            )
    checkbox_group_guidance = by_type.get("Checkbox Group")
    if checkbox_group_guidance and checkbox_group_option_sources:
        checkbox_group_example_sources = _example_source_ids(checkbox_group_guidance)
        if not checkbox_group_example_sources & checkbox_group_option_sources:
            issues.append(
                "Checkbox Group examples do not include the option source_ids identified in computed "
                "multi_select_group_candidates."
            )
    return issues


def _all_checkbox_examples_unsafe(
    policy: ExtractionPolicy,
    pattern_hints: dict[str, Any] | None = None,
) -> bool:
    checkbox_guidance = _guidance_by_type(policy).get("Checkbox")
    if not checkbox_guidance or not checkbox_guidance.examples:
        return False
    unsafe_sources = _hint_sources(pattern_hints, "single_select_pair_candidates") | _hint_sources(
        pattern_hints, "multi_select_group_candidates"
    )
    return all(
        _checkbox_example_is_option_only(example.visible_text)
        or bool(set(example.source_ids) & unsafe_sources)
        for example in checkbox_guidance.examples
    )


def _cleanup_ambiguous_policy(
    policy: ExtractionPolicy,
    issues: list[str],
    pattern_hints: dict[str, Any] | None = None,
) -> tuple[ExtractionPolicy, list[str]]:
    cleaned = policy.model_copy(deep=True)
    cleanup_notes: list[str] = []
    has_checkbox_issue = any("Checkbox" in issue for issue in issues)
    if has_checkbox_issue and _all_checkbox_examples_unsafe(cleaned, pattern_hints):
        cleaned.policy.question_types = [
            question_type for question_type in cleaned.policy.question_types if question_type != "Checkbox"
        ]
        cleaned.policy.question_type_guidance = [
            guidance for guidance in cleaned.policy.question_type_guidance if str(guidance.question_type) != "Checkbox"
        ]
        note = (
            "No standalone Checkbox pattern was confirmed. Classify peer-option controls as "
            "Radio Button, and classify shared multi-select option lists as Checkbox Group."
        )
        if note not in cleaned.policy.global_instructions:
            cleaned.policy.global_instructions.append(note)
        cleaned.warnings.append("Removed ambiguous Checkbox guidance because its examples were only peer option labels.")
        cleanup_notes.append("removed_ambiguous_checkbox")
    return cleaned, cleanup_notes


def _ensure_question_type_guidance(policy: ExtractionPolicy, question_type: str) -> QuestionTypePolicy:
    if question_type not in policy.policy.question_types:
        policy.policy.question_types.append(question_type)
    for guidance in policy.policy.question_type_guidance:
        if str(guidance.question_type) == question_type:
            return guidance
    guidance = QuestionTypePolicy(question_type=question_type)
    policy.policy.question_type_guidance.append(guidance)
    return guidance


def _append_unique(values: list[str], additions: list[str]) -> None:
    seen = {_compact_text(value).lower() for value in values}
    for addition in additions:
        key = _compact_text(addition).lower()
        if key and key not in seen:
            values.append(addition)
            seen.add(key)


def _hint_source_summary(hint: dict[str, Any]) -> str:
    return ", ".join(str(source_id) for source_id in hint.get("source_ids", []) if source_id)


def _hint_answer_text(labels: list[str]) -> str:
    return "\n\n".join(label for label in labels if label)


def _example_from_hint(hint: dict[str, Any]) -> QuestionTypeExample:
    labels = [str(label) for label in hint.get("option_labels", []) if label]
    shared_prompt = str(hint.get("shared_prompt_text") or "").strip()
    visible_text = "\n".join(part for part in [shared_prompt, *labels] if part)
    return QuestionTypeExample(
        page=hint.get("page"),
        source_ids=[str(source_id) for source_id in hint.get("source_ids", []) if source_id],
        nearby_widget_ids=[str(source_id) for source_id in hint.get("nearby_widget_ids", []) if source_id],
        visible_text=visible_text,
        why_this_type=str(hint.get("why_it_matters") or ""),
        question_text_rule=(
            f"Use the shared prompt text '{shared_prompt}' as question_text; do not include the option labels "
            "inside question_text."
            if shared_prompt
            else "Use the shared prompt or row text before the option labels as question_text."
        ),
        answer_text_rule=(
            f"Use printed option labels exactly as Answer Text: {_hint_answer_text(labels)}"
            if labels
            else "Use the printed option labels as Answer Text."
        ),
    )


def _merge_hint_examples(guidance: QuestionTypePolicy, hints: list[dict[str, Any]], limit: int = 4) -> None:
    existing = {tuple(example.source_ids) for example in guidance.examples}
    for hint in hints[:limit]:
        example = _example_from_hint(hint)
        key = tuple(example.source_ids)
        if key and key not in existing:
            guidance.examples.append(example)
            existing.add(key)


def _example_from_field_candidate(candidate: dict[str, Any]) -> QuestionTypeExample:
    question_type = str(candidate.get("question_type") or "")
    question_text = str(candidate.get("question_text") or "")
    answer_text = str(candidate.get("answer_text") or "")
    if answer_text:
        answer_rule = f"Use printed option labels exactly as Answer Text: {answer_text}"
    elif question_type in {"Text Box", "Text Area", "Date", "Number", "Signature"}:
        answer_rule = "Leave Answer Text blank because this is a blank user-entry field."
    else:
        answer_rule = "Follow the type guidance for Answer Text."
    return QuestionTypeExample(
        page=candidate.get("page"),
        source_ids=[str(source_id) for source_id in candidate.get("source_ids", []) if source_id],
        nearby_widget_ids=[str(source_id) for source_id in candidate.get("nearby_widget_ids", []) if source_id],
        visible_text=str(candidate.get("visible_text") or question_text),
        why_this_type=str(candidate.get("reason") or ""),
        question_text_rule=f"Use '{question_text}' as Question Text.",
        answer_text_rule=answer_rule,
    )


def _candidate_summary(candidate: dict[str, Any]) -> str:
    source_ids = ", ".join(str(source_id) for source_id in candidate.get("source_ids", []) if source_id)
    return (
        f"Page {candidate.get('page')}, {candidate.get('question_text')} "
        f"from source_ids {source_ids}"
    )


def _apply_field_candidates_to_policy(
    policy: ExtractionPolicy,
    field_candidates: list[dict[str, Any]],
) -> list[str]:
    notes: list[str] = []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for candidate in field_candidates:
        question_type = str(candidate.get("question_type") or "")
        if not question_type or question_type == "Checkbox":
            continue
        by_type.setdefault(question_type, []).append(candidate)

    for question_type, candidates in by_type.items():
        candidates = sorted(
            candidates,
            key=lambda candidate: (
                str(candidate.get("question_text") or "") == "Date",
                int(candidate.get("page") or 0),
                str(candidate.get("question_text") or ""),
            ),
        )
        guidance = _ensure_question_type_guidance(policy, question_type)
        if question_type in {"Text Box", "Date", "Number", "Signature"}:
            guidance.examples = []
        _append_unique(guidance.where_it_appears, [_candidate_summary(candidate) for candidate in candidates[:10]])
        if question_type == "Date":
            guidance.instruction = (
                "Classify labels that ask for a date as Date. Repeated short Date labels should use nearby "
                "visible context so the next agent can produce distinct rows."
            )
            guidance.question_text_instruction = (
                "Use the visible date label as Question Text. For repeated date labels, include the nearest "
                "row or signer context when needed to avoid duplicate-looking rows."
            )
            guidance.answer_text_instruction = "Leave Answer Text blank for blank date-entry fields."
            _append_unique(
                guidance.distinguishing_features,
                [
                    "Date labels sit beside short underline/text widgets and are not generic Text Box fields.",
                    "Repeated date labels need nearby row context to avoid duplicate-looking rows.",
                ],
            )
        elif question_type == "Number":
            guidance.instruction = "Classify fields whose label expects numeric input as Number, not Text Box."
            guidance.question_text_instruction = "Use the printed numeric label as Question Text."
            guidance.answer_text_instruction = "Leave Answer Text blank for blank numeric-entry fields."
            _append_unique(
                guidance.distinguishing_features,
                ["Short underline/text widgets whose visible label expects numeric input."],
            )
        elif question_type == "Text Box":
            guidance.instruction = (
                "Classify compact one-line blanks as Text Box. Split same-line labels into separate rows instead "
                "of keeping the whole line as one field."
            )
            guidance.question_text_instruction = (
                "Use each printed label before its own blank as Question Text, such as State or County."
            )
            guidance.answer_text_instruction = "Leave Answer Text blank for blank short text-entry fields."
            _append_unique(
                guidance.distinguishing_features,
                ["One-line blanks or text widgets beside labels, including compact same-line fields."],
            )
        elif question_type == "Signature":
            guidance.instruction = "Classify signer underlines as Signature when the label names a signer."
            guidance.question_text_instruction = (
                "Use the signer label near the underline as Question Text."
            )
            guidance.answer_text_instruction = "Leave Answer Text blank for blank signature lines."
            _append_unique(
                guidance.distinguishing_features,
                ["Long signature underline with signer label below it; separate nearby Date fields as Date rows."],
            )
        elif question_type == "Text Area":
            guidance.instruction = "Classify large multi-line fillable boxes as Text Area."
            guidance.question_text_instruction = "Use the visible prompt above the multi-line response area."
            guidance.answer_text_instruction = "Leave Answer Text blank for blank long-response fields."
            _append_unique(
                guidance.distinguishing_features,
                ["Large multi-line text widget or response box, not a compact one-line blank."],
            )

        existing = {tuple(example.source_ids) for example in guidance.examples}
        for candidate in candidates[:5]:
            example = _example_from_field_candidate(candidate)
            key = tuple(example.source_ids)
            if key and key not in existing:
                guidance.examples.append(example)
                existing.add(key)
        notes.append(f"enriched_{_dedupe_key(question_type).replace(' ', '_')}_from_field_candidates")
    return notes


def _apply_pattern_hints_to_policy(
    policy: ExtractionPolicy,
    pattern_hints: dict[str, Any],
) -> tuple[ExtractionPolicy, list[str]]:
    enriched = policy.model_copy(deep=True)
    notes: list[str] = []
    radio_hints = pattern_hints.get("single_select_pair_candidates", [])
    if radio_hints:
        radio = _ensure_question_type_guidance(enriched, "Radio Button")
        first = radio_hints[0]
        _append_unique(
            radio.where_it_appears,
            [
                f"Page {hint.get('page')}, source_ids {_hint_source_summary(hint)}"
                for hint in radio_hints[:6]
            ],
        )
        _append_unique(
            radio.distinguishing_features,
            [
                "This PDF draws some single-select controls as square boxes; the deciding clue is the peer "
                "option set under one shared prompt.",
                "Do not treat the visible box glyph or CheckBox widget type alone as Checkbox.",
            ],
        )
        _append_unique(
            radio.do_not_confuse_with,
            [
                "Checkbox: a standalone declaration with no peer option labels.",
                "Checkbox Group: many independent options under a check-all/select-all prompt.",
            ],
        )
        radio.instruction = (
            "Classify source-id clusters from computed single_select_pair_candidates as Radio Button."
        )
        radio.question_text_instruction = (
            "Use the shared label, row label, or left-side prompt as question_text. Do not include the "
            "printed option labels inside question_text."
        )
        first_labels = [str(label) for label in first.get("option_labels", []) if label]
        radio.answer_text_instruction = (
            "Use only the printed peer option labels as Answer Text, joined by exactly two newlines. "
            f"One detected option pattern uses {_hint_answer_text(first_labels)} at source_ids "
            f"{_hint_source_summary(first)}."
        )
        _merge_hint_examples(radio, radio_hints)
        notes.append("enriched_radio_button_from_pattern_hints")

    checkbox_group_hints = pattern_hints.get("multi_select_group_candidates", [])
    if checkbox_group_hints:
        group = _ensure_question_type_guidance(enriched, "Checkbox Group")
        first = checkbox_group_hints[0]
        option_labels = [str(label) for label in first.get("option_labels", []) if label]
        _append_unique(
            group.where_it_appears,
            [
                f"Page {hint.get('page')}, source_ids {_hint_source_summary(hint)}"
                for hint in checkbox_group_hints[:4]
            ],
        )
        _append_unique(
            group.distinguishing_features,
            [
                "This PDF's Checkbox Group is a shared check-all prompt followed by multiple independent "
                "option boxes.",
                "The group prompt is the question_text; the individual option labels are Answer Text options.",
            ],
        )
        _append_unique(
            group.do_not_confuse_with,
            [
                "Radio Button: peer options where one option should be selected.",
                "Checkbox: one standalone declaration, not a list under a shared prompt.",
            ],
        )
        group.instruction = (
            "Classify shared multi-select option lists as Checkbox Group, not separate Checkbox rows."
        )
        group.question_text_instruction = (
            "Use the shared group prompt as question_text."
        )
        group.answer_text_instruction = (
            "Use the printed option labels from the following option source_ids as Answer Text, joined by "
            f"exactly two newlines: {_hint_answer_text(option_labels)}"
        )
        _merge_hint_examples(group, checkbox_group_hints)
        notes.append("enriched_checkbox_group_from_pattern_hints")

    standalone_checkbox_hints = pattern_hints.get("standalone_checkbox_candidates", [])
    if not standalone_checkbox_hints and "Checkbox" in enriched.policy.question_types:
        enriched.policy.question_types = [
            question_type for question_type in enriched.policy.question_types if question_type != "Checkbox"
        ]
        enriched.policy.question_type_guidance = [
            guidance for guidance in enriched.policy.question_type_guidance if str(guidance.question_type) != "Checkbox"
        ]
        note = (
            "No standalone Checkbox pattern was confirmed by computed PDF hints; use Radio Button for peer "
            "option controls and Checkbox Group for shared multi-select lists."
        )
        if note not in enriched.policy.global_instructions:
            enriched.policy.global_instructions.append(note)
        notes.append("removed_checkbox_without_standalone_hint")
    notes.extend(_apply_field_candidates_to_policy(enriched, pattern_hints.get("field_candidates", [])))
    return enriched, notes


def _policy_repair_prompt(
    doc: DocStructure,
    profile: DocumentProfile,
    sections: list[SectionProfile],
    draft_policy: ExtractionPolicy,
    quality_issues: list[str],
) -> str:
    computed_hints = _policy_pattern_hints(doc)
    evidence = {
        "pdf_path": doc.pdf_path,
        "page_count": doc.page_count,
        "profile": profile.model_dump(),
        "planned_sections": [section.model_dump() for section in sections],
        "computed_pattern_hints": computed_hints,
        "computed_field_candidates": computed_hints.get("field_candidates", []),
        "pages": _policy_page_evidence(doc),
    }
    return (
        "The draft PDF field-observation policy failed quality checks. Repair it before the section "
        "extractor sees it. This policy should describe how fields, prompts, options, widgets, "
        "branches, repeated structures, and ignoreable page chrome appear in this PDF; it should not "
        "teach taxonomy definitions or extract workbook rows.\n\n"
        "Quality issues:\n"
        f"{json.dumps(quality_issues, ensure_ascii=False, indent=2)}\n\n"
        "Repair rules:\n"
        "- Return a complete ExtractionPolicy JSON object, not only a patch.\n"
        "- Keep every instruction anchored to this PDF's source_ids, prompt text, option text, widget clues, "
        "page areas, and layout.\n"
        "- If a visual pattern is ambiguous, describe the ambiguity and the location instead of adding generic definitions.\n"
        "- For option controls, say where the shared question is and where the printed answer options are.\n"
        "- Do not reuse the same source_ids or visible_text as examples for different visual patterns.\n"
        "- Keep the policy compact. The section extractor receives taxonomy and Excel-column rules separately.\n\n"
        f"Draft policy:\n{draft_policy.model_dump_json(indent=2)}\n\n"
        f"Document evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
    )


def _repair_extraction_policy(
    client: Any,
    doc: DocStructure,
    profile: DocumentProfile,
    sections: list[SectionProfile],
    image_pages: list[PageStructure],
    draft_policy: ExtractionPolicy,
    quality_issues: list[str],
    config: AgentConfig,
) -> tuple[ExtractionPolicy, dict[str, Any]]:
    content = _langchain_content(
        [page.image_path for page in image_pages],
        _policy_repair_prompt(doc, profile, sections, draft_policy, quality_issues),
    )
    parsed, telemetry = _invoke_langchain_json_model(
        client,
        model_id=config.policy_model_id,
        response_model=ExtractionPolicy,
        content=content,
        max_tokens=min(config.max_tokens, 7000),
        temperature=config.temperature,
        instruction="Repair and return the PDF-specific extraction policy as JSON.",
    )
    telemetry["stage"] = "repair_extraction_policy"
    return parsed, telemetry


def build_extraction_policy(
    client: Any,
    doc: DocStructure,
    profile: DocumentProfile,
    sections: list[SectionProfile],
    config: AgentConfig,
) -> tuple[ExtractionPolicy, dict[str, Any]]:
    image_pages = _policy_image_pages(doc, sections, config.max_policy_images)
    pattern_hints = _policy_pattern_hints(doc)
    content = _langchain_content(
        [page.image_path for page in image_pages],
        _policy_prompt(doc, profile, sections, image_pages),
    )
    parsed, telemetry = _invoke_langchain_json_model(
        client,
        model_id=config.policy_model_id,
        response_model=ExtractionPolicy,
        content=content,
        max_tokens=min(config.max_tokens, 7000),
        temperature=config.temperature,
        instruction="Return the PDF-specific extraction policy as JSON.",
    )
    telemetry["stage"] = "build_extraction_policy"
    telemetry["image_pages"] = [page.page_index + 1 for page in image_pages]
    telemetry["section_policy_count"] = len(parsed.policy.section_guidance)
    quality_issues = _policy_quality_issues(parsed, pattern_hints)
    telemetry["policy_quality_issues"] = quality_issues
    telemetry["policy_repair_attempted"] = bool(quality_issues)
    if quality_issues:
        repaired, repair_telemetry = _repair_extraction_policy(
            client,
            doc,
            profile,
            sections,
            image_pages,
            parsed,
            quality_issues,
            config,
        )
        repaired_issues = _policy_quality_issues(repaired, pattern_hints)
        parsed, cleanup_notes = _cleanup_ambiguous_policy(repaired, repaired_issues, pattern_hints)
        telemetry["policy_repair_telemetry"] = repair_telemetry
        telemetry["post_repair_quality_issues"] = repaired_issues
        telemetry["policy_cleanup_notes"] = cleanup_notes
    parsed, hint_notes = _apply_pattern_hints_to_policy(parsed, pattern_hints)
    telemetry["policy_hint_notes"] = hint_notes
    telemetry["final_policy_quality_issues"] = _policy_quality_issues(parsed, pattern_hints)
    return parsed, telemetry


def _policy_for_section(policy: ExtractionPolicy, section: SectionProfile) -> dict[str, Any]:
    prompt_policy = policy.policy
    section_key = _section_key(section.name)
    page_set = set(section.pages)
    matching_section_guidance = [
        guidance.model_dump()
        for guidance in prompt_policy.section_guidance
        if (
            _section_key(guidance.section_name) == section_key
            or bool(page_set.intersection(guidance.pages))
        )
    ]
    if not matching_section_guidance:
        matching_section_guidance = [
            guidance.model_dump() for guidance in prompt_policy.section_guidance
        ]
    return {
        "question_types": prompt_policy.question_types,
        "excel_columns": prompt_policy.excel_columns,
        "global_instructions": prompt_policy.global_instructions,
        "ignore_patterns": prompt_policy.ignore_patterns,
        "question_type_guidance": [
            guidance.model_dump() for guidance in prompt_policy.question_type_guidance
        ],
        "field_guidance": [guidance.model_dump() for guidance in prompt_policy.field_guidance],
        "section_guidance": matching_section_guidance,
    }


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
    extraction_policy: ExtractionPolicy,
    section: SectionProfile,
    pages: list[PageStructure],
    chunk_index: int,
    chunk_count: int,
) -> str:
    page_numbers = [page.page_index + 1 for page in pages]
    return _SECTION_EXTRACTOR_PROMPT.substitute(
        unsectioned_content=repr(UNSECTIONED_CONTENT_NAME),
        question_type_taxonomy=json.dumps(
            CANONICAL_QUESTION_TYPE_TAXONOMY,
            ensure_ascii=False,
            indent=2,
        ),
        excel_field_taxonomy=json.dumps(
            CANONICAL_OUTPUT_FIELD_TAXONOMY,
            ensure_ascii=False,
            indent=2,
        ),
        canonical_question_types=json.dumps(
            [item["question_type"] for item in CANONICAL_QUESTION_TYPE_TAXONOMY],
            ensure_ascii=False,
        ),
        canonical_excel_columns=json.dumps(CANONICAL_EXCEL_COLUMNS, ensure_ascii=False),
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
        extraction_policy=json.dumps(
            _policy_for_section(extraction_policy, section),
            ensure_ascii=False,
            indent=2,
        ),
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
    extraction_policy: ExtractionPolicy,
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
        content = _langchain_content(
            [page.image_path for page in pages],
            _extractor_prompt(
                doc,
                profile,
                extraction_policy,
                section,
                pages,
                chunk_index,
                len(chunks),
            ),
        )
        parsed, tele = _invoke_langchain_json_model(
            client,
            model_id=model_id,
            response_model=SectionExtraction,
            content=content,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            instruction="Return extracted canonical rows for this section or section page chunk as JSON.",
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


def _graph_build_extraction_policy(
    state: ExtractionGraphState,
    runtime: Runtime[ExtractionGraphContext],
) -> dict[str, Any]:
    progress = runtime.context.get("progress")
    if progress:
        progress("policy", "Building PDF-specific extraction policy...", None, None)
    policy, policy_telemetry = build_extraction_policy(
        runtime.context["client"],
        state["doc"],
        state["profile"],
        state.get("sections", []),
        runtime.context["config"],
    )
    warnings = [f"Policy: {warning}" for warning in policy.warnings]
    return {
        "extraction_policy": policy,
        "telemetry": [*state.get("telemetry", []), policy_telemetry],
        "warnings": [*state.get("warnings", []), *warnings],
    }


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
        state["extraction_policy"],
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
    graph.add_node("build_extraction_policy", _graph_build_extraction_policy)
    graph.add_node("extract_section", _graph_extract_next_section)
    graph.add_node("normalize_rows", _graph_normalize_rows)
    graph.add_edge(START, "profile_document")
    graph.add_edge("profile_document", "plan_sections")
    graph.add_edge("plan_sections", "build_extraction_policy")
    graph.add_conditional_edges(
        "build_extraction_policy",
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
        extraction_policy=final_state["extraction_policy"],
        raw_rows=final_state.get("raw_rows", []),
        rows=final_state.get("rows", []),
        telemetry=final_state.get("telemetry", []),
        warnings=final_state.get("warnings", []),
    )
