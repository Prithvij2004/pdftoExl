from __future__ import annotations

import json
import re
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from app.config import settings
from app.extraction.llm_client import BaseLLMClient


JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _preferred_inference_profile_prefix(aws_region: str) -> str:
    return "eu" if aws_region.startswith("eu-") else "us"


def _ensure_inference_profile_id(model_id: str, aws_region: str) -> str:
    model_id = (model_id or "").strip()
    if model_id.startswith(("us.", "eu.")):
        return model_id
    if model_id.startswith("amazon.nova-"):
        return f"{_preferred_inference_profile_prefix(aws_region)}.{model_id}"
    return model_id


def _parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    candidates = [m.group(1).strip() for m in JSON_BLOCK_RE.finditer(text)]
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    candidates.append(text)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("Model response did not contain a valid JSON object.")


def _save_failed_response(prompt: str, response: str, error: Exception, attempt: int) -> None:
    out_dir = settings.output_dir / "llm_failures"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    path = out_dir / f"llm_failure_{stamp}_attempt_{attempt + 1}.json"
    payload = {
        "error": str(error),
        "attempt": attempt + 1,
        "prompt_preview": prompt[:4000],
        "response": response,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class BedrockLLMClient(BaseLLMClient):
    def __init__(self) -> None:
        cfg = Config(connect_timeout=60, read_timeout=900, retries={"max_attempts": 1})
        self._client = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)
        self._model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)

    def _invoke_text(self, prompt: str) -> str:
        try:
            resp = self._client.converse(
                modelId=self._model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": settings.llm_max_tokens,
                    "temperature": settings.llm_temperature,
                },
            )
        except NoCredentialsError as e:
            raise RuntimeError("AWS credentials are required for Bedrock extraction.") from e
        except (ClientError, BotoCoreError) as e:
            raise RuntimeError(f"Bedrock invocation failed: {e}") from e

        content = resp.get("output", {}).get("message", {}).get("content", [])
        if not content:
            raise RuntimeError("Bedrock returned an empty response.")
        parts = [block.get("text", "") for block in content if isinstance(block, dict)]
        text = "\n".join(part for part in parts if part)
        if not text.strip():
            raise RuntimeError("Bedrock returned an empty text response.")
        return text

    def invoke_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        schema_text = json.dumps(schema, ensure_ascii=False)
        last_error: Exception | None = None

        for attempt in range(3):
            suffix = (
                "\n\nReturn JSON only. The JSON must conform to this JSON Schema:\n"
                f"{schema_text}"
            )
            if last_error is not None:
                suffix += (
                    "\n\nYour previous response was invalid JSON or did not match the schema. "
                    "Repair it. Return a single JSON object only. "
                    "If the previous output was too long, reduce source_text/display_text to short excerpts "
                    "and do not copy blank underline runs."
                )
            try:
                full_prompt = prompt + suffix
                response = self._invoke_text(full_prompt)
                try:
                    return _parse_json_object(response)
                except Exception as parse_error:
                    _save_failed_response(full_prompt, response, parse_error, attempt)
                    raise
            except Exception as e:
                last_error = e
                time.sleep(2**attempt)

        raise RuntimeError(f"LLM failed to return valid JSON after retries: {last_error}") from last_error
