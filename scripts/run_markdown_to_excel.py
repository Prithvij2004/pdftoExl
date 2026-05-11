from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.pipeline.markdown_to_excel import convert_markdown_to_excel  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert extracted Markdown to EAB-style Excel.")
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    result = convert_markdown_to_excel(args.markdown, output_path=args.output)
    print(f"Extracted JSON path: {result.extracted_json_path}")
    print(f"Refined JSON path: {result.refined_json_path}")
    print(f"Validation issues path: {result.validation_issues_path}")
    print(f"Workbook rows path: {result.workbook_rows_path}")
    print(f"Review report path: {result.review_report_path}")
    print(f"Excel output path: {result.excel_path}")
    print(f"Needs review count: {result.needs_review_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
