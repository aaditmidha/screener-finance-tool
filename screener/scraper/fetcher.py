"""High-level fetch orchestration: HTTP first, Playwright on a hard block.

This is the entry point the rest of the app should use. It hides the decision
of *how* a page is fetched behind a single :func:`fetch_page` call.
"""

import logging
from typing import Optional

from requests import Session

from screener.config import CONFIG
from screener.scraper import client, playwright_fetcher
from screener.scraper.exceptions import BlockedError

logger = logging.getLogger(__name__)

_pw_enabled = CONFIG["scraper"]["playwright"]["enabled"]


def fetch_page(url: str, session: Optional[Session] = None) -> str:
    """Fetch *url*, transparently falling back to Playwright if HTTP is blocked.

    Tries the fast :mod:`requests` path first. If the server hard-blocks the
    request (403/429/503 → :class:`BlockedError`) and Playwright is enabled in
    config, it retries the URL through a headless browser.

    Args:
        url: Fully-qualified URL to fetch.
        session: Optional requests session to reuse for the HTTP attempt.

    Returns:
        The page HTML as text.

    Raises:
        BlockedError: If the page is blocked and the Playwright fallback is
            disabled in config.
        FetchError: If both the HTTP and Playwright paths fail.
    """
    try:
        return client.fetch(url, session=session)
    except BlockedError as exc:
        if not _pw_enabled:
            logger.error("HTTP blocked for %s and Playwright fallback disabled", url)
            raise
        logger.info("HTTP blocked (HTTP %d); retrying %s via Playwright", exc.status_code, url)
        return playwright_fetcher.fetch(url)
