"""Tests for the driver-based P&L forecast."""

import pytest

from screener.models import forecast
from screener.scraper.parser import parse_company_financials

# Revenue compounds at exactly 10% (1000 -> 1331 over 3 years); EBITDA margin 20%.
_PAGE = """
<html><body><h1>Acme</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>1,000</td><td>1,100</td><td>1,210</td><td>1,331</td></tr>
      <tr><td>Operating Profit</td><td>200</td><td>220</td><td>242</td><td>266.2</td></tr>
      <tr><td>Depreciation</td><td>30</td><td>33</td><td>36</td><td>40</td></tr>
      <tr><td>Interest</td><td>20</td><td>20</td><td>20</td><td>20</td></tr>
      <tr><td>Other Income</td><td>10</td><td>10</td><td>10</td><td>10</td></tr>
      <tr><td>Profit before tax</td><td>160</td><td>177</td><td>196</td><td>216</td></tr>
      <tr><td>Tax %</td><td>25%</td><td>25%</td><td>25%</td><td>25%</td></tr>
      <tr><td>Net Profit</td><td>120</td><td>133</td><td>147</td><td>162</td></tr>
      <tr><td>EPS in Rs</td><td>12</td><td>13.3</td><td>14.7</td><td>16.2</td></tr>
    </tbody>
  </table></section>
</body></html>
"""


@pytest.fixture()
def fin():
    return parse_company_financials(_PAGE)


def _row(result, label):
    return next(r for r in result.rows if r.label == label)


class TestDefaultAssumptions:
    def test_growth_is_historical_cagr(self, fin) -> None:
        a = forecast.default_assumptions(fin)
        assert a.revenue_growth == pytest.approx(0.10, abs=1e-3)

    def test_margin_from_latest(self, fin) -> None:
        a = forecast.default_assumptions(fin)
        assert a.ebitda_margin == pytest.approx(266.2 / 1331, abs=1e-3)

    def test_none_without_revenue(self) -> None:
        empty = parse_company_financials("<html><body></body></html>")
        assert forecast.default_assumptions(empty) is None


class TestProject:
    def test_periods_span_history_plus_forecast(self, fin) -> None:
        result = forecast.project(fin)
        assert result.n_history == 4
        assert len(result.forecast_periods) == 3
        assert len(result.periods) == 7
        assert all(p.endswith("E") for p in result.forecast_periods)
        assert "2026" in result.forecast_periods[0]

    def test_revenue_compounds(self, fin) -> None:
        result = forecast.project(fin)
        rev = _row(result, "Revenue from operations").values
        assert rev[4] == pytest.approx(1331 * 1.10, rel=1e-3)
        assert rev[5] == pytest.approx(1331 * 1.10 ** 2, rel=1e-3)
        assert rev[6] == pytest.approx(1331 * 1.10 ** 3, rel=1e-3)

    def test_ebitda_uses_margin(self, fin) -> None:
        result = forecast.project(fin)
        rev = _row(result, "Revenue from operations").values
        ebitda = _row(result, "EBITDA").values
        assert ebitda[4] == pytest.approx(rev[4] * (266.2 / 1331), rel=1e-3)

    def test_forecast_eps_positive(self, fin) -> None:
        result = forecast.project(fin)
        eps = _row(result, "EPS (INR)").values
        assert eps[4] is not None and eps[4] > eps[3]

    def test_override_assumptions(self, fin) -> None:
        base = forecast.default_assumptions(fin)
        bumped = forecast.ForecastAssumptions(
            revenue_growth=0.20, ebitda_margin=base.ebitda_margin,
            depreciation_pct=base.depreciation_pct, other_income=base.other_income,
            interest=base.interest, tax_rate=base.tax_rate, shares=base.shares)
        result = forecast.project(fin, bumped)
        rev = _row(result, "Revenue from operations").values
        assert rev[4] == pytest.approx(1331 * 1.20, rel=1e-3)

    def test_none_without_pl(self) -> None:
        empty = parse_company_financials("<html><body></body></html>")
        assert forecast.project(empty) is None
