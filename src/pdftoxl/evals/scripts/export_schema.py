"""Export Boundary 1 JSON Schema from Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from pdftoxl.evals.contracts import EnrichedDocument


def export(path: Path) -> None:
    schema = EnrichedDocument.model_json_schema()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    target = repo_root / "developer-docs" / "evals" / "schema" / "enriched.schema.json"
    export(target)
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
