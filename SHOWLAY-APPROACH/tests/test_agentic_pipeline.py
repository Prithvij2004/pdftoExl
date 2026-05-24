from __future__ import annotations

import json
import sys
from pathlib import Path

SHOWLAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHOWLAY_DIR))

from showlay.agentic import (  # noqa: E402
    AgentConfig,
    _adapt_tool_data,
    _apply_pattern_hints_to_policy,
    _cleanup_ambiguous_policy,
    _extractor_prompt,
    _field_candidate_hints,
    _json_from_text,
    _normalize_profile,
    _parse_model_json_output,
    _policy_pattern_hints,
    _policy_prompt,
    _policy_quality_issues,
    _profile_prompt,
    document_extraction_graph,
    extract_document_agentic,
)
from showlay.canonical import (  # noqa: E402
    DocumentProfile,
    ExtractionPolicy,
    SectionExtraction,
    SectionProfile,
)
from showlay.extract import DocStructure, PageStructure  # noqa: E402


def sample_policy_payload() -> dict:
    return {
        "form_summary": "TOP LEVEL SUMMARY SHOULD NOT REACH SECTION PROMPT",
        "policy": {
            "question_types": ["Radio Button", "Text Box"],
            "excel_columns": [
                "Section",
                "Question Type",
                "Question Text",
                "Answer Text",
                "Branching Logic",
            ],
            "global_instructions": [
                "Radio choices appear as option labels near small empty squares.",
                "Keep printed item codes in Question Text and external_id.",
            ],
            "ignore_patterns": ["Repeated page header"],
            "question_type_guidance": [
                {
                    "question_type": "Radio Button",
                    "pdf_clues": ["Yes and No peer options"],
                    "instruction": "Recognize a single-select group from peer option boxes near one prompt.",
                    "question_text_instruction": "Use the visible prompt before the Yes and No options.",
                    "answer_text_instruction": "Write printed option labels Yes and No separated by exactly two newlines.",
                    "branching_instruction": "Use visible skip wording to fill branching_source and Q refs when known.",
                    "where_it_appears": ["Page 1 near T001_001, T001_002, and T001_003."],
                    "distinguishing_features": [
                        "Yes and No are peer option labels under one shared prompt, not standalone prompts."
                    ],
                    "do_not_confuse_with": ["Do not emit Yes and No as separate Checkbox rows."],
                    "examples": [
                        {
                            "page": 1,
                            "source_ids": ["T001_001", "T001_002", "T001_003"],
                            "nearby_widget_ids": [],
                            "visible_text": "B0100. Hearing / Yes / No",
                            "why_this_type": "One prompt with mutually exclusive peer options.",
                            "question_text_rule": "Use B0100. Hearing as question_text.",
                            "answer_text_rule": "Use Yes\\n\\nNo as answer_text.",
                        }
                    ],
                },
                {
                    "question_type": "Display",
                    "pdf_clues": ["Instruction text with no input widget"],
                    "instruction": "Recognize visible instructions or explanatory text that has no user input.",
                    "question_text_instruction": "Use the instruction text itself as question_text.",
                    "answer_text_instruction": "Leave answer_text blank unless it is a New Section marker.",
                    "branching_instruction": "Use only if the display text is conditional.",
                    "where_it_appears": ["Page 1 instruction block T001_010."],
                    "distinguishing_features": ["No blank, widget, checkbox, or option labels nearby."],
                    "do_not_confuse_with": ["Do not treat instruction paragraphs as Text Area."],
                    "examples": [
                        {
                            "page": 1,
                            "source_ids": ["T001_010"],
                            "nearby_widget_ids": [],
                            "visible_text": "Read all instructions before completing this form.",
                            "why_this_type": "Static instruction text without any user input control.",
                            "question_text_rule": "Use the full instruction text.",
                            "answer_text_rule": "Leave answer_text blank.",
                        }
                    ],
                }
            ],
            "field_guidance": [
                {
                    "field_name": "Branching Logic",
                    "pdf_clues": ["skip wording"],
                    "instruction": "Preserve source wording in branching_source and use Q refs in Branching Logic.",
                },
                {
                    "field_name": "branching_source",
                    "pdf_clues": ["If B0100 = Yes, skip to GG0115"],
                    "instruction": "Keep the original visible skip wording and item codes.",
                },
                {
                    "field_name": "question_rule",
                    "pdf_clues": ["display/applicability wording"],
                    "instruction": "Use only when the condition controls whether the current row should show.",
                }
            ],
            "section_guidance": [
                {
                    "section_name": "Section A",
                    "pages": [1],
                    "instructions": ["Section A contains coded clinical fields."],
                }
            ],
        },
        "warnings": ["TOP LEVEL WARNING SHOULD NOT REACH SECTION PROMPT"],
    }


def test_policy_adapter_accepts_single_item_policy_list():
    adapted = _adapt_tool_data([sample_policy_payload()], ExtractionPolicy)

    assert adapted["policy"]["question_types"] == ["Radio Button", "Text Box"]


def test_policy_adapter_accepts_guidance_list():
    adapted = _adapt_tool_data(
        [
            {"question_type": "Text Box", "instruction": "Use short blank fields."},
            {"question_type": "Date", "instruction": "Use date-labeled fields."},
        ],
        ExtractionPolicy,
    )

    assert adapted["policy"]["question_types"] == ["Text Box", "Date"]
    assert adapted["policy"]["question_type_guidance"][0]["question_type"] == "Text Box"


def test_json_parser_prefers_object_when_requested():
    payload = f'Question types: ["Text Box"]\n{json.dumps(sample_policy_payload())}'

    parsed = _json_from_text(payload, prefer_object=True)

    assert parsed["policy"]["question_types"] == ["Radio Button", "Text Box"]


def test_model_json_parser_skips_bbox_array_for_section_rows():
    raw = (
        "bbox: [36.0, 92.78, 113.47, 103.34]\n"
        '{"rows": [{"section": "A", "question_type": "Display", "question_text": "Intro"}]}'
    )

    parsed = _parse_model_json_output(raw, SectionExtraction)

    assert parsed.rows[0].question_text == "Intro"


def sample_profile_payload() -> dict:
    return {
        "form_title": "Sample Assessment",
        "form_version": "v1",
        "complexity": "medium",
        "sections": [{"name": "Section A", "pages": [1]}],
        "shared_legends": [],
        "extraction_strategy": "single_call",
        "recommended_model_tier": "low_cost",
    }


def sample_section_payload() -> dict:
    return {
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


def sample_applicant_section_payload() -> dict:
    return {
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


def _tool_name(kwargs: dict) -> str | None:
    tool_config = kwargs.get("toolConfig") or {}
    return ((tool_config.get("tools") or [{}])[0].get("toolSpec") or {}).get("name")


def _text_response(payload: dict) -> dict:
    return {
        "stopReason": "end_turn",
        "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
        "usage": {"inputTokens": 10, "outputTokens": 20},
    }


def _tool_response(tool_name: str, payload: dict) -> dict:
    return {
        "output": {"message": {"content": [{"toolUse": {"name": tool_name, "input": payload}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 20},
    }


class FakeBedrockClient:
    def converse(self, **kwargs):
        tool_name = _tool_name(kwargs)
        if tool_name is None:
            text = str(kwargs["messages"][0]["content"][-1].get("text", "")).lower()
            if "section profile and extraction strategy" in text:
                return _text_response(sample_profile_payload())
            if "extracted canonical rows" in text:
                return _text_response(sample_section_payload())
            return _text_response(sample_policy_payload())
        if tool_name == "record_document_profile":
            payload = sample_profile_payload()
        elif tool_name == "record_extraction_policy":
            payload = sample_policy_payload()
        else:
            payload = sample_section_payload()
        return _tool_response(tool_name, payload)


class FakeRetrySectionClient:
    def __init__(self):
        self.section_calls = 0

    def converse(self, **kwargs):
        tool_name = _tool_name(kwargs)
        if tool_name is None:
            text = str(kwargs["messages"][0]["content"][-1].get("text", "")).lower()
            if "section profile and extraction strategy" in text:
                return _text_response(sample_profile_payload())
            if "return the pdf-specific extraction policy as json" in text:
                return _text_response(sample_policy_payload())
            self.section_calls += 1
            return _text_response(sample_applicant_section_payload())
        if tool_name == "record_document_profile":
            payload = sample_profile_payload()
            return _tool_response(tool_name, payload)

        self.section_calls += 1
        if self.section_calls == 1:
            return {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "I will return the extraction, but not as a tool."}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }

        return _text_response(sample_applicant_section_payload())


class ModelErrorException(Exception):
    pass


class FakeToolUseModelErrorClient(FakeRetrySectionClient):
    def converse(self, **kwargs):
        tool_name = _tool_name(kwargs)
        if tool_name is None:
            text = str(kwargs["messages"][0]["content"][-1].get("text", "")).lower()
            if "section profile and extraction strategy" in text:
                return _text_response(sample_profile_payload())
            if "return the pdf-specific extraction policy as json" in text:
                return _text_response(sample_policy_payload())
            self.section_calls += 1
            return _text_response(sample_applicant_section_payload())
        if tool_name == "record_section_extraction":
            self.section_calls += 1
            raise ModelErrorException("Model produced invalid sequence as part of ToolUse.")
        if tool_name == "record_document_profile":
            return super().converse(**kwargs)

        return _text_response(sample_applicant_section_payload())


class FakeChatBedrockToolUseModelErrorClient(FakeRetrySectionClient):
    def converse(self, **kwargs):
        tool_name = _tool_name(kwargs)
        if tool_name is not None:
            raise AssertionError("Section extraction should not use Bedrock toolConfig")

        text = str(kwargs["messages"][0]["content"][-1].get("text", "")).lower()
        if "section profile and extraction strategy" in text:
            return _text_response(sample_profile_payload())
        if "return the pdf-specific extraction policy as json" in text:
            return _text_response(sample_policy_payload())

        self.section_calls += 1
        if "tool calling is disabled" not in text:
            raise ModelErrorException("Model produced invalid sequence as part of ToolUse.")
        return _text_response(sample_applicant_section_payload())


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
        "build_extraction_policy",
        "extract_section",
        "normalize_rows",
        "__end__",
    }


def test_agent_config_defaults_to_nova_pro_for_section_extraction(monkeypatch):
    monkeypatch.delenv("BEDROCK_PROFILER_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_EXTRACTOR_DEFAULT_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_EXTRACTOR_COMPLEX_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_POLICY_MODEL_ID", raising=False)

    config = AgentConfig()

    assert config.profiler_model_id == "us.amazon.nova-2-lite-v1:0"
    assert config.extractor_default_model_id == "us.amazon.nova-pro-v1:0"
    assert config.extractor_complex_model_id == "us.amazon.nova-pro-v1:0"
    assert config.policy_model_id == "us.amazon.nova-pro-v1:0"


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
    assert result.extraction_policy.policy.question_types == ["Radio Button", "Text Box"]
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


def test_policy_prompt_focuses_on_pdf_field_observations(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    page = PageStructure(
        page_index=0,
        width=612,
        height=792,
        image_path=str(image_path),
        text_blocks=[
            {"source_id": "T001_001", "text": "B0100. Hearing", "rect": [1, 2, 3, 4]},
            {"source_id": "T001_002", "text": "Yes", "rect": [5, 2, 6, 4]},
            {"source_id": "T001_003", "text": "No", "rect": [7, 2, 8, 4]},
            {"source_id": "T001_004", "text": "If Yes, skip to GG0115", "rect": [1, 8, 8, 10]},
        ],
        widgets=[],
    )
    doc = DocStructure(pdf_path="/tmp/sample.pdf", page_count=1, has_acroform=False, pages=[page])
    section = SectionProfile(name="Section A", pages=[1])
    profile = DocumentProfile(form_title="Sample", sections=[section])

    prompt = _policy_prompt(doc, profile, [section], [page])

    assert "Your job is NOT to extract final rows" in prompt
    assert "Only describe how input fields and static content are presented in this PDF" in prompt
    assert "Do not teach the taxonomy" in prompt
    assert "label and blank positions" in prompt
    assert "where the shared question/prompt appears" in prompt
    assert "where printed answer options appear" in prompt
    assert "relative to the field it controls" in prompt
    assert "computed_pattern_hints" in prompt
    assert "computed_field_candidates" in prompt
    assert "Document evidence" in prompt
    assert "question_text_instruction" in prompt
    assert "answer_text_instruction" in prompt
    assert "distinguishing_features" in prompt
    assert "Question type taxonomy" not in prompt
    assert "Output field taxonomy" not in prompt
    assert "Canonical question types to consider" not in prompt
    assert "Canonical Excel columns to target" not in prompt


def test_policy_pattern_hints_separate_radio_and_checkbox_group(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=2,
        has_acroform=True,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {
                        "source_id": "T001_005",
                        "text": "Record ID________________ Total Amount_______ Choice  \uf071  A   \uf071  B",
                        "rect": [72, 153, 511, 166],
                    },
                    {
                        "source_id": "T001_033",
                        "text": "The person has multiple complex chronic or acquired health",
                        "rect": [103, 584, 374, 595],
                    },
                    {"source_id": "T001_034", "text": "conditions.", "rect": [103, 597, 155, 609]},
                    {"source_id": "T001_035", "text": "\uf071  C", "rect": [451, 583, 482, 595]},
                    {"source_id": "T001_036", "text": "\uf071  D", "rect": [451, 596, 480, 609]},
                ],
                widgets=[],
            ),
            PageStructure(
                page_index=1,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {
                        "source_id": "T002_001",
                        "text": "Select all applicable items:",
                        "rect": [72, 82, 425, 94],
                    },
                    {
                        "source_id": "T002_002",
                        "text": "\uf071   First item",
                        "rect": [72, 106, 248, 120],
                    },
                    {
                        "source_id": "T002_003",
                        "text": "\uf071   Second item",
                        "rect": [72, 160, 173, 174],
                    },
                ],
                widgets=[],
            ),
        ],
    )

    hints = _policy_pattern_hints(doc)

    assert any(hint["option_labels"] == ["A", "B"] for hint in hints["single_select_pair_candidates"])
    assert any(hint["option_labels"] == ["C", "D"] for hint in hints["single_select_pair_candidates"])
    assert hints["multi_select_group_candidates"][0]["shared_prompt_text"].startswith("Select all")
    assert hints["standalone_checkbox_candidates"] == []


def test_field_candidate_hints_find_compact_dates_numbers_and_signature_dates(tmp_path):
    image_path = tmp_path / "p1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = DocStructure(
        pdf_path="/tmp/sample.pdf",
        page_count=2,
        has_acroform=True,
        pages=[
            PageStructure(
                page_index=0,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {
                        "source_id": "T001_004",
                        "text": "Contact Name: ______ Start Date ____________",
                        "rect": [72, 141, 537, 153],
                    },
                    {
                        "source_id": "T001_005",
                        "text": "Reference Code________________ Total Amount_______ Choice  \uf071  A   \uf071  B  Service Date_______________",
                        "rect": [72, 153, 511, 166],
                    },
                    {
                        "source_id": "T001_007",
                        "text": "Location _______________________ Status____________ Item Count____________ Region________________",
                        "rect": [72, 181, 515, 193],
                    },
                ],
                widgets=[
                    {"source_id": "W001_002", "label": "Start Date", "type": "Text", "rect": [469, 138, 535, 151]},
                    {"source_id": "W001_004", "label": "Total Amount", "type": "Text", "rect": [246, 152, 284, 164]},
                ],
            ),
            PageStructure(
                page_index=1,
                width=612,
                height=792,
                image_path=str(image_path),
                text_blocks=[
                    {"source_id": "T002_014", "text": "Reviewer Signature", "rect": [72, 346, 205, 357]},
                    {"source_id": "T002_015", "text": "Date", "rect": [324, 346, 348, 357]},
                ],
                widgets=[
                    {"source_id": "W002_008", "label": "Date", "type": "Text", "rect": [324, 314, 444, 340]},
                ],
            ),
        ],
    )

    candidates = _field_candidate_hints(doc)
    labels_by_type = {
        (candidate["question_type"], candidate["question_text"])
        for candidate in candidates
    }

    assert ("Date", "Start Date") in labels_by_type
    assert ("Date", "Service Date") in labels_by_type
    assert ("Number", "Total Amount") in labels_by_type
    assert ("Number", "Item Count") in labels_by_type
    assert ("Text Box", "Status") in labels_by_type
    assert ("Text Box", "Region") in labels_by_type
    assert ("Radio Button", "Choice") in labels_by_type
    assert ("Date", "Reviewer Signature Date") in labels_by_type


def test_policy_quality_cleanup_removes_option_only_checkbox():
    bad_policy = ExtractionPolicy.model_validate(
        {
            "policy": {
                "question_types": ["Checkbox", "Radio Button", "Checkbox Group"],
                "question_type_guidance": [
                    {
                        "question_type": "Checkbox",
                        "examples": [
                            {
                                "page": 1,
                                "source_ids": ["T001_035"],
                                "visible_text": "\uf071  A",
                            }
                        ],
                    },
                    {
                        "question_type": "Radio Button",
                        "examples": [
                            {
                                "page": 1,
                                "source_ids": ["T001_035", "T001_036"],
                                "visible_text": "\uf071  A\n\uf071  B",
                            }
                        ],
                    },
                    {
                        "question_type": "Checkbox Group",
                        "examples": [
                            {
                                "page": 4,
                                "source_ids": ["T004_002", "T004_003"],
                                "visible_text": "Select all applicable items\n\uf071 First item",
                            }
                        ],
                    },
                ],
            }
        }
    )

    issues = _policy_quality_issues(bad_policy)
    cleaned, notes = _cleanup_ambiguous_policy(bad_policy, issues)

    assert issues
    assert "Checkbox" not in cleaned.policy.question_types
    assert all(str(guidance.question_type) != "Checkbox" for guidance in cleaned.policy.question_type_guidance)
    assert notes == ["removed_ambiguous_checkbox"]

    choice_policy = ExtractionPolicy.model_validate(
        {
            "policy": {
                "question_types": ["Checkbox", "Radio Button"],
                "question_type_guidance": [
                    {
                        "question_type": "Checkbox",
                        "examples": [{"page": 1, "source_ids": ["T001_005"], "visible_text": "Choice"}],
                    },
                    {
                        "question_type": "Radio Button",
                        "examples": [{"page": 1, "source_ids": ["T001_005"], "visible_text": "Choice A B"}],
                    },
                ],
            }
        }
    )
    hints = {
        "single_select_pair_candidates": [
            {"source_ids": ["T001_005"], "option_source_ids": ["T001_005"], "option_labels": ["A", "B"]}
        ],
        "multi_select_group_candidates": [],
    }

    choice_issues = _policy_quality_issues(choice_policy, hints)
    choice_cleaned, choice_notes = _cleanup_ambiguous_policy(choice_policy, choice_issues, hints)

    assert any("computed layout hints" in issue for issue in choice_issues)
    assert "Checkbox" not in choice_cleaned.policy.question_types
    assert choice_notes == ["removed_ambiguous_checkbox"]


def test_pattern_hints_enrich_choice_policy_with_option_sources():
    policy = ExtractionPolicy.model_validate(
        {
            "policy": {
                "question_types": ["Checkbox", "Radio Button", "Checkbox Group"],
                "question_type_guidance": [
                    {
                        "question_type": "Radio Button",
                        "question_text_instruction": "Use the shared prompt text.",
                        "answer_text_instruction": "Collect all option labels into answer text separated by two newlines.",
                        "examples": [{"page": 1, "source_ids": ["T001_030"], "visible_text": "Choice"}],
                    },
                    {
                        "question_type": "Checkbox Group",
                        "question_text_instruction": "Use the shared prompt text.",
                        "answer_text_instruction": "Collect all option labels into answer text separated by two newlines.",
                        "examples": [{"page": 4, "source_ids": ["T004_002"], "visible_text": "Select all applicable items"}],
                    },
                    {
                        "question_type": "Checkbox",
                        "examples": [{"page": 1, "source_ids": ["T001_005"], "visible_text": "Choice"}],
                    },
                ],
            }
        }
    )
    hints = {
        "single_select_pair_candidates": [
            {
                "page": 1,
                "source_ids": ["T001_005"],
                "option_source_ids": ["T001_005"],
                "nearby_widget_ids": ["W001_005", "W001_006"],
                "shared_prompt_text": "Choice",
                "option_labels": ["A", "B"],
                "why_it_matters": "The labels are peer options.",
            }
        ],
        "multi_select_group_candidates": [
            {
                "page": 4,
                "source_ids": ["T004_002", "T004_003", "T004_004"],
                "option_source_ids": ["T004_003", "T004_004"],
                "nearby_widget_ids": ["W004_001", "W004_002"],
                "shared_prompt_text": "Select all applicable items:",
                "option_labels": ["First item", "Second item"],
                "why_it_matters": "Shared multi-select prompt with multiple independent options.",
            }
        ],
        "standalone_checkbox_candidates": [],
    }

    enriched, notes = _apply_pattern_hints_to_policy(policy, hints)

    assert "Checkbox" not in enriched.policy.question_types
    assert "enriched_radio_button_from_pattern_hints" in notes
    assert "enriched_checkbox_group_from_pattern_hints" in notes
    assert _policy_quality_issues(enriched, hints) == []
    group = next(g for g in enriched.policy.question_type_guidance if str(g.question_type) == "Checkbox Group")
    assert any("T004_003" in example.source_ids for example in group.examples)

    hints["field_candidates"] = [
        {
            "question_type": "Date",
            "question_text": "Start Date",
            "page": 1,
            "source_ids": ["T001_004"],
            "nearby_widget_ids": ["W001_002"],
            "visible_text": "Start Date ______",
            "reason": "Printed date label with blank.",
        },
        {
            "question_type": "Number",
            "question_text": "Total Amount",
            "page": 1,
            "source_ids": ["T001_005"],
            "nearby_widget_ids": ["W001_004"],
            "visible_text": "Total Amount_______",
            "reason": "Printed numeric label with blank.",
        },
    ]
    enriched_with_fields, notes_with_fields = _apply_pattern_hints_to_policy(policy, hints)

    assert "Date" in enriched_with_fields.policy.question_types
    assert "Number" in enriched_with_fields.policy.question_types
    assert "enriched_date_from_field_candidates" in notes_with_fields
    assert "enriched_number_from_field_candidates" in notes_with_fields


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


def test_agentic_pipeline_uses_chatbedrock_json_for_section_extraction(tmp_path, monkeypatch):
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

    extract_telemetry = [item for item in result.telemetry if item.get("stage") == "extract_section"]
    assert client.section_calls == 1
    assert result.rows[0].question_text == "Applicant Name"
    assert extract_telemetry[0]["provider_wrapper"] == "ChatBedrockConverse"
    assert extract_telemetry[0]["json_schema_mode"] is True
    assert extract_telemetry[0]["fallback_used"] is False


def test_agentic_pipeline_does_not_call_bedrock_tool_use_for_section_extraction(
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

    extract_telemetry = [item for item in result.telemetry if item.get("stage") == "extract_section"]
    assert client.section_calls == 1
    assert result.rows[0].question_text == "Applicant Name"
    assert extract_telemetry[0]["provider_wrapper"] == "ChatBedrockConverse"
    assert extract_telemetry[0]["fallback_used"] is False


def test_agentic_pipeline_falls_back_when_chatbedrock_tooluse_sequence_fails(
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
    client = FakeChatBedrockToolUseModelErrorClient()
    monkeypatch.setattr("showlay.agentic._bedrock_runtime", lambda: client)

    result = extract_document_agentic(doc, config=AgentConfig(max_section_images=2))

    extract_telemetry = [item for item in result.telemetry if item.get("stage") == "extract_section"]
    assert client.section_calls == 2
    assert result.rows[0].question_text == "Applicant Name"
    assert extract_telemetry[0]["provider_wrapper"] == "boto3.converse"
    assert extract_telemetry[0]["fallback_used"] is True


def test_extractor_prompt_is_generic_and_policy_driven(tmp_path):
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

    policy = ExtractionPolicy.model_validate(sample_policy_payload())

    prompt = _extractor_prompt(doc, profile, policy, section, [page], 0, 1)

    assert "Question Type taxonomy:" in prompt
    assert '"question_type": "Text Box"' in prompt
    assert '"question_type": "Radio Button"' in prompt
    assert "Excel/output field meanings:" in prompt
    assert '"field_name": "Question Text"' in prompt
    assert '"field_name": "Answer Text"' in prompt
    assert '"field_name": "Sequence"' in prompt
    assert "Previous model input: extraction policy / PDF field-observation notes" in prompt
    assert "That policy is not a final answer and not a taxonomy" in prompt
    assert "Dropdown /" not in prompt
    assert "Date /" not in prompt
    assert "Group Table /" not in prompt
    assert "- Section Header:" not in prompt
    assert "Do not use question types outside the canonical Question Type values" in prompt
    assert '"question_types": [' in prompt
    assert '"excel_columns": [' in prompt
    assert "answer_text_instruction" in prompt
    assert "branching_instruction" in prompt
    assert "distinguishing_features" in prompt
    assert "why_this_type" in prompt
    assert "TOP LEVEL SUMMARY SHOULD NOT REACH SECTION PROMPT" not in prompt
    assert "TOP LEVEL WARNING SHOULD NOT REACH SECTION PROMPT" not in prompt
    assert "Section A contains coded clinical fields." in prompt
