"""
Excel writer.

Strategy: copy the source TRUTH workbook as a template (preserves all metadata rows 1-12,
formatting, validation, dropdowns), find the header row dynamically by scanning for
"QuestionType" column header, then overwrite data rows starting at header_row+1.

Also writes a companion *_review.xlsx with 4 extra columns (Sequence, Confidence,
Review Reasons, Page) sorted by confidence ascending — the human-review queue.
"""
from __future__ import annotations
import shutil
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook, Workbook
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

from .schema import Row, COLUMNS_28, map_template_columns, FIELD_HEADER_ALIASES


_GREEN = PatternFill(start_color="D6F5D6", end_color="D6F5D6", fill_type="solid")
_YELLOW = PatternFill(start_color="FFF2C7", end_color="FFF2C7", fill_type="solid")
_RED = PatternFill(start_color="FBD3D3", end_color="FBD3D3", fill_type="solid")


_QTYPE_NAMES = {"questiontype", "question type"}


def _find_header_row(ws) -> tuple[int, dict[str, int]]:
    """Locate the row containing the QuestionType / Question Type header.
    Different forms use different exact spellings (no space vs space, English-only vs bilingual)."""
    for r in range(1, min(ws.max_row, 30) + 1):
        for c in range(1, min(ws.max_column, 40) + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and " ".join(v.split()).strip().lower() in _QTYPE_NAMES:
                row_vals = {ws.cell(row=r, column=cc).value: cc for cc in range(1, ws.max_column + 1)}
                return r, row_vals
    raise ValueError("Could not find header row with QuestionType / Question Type column.")


def _select_assessment_sheet(wb) -> str:
    # Prefer "Assessment v2", then "Assessment", else first sheet that has a 'QuestionType' header.
    for candidate in ["Assessment v2", "Assessment"]:
        if candidate in wb.sheetnames:
            return candidate
    return wb.sheetnames[0]


def _confidence_fill(c: float) -> Optional[PatternFill]:
    if c >= 0.9:
        return _GREEN
    if c >= 0.7:
        return _YELLOW
    return _RED


def _clear_data_rows(ws, header_row: int, max_col: int):
    if ws.max_row <= header_row:
        return
    for r in range(header_row + 1, ws.max_row + 1):
        for c in range(1, max_col + 1):
            ws.cell(row=r, column=c).value = None
            ws.cell(row=r, column=c).fill = PatternFill(fill_type=None)


def write_workbook(template_path: str, out_path: str, rows: list[Row],
                   sheet_name: Optional[str] = None) -> str:
    """Clone template, find header row, write rows below it. Returns out_path."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, out_path)
    wb = load_workbook(out_path)
    target_sheet = sheet_name or _select_assessment_sheet(wb)
    ws = wb[target_sheet]
    header_row, _ = _find_header_row(ws)

    # Build {col_idx: header_string} from the actual template header row, then
    # resolve our internal Row field names → worksheet columns via aliases.
    header_values = {cc: ws.cell(row=header_row, column=cc).value
                     for cc in range(1, ws.max_column + 1)}
    field_to_col = map_template_columns(header_values)
    if not field_to_col:
        raise ValueError("Template header row contained no recognized columns.")

    max_col_used = max(list(field_to_col.values()) + [ws.max_column])
    _clear_data_rows(ws, header_row, max_col_used)

    field_order = list(FIELD_HEADER_ALIASES.keys())  # stable iteration

    for i, r in enumerate(rows, start=1):
        ws_row = header_row + i
        fill = _confidence_fill(r.confidence)
        for fname in field_order:
            cidx = field_to_col.get(fname)
            if cidx is None:
                continue
            value = getattr(r, fname, "")
            if fname == "sequence":
                value = value if value is not None else ""
            cell = ws.cell(row=ws_row, column=cidx)
            cell.value = value
            if fill:
                cell.fill = fill
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    wb.save(out_path)
    return out_path


def write_review_sidecar(out_path: str, rows: list[Row]) -> str:
    """Compact human-review queue: rows sorted by confidence asc, with reasons + source page."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Review Queue"
    headers = ["Confidence", "Page", "Sequence", "QuestionType", "Question Text",
               "Section", "Answer Text", "Branching Logic", "Required",
               "Review Reasons"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True)

    sorted_rows = sorted(rows, key=lambda r: (r.confidence, r.sequence or 0))
    for i, r in enumerate(sorted_rows, start=2):
        ws.cell(row=i, column=1, value=r.confidence)
        ws.cell(row=i, column=2, value=r.page or "")
        ws.cell(row=i, column=3, value=r.sequence or "")
        ws.cell(row=i, column=4, value=r.question_type)
        ws.cell(row=i, column=5, value=r.question_text)
        ws.cell(row=i, column=6, value=r.section)
        ws.cell(row=i, column=7, value=r.answer_text)
        ws.cell(row=i, column=8, value=r.branching_logic)
        ws.cell(row=i, column=9, value=r.required)
        ws.cell(row=i, column=10, value="; ".join(r.review_reasons))
        fill = _confidence_fill(r.confidence)
        if fill:
            for c in range(1, len(headers) + 1):
                ws.cell(row=i, column=c).fill = fill

    for c, w in enumerate([10, 6, 9, 14, 60, 22, 40, 28, 9, 40], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[1].height = 22
    wb.save(out_path)
    return out_path
