from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import load_pipeline_config
from app.services.page_preparation import PagePreparer


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare PDF pages for extraction.")
    parser.add_argument("pdf", type=Path, help="Path to the source PDF.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON metadata.",
    )
    args = parser.parse_args()

    prepared = PagePreparer(load_pipeline_config()).prepare(args.pdf)
    print(json.dumps(prepared.to_dict(), indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
