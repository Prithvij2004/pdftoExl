from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.config import PipelineConfig
from app.services.bedrock_agent import build_bedrock_agent
from app.services.chunker import Chunk


CHUNK_EXTRACTOR_VERSION = "chunk-extractor-v1"

CANONICAL_QUESTION_TYPES: tuple[str, ...] = (
    "Display",
    "Text Box",
    "Text Area",
    "Date",
    "Number",
    "Signature",
    "Radio Button",
    "Checkbox",
    "Dropdown",
    "Group Table",
)

QuestionType = Literal[
    "Display",
    "Text Box",
    "Text Area",
    "Date",
    "Number",
    "Signature",
    "Radio Button",
    "Checkbox",
    "Dropdown",
    "Group Table",
]

_BRANCHING_TEMPLATES: tuple[str, ...] = (
    'If Q{n} = checked(selected)',
    'Display if Q{n} = "{option_text_verbatim}"',
)


class ExtractedRow(BaseModel):
    """One structured row extracted from a chunk."""

    model_config = ConfigDict(frozen=True)

    section: str | None = Field(description="Section heading the row belongs to.")
    sequence: int | None = Field(description="Document-wide sequence number; always None until merge_resolve assigns it.")
    question_rule: str | None = Field(description="Optional question rule (validation, formatting hint).")
    question_type: str = Field(description=f"Canonical question type. One of: {', '.join(CANONICAL_QUESTION_TYPES)}.")
    question_text: str = Field(description="Verbatim question stem text.")
    branching_logic: str | None = Field(description="Branching rule, when present, expressed as one of the canonical templates.")
    answer_text: str | None = Field(description="Visible answer text or option list, when present.")
    source_ids: list[str] = Field(description="Layout block IDs that back this row.")
    chunk_id: str = Field(description="Chunk ID that produced this row.")
    page_numbers: list[int] = Field(description="Pages the row spans.")
    confidence: float = Field(description="Extractor self-reported confidence in [0, 1].")

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "sequence": self.sequence,
            "question_rule": self.question_rule,
            "question_type": self.question_type,
            "question_text": self.question_text,
            "branching_logic": self.branching_logic,
            "answer_text": self.answer_text,
            "source_ids": list(self.source_ids),
            "chunk_id": self.chunk_id,
            "page_numbers": list(self.page_numbers),
            "confidence": self.confidence,
        }


class ChunkExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="ID of the chunk this result was produced for.")
    rows: list[ExtractedRow] = Field(default_factory=list, description="Rows extracted from the chunk.")
    error: str | None = Field(default=None, description="Error message when chunk extraction failed; None on success.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "rows": [row.to_dict() for row in self.rows],
            "error": self.error,
        }


class ChunkExtractedRow(BaseModel):
    """Schema the LLM is asked to produce for each extracted row.

    Excludes pipeline-managed fields (chunk_id, page_numbers) which are filled in
    deterministically by the wrapper.
    """

    model_config = ConfigDict(frozen=True)

    section: str | None = Field(default=None, description="Section heading the row belongs to.")
    sequence: int | None = Field(default=None, description="MUST be null at this stage; sequence is assigned downstream.")
    question_rule: str | None = Field(default=None, description="Optional question rule (validation, formatting hint).")
    question_type: QuestionType = Field(description="Canonical question type.")
    question_text: str = Field(description="Verbatim question stem text. Use 'New Section' for synthetic section markers.")
    branching_logic: str | None = Field(default=None, description="When present, must use a canonical branching template verbatim.")
    answer_text: str | None = Field(default=None, description="Answer text, options list, or section name (for New Section markers).")
    source_ids: list[str] = Field(default_factory=list, description="Layout block IDs that back this row.")
    confidence: float = Field(default=0.5, description="Self-reported confidence in [0, 1].")


class ChunkExtractionPayload(BaseModel):
    """Top-level schema returned by the chunk extractor LLM."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="Chunk ID being extracted; must echo the input chunk_id.")
    rows: list[ChunkExtractedRow] = Field(default_factory=list, description="Extracted rows in document order.")


class ChunkExtractor(Protocol):
    def extract(self, chunk: Chunk) -> ChunkExtractionResult:
        ...


_CHUNK_EXTRACTOR_SYSTEM_PROMPT = (
    "You extract structured form rows from ONE pre-segmented chunk of a PDF form. "
    "Each row uses a canonical question_type and may include a branching_logic "
    "rule expressed in one of the canonical templates. Leave sequence as null. "
    "COMPLETENESS IS MANDATORY: every structure_hint block in the chunk must "
    "produce exactly one row, EXCEPT blocks whose role is 'repeated_header' or "
    "'page_footer' (those carry only form metadata / page chrome and are dropped). "
    "Do not invent rows that are not present in the chunk, and do not silently "
    "skip blocks either. If the chunk is only a genuine in-page sub-heading "
    "(NOT the document title or a repeated page header/footer), emit a single "
    "Display row with question_text='New Section' and answer_text=<section name>."
)


class BedrockQwenChunkExtractor:
    def __init__(
        self,
        client: Any | None = None,
        *,
        config: PipelineConfig | None = None,
        agent: Any | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.agent = agent

    def extract(self, chunk: Chunk) -> ChunkExtractionResult:
        agent = self.agent
        if agent is None:
            config = self.config
            if config is None:
                raise ValueError("BedrockQwenChunkExtractor requires a PipelineConfig.")
            if config.bedrock_region is None and self.client is None:
                raise ValueError("BEDROCK_REGION is required for Bedrock chunk extraction.")
            agent = build_bedrock_agent(
                model_id=config.bedrock_text_model_id,
                region=config.bedrock_region,
                output_type=ChunkExtractionPayload,
                system_prompt=_CHUNK_EXTRACTOR_SYSTEM_PROMPT,
                bedrock_client=self.client,
                max_tokens=4096,
            )
        result = agent.run_sync(_chunk_extraction_prompt(chunk))
        rows = _rows_from_payload(result.output, chunk)
        return ChunkExtractionResult(chunk_id=chunk.chunk_id, rows=rows)


class ChunkExtractionService:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        extractor: ChunkExtractor | None = None,
    ) -> None:
        self.config = config
        if extractor is None and config.bedrock_region is not None:
            extractor = BedrockQwenChunkExtractor(config=config)
        self.extractor = extractor

    def extract_all(self, chunks: list[Chunk]) -> list[ChunkExtractionResult]:
        if not chunks:
            return []
        if self.extractor is None:
            raise RuntimeError(
                "ChunkExtractionService has no extractor configured; "
                "provide one explicitly or set BEDROCK_REGION."
            )
        max_workers = max(1, self.config.max_concurrent_bedrock_calls)
        results: list[ChunkExtractionResult | None] = [None] * len(chunks)

        def _run(index: int, chunk: Chunk) -> tuple[int, ChunkExtractionResult]:
            try:
                return index, self.extractor.extract(chunk)
            except Exception as exc:  # noqa: BLE001
                return index, ChunkExtractionResult(
                    chunk_id=chunk.chunk_id,
                    rows=[],
                    error=f"{type(exc).__name__}: {exc}",
                )

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_run, i, chunk) for i, chunk in enumerate(chunks)]
            for future in futures:
                index, result = future.result()
                results[index] = result

        return [result for result in results if result is not None]


def _rows_from_payload(payload: ChunkExtractionPayload, chunk: Chunk) -> list[ExtractedRow]:
    rows: list[ExtractedRow] = []
    for raw in payload.rows:
        question_text = raw.question_text.strip()
        if not question_text:
            continue
        section = raw.section.strip() if isinstance(raw.section, str) and raw.section.strip() else chunk.section_hint
        rows.append(
            ExtractedRow(
                section=section,
                sequence=None,
                question_rule=_clean_optional(raw.question_rule),
                question_type=raw.question_type,
                question_text=question_text,
                branching_logic=_clean_optional(raw.branching_logic),
                answer_text=_clean_optional(raw.answer_text),
                source_ids=list(raw.source_ids),
                chunk_id=chunk.chunk_id,
                page_numbers=list(chunk.pages),
                confidence=_clamp_confidence(raw.confidence),
            )
        )
    return rows


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _clamp_confidence(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _coerce_rows(raw_rows: Any, chunk: Chunk) -> list[ExtractedRow]:
    if not isinstance(raw_rows, list):
        return []
    rows: list[ExtractedRow] = []
    for raw_row in raw_rows:
        row = _coerce_row(raw_row, chunk)
        if row is not None:
            rows.append(row)
    return rows


def _coerce_row(raw_row: Any, chunk: Chunk) -> ExtractedRow | None:
    if not isinstance(raw_row, dict):
        return None
    question_type = raw_row.get("question_type")
    if not isinstance(question_type, str) or question_type not in CANONICAL_QUESTION_TYPES:
        return None
    question_text = raw_row.get("question_text")
    if not isinstance(question_text, str) or not question_text.strip():
        return None

    section = _optional_string(raw_row.get("section")) or chunk.section_hint
    question_rule = _optional_string(raw_row.get("question_rule"))
    branching_logic = _optional_string(raw_row.get("branching_logic"))
    answer_text = _optional_string(raw_row.get("answer_text"))
    source_ids = _string_list(raw_row.get("source_ids"))
    confidence = _coerce_float(raw_row.get("confidence"), default=0.5)

    return ExtractedRow(
        section=section,
        sequence=None,
        question_rule=question_rule,
        question_type=question_type,
        question_text=question_text.strip(),
        branching_logic=branching_logic,
        answer_text=answer_text,
        source_ids=source_ids,
        chunk_id=chunk.chunk_id,
        page_numbers=list(chunk.pages),
        confidence=confidence,
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int))]


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result < 0.0:
        return 0.0
    if result > 1.0:
        return 1.0
    return result


def _chunk_extraction_prompt(chunk: Chunk) -> str:
    types_block = ", ".join(CANONICAL_QUESTION_TYPES)
    branching_block = "\n".join(f"  {tpl}" for tpl in _BRANCHING_TEMPLATES)
    structure_block = "\n".join(f"- {hint}" for hint in chunk.structure_hints) or "- (none)"
    pages_block = ", ".join(str(p) for p in chunk.pages) or "(none)"

    return (
        "Extract structured form rows from this chunk. Echo the chunk_id verbatim.\n\n"
        "COVERAGE CONTRACT — read first:\n"
        "Every block listed under structure_hints below MUST appear in your output as exactly one "
        "row, with that block's ID in source_ids, EXCEPT blocks whose role is 'repeated_header' "
        "or 'page_footer' (drop those — they are page chrome / form metadata). Use the role tag "
        "to choose question_type:\n"
        "  • role=section_heading → ONE Display row, question_text='New Section', "
        "answer_text=<the heading text>.\n"
        "  • role=instruction → ONE Display row, question_text=<the instruction text> "
        "(answer_text may be null, or the trailing body if the block reads as heading + body).\n"
        "  • role=blank_field → ONE input row; pick the canonical input type from the LABEL "
        "semantics (Date / Number / Signature / Text Area / Dropdown / Radio Button / Checkbox / "
        "Text Box) — Text Box is the LAST resort.\n"
        "  • role=signature_block → ONE Signature row.\n"
        "  • role=choice_option → fold into the parent question's row (one row per question stem, "
        "options pipe-separated in answer_text); do not emit one row per option.\n"
        "  • role=table → ONE Group Table parent row plus one row per column header (see Group "
        "Table rules below).\n"
        "Before you finish, mentally cross-check: the count of distinct block IDs you cited in "
        "source_ids (excluding repeated_header / page_footer) should equal the count of those "
        "blocks in structure_hints. If a block has no obvious mapping, still emit a Display row "
        "for it rather than dropping it silently.\n\n"
        f"Allowed question_type values: {types_block}.\n\n"
        "Canonical question type definitions:\n"
        "- Display: Static instruction, notice, explanatory text, or synthetic section marker. "
        'Section markers use question_text="New Section" and answer_text=<section name>. '
        "For a non-section instruction block, put the instruction text in question_text. "
        "Display is a LAST RESORT for non-input text — only choose it when the content is truly static "
        "and not an answerable field, but instruction blocks DO map to Display (do not drop them).\n"
        "- Text Box: Single-line free-text input. Text Box is ALSO a LAST RESORT among input types — "
        "even when a field looks like a plain blank line, evaluate Date / Number / Signature / Text Area / "
        "Dropdown / Radio Button / Checkbox FIRST. Visual appearance alone is not decisive: a blank line "
        "labelled 'DOB' is Date, 'Age' is Number, 'Signature of guardian' is Signature, regardless of how "
        "the box is drawn. Only fall back to Text Box when no other type matches the label semantics.\n"
        "- Text Area: Multi-line free-text or narrative/explanation area.\n"
        "- Date: Date-like input (date, DOB, effective date, signature date, month/year). Choose Date "
        "whenever the label implies a date answer, even if the field visually looks like a plain blank.\n"
        "- Number: Numeric-only input (age, score, count, amount, percentage, #). Choose Number whenever "
        "the label implies a numeric answer, even if the field visually looks like a plain blank.\n"
        "- Signature: Signature, initials, signer name, or authorized representative line. Choose Signature "
        "whenever the label implies a signature, even if the field visually looks like a plain blank.\n"
        "- Radio Button: Single-select with mutually exclusive options. Stem in question_text, options in answer_text.\n"
        "- Checkbox: Standalone checkbox or independent check item.\n"
        "- Dropdown: Single-select dropdown/list. Visible options go in answer_text.\n"
        "- Group Table: Use for any multi-row / multi-column table. Emit ONE parent Group Table row whose "
        "question_text is the table's visible title; if the table has no visible title, synthesize a short "
        "descriptive title from the table's context (surrounding heading or column theme). answer_text on the "
        "parent may be null. IMMEDIATELY AFTER the parent, emit ONE row per column header in left-to-right "
        "order: question_text = the column header label, question_type = the canonical type that best fits "
        "that column's expected cell content (Date / Number / Signature / Text Box / Text Area / Dropdown / "
        "Radio Button / Checkbox), chosen from the column header's semantics. Do NOT extract table data rows "
        "(the per-row cell values).\n\n"
        "branching_logic, when set, must use ONLY one of these templates verbatim "
        "(Q{n} placeholders are resolved later, leave them as-is):\n"
        f"{branching_block}\n\n"
        "SECTION SEMANTICS — IMPORTANT:\n"
        "- A 'section' is a SUB-HEADING within a page that groups a topical block of questions "
        "(e.g. 'Demographics', 'Medical History', 'Fall Incident Details', 'Consent & Signatures').\n"
        "- The PDF filename, the document/form overall title, and any repeated page header / page "
        "footer (form id, revision, page number, branding, running title) are NOT sections. Never "
        "emit a 'New Section' Display row for them, and do not put them in the section field of any row.\n"
        "- Only emit a Display row with question_text=\"New Section\" and answer_text=<section name> "
        "when the chunk contains a genuine in-page sub-heading that introduces a new topical block.\n"
        "- If the chunk only contains a non-section page header / page footer / document title and "
        "carries no completion-critical instruction, do not emit a section marker for it.\n\n"
        f"chunk_id: {chunk.chunk_id}\n"
        f"pages: {pages_block}\n"
        f"section_hint: {chunk.section_hint or '(none)'}\n"
        f"chunk_type_hint: {chunk.chunk_type_hint or '(none)'}\n\n"
        "structure_hints:\n"
        f"{structure_block}\n\n"
        "chunk_markdown:\n"
        f"{chunk.markdown}\n"
    )
