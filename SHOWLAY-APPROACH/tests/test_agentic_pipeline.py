from __future__ import annotations

import json
import sys
from pathlib import Path

SHOWLAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHOWLAY_DIR))

from showlay.agentic import (  # noqa: E402
    AgentConfig,
    _extractor_prompt,
    _normalize_profile,
    _profile_prompt,
    document_extraction_graph,
    extract_document_agentic,
)
from showlay.canonical import DocumentProfile, SectionProfile  # noqa: E402
from showlay.extract import DocStructure, PageStructure  # noqa: E402


class FakeBedrockClient:
    def converse(self, **kwargs):
        tool_name = kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"]
        if tool_name == "record_document_profile":
            payload = {
                "form_title": "Sample Assessment",
                "form_version": "v1",
                "complexity": "medium",
                "sections": [{"name": "Section A", "pages": [1]}],
                "shared_legends": [],
                "extraction_strategy": "single_call",
                "recommended_model_tier": "low_cost",
            }
        else:
            payload = {
                "section_name": "Section A",
                "rows": [
                    {
                        "section": "Section A",
                        "question_type": "Radio Button",
                        "question_text": "B0100. Hearing",
                        "answer_text": "Yes\n\nNo",
                        "external_id": "B0100",
                        "branching_source": "If B0100 = Yes, skip to GG0115",
                        "page": 1,
                    },
                    {
                        "section": "Section A",
                        "question_type": "Text Box",
                        "question_text": "GG0115. Functional limitation",
                        "external_id": "GG0115",
                        "page": 1,
                    },
                ],
                "warnings": [],
            }
        return {
            "output": {"message": {"content": [{"toolUse": {"name": tool_name, "input": payload}}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }


class FakeRetrySectionClient:
    def __init__(self):
        self.section_calls = 0

    def converse(self, **kwargs):
        tool_config = kwargs.get("toolConfig") or {}
        tool_name = ((tool_config.get("tools") or [{}])[0].get("toolSpec") or {}).get("name")
        if tool_name == "record_document_profile":
            payload = {
                "form_title": "Sample Assessment",
                "form_version": "v1",
                "complexity": "medium",
                "sections": [{"name": "Section A", "pages": [1]}],
                "shared_legends": [],
                "extraction_strategy": "single_call",
                "recommended_model_tier": "low_cost",
            }
            return {
                "output": {"message": {"content": [{"toolUse": {"name": tool_name, "input": payload}}]}},
                "usage": {"inputTokens": 10, "outputTokens": 20},
            }

        self.section_calls += 1
        if self.section_calls == 1:
            return {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "I will return the extraction, but not as a tool."}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }

        payload = {
            "section_name": "Section A",
            "rows": [
                {
                    "section": "Section A",
                    "question_type": "Text Box",
                    "question_text": "Applicant Name",
                    "page": 1,
                }
            ],
            "warnings": [],
        }
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }


class ModelErrorException(Exception):
    pass


class FakeToolUseModelErrorClient(FakeRetrySectionClient):
    def converse(self, **kwargs):
        tool_config = kwargs.get("toolConfig") or {}
        tool_name = ((tool_config.get("tools") or [{}])[0].get("toolSpec") or {}).get("name")
        if tool_name == "record_section_extraction":
            self.section_calls += 1
            raise ModelErrorException("Model produced invalid sequence as part of ToolUse.")
        if tool_name == "record_document_profile":
            return super().converse(**kwargs)

        payload = {
            "section_name": "Section A",
            "rows": [
                {
                    "section": "Section A",
                    "question_type": "Text Box",
                    "question_text": "Applicant Name",
                    "page": 1,
                }
            ],
            "warnings": [],
        }
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "usage": {"inputTokens": 11, "outputTokens": 21},
        }


class FakeTokenLimitClient(FakeBedrockClient):
    def __init__(self):
        self.max_tokens_seen: list[int] = []

    def converse(self, **kwargs):
        self.max_tokens_seen.append(kwargs["inferenceConfig"]["maxTokens"])
        return super().converse(**kwargs)


def test_document_extraction_graph_is_compiled_langgraph():
    graph = document_extraction_graph.get_graph()

    assert hasattr(document_extraction_graph, "invoke")
    assert set(graph.nodes) >= {
        "__start__",
        "profile_document",
        "plan_sections",
        "extract_section",
        "normalize_rows",
        "__end__",
    }


def test_agent_config_defaults_to_nova_pro_for_section_extraction(monkeypatch):
    monkeypatch.delenv("BEDROCK_PROFILER_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_EXTRACTOR_DEFAULT_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_EXTRACTOR_COMPLEX_MODEL_ID", raising=False)

    config = AgentConfig()

    assert config.profiler_model_id == "us.amazon.nova-2-lite-v1:0"
    assert config.extractor_default_model_id == "us.amazon.nova-pro-v1:0"
    assert config.extractor_complex_model_id == "us.amazon.nova-pro-v1:0"


def test_agentic_pipeline_profiles_extracts_and_normalizes(tmp_path, monkeypatch):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=1,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[{"source_id": "T001_001", "text": "B0100. Hearing", "rect": [1, 2, 3, 4]}],
                widgets=[],
            )
        ],
    )
    monkeypatch.setattr("showlay.agentic._bedrock_runtime", lambda: FakeBedrockClient())

    result = extract_document_agentic(doc, config=AgentConfig(max_section_images=2))

    assert result.profile.form_title == "Sample Assessment"
    assert [row.sequence for row in result.rows] == [1, 2]
    assert result.rows[0].branching_logic == "If Q1 = Yes, skip to Q2"
    assert result.rows[0].question_rule == ""
    assert result.rows[1].answer_validation == "default characters = 100"


def test_profile_prompt_and_normalizer_reject_generic_header_sections(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=2,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {"source_id": "T001_001", "text": "Safety Determination Request Form", "rect": [1, 2, 3, 4]},
                    {"source_id": "T001_002", "text": "Total Acuity Score of PAE as submitted:", "rect": [1, 10, 3, 14]},
                    {"source_id": "T001_002", "text": "Current Living Arrangements:", "rect": [1, 20, 3, 24]},
                ],
                widgets=[],
            ),
            PageStructure(
                page_index=1,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {"source_id": "T002_001", "text": "Justification for Safety Determination Request:", "rect": [1, 2, 3, 4]},
                ],
                widgets=[],
            ),
        ],
    )
    prompt = _profile_prompt(doc)
    profile = DocumentProfile(
        form_title="Safety Determination Request Form",
        sections=[
            SectionProfile(name="Header and Introduction", pages=[1]),
            SectionProfile(name="Current Living Arrangements", pages=[1]),
            SectionProfile(name="Justification for Safety Determination Request", pages=[1, 2]),
        ],
        extraction_strategy="section_by_section",
    )

    normalized = _normalize_profile(profile, doc)

    assert "Header and Introduction" in prompt
    assert "Do NOT create sections for page chrome" in prompt
    assert "Unsectioned Content" in prompt
    assert [section.name for section in normalized.sections] == [
        "Unsectioned Content",
        "Current Living Arrangements",
        "Justification for Safety Determination Request",
    ]
    assert normalized.sections[0].pages == [1]


def test_profile_normalizer_adds_unsectioned_for_content_before_first_heading(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=1,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {"source_id": "T001_001", "text": "Safety Determination Request Form", "rect": [1, 2, 3, 4]},
                    {"source_id": "T001_002", "text": "Total Acuity Score of PAE as submitted:", "rect": [1, 10, 3, 14]},
                    {"source_id": "T001_003", "text": "Current Living Arrangements:", "rect": [1, 20, 3, 24]},
                ],
                widgets=[],
            ),
        ],
    )
    profile = DocumentProfile(
        form_title="Safety Determination Request Form",
        sections=[SectionProfile(name="Current Living Arrangements", pages=[1])],
        extraction_strategy="single_call",
    )

    normalized = _normalize_profile(profile, doc)

    assert [section.name for section in normalized.sections] == [
        "Unsectioned Content",
        "Current Living Arrangements",
    ]
    assert normalized.sections[0].pages == [1]


def test_profile_normalizer_keeps_every_page_covered(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=3,
        has_acroform=False,
        pages=[
            PageStructure(page_index=0, width=612, height=792, image_path=str(image_path), text_blocks=[], widgets=[]),
            PageStructure(page_index=1, width=612, height=792, image_path=str(image_path), text_blocks=[], widgets=[]),
            PageStructure(page_index=2, width=612, height=792, image_path=str(image_path), text_blocks=[], widgets=[]),
        ],
    )
    profile = DocumentProfile(
        form_title="Sample",
        sections=[
            SectionProfile(name="Current Living Arrangements", pages=[1]),
            SectionProfile(name="Recent Falls Information", pages=[3]),
        ],
        extraction_strategy="section_by_section",
    )

    normalized = _normalize_profile(profile, doc)
    covered = sorted({page for section in normalized.sections for page in section.pages})

    assert covered == [1, 2, 3]
    assert normalized.sections[0].name == "Unsectioned Content"
    assert normalized.sections[0].pages == [2]


def test_profile_normalizer_uses_unsectioned_content_when_no_sections_exist(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=1,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {"source_id": "T001_001", "text": "Sample Form Title", "rect": [1, 2, 3, 4]},
                    {"source_id": "T001_002", "text": "Applicant Name", "rect": [1, 20, 3, 24]},
                ],
                widgets=[],
            ),
        ],
    )
    profile = DocumentProfile(
        form_title="Sample Form Title",
        sections=[SectionProfile(name="Sample Form Title", pages=[1])],
        extraction_strategy="single_call",
    )

    normalized = _normalize_profile(profile, doc)

    assert [section.name for section in normalized.sections] == ["Unsectioned Content"]
    assert normalized.sections[0].pages == [1]


def test_agentic_pipeline_caps_bedrock_max_tokens(tmp_path, monkeypatch):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=1,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[{"source_id": "T001_001", "text": "Applicant Name", "rect": [1, 2, 3, 4]}],
                widgets=[],
            )
        ],
    )
    client = FakeTokenLimitClient()
    monkeypatch.setattr("showlay.agentic._bedrock_runtime", lambda: client)

    result = extract_document_agentic(doc, config=AgentConfig(max_tokens=12000))

    assert max(client.max_tokens_seen) == 9000
    assert any(item.get("requested_max_tokens") == 12000 for item in result.telemetry)


def test_agentic_pipeline_retries_when_section_tool_use_is_missing(tmp_path, monkeypatch):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=1,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[{"source_id": "T001_001", "text": "Applicant Name", "rect": [1, 2, 3, 4]}],
                widgets=[],
            )
        ],
    )
    client = FakeRetrySectionClient()
    monkeypatch.setattr("showlay.agentic._bedrock_runtime", lambda: client)

    result = extract_document_agentic(doc, config=AgentConfig(max_section_images=2))

    assert client.section_calls == 2
    assert result.rows[0].question_text == "Applicant Name"
    assert any(item.get("fallback_used") for item in result.telemetry)


def test_agentic_pipeline_falls_back_when_bedrock_tool_use_sequence_fails(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=1,
        has_acroform=False,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[{"source_id": "T001_001", "text": "Applicant Name", "rect": [1, 2, 3, 4]}],
                widgets=[],
            )
        ],
    )
    client = FakeToolUseModelErrorClient()
    monkeypatch.setattr("showlay.agentic._bedrock_runtime", lambda: client)

    result = extract_document_agentic(doc, config=AgentConfig(max_section_images=2))

    assert client.section_calls == 1
    assert result.rows[0].question_text == "Applicant Name"
    assert any(item.get("fallback_used") for item in result.telemetry)


def test_extractor_prompt_includes_question_type_definitions(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    page = PageStructure(
        page_index=0,
        width=612,
        height=792,
        image_path=str(image_path),
        text_blocks=[],
        widgets=[],
    )
    doc = DocStructure(pdf_path="/tmp/sample.pdf", page_count=1, has_acroform=False, pages=[page])
    section = SectionProfile(name="Section A", pages=[1])
    profile = DocumentProfile(form_title="Sample", sections=[section])

    prompt = _extractor_prompt(doc, profile, section, [page], 0, 1)

    assert "Text Box: short single-line free text field" in prompt
    assert "Radio Button: multiple options where the user should select exactly one answer" in prompt
    assert "Group Table: repeatable table/grid parent" in prompt
    assert "Dropdown /" not in prompt
    assert "Date /" not in prompt
    assert "Group Table /" not in prompt
    assert "- Section Header:" not in prompt
    assert "branching_source: when the PDF gives skip, display, or applicability wording" in prompt
    assert "external_id: when a printed item/code clearly identifies the row" in prompt
    assert "source_ids: list the T### and W### ids" in prompt
    assert "Do not create new workbook columns" in prompt
    assert "Branching Logic: use the workbook convention Q{sequence_number}" in prompt
    assert "Question Rule: the workbook description defines this as the requirement to show a question" in prompt
