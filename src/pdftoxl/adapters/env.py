"""Load .env into os.environ. Kept here so pipeline code never imports dotenv."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(dotenv_path: Path | None = None) -> None:
    from dotenv import load_dotenv

    path = dotenv_path or Path(os.environ.get("PDFTOXL_DOTENV", ".env"))
    if path.exists():
        load_dotenv(path, override=False)
