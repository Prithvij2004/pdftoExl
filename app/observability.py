from __future__ import annotations

import os

_configured = False


def configure_logfire(service_name: str = "pdftoExl") -> None:
    """Initialise Logfire once per process.

    No-op when `logfire` is not installed. Sends spans only when a Logfire
    token is configured (via `LOGFIRE_TOKEN` or `logfire auth`); otherwise
    spans are recorded locally but not exported, so the app runs unchanged
    in environments without a token.
    """
    global _configured
    if _configured:
        return

    try:
        import logfire
    except ImportError:
        _configured = True
        return

    logfire.configure(
        service_name=service_name,
        send_to_logfire="if-token-present",
        console=False if os.environ.get("LOGFIRE_CONSOLE", "").lower() in {"0", "false", ""} else None,
    )
    logfire.instrument_pydantic_ai()
    _configured = True
