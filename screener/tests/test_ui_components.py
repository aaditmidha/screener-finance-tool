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


class TestOperationalToDf:
    def _op(self):
        from screener.models.operational import OperationalData, OperationalMetric
        return OperationalData(
            periods=["Mar 2024", "Mar 2025"],
            metrics=[
                OperationalMetric("EBITDA margin %", [0.20, None], "pct"),
                OperationalMetric("Asset turnover", [0.83, 1.10], "x"),
                OperationalMetric("Receivable days", [58.4, 60.0], "days"),
            ],
        )

    def test_formats_by_unit(self) -> None:
        df = components.operational_to_df(self._op())
        assert df.loc["EBITDA margin %", "Mar 2024"] == "20.0%"
        assert df.loc["Asset turnover", "Mar 2025"] == "1.10x"
        assert df.loc["Receivable days", "Mar 2024"] == "58"

    def test_none_rendered_as_dash(self) -> None:
        df = components.operational_to_df(self._op())
        assert df.loc["EBITDA margin %", "Mar 2025"] == "—"

    def test_empty_when_no_metrics(self) -> None:
        from screener.models.operational import OperationalData
        assert components.operational_to_df(OperationalData(periods=[], metrics=[])).empty


class TestHeadlineKpis:
    _PAGE = """
    <html><body><h1>Co</h1>
      <section id="profit-loss"><table>
        <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
        <tbody>
          <tr><td>Sales</td><td>10,000</td><td>11,000</td></tr>
          <tr><td>Operating Profit</td><td>2,000</td><td>2,200</td></tr>
          <tr><td>Net Profit</td><td>1,200</td><td>1,350</td></tr>
        </tbody>
      </table></section>
    </body></html>
    """

    def test_kpis_computed(self) -> None:
        fin = parse_company_financials(self._PAGE)
        kpis = dict(components.headline_kpis(fin))
        assert kpis["Revenue (₹ cr)"] == "11,000"
        assert kpis["Net profit (₹ cr)"] == "1,350"
        assert kpis["EBITDA margin"] == "20.0%"
        assert kpis["Revenue YoY"] == "+10.0%"

    def test_empty_without_pl(self) -> None:
        fin = parse_company_financials("<html><body></body></html>")
        assert components.headline_kpis(fin) == []


class TestStyleStatementDf:
    def test_returns_styler(self) -> None:
        import pandas as pd
        from pandas.io.formats.style import Styler
        df = pd.DataFrame({"Mar 2024": [10000.0, None]}, index=["Sales", "X"])
        assert isinstance(components.style_statement_df(df), Styler)


class TestDataQualityNote:
    def test_empty_when_all_exact(self) -> None:
        assert components.data_quality_note([], []) == ""

    def test_lists_both_kinds(self) -> None:
        note = components.data_quality_note(
            ["COGS ≈ Total Expenses"], ["trade receivables (DSRI neutral)"]
        )
        assert "Approximated" in note and "COGS" in note
        assert "Unavailable" in note and "receivables" in note


class TestForensicBadge:
    def _score(self, verdict: str, score: float):
        from screener.models.forensic_score import ForensicScore
        return ForensicScore(score=score, verdict=verdict, components=[])

    def test_healthy_green(self) -> None:
        emoji, _c, caption = components.forensic_badge(self._score("healthy", 82))
        assert emoji == "🟢"
        assert "82/100" in caption

    def test_high_risk_red(self) -> None:
        emoji, _c, caption = components.forensic_badge(self._score("high_risk", 30))
        assert emoji == "🔴"
        assert "high risk" in caption

    def test_gauge_reflects_score(self) -> None:
        figure = components.build_forensic_gauge(self._score("watch", 66))
        assert figure.data[0].type == "indicator"
        assert figure.data[0].value == 66
        # three coloured zones (red/amber/green)
        assert len(figure.data[0].gauge.steps) == 3


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
