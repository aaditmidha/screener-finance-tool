"""Tests for the composite forensic red-flag score."""

import pytest

from screener.models import forensic_score
from screener.models.pledge_monitor import PledgePoint
from screener.scraper.parser import parse_company_financials

# Healthy profile: strong cash conversion, low leverage.
_HEALTHY = """
<html><body><h1>Healthy Co</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>9,000</td><td>10,000</td><td>11,000</td></tr>
      <tr><td>Operating Profit</td><td>1,800</td><td>2,000</td><td>2,200</td></tr>
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
      <tr><td>Total Assets</td><td>11,000</td><td>12,000</td><td>13,000</td></tr>
    </tbody>
  </table></section>
  <section id="cash-flow"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Cash from Operating Activity</td><td>1,250</td><td>1,400</td><td>1,550</td></tr>
    </tbody>
  </table></section>
</body></html>
"""

# Stressed profile: profit ≫ cash (poor quality) and heavy debt.
_STRESSED = """
<html><body><h1>Stressed Co</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>5,000</td><td>7,000</td><td>9,500</td></tr>
      <tr><td>Operating Profit</td><td>800</td><td>1,100</td><td>1,500</td></tr>
      <tr><td>Depreciation</td><td>100</td><td>110</td><td>120</td></tr>
      <tr><td>Net Profit</td><td>600</td><td>900</td><td>1,300</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Equity Capital</td><td>500</td><td>500</td><td>500</td></tr>
      <tr><td>Reserves</td><td>1,500</td><td>1,800</td><td>2,000</td></tr>
      <tr><td>Borrowings</td><td>4,000</td><td>5,500</td><td>7,500</td></tr>
      <tr><td>Total Assets</td><td>8,000</td><td>10,000</td><td>13,000</td></tr>
    </tbody>
  </table></section>
  <section id="cash-flow"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Cash from Operating Activity</td><td>250</td><td>300</td><td>320</td></tr>
    </tbody>
  </table></section>
</body></html>
"""


class TestCompute:
    def test_healthy_scores_high(self) -> None:
        score = forensic_score.compute(parse_company_financials(_HEALTHY))
        assert score.score >= 75
        assert score.verdict == "healthy"

    def test_stressed_scores_low(self) -> None:
        score = forensic_score.compute(parse_company_financials(_STRESSED))
        # weak cash conversion + high D/E should drag it down
        assert score.score < 75
        assert score.verdict in ("watch", "high_risk")

    def test_components_present(self) -> None:
        score = forensic_score.compute(parse_company_financials(_HEALTHY))
        names = {c.name for c in score.components}
        assert "Manipulation (Beneish)" in names
        assert "Earnings quality" in names
        assert "Leverage (D/E)" in names

    def test_leverage_detail(self) -> None:
        score = forensic_score.compute(parse_company_financials(_STRESSED))
        lev = next(c for c in score.components if c.name == "Leverage (D/E)")
        assert lev.available
        # FY25 D/E = 7500 / (500+2000) = 3.0 → above risky bound → 0
        assert lev.score == 0.0

    def test_pledge_included_when_supplied(self) -> None:
        history = [PledgePoint("Mar 2024", 10.0), PledgePoint("Mar 2025", 55.0)]
        score = forensic_score.compute(parse_company_financials(_HEALTHY), pledge_history=history)
        pledge = next(c for c in score.components if c.name == "Promoter pledge")
        assert pledge.available
        assert pledge.score == 0.0          # 55% pledged → high risk

    def test_pledge_unavailable_without_data(self) -> None:
        score = forensic_score.compute(parse_company_financials(_HEALTHY))
        pledge = next(c for c in score.components if c.name == "Promoter pledge")
        assert not pledge.available

    def test_high_pledge_lowers_composite(self) -> None:
        base = forensic_score.compute(parse_company_financials(_HEALTHY)).score
        with_pledge = forensic_score.compute(
            parse_company_financials(_HEALTHY),
            pledge_history=[PledgePoint("Mar 2024", 10.0), PledgePoint("Mar 2025", 55.0)],
        ).score
        assert with_pledge < base

    def test_weights_renormalise_over_available(self) -> None:
        """With no pledge data, the score still uses the other 3 components."""
        score = forensic_score.compute(parse_company_financials(_HEALTHY))
        assert 0 <= score.score <= 100
        assert any(c.available for c in score.components)

    def test_no_data_is_high_risk(self) -> None:
        score = forensic_score.compute(parse_company_financials("<html><body></body></html>"))
        assert score.verdict == "high_risk"
        assert all(not c.available for c in score.components)
