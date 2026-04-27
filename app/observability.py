from __future__ import annotations

import logfire
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

from app.config import settings


_configured = False


def configure_logfire() -> None:
    """Idempotently configure Logfire and instrument the libraries we use.

    Honors LOGFIRE_TOKEN via settings.logfire_token. When no token is set and
    `send_to_logfire="if-token-present"`, Logfire still records spans locally
    but does not export them — useful for tests.
    """
    global _configured
    if _configured:
        return

    logfire.configure(
        token=settings.logfire_token,
        service_name=settings.logfire_service_name,
        environment=settings.logfire_environment,
        send_to_logfire=settings.logfire_send_to_logfire,
    )

    logfire.instrument_pydantic_ai()
    BotocoreInstrumentor().instrument()

    _configured = True
