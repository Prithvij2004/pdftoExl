"""Tests for the adapters: env loader, logging config, Bedrock wrapper."""
from __future__ import annotations

import json

from pdftoxl.adapters.bedrock import (
    BedrockClient,
    BedrockSettings,
    build_bedrock_client,
)
from pdftoxl.adapters.env import load_env
from pdftoxl.adapters.logging import configure_logging, get_logger


def test_load_env_no_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PDFTOXL_DOTENV", raising=False)
    # No .env here; should not raise.
    load_env()


def test_load_env_reads_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_VAR=hello-from-dotenv\n")
    monkeypatch.delenv("MY_TEST_VAR", raising=False)
    load_env(env_file)
    import os
    assert os.environ.get("MY_TEST_VAR") == "hello-from-dotenv"


def test_load_env_honors_dotenv_env_var(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("MY_OTHER_VAR=yes\n")
    monkeypatch.setenv("PDFTOXL_DOTENV", str(env_file))
    monkeypatch.delenv("MY_OTHER_VAR", raising=False)
    load_env()
    import os
    assert os.environ.get("MY_OTHER_VAR") == "yes"


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ALREADY_SET=from-dotenv\n")
    monkeypatch.setenv("ALREADY_SET", "from-process")
    load_env(env_file)
    import os
    assert os.environ["ALREADY_SET"] == "from-process"


def test_configure_logging_console_smoke():
    configure_logging(level="DEBUG", renderer="console")
    log = get_logger("unit")
    # Should not raise when emitting a record.
    log.info("hello", key="value")


def test_configure_logging_json_renderer():
    configure_logging(level="INFO", renderer="json")
    log = get_logger("unit")
    log.info("event", k=1)


def test_configure_logging_invalid_level_does_not_raise():
    # The helper uses getattr(..., default=INFO) so an invalid level name
    # must not crash the process.
    configure_logging(level="NOT_A_LEVEL", renderer="console")
    get_logger("unit").info("still-works")


def test_get_logger_binds_stage():
    log = get_logger("my-stage")
    # structlog BoundLogger exposes `_context` dict with bound values.
    ctx = log._context if hasattr(log, "_context") else {}
    assert ctx.get("stage") == "my-stage"


class _FakeBody:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw


class _FakeBedrockRuntime:
    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    def invoke_model(self, *, modelId, body, contentType, accept):
        self.calls.append(
            {"modelId": modelId, "body": body, "contentType": contentType, "accept": accept}
        )
        return {"body": _FakeBody(self._payload)}


def test_bedrock_client_invoke_builds_messages_body_and_joins_text():
    fake = _FakeBedrockRuntime(
        {
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "tool_use", "text": "IGNORED"},
                {"type": "text", "text": "world"},
            ]
        }
    )
    settings = BedrockSettings(model_id="m1", region="us-east-1", max_tokens=256, temperature=0.2)
    client = BedrockClient(settings, client=fake)
    result = client.invoke("prompt goes here", system="be brief")

    assert result == "Hello world"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["modelId"] == "m1"
    assert call["contentType"] == "application/json"
    body = json.loads(call["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.2
    assert body["messages"] == [{"role": "user", "content": "prompt goes here"}]
    assert body["system"] == "be brief"


def test_bedrock_client_omits_system_when_none():
    fake = _FakeBedrockRuntime({"content": [{"type": "text", "text": "ok"}]})
    client = BedrockClient(
        BedrockSettings(model_id="m", region="us-east-1"), client=fake
    )
    client.invoke("hi")
    body = json.loads(fake.calls[0]["body"])
    assert "system" not in body


def test_bedrock_client_empty_content_returns_empty_string():
    fake = _FakeBedrockRuntime({"content": []})
    client = BedrockClient(
        BedrockSettings(model_id="m", region="us-east-1"), client=fake
    )
    assert client.invoke("hi") == ""


def test_build_bedrock_client_returns_bedrockclient(monkeypatch):
    """build_bedrock_client builds a real client — patch boto3 so no AWS is touched."""
    import pdftoxl.adapters.bedrock as mod

    monkeypatch.setattr(
        mod.BedrockClient, "_build_client", staticmethod(lambda s: object())
    )
    client = build_bedrock_client(BedrockSettings(model_id="m", region="us-east-1"))
    assert isinstance(client, BedrockClient)
