"""Tests for the operational-efficiency metrics model."""

import pytest

from screener.models import operational
from screener.scraper import schedules
from screener.scraper.parser import parse_company_financials

# Page with PL + BS + CF and expand-API notes (receivables/inventory/payables).
_PAGE = """
<html><body><h1>ManufCo</h1>
  <a href="/api/company/1/chart/"></a>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>8,000</td><td>10,000</td></tr>
      <tr><td>Operating Profit</td><td>1,600</td><td>2,000</td></tr>
      <tr><td>Depreciation</td><td>300</td><td>400</td></tr>
      <tr><td>Net Profit</td><td>900</td><td>1,200</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Fixed Assets</td><td>4,000</td><td>4,500</td></tr>
      <tr><td>Total Assets</td><td>10,000</td><td>12,000</td></tr>
    </tbody>
  </table></section>
  <section id="cash-flow"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Cash from Operating Activity</td><td>1,400</td><td>1,500</td></tr>
    </tbody>
  </table></section>
</body></html>
"""

_SCHEDULES = {
    "Other Assets": {
        "Trade receivables": {"Mar 2024": "1,200", "Mar 2025": "1,600"},
        "Inventories": {"Mar 2024": "900", "Mar 2025": "1,100"},
    },
    "Other Liabilities": {
        "Trade Payables": {"Mar 2024": "700", "Mar 2025": "850"},
    },
}


def _fake_fetch(url: str) -> str:
    import json
    for parent, payload in _SCHEDULES.items():
        if parent.replace(" ", "%20") in url:
            return json.dumps(payload)
    return json.dumps({})


@pytest.fixture()
def op():
    fin = parse_company_financials(_PAGE)
    schedules.enrich(fin, _PAGE, fetch_json=_fake_fetch)
    return operational.compute(fin)


def _metric(op, label):
    return next(m for m in op.metrics if m.label == label)


class TestCompute:
    def test_periods_match_pl(self, op) -> None:
        assert op.periods == ["Mar 2024", "Mar 2025"]

    def test_revenue_growth(self, op) -> None:
        # FY25 = 10000/8000 - 1 = 0.25; FY24 has no prior → None
        m = _metric(op, "Revenue growth %")
        assert m.values[0] is None
        assert m.values[1] == pytest.approx(0.25)

    def test_ebitda_margin(self, op) -> None:
        # 2000 / 10000
        assert _metric(op, "EBITDA margin %").values[1] == pytest.approx(0.20)

    def test_asset_turnover(self, op) -> None:
        # 10000 / 12000
        assert _metric(op, "Asset turnover").values[1] == pytest.approx(0.8333, abs=1e-3)

    def test_receivable_days_from_notes(self, op) -> None:
        # 1600/10000 * 365 = 58.4
        assert _metric(op, "Receivable days").values[1] == pytest.approx(58.4, abs=0.1)

    def test_cash_conversion_cycle(self, op) -> None:
        # COGS = 10000-2000 = 8000; DSO=58.4, DIO=1100/8000*365=50.19,
        # DPO=850/8000*365=38.78 → CCC ≈ 69.8
        assert _metric(op, "Cash conversion cycle (days)").values[1] == pytest.approx(69.8, abs=0.3)

    def test_cfo_to_ebitda(self, op) -> None:
        # 1500 / 2000
        assert _metric(op, "CFO / EBITDA").values[1] == pytest.approx(0.75)

    def test_metric_fmt_units(self, op) -> None:
        assert _metric(op, "EBITDA margin %").fmt == "pct"
        assert _metric(op, "Asset turnover").fmt == "x"
        assert _metric(op, "Receivable days").fmt == "days"


class TestGracefulDegradation:
    def test_no_pl_returns_empty(self) -> None:
        fin = parse_company_financials("<html><body></body></html>")
        result = operational.compute(fin)
        assert result.periods == []
        assert result.metrics == []

    def test_missing_notes_omits_wc_metrics(self) -> None:
        """Without receivables/inventory notes, day-metrics are dropped."""
        fin = parse_company_financials(_PAGE)   # not enriched
        result = operational.compute(fin)
        labels = {m.label for m in result.metrics}
        assert "Receivable days" not in labels
        assert "EBITDA margin %" in labels       # margin still computable
