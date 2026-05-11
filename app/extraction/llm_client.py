from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseLLMClient(ABC):
    @abstractmethod
    def invoke_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Invoke the LLM and return a JSON object matching the requested schema."""
