"""HTTP client: a reusable requests.Session with exponential-backoff retries.

This is the *primary* fetch path. When the server blocks plain HTTP requests
(403/429/503), callers should fall back to :mod:`screener.scraper.playwright_fetcher`.
"""

import logging
import time
from typing import Optional

import requests
from requests import Response, Session

from screener.config import CONFIG
from screener.scraper.exceptions import BlockedError, FetchError

logger = logging.getLogger(__name__)

_cfg = CONFIG["scraper"]


def build_session() -> Session:
    """Create a requests.Session pre-configured with the project User-Agent.

    Returns:
        A new :class:`requests.Session` with default headers applied.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": _cfg["user_agent"]})
    return session


def _backoff_delay(attempt: int) -> float:
    """Return the exponential-backoff delay (seconds) for a given attempt.

    Delay grows as ``base * 2**(attempt - 1)`` and is capped at the configured
    maximum to avoid unbounded waits.

    Args:
        attempt: 1-based attempt number that just failed.

    Returns:
        Delay in seconds before the next attempt.
    """
    base = _cfg["retry_backoff_seconds"]
    cap = _cfg["retry_backoff_max_seconds"]
    return min(base * (2 ** (attempt - 1)), cap)


def fetch(url: str, session: Optional[Session] = None) -> str:
    """Fetch *url* over HTTP with exponential-backoff retries.

    Args:
        url: Fully-qualified URL to fetch.
        session: Optional existing session to reuse; one is created if omitted.

    Returns:
        The response body as text.

    Raises:
        BlockedError: If the server returns a configured blocking status code.
            Callers may catch this to trigger the Playwright fallback.
        FetchError: If the request fails for any other reason after all retries.
    """
    s = session or build_session()
    attempts = _cfg["retry_attempts"]
    timeout = _cfg["request_timeout_seconds"]
    blocking_codes = set(_cfg["playwright"]["fallback_status_codes"])

    last_reason = "unknown error"

    for attempt in range(1, attempts + 1):
        try:
            response: Response = s.get(url, timeout=timeout)
        except requests.Timeout as exc:
            last_reason = f"timeout after {timeout}s"
            logger.warning("Attempt %d/%d timed out for %s: %s", attempt, attempts, url, exc)
        except requests.ConnectionError as exc:
            last_reason = f"connection error: {exc}"
            logger.warning("Attempt %d/%d connection error for %s: %s", attempt, attempts, url, exc)
        except requests.RequestException as exc:
            last_reason = f"request error: {exc}"
            logger.warning("Attempt %d/%d request error for %s: %s", attempt, attempts, url, exc)
        else:
            # Got a response — decide based on status code.
            if response.status_code in blocking_codes:
                logger.warning(
                    "Server blocked %s with HTTP %d (attempt %d/%d)",
                    url, response.status_code, attempt, attempts,
                )
                # No point retrying a hard block — surface immediately so the
                # caller can switch to the Playwright fallback.
                raise BlockedError(url, response.status_code)
            if response.ok:
                logger.debug("Fetched %s (HTTP %d, %d bytes)", url, response.status_code, len(response.content))
                time.sleep(_cfg["rate_limit_delay_seconds"])
                return response.text
            last_reason = f"HTTP {response.status_code}"
            logger.warning(
                "Attempt %d/%d got HTTP %d for %s", attempt, attempts, response.status_code, url
            )

        if attempt < attempts:
            delay = _backoff_delay(attempt)
            logger.info("Backing off %.1fs before retrying %s", delay, url)
            time.sleep(delay)

    raise FetchError(url, f"all {attempts} attempts failed ({last_reason})")
