"""Tests for the HTTP client (retry, backoff, blocking, success)."""

from unittest.mock import MagicMock

import pytest
import requests

from screener.scraper import client
from screener.scraper.exceptions import BlockedError, FetchError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real sleeps so retry/backoff tests run instantly."""
    monkeypatch.setattr(client.time, "sleep", lambda _s: None)


def _response(status_code: int, text: str = "<html></html>") -> MagicMock:
    """Build a fake requests.Response with the given status and body."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = text.encode()
    resp.ok = 200 <= status_code < 400
    return resp


def _session_returning(*responses: MagicMock) -> MagicMock:
    """Build a fake Session whose .get() yields the given responses in order."""
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    return session


def test_successful_fetch_returns_text() -> None:
    """A 200 response should return the body text."""
    session = _session_returning(_response(200, "<html>ok</html>"))
    assert client.fetch("https://x.test/a", session=session) == "<html>ok</html>"


def test_retries_then_succeeds() -> None:
    """A transient 500 followed by a 200 should succeed."""
    session = _session_returning(_response(500), _response(200, "recovered"))
    assert client.fetch("https://x.test/a", session=session) == "recovered"
    assert session.get.call_count == 2


def test_blocking_status_raises_blocked_error() -> None:
    """A 403 should raise BlockedError immediately (no further retries)."""
    session = _session_returning(_response(403))
    with pytest.raises(BlockedError) as exc_info:
        client.fetch("https://x.test/a", session=session)
    assert exc_info.value.status_code == 403
    assert session.get.call_count == 1


def test_exhausted_retries_raise_fetch_error() -> None:
    """Repeated 500s should raise FetchError after all attempts."""
    session = _session_returning(_response(500), _response(500), _response(500))
    with pytest.raises(FetchError):
        client.fetch("https://x.test/a", session=session)


def test_timeout_is_retried_then_raises() -> None:
    """Connection timeouts should be retried and finally raise FetchError."""
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.Timeout("slow")
    with pytest.raises(FetchError):
        client.fetch("https://x.test/a", session=session)


def test_backoff_grows_exponentially() -> None:
    """_backoff_delay should double each attempt up to the configured cap."""
    d1 = client._backoff_delay(1)
    d2 = client._backoff_delay(2)
    d3 = client._backoff_delay(3)
    assert d2 == pytest.approx(d1 * 2)
    assert d3 == pytest.approx(d1 * 4)
    cap = client._cfg["retry_backoff_max_seconds"]
    assert client._backoff_delay(20) <= cap
