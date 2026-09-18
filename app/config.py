"""GridWise service configuration.

All configuration is 12-factor: environment variables only, never baked-in secrets.
Documented (names only) in README.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PROVIDER = "mock"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_TRANSPORT_RETRIES = 1
DEFAULT_PORT = 8000

SUPPORTED_PROVIDERS = ("mock", "anthropic", "openai")

DEFAULT_MODEL_BY_PROVIDER = {
    "mock": "gridwise-mock-interpreter",
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o",
}


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    api_key: str | None
    timeout_seconds: float
    transport_retries: int
    port: int


def get_settings() -> Settings:
    """Read settings from the environment on every call (cheap, and test-friendly)."""
    provider = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        provider = DEFAULT_PROVIDER

    model = os.getenv("LLM_MODEL", "").strip() or DEFAULT_MODEL_BY_PROVIDER[provider]

    api_key = (
        os.getenv("LLM_API_KEY", "").strip()
        or os.getenv("ANTHROPIC_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or None
    )

    try:
        timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    timeout_seconds = max(1.0, min(timeout_seconds, 20.0))

    try:
        transport_retries = int(os.getenv("LLM_TRANSPORT_RETRIES", str(DEFAULT_TRANSPORT_RETRIES)))
    except ValueError:
        transport_retries = DEFAULT_TRANSPORT_RETRIES
    transport_retries = max(0, min(transport_retries, 3))

    try:
        port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    return Settings(
        provider=provider,
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        transport_retries=transport_retries,
        port=port,
    )
