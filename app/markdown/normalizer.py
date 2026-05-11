from __future__ import annotations

import re

from app.markdown.cleaner import clean_markdown_line


_PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.IGNORECASE)


def normalize_markdown(markdown: str) -> str:
    markdown = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    markdown = _PAGE_MARKER_RE.sub(lambda m: f"<!-- page: {m.group(1)} -->", markdown)

    out: list[str] = []
    blank_count = 0
    for raw_line in markdown.split("\n"):
        line = clean_markdown_line(raw_line)
        if not line.strip():
            blank_count += 1
            if blank_count <= 1:
                out.append("")
            continue

        blank_count = 0
        out.append(line)

    normalized = "\n".join(out).strip()
    return normalized + "\n" if normalized else ""
