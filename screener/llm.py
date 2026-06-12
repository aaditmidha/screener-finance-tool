"""Shared LLM access — Groq only, by project policy.

Single place that builds the chat client and sends completions, so every
LLM-backed feature (tearsheet, guidance extraction, …) inherits the same
rules: the provider is always Groq (free tier; never the Anthropic API), and
the key comes from the env var named in ``llm.api_key_env`` — never hardcoded.

The client is injectable everywhere, so callers' tests never need a key.
"""

import logging
import os
from typing import Any, Protocol

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["llm"]


class LLMError(Exception):
    """Raised when the LLM client cannot be built or returns no usable text."""


class ChatClient(Protocol):
    """Structural type for the subset of the Groq client we use."""

    chat: Any  # client.chat.completions.create(...)


def resolve_provider() -> str:
    """Return the provider to use — always ``"groq"`` by project policy.

    Logs a warning if config asks for anything else, then uses Groq anyway.

    Returns:
        The string ``"groq"``.
    """
    configured = str(_cfg.get("provider", "groq")).lower()
    if configured != "groq":
        logger.warning(
            "LLM provider %r requested but project policy forces Groq; ignoring.",
            configured,
        )
    return "groq"


def build_client() -> ChatClient:
    """Construct a Groq client from the configured environment variable.

    Returns:
        An initialised Groq client.

    Raises:
        LLMError: If the key env var is unset or the groq package is missing.
    """
    resolve_provider()
    key_env = _cfg["api_key_env"]
    api_key = os.environ.get(key_env)
    if not api_key:
        raise LLMError(
            f"Environment variable {key_env!r} is not set; cannot call the Groq API."
        )
    try:
        from groq import Groq
    except ImportError as exc:
        raise LLMError("The 'groq' package is not installed.") from exc
    return Groq(api_key=api_key)


def chat(
    system_prompt: str,
    user_prompt: str,
    client: ChatClient | None = None,
    **overrides: Any,
) -> str:
    """Send one system+user exchange and return the assistant text.

    Args:
        system_prompt: System message content.
        user_prompt: User message content.
        client: Optional pre-built client (injected in tests). Defaults to a
            Groq client built from the environment.
        **overrides: Optional per-call overrides for model/temperature/
            max_tokens; defaults come from the ``llm`` config block.

    Returns:
        The assistant's response text, stripped.

    Raises:
        LLMError: If the response is malformed or empty.
    """
    client = client or build_client()
    response = client.chat.completions.create(
        model=overrides.get("model", _cfg["model"]),
        temperature=overrides.get("temperature", _cfg["temperature"]),
        max_tokens=overrides.get("max_tokens", _cfg["max_tokens"]),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    try:
        text = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        raise LLMError(f"Malformed LLM response: {exc}") from exc
    if not text or not text.strip():
        raise LLMError("LLM returned an empty response.")
    return text.strip()
