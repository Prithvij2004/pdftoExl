from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.excel.excel_writer import write_workbook_rows_to_excel
from app.extraction.refiner import refine_document_extraction
from app.extraction.structured_extractor import extract_document_from_markdown
from app.mapping.workbook_mapper import map_document_to_workbook_rows
from app.schemas.workbook_schema import WorkbookRow
from app.validation.form_validator import validate_document
from app.validation.validation_models import ValidationIssue


@dataclass(frozen=True)
class ExcelConversionResult:
    excel_path: Path
    extracted_json_path: Path
    refined_json_path: Path
    validation_issues_path: Path
    workbook_rows_path: Path
    review_report_path: Path
    needs_review_count: int


def _save_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")  # type: ignore[attr-defined]
    elif isinstance(payload, list):
        data = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in payload]
    else:
        data = payload
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _review_report(rows: list[WorkbookRow], issues: list[ValidationIssue]) -> dict[str, object]:
    items = [
        {
            "sequence": row.sequence,
            "question_type": row.question_type,
            "question_text": row.question_text,
            "review_reason": row.review_reason,
        }
        for row in rows
        if row.needs_review
    ]
    return {
        "needs_review_count": len(items),
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "items_needing_review": items,
    }


def convert_markdown_to_excel(markdown_path: Path, output_path: Path | None = None) -> ExcelConversionResult:
    markdown_path = Path(markdown_path)
    stem = markdown_path.stem

    extracted = extract_document_from_markdown(markdown_path)
    extracted_json_path = settings.output_dir / "extraction" / f"{stem}_extracted.json"

    refined = refine_document_extraction(extracted, file_stem=stem)
    refined_json_path = settings.output_dir / "extraction" / f"{stem}_refined.json"

    validated, issues = validate_document(refined)
    validation_issues_path = _save_json(settings.output_dir / "validation" / f"{stem}_issues.json", issues)

    rows = map_document_to_workbook_rows(validated)
    workbook_rows_path = _save_json(settings.output_dir / "workbook_rows" / f"{stem}_workbook_rows.json", rows)

    report = _review_report(rows, issues)
    review_report_path = _save_json(settings.output_dir / "review" / f"{stem}_review_report.json", report)

    excel_path = output_path or (settings.downloads_dir / f"{stem}_generated.xlsx")
    write_workbook_rows_to_excel(rows, excel_path)

    return ExcelConversionResult(
        excel_path=excel_path,
        extracted_json_path=extracted_json_path,
        refined_json_path=refined_json_path,
        validation_issues_path=validation_issues_path,
        workbook_rows_path=workbook_rows_path,
        review_report_path=review_report_path,
        needs_review_count=int(report["needs_review_count"]),
    )
