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


def _response(status_code: int, text: str = "<html></html>",
              headers: dict | None = None) -> MagicMock:
    """Build a fake requests.Response with the given status and body."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = text.encode()
    resp.ok = 200 <= status_code < 400
    resp.headers = headers or {}
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
    """A 403 (hard block) should raise BlockedError immediately."""
    session = _session_returning(_response(403))
    with pytest.raises(BlockedError) as exc_info:
        client.fetch("https://x.test/a", session=session)
    assert exc_info.value.status_code == 403
    assert session.get.call_count == 1   # no polite retries for a hard block


def test_transient_block_retries_then_raises() -> None:
    """A persistent 429 should be politely retried before BlockedError."""
    session = _session_returning(_response(429), _response(429), _response(429))
    with pytest.raises(BlockedError) as exc_info:
        client.fetch("https://x.test/a", session=session)
    assert exc_info.value.status_code == 429
    # block_retries=2 → 2 polite retries, then raise on the 3rd attempt.
    assert session.get.call_count == 3


def test_transient_block_then_success() -> None:
    """A 503 that clears should succeed without surfacing a block."""
    session = _session_returning(_response(503), _response(200, "recovered"))
    assert client.fetch("https://x.test/a", session=session) == "recovered"


def test_retry_after_header_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A numeric Retry-After should drive the wait on a 429."""
    waits: list[float] = []
    monkeypatch.setattr(client.time, "sleep", lambda s: waits.append(s))
    session = _session_returning(_response(429, headers={"Retry-After": "7"}),
                                 _response(200, "ok"))
    assert client.fetch("https://x.test/a", session=session) == "ok"
    assert waits[0] == 7.0


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


def test_backoff_grows_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    """_backoff_delay should double each attempt up to the configured cap."""
    # Pin jitter to 0 so the exponential relationship is exact.
    monkeypatch.setattr(client.random, "uniform", lambda _lo, _hi: 0.0)
    d1 = client._backoff_delay(1)
    d2 = client._backoff_delay(2)
    d3 = client._backoff_delay(3)
    assert d2 == pytest.approx(d1 * 2)
    assert d3 == pytest.approx(d1 * 4)
    cap = client._cfg["retry_backoff_max_seconds"]
    assert client._backoff_delay(20) <= cap + client._cfg["retry"]["jitter_seconds"]


def test_backoff_includes_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jitter must be added on top of the exponential base delay."""
    monkeypatch.setattr(client.random, "uniform", lambda _lo, hi: hi)  # max jitter
    base = client._cfg["retry_backoff_seconds"]
    jitter = client._cfg["retry"]["jitter_seconds"]
    assert client._backoff_delay(1) == pytest.approx(base + jitter)
