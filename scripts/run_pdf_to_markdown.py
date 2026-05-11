from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.pipeline.pdf_to_markdown import convert_pdf_to_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a PDF to normalized Markdown.")
    parser.add_argument("--input", required=True, type=Path, help="Path to input PDF.")
    parser.add_argument("--parser", default=None, help="Parser name: docling, pdfplumber, or pymupdf.")
    parser.add_argument("--output-dir", default=None, type=Path, help="Output directory. Defaults to env OUTPUT_DIR.")
    parser.add_argument("--preview", action="store_true", help="Print the first 1000 Markdown characters.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = convert_pdf_to_markdown(args.input, parser_name=args.parser, output_dir=args.output_dir)

    print(f"Parser used: {result.parser}")
    print(f"Markdown output path: {result.markdown_path}")
    print(f"Metadata output path: {result.metadata_path}")
    print(f"Page count: {result.page_count}")

    if args.preview:
        markdown = result.markdown_path.read_text(encoding="utf-8")
        print("\n--- Preview ---")
        print(markdown[:1000])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
