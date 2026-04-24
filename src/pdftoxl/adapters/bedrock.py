"""AWS Bedrock adapter.

Thin wrapper around boto3 so pipeline stages never import boto3 directly.
Credentials are resolved by boto3's default chain (env vars from `.env`,
shared credentials file, IAM role, etc.).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class LLMClient(Protocol):
    def invoke(self, prompt: str, *, system: str | None = None) -> str: ...


@dataclass(frozen=True)
class BedrockSettings:
    model_id: str
    region: str
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout_seconds: int = 60


class BedrockClient:
    """Invokes Anthropic models on AWS Bedrock via the Messages API."""

    def __init__(self, settings: BedrockSettings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: BedrockSettings) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "bedrock-runtime",
            region_name=settings.region,
            config=Config(read_timeout=settings.timeout_seconds),
        )

    def invoke(self, prompt: str, *, system: str | None = None) -> str:
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._settings.max_tokens,
            "temperature": self._settings.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

        response = self._client.invoke_model(
            modelId=self._settings.model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        parts = payload.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def build_bedrock_client(settings: BedrockSettings) -> LLMClient:
    return BedrockClient(settings)
