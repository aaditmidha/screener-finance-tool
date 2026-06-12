"""Cache staleness logic.

Decides whether a stored record is fresh enough to use or should be re-scraped,
based on ``cache.max_age_days`` in config.yaml.
"""

import logging
from datetime import datetime, timedelta, timezone

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_max_age_days: int = CONFIG["cache"]["max_age_days"]


def max_age() -> timedelta:
    """Return the configured maximum cache age as a timedelta."""
    return timedelta(days=_max_age_days)


def is_stale(last_updated: datetime | None, now: datetime | None = None) -> bool:
    """Return True if a record's data is older than the configured max age.

    Args:
        last_updated: When the record was last written. ``None`` (never stored)
            is treated as stale so a first scrape always runs.
        now: Current time, injectable for testing. Defaults to UTC now.

    Returns:
        True if the record is missing or older than ``cache.max_age_days``.
    """
    if last_updated is None:
        return True

    current = now or datetime.now(timezone.utc)

    # Treat naive timestamps as UTC so comparisons never raise.
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    age = current - last_updated
    stale = age > max_age()
    logger.debug("Cache age=%s stale=%s (max=%s)", age, stale, max_age())
    return stale
