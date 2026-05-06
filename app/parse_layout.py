from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.layout_parser import CheapLayoutParser, available_layout_parser_backends


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse cheap text/layout data from a PDF.")
    parser.add_argument("pdf", type=Path, help="Path to the source PDF.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON raw page model.",
    )
    parser.add_argument(
        "--backend",
        choices=available_layout_parser_backends(),
        default=None,
        help="Cheap parser backend to use.",
    )
    args = parser.parse_args()

    raw_model = CheapLayoutParser(backend=args.backend).parse(args.pdf)
    print(json.dumps(raw_model.to_dict(), indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
