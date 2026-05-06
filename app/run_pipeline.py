from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import load_pipeline_config
from app.observability import configure_logfire
from app.services.pipeline import CurrentExtractionPipeline

configure_logfire(service_name="pdftoExl-cli")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the implemented PDF extraction pipeline phases."
    )
    parser.add_argument("pdf", type=Path, help="Path to the source PDF.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write full pipeline JSON to this path instead of stdout.",
    )
    parser.add_argument(
        "--semantic-output",
        type=Path,
        help="Write compact semantic prompt text to this path.",
    )
    parser.add_argument(
        "--parser-markdown-output",
        type=Path,
        help="Write parser-path Markdown for parser-routed pages to this path.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    result = CurrentExtractionPipeline(load_pipeline_config()).run(args.pdf)
    payload = json.dumps(result.to_dict(), indent=2 if args.pretty else None)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)

    if args.semantic_output:
        args.semantic_output.parent.mkdir(parents=True, exist_ok=True)
        args.semantic_output.write_text(
            result.to_semantic_prompt_text() + "\n",
            encoding="utf-8",
        )

    if args.parser_markdown_output:
        args.parser_markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.parser_markdown_output.write_text(
            result.to_parser_markdown(),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
