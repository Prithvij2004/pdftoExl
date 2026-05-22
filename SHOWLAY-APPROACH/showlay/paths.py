"""Filesystem paths for the standalone SHOWLAY app."""
from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else APP_DIR / path


RUNTIME_DIR = _path_from_env("SHOWLAY_RUNTIME_DIR", APP_DIR / "runtime")
SUPPORT_DOCS_DIR = _path_from_env("SHOWLAY_SUPPORT_DOCS_DIR", APP_DIR / "support_docs")

DEFAULT_TEMPLATE_FILENAME = "CHOICES Safety Determination Request Form Final_11_20.xlsx"
CHOICES_TEMPLATE_FILENAME = DEFAULT_TEMPLATE_FILENAME
LEGACY_CHOICES_TEMPLATE_FILENAME = (
    "CHOICES Safety Determination Request Form Final_11_20 1.xlsx"
)
TXLTSS_TEMPLATE_FILENAME = (
    "TX LTSS - 1700-3, Individual Service Plan - Signature Page.xlsx"
)
CHOICES_PDF_FILENAME = "CHOICES Safety Determination Form.pdf"
TXLTSS_PDF_FILENAME = "sph_rev25-3_H1700-3_final_approved.pdf"


def app_path(*parts: str) -> Path:
    return APP_DIR.joinpath(*parts)


def support_doc_path(filename: str, *, must_exist: bool = True) -> Path:
    path = SUPPORT_DOCS_DIR / filename
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"Missing support document: {path}")
    return path


def default_template_path() -> Path:
    candidates: list[Path] = []
    env_template = os.environ.get("SHOWLAY_TEMPLATE_PATH")
    if env_template:
        path = Path(env_template).expanduser()
        candidates.append(path if path.is_absolute() else APP_DIR / path)

    candidates.extend(
        [
            SUPPORT_DOCS_DIR / DEFAULT_TEMPLATE_FILENAME,
            SUPPORT_DOCS_DIR / LEGACY_CHOICES_TEMPLATE_FILENAME,
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    checked = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "Could not find the default template workbook inside SHOWLAY-APPROACH. "
        f"Checked: {checked}"
    )
