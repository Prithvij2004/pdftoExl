from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BasePDFParser(ABC):
    @abstractmethod
    def parse_to_markdown(self, pdf_path: Path) -> str:
        """Parse a PDF file and return Markdown."""
