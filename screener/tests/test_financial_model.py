"""Tests for the shared canonical financial derivation layer."""

import pytest

from screener.exporters import financial_model as fm
from screener.scraper.parser import parse_company_financials

_PAGE = """
<html><body><h1>Acme</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>1,000</td><td>1,200</td></tr>
      <tr><td>Expenses +</td><td>800</td><td>930</td></tr>
      <tr><td>Operating Profit</td><td>200</td><td>270</td></tr>
      <tr><td>Depreciation</td><td>40</td><td>50</td></tr>
      <tr><td>Interest</td><td>20</td><td>18</td></tr>
      <tr><td>Profit before tax</td><td>150</td><td>210</td></tr>
      <tr><td>Tax %</td><td>25%</td><td>24%</td></tr>
      <tr><td>Net Profit</td><td>112</td><td>160</td></tr>
      <tr><td>EPS in Rs</td><td>11.2</td><td>16.0</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Equity Capital</td><td>100</td><td>100</td></tr>
      <tr><td>Reserves</td><td>500</td><td>620</td></tr>
      <tr><td>Borrowings +</td><td>300</td><td>240</td></tr>
      <tr><td>Other Liabilities +</td><td>150</td><td>180</td></tr>
      <tr><td>Total Liabilities</td><td>1,050</td><td>1,140</td></tr>
      <tr><td>Fixed Assets +</td><td>600</td><td>650</td></tr>
      <tr><td>CWIP</td><td>20</td><td>30</td></tr>
      <tr><td>Investments</td><td>80</td><td>90</td></tr>
      <tr><td>Other Assets +</td><td>350</td><td>370</td></tr>
      <tr><td>Total Assets</td><td>1,050</td><td>1,140</td></tr>
    </tbody>
  </table></section>
  <section id="cash-flow"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Cash from Operating Activity +</td><td>180</td><td>240</td></tr>
      <tr><td>Cash from Investing Activity +</td><td>-90</td><td>-110</td></tr>
      <tr><td>Cash from Financing Activity +</td><td>-70</td><td>-120</td></tr>
      <tr><td>Net Cash Flow</td><td>20</td><td>10</td></tr>
    </tbody>
  </table></section>
  <section id="ratios"><table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Debtor Days</td><td>45</td><td>48</td></tr>
      <tr><td>Inventory Days</td><td>60</td><td>62</td></tr>
      <tr><td>ROCE %</td><td>22%</td><td>26%</td></tr>
    </tbody>
  </table></section>
</body></html>
"""


@pytest.fixture()
def fin():
    return parse_company_financials(_PAGE)


def _row(rows, label):
    return next(r for r in rows if r.label == label)


class TestIncomeStatement:
    def test_core_rows_present(self, fin) -> None:
        labels = [r.label for r in fm.income_statement(fin)]
        for expected in ("Revenue from operations", "EBITDA", "EBIT",
                         "Profit before tax", "Profit after tax", "EPS (INR)"):
            assert expected in labels

    def test_ebit_is_ebitda_less_depreciation(self, fin) -> None:
        ebit = _row(fm.income_statement(fin), "EBIT")
        assert ebit.values == [200 - 40, 270 - 50]


class TestCommonSizeAndGrowth:
    def test_ebitda_margin_ratio(self, fin) -> None:
        margin = _row(fm.common_size(fin), "EBITDA margin")
        assert margin.values[1] == pytest.approx(270 / 1200)
        assert margin.kind == "pct"

    def test_revenue_growth(self, fin) -> None:
        g = _row(fm.growth(fin), "Revenue growth")
        assert g.values[0] is None
        assert g.values[1] == pytest.approx(1200 / 1000 - 1)


class TestBalanceSheetCashFlow:
    def test_shareholders_funds_summed(self, fin) -> None:
        nw = _row(fm.balance_sheet(fin), "Shareholders' funds")
        assert nw.values == [600, 720]

    def test_cash_flow_operating(self, fin) -> None:
        cfo = _row(fm.cash_flow(fin), "Cash from operating activity")
        assert cfo.values == [180, 240]


class TestRatios:
    def test_roe_and_de(self, fin) -> None:
        rows = fm.ratios(fin)
        roe = _row(rows, "ROE")
        assert roe.values[1] == pytest.approx(160 / 720)
        de = _row(rows, "Debt / equity")
        assert de.values[1] == pytest.approx(240 / 720)

    def test_roce_prefers_screener_value(self, fin) -> None:
        roce = _row(fm.ratios(fin), "ROCE")
        # Screener reports 22% / 26% → stored as ratios.
        assert roce.values == pytest.approx([0.22, 0.26])

    def test_headers_have_followers(self, fin) -> None:
        rows = fm.ratios(fin)
        for i, row in enumerate(rows):
            if row.kind == "header":
                assert i + 1 < len(rows) and rows[i + 1].kind != "header"


class TestSummarySections:
    def test_sections_ordered_and_nonempty(self, fin) -> None:
        sections = fm.summary_sections(fin)
        titles = [s.title for s in sections]
        assert titles == ["Income statement", "Common-size (% of revenue)",
                          "Growth (% YoY)", "Balance sheet", "Cash flow", "Ratios & returns"]
        assert all(s.rows for s in sections)

    def test_empty_fin_yields_no_sections(self) -> None:
        empty = parse_company_financials("<html><body></body></html>")
        assert fm.summary_sections(empty) == []
