"""Tests for the shared LLM helper (Groq policy, client building, chat)."""

from types import SimpleNamespace

import pytest

from screener import llm
from screener.llm import LLMError, build_client, chat, resolve_provider


class _FakeClient:
    def __init__(self, content: str | None) -> None:
        self.last_kwargs: dict | None = None
        message = SimpleNamespace(content=content)
        self._response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class TestResolveProvider:
    def test_default_is_groq(self) -> None:
        assert resolve_provider() == "groq"

    def test_forces_groq_over_other_providers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project policy: even 'anthropic' in config must resolve to Groq."""
        monkeypatch.setitem(llm._cfg, "provider", "anthropic")
        assert resolve_provider() == "groq"


class TestBuildClient:
    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(llm._cfg["api_key_env"], raising=False)
        with pytest.raises(LLMError):
            build_client()


class TestChat:
    def test_returns_stripped_content(self) -> None:
        client = _FakeClient("  hello  ")
        assert chat("sys", "user", client=client) == "hello"

    def test_uses_config_defaults(self) -> None:
        client = _FakeClient("ok")
        chat("sys", "user", client=client)
        assert client.last_kwargs["model"] == llm._cfg["model"]
        roles = [m["role"] for m in client.last_kwargs["messages"]]
        assert roles == ["system", "user"]

    def test_overrides_take_precedence(self) -> None:
        client = _FakeClient("ok")
        chat("sys", "user", client=client, max_tokens=42)
        assert client.last_kwargs["max_tokens"] == 42

    def test_empty_response_raises(self) -> None:
        with pytest.raises(LLMError):
            chat("sys", "user", client=_FakeClient("   "))

    def test_none_content_raises(self) -> None:
        with pytest.raises(LLMError):
            chat("sys", "user", client=_FakeClient(None))
