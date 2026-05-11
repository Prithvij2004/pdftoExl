from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.pipeline.pdf_to_excel import convert_pdf_to_excel  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert PDF form to EAB-style Excel.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--parser", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    result = convert_pdf_to_excel(args.input, parser_name=args.parser)
    print(f"Markdown path: {result.markdown.markdown_path}")
    print(f"Metadata path: {result.markdown.metadata_path}")
    print(f"Extracted JSON path: {result.excel.extracted_json_path}")
    print(f"Refined JSON path: {result.excel.refined_json_path}")
    print(f"Validation issues path: {result.excel.validation_issues_path}")
    print(f"Workbook rows path: {result.excel.workbook_rows_path}")
    print(f"Review report path: {result.excel.review_report_path}")
    print(f"Excel output path: {result.excel.excel_path}")
    print(f"Needs review count: {result.excel.needs_review_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
