"""Playwright-based fetcher used as a fallback when plain HTTP is blocked.

A real browser executes JavaScript and presents a full browser fingerprint,
which gets past most of the 403/429 walls that block :mod:`requests`. This is
slower and heavier, so it is only used when :mod:`screener.scraper.client`
raises :class:`~screener.scraper.exceptions.BlockedError`.
"""

import logging

from screener.config import CONFIG
from screener.scraper.exceptions import FetchError

logger = logging.getLogger(__name__)

_cfg = CONFIG["scraper"]
_pw_cfg = _cfg["playwright"]


def fetch(url: str) -> str:
    """Fetch *url* by driving a headless browser via Playwright.

    Args:
        url: Fully-qualified URL to fetch.

    Returns:
        The fully-rendered page HTML.

    Raises:
        FetchError: If Playwright is not installed/available, or navigation
            fails for any reason.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
    except ImportError as exc:
        raise FetchError(url, f"Playwright is not installed: {exc}") from exc

    logger.info("Falling back to Playwright for %s", url)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=_pw_cfg["headless"])
            try:
                page = browser.new_page(user_agent=_cfg["user_agent"])
                page.goto(url, timeout=_pw_cfg["nav_timeout_ms"], wait_until="domcontentloaded")
                html = page.content()
                logger.debug("Playwright fetched %s (%d bytes)", url, len(html))
                return html
            finally:
                browser.close()
    except PlaywrightTimeout as exc:
        raise FetchError(url, f"Playwright navigation timed out: {exc}") from exc
    except PlaywrightError as exc:
        raise FetchError(url, f"Playwright error: {exc}") from exc
