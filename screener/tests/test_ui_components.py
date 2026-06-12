"""Tests for the pure UI helper functions (no Streamlit needed)."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from screener.database import cache
from screener.models.beneish import BeneishIndices, BeneishResult
from screener.scraper.parser import parse_company_financials
from screener.ui import components

_PAGE_NO_WC = """
<html><body><h1>Infosys</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody><tr><td>Sales</td><td>146,767</td><td>153,670</td></tr></tbody>
  </table></section>
</body></html>
"""

_PAGE_WITH_WC = """
<html><body><h1>ManufCo</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>8,000</td><td>9,000</td></tr>
      <tr><td>Operating Profit</td><td>3,200</td><td>3,600</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody>
      <tr><td>Trade receivables</td><td>3,200</td><td>3,400</td></tr>
      <tr><td>Inventories</td><td>5,600</td><td>5,800</td></tr>
      <tr><td>Trade payables</td><td>3,400</td><td>3,700</td></tr>
    </tbody>
  </table></section>
</body></html>
"""


def _beneish(verdict: str, score: float) -> BeneishResult:
    idx = BeneishIndices(1, 1, 1, 1, 1, 1, 1, 0)
    return BeneishResult(m_score=score, indices=idx, verdict=verdict)


class TestFinancialTableToDf:
    def test_converts_rows_and_periods(self) -> None:
        fin = parse_company_financials(_PAGE_NO_WC)
        df = components.financial_table_to_df(fin.profit_loss)
        assert list(df.columns) == ["Mar 2023", "Mar 2024"]
        assert df.loc["Sales", "Mar 2024"] == 153670.0

    def test_none_table_empty_df(self) -> None:
        assert components.financial_table_to_df(None).empty


class TestBeneishFlag:
    def test_manipulator_is_red(self) -> None:
        emoji, colour, caption = components.beneish_flag(_beneish("manipulator", -1.5))
        assert emoji == "🔴"
        assert "manipulator" in caption

    def test_non_manipulator_is_green(self) -> None:
        emoji, _colour, _caption = components.beneish_flag(_beneish("non_manipulator", -2.5))
        assert emoji == "🟢"

    def test_none_is_neutral(self) -> None:
        emoji, _colour, caption = components.beneish_flag(None)
        assert emoji == "⚪"
        assert "unavailable" in caption.lower()


class TestFormatFreshness:
    def test_never(self) -> None:
        assert components.format_freshness(None) == "Never scraped"

    def test_hours_ago_fresh(self) -> None:
        now = datetime(2026, 6, 12, tzinfo=timezone.utc)
        updated = now - timedelta(hours=2)
        text = components.format_freshness(updated, now=now)
        assert "2h ago" in text and "fresh" in text

    def test_stale_when_beyond_window(self) -> None:
        now = datetime(2026, 6, 12, tzinfo=timezone.utc)
        updated = now - cache.max_age() - timedelta(days=1)
        text = components.format_freshness(updated, now=now)
        assert "stale" in text

    def test_minutes_ago(self) -> None:
        now = datetime(2026, 6, 12, tzinfo=timezone.utc)
        updated = now - timedelta(minutes=30)
        assert "30m ago" in components.format_freshness(updated, now=now)


class TestDataQualityNote:
    def test_empty_when_all_exact(self) -> None:
        assert components.data_quality_note([], []) == ""

    def test_lists_both_kinds(self) -> None:
        note = components.data_quality_note(
            ["COGS ≈ Total Expenses"], ["trade receivables (DSRI neutral)"]
        )
        assert "Approximated" in note and "COGS" in note
        assert "Unavailable" in note and "receivables" in note


class TestPledgeComponents:
    def _result(self, level: str, latest: float = 25.0, rising: bool = False):
        from screener.models.pledge_monitor import PledgeResult
        return PledgeResult(latest_pct=latest, max_pct=latest,
                            rising=rising, risk_level=level)

    def test_high_risk_is_red(self) -> None:
        emoji, _c, caption = components.pledge_badge(self._result("high", 45.0))
        assert emoji == "🔴"
        assert "45.0%" in caption

    def test_low_risk_is_green(self) -> None:
        emoji, _c, _t = components.pledge_badge(self._result("low", 5.0))
        assert emoji == "🟢"

    def test_rising_noted_in_caption(self) -> None:
        _e, _c, caption = components.pledge_badge(self._result("high", 30.0, rising=True))
        assert "rising" in caption

    def test_pledge_figure_has_threshold_lines(self) -> None:
        from screener.models.pledge_monitor import PledgePoint
        history = [PledgePoint("Mar 2023", 10.0), PledgePoint("Mar 2024", 30.0)]
        figure = components.build_pledge_figure(history)
        assert list(figure.data[0].y) == [10.0, 30.0]
        # warning + critical hlines present as layout shapes
        assert len(figure.layout.shapes) == 2


class TestWorkingCapital:
    def test_missing_rows_returns_empty(self) -> None:
        fin = parse_company_financials(_PAGE_NO_WC)
        assert components.working_capital_quarters(fin) == []

    def test_builds_quarters_when_rows_present(self) -> None:
        fin = parse_company_financials(_PAGE_WITH_WC)
        quarters = components.working_capital_quarters(fin)
        assert len(quarters) == 2
        q = quarters[-1]
        assert q.label == "Mar 2024"
        assert q.revenue == pytest.approx(9000)
        # COGS = Sales − Operating Profit = 9000 − 3600
        assert q.cogs == pytest.approx(5400)
        assert q.receivables == pytest.approx(3400)

    def test_heatmap_figure_has_four_metric_rows(self) -> None:
        fin = parse_company_financials(_PAGE_WITH_WC)
        quarters = components.working_capital_quarters(fin)
        from screener.models import working_capital as wc
        figure = components.build_wc_heatmap_figure(wc.heatmap_data(quarters))
        # one Heatmap trace, 4 metric rows (DSO/DIO/DPO/CCC)
        assert figure.data[0].type == "heatmap"
        assert list(figure.data[0].y) == ["DSO", "DIO", "DPO", "CCC"]
        assert len(figure.data[0].z) == 4
