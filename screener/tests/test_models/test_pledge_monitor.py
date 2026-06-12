"""Tests for the promoter pledge risk monitor."""

import pytest

from screener.models.pledge_monitor import (
    PledgePoint,
    PledgeResult,
    analyze,
    parse_pledge_history,
)

# Screener-style shareholding section with a pledge row.
_PAGE = """
<html><body>
  <section id="shareholding">
    <table>
      <thead><tr><th></th><th>Mar 2023</th><th>Sep 2023</th><th>Mar 2024</th><th>Sep 2024</th></tr></thead>
      <tbody>
        <tr><td>Promoters +</td><td>55.0</td><td>55.0</td><td>54.8</td><td>54.5</td></tr>
        <tr><td>Pledged percentage</td><td>5.0</td><td>18.0</td><td>25.0</td><td>45.0</td></tr>
      </tbody>
    </table>
  </section>
</body></html>
"""


def _series(*pcts: float) -> list[PledgePoint]:
    return [PledgePoint(period=f"P{i}", pledge_pct=p) for i, p in enumerate(pcts)]


class TestParse:
    def test_extracts_pledge_row(self) -> None:
        points = parse_pledge_history(_PAGE)
        assert [p.pledge_pct for p in points] == [5.0, 18.0, 25.0, 45.0]
        assert points[0].period == "Mar 2023"

    def test_page_without_shareholding_returns_empty(self) -> None:
        assert parse_pledge_history("<html><body></body></html>") == []


class TestAnalyze:
    def test_zero_pledge_is_none_risk(self) -> None:
        result = analyze(_series(0.0, 0.0, 0.0))
        assert result.risk_level == "none"
        assert result.crossings == []

    def test_low_stable_pledge(self) -> None:
        result = analyze(_series(5.0, 6.0, 5.5))
        assert result.risk_level == "low"

    def test_warning_crossing_detected(self) -> None:
        result = analyze(_series(10.0, 25.0))
        assert ("P1", 20.0) in result.crossings
        assert result.risk_level in ("elevated", "high")

    def test_critical_crossing_is_high(self) -> None:
        result = analyze(_series(10.0, 25.0, 45.0))
        assert ("P2", 40.0) in result.crossings
        assert result.risk_level == "high"

    def test_rising_above_warning_is_high(self) -> None:
        """>20% and still climbing must escalate to high even below 40%."""
        result = analyze(_series(15.0, 22.0, 28.0, 35.0))
        assert result.rising is True
        assert result.risk_level == "high"

    def test_max_and_latest_tracked(self) -> None:
        result = analyze(_series(10.0, 45.0, 30.0))
        assert result.max_pct == 45.0
        assert result.latest_pct == 30.0

    def test_empty_history_raises(self) -> None:
        with pytest.raises(ValueError):
            analyze([])

    def test_returns_result_type(self) -> None:
        assert isinstance(analyze(_series(1.0)), PledgeResult)


class TestPriceCrossReference:
    def test_drop_after_crossing_flagged(self) -> None:
        """A >15% fall within 2 periods of a crossing must produce an event."""
        history = _series(10.0, 25.0, 26.0, 27.0)
        prices = {"P0": 100.0, "P1": 100.0, "P2": 80.0, "P3": 78.0}
        result = analyze(history, prices=prices)
        assert len(result.price_events) == 1
        event = result.price_events[0]
        assert event.period == "P1"
        # Worst return within the lookahead window: 78/100 − 1
        assert event.price_drop == pytest.approx(-0.22)

    def test_no_event_when_price_holds(self) -> None:
        history = _series(10.0, 25.0, 26.0)
        prices = {"P0": 100.0, "P1": 100.0, "P2": 98.0}
        result = analyze(history, prices=prices)
        assert result.price_events == []

    def test_no_prices_no_events(self) -> None:
        result = analyze(_series(10.0, 25.0))
        assert result.price_events == []
