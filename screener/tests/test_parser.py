"""Tests for the Screener financials parser.

Uses a fixture page mimicking Screener's section/table structure so parsing is
validated without a live fetch.
"""

import pytest

from screener.scraper.parser import (
    CompanyFinancials,
    FinancialTable,
    clean_number,
    parse_company_financials,
    parse_table,
)
from bs4 import BeautifulSoup

# Minimal Screener-style page: a P&L and a balance-sheet section.
_PAGE = """
<html><body>
  <h1>Infosys Ltd</h1>
  <div class="company-ratios"><span id="nse-ticker">INFY</span></div>

  <section id="profit-loss">
    <table class="data-table">
      <thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
      <tbody>
        <tr><td class="text">Sales +</td><td>121,641</td><td>146,767</td><td>153,670</td></tr>
        <tr><td class="text">Operating Profit</td><td>31,000</td><td>30,500</td><td>32,016</td></tr>
        <tr><td class="text">Net Profit +</td><td>22,110</td><td>24,095</td><td>26,233</td></tr>
        <tr><td class="text">OPM %</td><td>25%</td><td>21%</td><td>21%</td></tr>
      </tbody>
    </table>
  </section>

  <section id="balance-sheet">
    <table class="data-table">
      <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
      <tbody>
        <tr><td class="text">Total Assets</td><td>1,25,816</td><td>1,37,814</td></tr>
        <tr><td class="text">Borrowings +</td><td>-</td><td>8,359</td></tr>
      </tbody>
    </table>
  </section>
</body></html>
"""


class TestCleanNumber:
    @pytest.mark.parametrize("text,expected", [
        ("1,234", 1234.0),
        ("1,23,456", 123456.0),       # Indian grouping
        ("153,670", 153670.0),
        ("-12.5", -12.5),
        ("21%", 21.0),
        ("1,234.56", 1234.56),
        ("", None),
        ("-", None),
        ("–", None),                  # en dash placeholder
        ("n/a", None),
        (None, None),
    ])
    def test_parsing(self, text, expected) -> None:
        assert clean_number(text) == expected


class TestParseTable:
    @pytest.fixture()
    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(_PAGE, "lxml")

    def test_returns_financial_table(self, soup) -> None:
        table = parse_table(soup, "profit-loss")
        assert isinstance(table, FinancialTable)

    def test_periods_parsed(self, soup) -> None:
        table = parse_table(soup, "profit-loss")
        assert table.periods == ["Mar 2022", "Mar 2023", "Mar 2024"]

    def test_row_values_and_label_cleaned(self, soup) -> None:
        table = parse_table(soup, "profit-loss")
        # "Sales +" → label "Sales", commas stripped
        assert table.rows["Sales"] == [121641.0, 146767.0, 153670.0]

    def test_row_lookup_is_fuzzy(self, soup) -> None:
        table = parse_table(soup, "profit-loss")
        assert table.row("net profit") == [22110.0, 24095.0, 26233.0]

    def test_latest_skips_trailing_none(self, soup) -> None:
        table = parse_table(soup, "balance-sheet")
        # Borrowings: [None, 8359] → latest is 8359
        assert table.latest("Borrowings") == 8359.0

    def test_percentage_row_parsed_as_number(self, soup) -> None:
        table = parse_table(soup, "profit-loss")
        assert table.row("OPM") == [25.0, 21.0, 21.0]

    def test_missing_section_returns_none(self, soup) -> None:
        assert parse_table(soup, "cash-flow") is None


class TestParseCompanyFinancials:
    def test_extracts_name_and_symbol(self) -> None:
        fin = parse_company_financials(_PAGE)
        assert isinstance(fin, CompanyFinancials)
        assert fin.name == "Infosys Ltd"
        assert fin.symbol == "INFY"

    def test_populates_present_statements_only(self) -> None:
        fin = parse_company_financials(_PAGE)
        assert fin.profit_loss is not None
        assert fin.balance_sheet is not None
        assert fin.cash_flow is None          # absent in fixture
        assert fin.quarters is None

    def test_indian_grouping_in_balance_sheet(self) -> None:
        fin = parse_company_financials(_PAGE)
        assert fin.balance_sheet.row("Total Assets") == [125816.0, 137814.0]

    def test_empty_page_yields_empty_financials(self) -> None:
        fin = parse_company_financials("<html><body></body></html>")
        assert fin.name == ""
        assert fin.profit_loss is None
