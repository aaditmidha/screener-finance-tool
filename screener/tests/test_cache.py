"""Tests for cache staleness logic."""

from datetime import datetime, timedelta, timezone

from screener.database import cache


def test_missing_record_is_stale() -> None:
    """A None timestamp (never stored) must be treated as stale."""
    assert cache.is_stale(None) is True


def test_fresh_record_is_not_stale() -> None:
    """A record updated just now must not be stale."""
    now = datetime.now(timezone.utc)
    assert cache.is_stale(now, now=now) is False


def test_old_record_is_stale() -> None:
    """A record older than max_age must be stale."""
    now = datetime.now(timezone.utc)
    old = now - cache.max_age() - timedelta(seconds=1)
    assert cache.is_stale(old, now=now) is True


def test_boundary_just_inside_window_is_fresh() -> None:
    """A record exactly one second inside the window must be fresh."""
    now = datetime.now(timezone.utc)
    recent = now - cache.max_age() + timedelta(seconds=1)
    assert cache.is_stale(recent, now=now) is False


def test_naive_timestamp_is_handled() -> None:
    """Naive datetimes must be treated as UTC, not raise."""
    now = datetime.now(timezone.utc)
    naive_old = (now - cache.max_age() - timedelta(days=1)).replace(tzinfo=None)
    assert cache.is_stale(naive_old, now=now) is True
