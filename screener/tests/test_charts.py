"""Tests for the focus-chart builders (pure Plotly figure factories)."""

import pytest

from screener.scraper.parser import parse_company_financials
from screener.ui import charts

_PAGE = """
<html><body><h1>Co</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>9,000</td><td>10,000</td><td>11,000</td></tr>
      <tr><td>Operating Profit</td><td>1,800</td><td>2,000</td><td>2,310</td></tr>
      <tr><td>Depreciation</td><td>200</td><td>210</td><td>220</td></tr>
      <tr><td>Net Profit</td><td>1,200</td><td>1,350</td><td>1,500</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Equity Capital</td><td>500</td><td>500</td><td>500</td></tr>
      <tr><td>Reserves</td><td>7,500</td><td>8,500</td><td>9,500</td></tr>
      <tr><td>Borrowings</td><td>1,000</td><td>900</td><td>800</td></tr>
    </tbody>
  </table></section>
  <section id="cash-flow"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody><tr><td>Cash from Operating Activity</td><td>1,150</td><td>1,300</td><td>1,450</td></tr></tbody>
  </table></section>
</body></html>
"""


@pytest.fixture()
def fin():
    return parse_company_financials(_PAGE)


def _empty():
    return parse_company_financials("<html><body></body></html>")


class TestBuilders:
    def test_revenue_profit(self, fin) -> None:
        fig = charts.revenue_profit_chart(fin)
        types = sorted(t.type for t in fig.data)
        assert types == ["bar", "scatter"]            # revenue bar + PAT line
        assert list(fig.data[0].x) == ["Mar 2023", "Mar 2024", "Mar 2025"]

    def test_margins(self, fin) -> None:
        fig = charts.margins_chart(fin)
        names = {t.name for t in fig.data}
        assert {"EBITDA margin", "EBIT margin", "PAT margin"} <= names
        # FY25 EBITDA margin = 2310/11000*100 = 21.0
        ebitda = next(t for t in fig.data if t.name == "EBITDA margin")
        assert ebitda.y[-1] == pytest.approx(21.0)

    def test_returns(self, fin) -> None:
        fig = charts.returns_chart(fin)
        roe = next(t for t in fig.data if t.name == "ROE")
        # FY25 ROE = 1500 / (500+9500) * 100 = 15.0
        assert roe.y[-1] == pytest.approx(15.0)

    def test_cash_conversion(self, fin) -> None:
        fig = charts.cash_conversion_chart(fin)
        # FY25 CFO/PAT = 1450/1500*100 ≈ 96.67
        assert fig.data[0].y[-1] == pytest.approx(96.67, abs=0.1)


class TestGracefulDegradation:
    def test_each_builder_none_without_data(self) -> None:
        empty = _empty()
        assert charts.revenue_profit_chart(empty) is None
        assert charts.margins_chart(empty) is None
        assert charts.returns_chart(empty) is None
        assert charts.cash_conversion_chart(empty) is None

    def test_focus_charts_aggregates_available(self, fin) -> None:
        titles = [t for t, _f in charts.focus_charts(fin)]
        assert titles == ["Revenue & PAT", "Margins", "Return ratios", "Cash conversion"]

    def test_focus_charts_empty_when_no_data(self) -> None:
        assert charts.focus_charts(_empty()) == []
