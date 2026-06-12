"""Tests for the fetch orchestration (HTTP → Playwright fallback)."""

import pytest

from screener.scraper import fetcher
from screener.scraper.exceptions import BlockedError


def test_returns_http_result_when_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """When HTTP succeeds, Playwright must not be called."""
    monkeypatch.setattr(fetcher.client, "fetch", lambda url, session=None: "<html>http</html>")

    def _fail(_url: str) -> str:
        raise AssertionError("Playwright should not be called on success")

    monkeypatch.setattr(fetcher.playwright_fetcher, "fetch", _fail)
    assert fetcher.fetch_page("https://x.test/a") == "<html>http</html>"


def test_falls_back_to_playwright_on_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BlockedError from HTTP should trigger the Playwright fallback."""
    def _blocked(url: str, session: object = None) -> str:
        raise BlockedError(url, 403)

    monkeypatch.setattr(fetcher.client, "fetch", _blocked)
    monkeypatch.setattr(fetcher.playwright_fetcher, "fetch", lambda url: "<html>pw</html>")
    monkeypatch.setattr(fetcher, "_pw_enabled", True)
    assert fetcher.fetch_page("https://x.test/a") == "<html>pw</html>"


def test_block_reraised_when_fallback_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Playwright disabled, a BlockedError should propagate."""
    def _blocked(url: str, session: object = None) -> str:
        raise BlockedError(url, 429)

    monkeypatch.setattr(fetcher.client, "fetch", _blocked)
    monkeypatch.setattr(fetcher, "_pw_enabled", False)
    with pytest.raises(BlockedError):
        fetcher.fetch_page("https://x.test/a")
