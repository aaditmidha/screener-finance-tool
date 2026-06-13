"""HTTP client: a reusable requests.Session with exponential-backoff retries.

This is the *primary* fetch path. When the server blocks plain HTTP requests
(403/429/503), callers should fall back to :mod:`screener.scraper.playwright_fetcher`.
"""

import logging
import random
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

    Delay grows as ``base * 2**(attempt - 1)``, capped at the configured
    maximum, plus random jitter in ``[0, jitter_seconds]``. Jitter prevents a
    thundering-herd of synchronised retries hammering the server in lockstep.

    Args:
        attempt: 1-based attempt number that just failed.

    Returns:
        Delay in seconds before the next attempt.
    """
    base = _cfg["retry_backoff_seconds"]
    cap = _cfg["retry_backoff_max_seconds"]
    jitter = _cfg.get("retry", {}).get("jitter_seconds", 0.0)
    return min(base * (2 ** (attempt - 1)), cap) + random.uniform(0, jitter)


def _retry_after_seconds(response: Response) -> float | None:
    """Return the server's Retry-After delay in seconds, if it sent one.

    Args:
        response: The HTTP response (typically a 429/503).

    Returns:
        The delay in seconds for a numeric Retry-After header, else None.
    """
    raw = response.headers.get("Retry-After", "").strip()
    if raw.isdigit():
        return float(raw)
    return None


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
    _retry_cfg = _cfg.get("retry", {})
    block_retries = _retry_cfg.get("block_retries", 0)
    transient_codes = set(_retry_cfg.get("transient_codes", []))

    last_reason = "unknown error"
    block_seen = 0

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
                code = response.status_code
                # Transient blocks (429/503) get a few polite waits — honouring
                # Retry-After when present. A hard block (403) fails fast so the
                # caller switches to the Playwright fallback without delay.
                if code in transient_codes and block_seen < block_retries and attempt < attempts:
                    block_seen += 1
                    wait = _retry_after_seconds(response) or _backoff_delay(attempt)
                    logger.warning(
                        "Transient block %s HTTP %d; waiting %.1fs (block retry %d/%d)",
                        url, code, wait, block_seen, block_retries,
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    "Server blocked %s with HTTP %d — surfacing to Playwright fallback",
                    url, code,
                )
                raise BlockedError(url, code)
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
