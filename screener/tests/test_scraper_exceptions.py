"""Tests for the scraper exception hierarchy."""

from screener.scraper.exceptions import (
    BlockedError,
    FetchError,
    ParseError,
    ScraperError,
)


def test_all_inherit_scraper_error() -> None:
    """Every custom exception must be catchable as ScraperError."""
    assert issubclass(FetchError, ScraperError)
    assert issubclass(BlockedError, ScraperError)
    assert issubclass(ParseError, ScraperError)


def test_blocked_error_is_fetch_error() -> None:
    """BlockedError must be a specialisation of FetchError."""
    assert issubclass(BlockedError, FetchError)


def test_blocked_error_carries_status_code() -> None:
    """BlockedError must expose url and status_code attributes."""
    err = BlockedError("https://x.test/a", 403)
    assert err.url == "https://x.test/a"
    assert err.status_code == 403
    assert "403" in str(err)


def test_parse_error_carries_field() -> None:
    """ParseError must name the missing field in its message."""
    err = ParseError("https://x.test/a", "revenue")
    assert err.field == "revenue"
    assert "revenue" in str(err)
