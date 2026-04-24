from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import FixtureManifest


def load_fixtures(yaml_path: Path, repo_root: Path | None = None) -> list[FixtureManifest]:
    yaml_path = Path(yaml_path)
    root = repo_root or yaml_path.parent.parent
    with yaml_path.open() as fh:
        data = yaml.safe_load(fh)
    items = data.get("fixtures", [])
    out: list[FixtureManifest] = []
    for item in items:
        resolved = dict(item)
        for key in ("pdf_path", "golden_xlsx_path", "reference_xlsx_path"):
            p = Path(resolved[key])
            if not p.is_absolute():
                p = (root / p).resolve()
            resolved[key] = p
        out.append(FixtureManifest(**resolved))
    return out


def find_fixture(fixtures: list[FixtureManifest], fixture_id: str) -> FixtureManifest:
    for f in fixtures:
        if f.id == fixture_id:
            return f
    raise KeyError(f"Fixture not found: {fixture_id}")
