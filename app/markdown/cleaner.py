from __future__ import annotations

import re


_BULLET_RE = re.compile(r"^(\s*)[•●○▪■]\s+")
_SPACES_RE = re.compile(r"[ \t]{2,}")


def clean_markdown_line(line: str) -> str:
    line = line.rstrip()
    line = _BULLET_RE.sub(r"\1- ", line)

    stripped = line.lstrip()
    if stripped.startswith("|") or "<!-- page:" in stripped:
        return line

    if "_" not in line:
        line = _SPACES_RE.sub(" ", line)
    return line
