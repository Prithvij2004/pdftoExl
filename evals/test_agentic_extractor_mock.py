from __future__ import annotations

import asyncio
from pathlib import Path

from app.models import ExtractionResult, ExtractedRow, QuestionType
from app.services import agentic_extractor
from app.services.pdf_batches import PdfBatch


class _RunResult:
    def __init__(self, output):
        self.output = output


class _StubAgent:
    def __init__(self, output):
        self._output = output

    async def run(self, *_args, **_kwargs):
        return _RunResult(self._output)


def test_agentic_extractor_offline_uses_finalizer_when_available(monkeypatch, tmp_path: Path) -> None:
    def _fake_batches(_pdf_path: Path, _batch_size: int):
        yield PdfBatch(batch_index=0, start_page_number=1, end_page_number=1, pdf_bytes=b"%PDF-1.4\n%")

    analysis = agentic_extractor.BatchAnalysis(
        candidates=[
            agentic_extractor.FieldCandidate(
                question_type=QuestionType.TEXT_BOX,
                question_text="Full name",
                answer_text="",
                page_number=1,
                source_order=0,
                confidence=0.9,
                rationale="Label next to a single-line blank.",
            )
        ]
    )
    finalized = ExtractionResult(
        rows=[
            ExtractedRow(
                question_type=QuestionType.TEXT_BOX,
                question_text="Full name",
                answer_text="",
                page_number=1,
                source_order=0,
                confidence=0.9,
                meta={"rationale": "ok"},
            )
        ]
    )

    def _fake_bundle():
        return agentic_extractor._AgentBundle(
            analysis_agent=_StubAgent(analysis),
            finalize_agent=_StubAgent(finalized),
        )

    monkeypatch.setattr(agentic_extractor, "iter_pdf_page_batches", _fake_batches)
    monkeypatch.setattr(agentic_extractor, "_bedrock_agent_bundle", _fake_bundle)

    pdf_path = tmp_path / "dummy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%")
    rows = asyncio.run(agentic_extractor.extract_rows_from_pdf_agentic(pdf_path))

    assert len(rows) == 1
    assert rows[0].question_type == QuestionType.TEXT_BOX
    assert rows[0].question_text == "Full name"


def test_agentic_extractor_offline_falls_back_to_candidates_on_finalize_failure(monkeypatch, tmp_path: Path) -> None:
    def _fake_batches(_pdf_path: Path, _batch_size: int):
        yield PdfBatch(batch_index=0, start_page_number=1, end_page_number=1, pdf_bytes=b"%PDF-1.4\n%")

    analysis = agentic_extractor.BatchAnalysis(
        candidates=[
            agentic_extractor.FieldCandidate(
                question_type=QuestionType.DISPLAY,
                question_text="Instructions",
                answer_text="",
                page_number=1,
                source_order=0,
                confidence=0.8,
                rationale="Static paragraph at top of page.",
            )
        ]
    )

    class _FailingAgent:
        async def run(self, *_args, **_kwargs):
            raise ValueError("finalize failed")

    def _fake_bundle():
        return agentic_extractor._AgentBundle(
            analysis_agent=_StubAgent(analysis),
            finalize_agent=_FailingAgent(),
        )

    monkeypatch.setattr(agentic_extractor, "iter_pdf_page_batches", _fake_batches)
    monkeypatch.setattr(agentic_extractor, "_bedrock_agent_bundle", _fake_bundle)

    pdf_path = tmp_path / "dummy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%")
    rows = asyncio.run(agentic_extractor.extract_rows_from_pdf_agentic(pdf_path))

    assert len(rows) == 1
    assert rows[0].question_type == QuestionType.DISPLAY
    assert rows[0].meta.get("rationale")

