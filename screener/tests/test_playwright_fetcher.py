"""Tests for the Playwright fallback fetcher.

These mock out ``playwright.sync_api.sync_playwright`` so no real browser is
launched — we only verify the orchestration and error mapping.
"""

from unittest.mock import MagicMock

import pytest

from screener.scraper import playwright_fetcher
from screener.scraper.exceptions import FetchError


def _fake_sync_playwright(html: str) -> MagicMock:
    """Build a fake sync_playwright() context manager returning *html*."""
    fake_pw = MagicMock()
    page = fake_pw.chromium.launch.return_value.new_page.return_value
    page.content.return_value = html

    cm = MagicMock()
    cm.__enter__.return_value = fake_pw
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def test_returns_rendered_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: returns the page content from the headless browser."""
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", _fake_sync_playwright("<html>rendered</html>")
    )
    assert playwright_fetcher.fetch("https://x.test/a") == "<html>rendered</html>"


def test_timeout_maps_to_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Playwright timeout should be wrapped as a FetchError."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    fake_pw = MagicMock()
    page = fake_pw.chromium.launch.return_value.new_page.return_value
    page.goto.side_effect = PlaywrightTimeout("nav timed out")

    cm = MagicMock()
    cm.__enter__.return_value = fake_pw
    cm.__exit__.return_value = False
    monkeypatch.setattr("playwright.sync_api.sync_playwright", MagicMock(return_value=cm))

    with pytest.raises(FetchError):
        playwright_fetcher.fetch("https://x.test/a")
