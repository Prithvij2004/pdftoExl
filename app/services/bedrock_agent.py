from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.settings import ModelSettings

from app.observability import configure_logfire

configure_logfire()


T = TypeVar("T", bound=BaseModel)


def build_bedrock_agent(
    *,
    model_id: str,
    region: str | None,
    output_type: type[T],
    system_prompt: str,
    bedrock_client: Any | None = None,
    max_tokens: int = 4096,
) -> Agent[None, T]:
    """Build a typed Bedrock-backed pydantic_ai Agent that returns `output_type`.

    Either `region` or an explicit `bedrock_client` must be provided.
    """
    if bedrock_client is None and region is None:
        raise ValueError("BEDROCK_REGION or an explicit bedrock_client is required.")

    if bedrock_client is not None:
        provider = BedrockProvider(bedrock_client=bedrock_client)
    else:
        provider = BedrockProvider(region_name=region)

    model = BedrockConverseModel(model_name=model_id, provider=provider)
    return Agent(
        model,
        output_type=output_type,
        system_prompt=system_prompt,
        model_settings=ModelSettings(max_tokens=max_tokens, temperature=0.0),
    )
