"""Tests for the acquisition orchestrator (fetch → parse → persist, search)."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database.models import Base
from screener.scraper.acquisition import (
    CompanyDataService,
    CompanySearchResult,
    map_to_annual_records,
    period_to_date,
    search_companies,
)
from screener.scraper.parser import parse_company_financials

_PAGE = """
<html><body>
  <h1>Infosys Ltd</h1>
  <div class="company-ratios"><span id="nse-ticker">INFY</span></div>
  <section id="profit-loss"><table class="data-table">
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody>
      <tr><td class="text">Sales +</td><td>146,767</td><td>153,670</td></tr>
      <tr><td class="text">Operating Profit</td><td>34,000</td><td>36,694</td></tr>
      <tr><td class="text">Depreciation</td><td>3,500</td><td>4,678</td></tr>
      <tr><td class="text">Net Profit +</td><td>24,095</td><td>26,233</td></tr>
      <tr><td class="text">EPS in Rs</td><td>57.6</td><td>63.4</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table class="data-table">
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th></tr></thead>
    <tbody>
      <tr><td class="text">Equity Capital</td><td>2,098</td><td>2,098</td></tr>
      <tr><td class="text">Reserves</td><td>73,000</td><td>85,912</td></tr>
      <tr><td class="text">Borrowings +</td><td>7,000</td><td>8,359</td></tr>
      <tr><td class="text">Total Assets</td><td>1,25,816</td><td>1,37,814</td></tr>
    </tbody>
  </table></section>
</body></html>
"""


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class TestPeriodToDate:
    @pytest.mark.parametrize("label,expected", [
        ("Mar 2024", date(2024, 3, 31)),
        ("Dec 2023", date(2023, 12, 31)),
        ("Jun 2022", date(2022, 6, 30)),
        ("Feb 2024", date(2024, 2, 29)),    # leap year
        ("garbage", None),
        ("", None),
    ])
    def test_conversion(self, label: str, expected) -> None:
        assert period_to_date(label) == expected


class TestSearchCompanies:
    def test_parses_results(self) -> None:
        payload = '[{"name": "Infosys Ltd", "url": "/company/INFY/"},' \
                  ' {"name": "Infobeans", "url": "/company/INFOBEAN/consolidated/"}]'
        results = search_companies("info", fetch_json=lambda url: payload)
        assert results == [
            CompanySearchResult("INFY", "Infosys Ltd", "/company/INFY/"),
            CompanySearchResult("INFOBEAN", "Infobeans", "/company/INFOBEAN/consolidated/"),
        ]

    def test_blank_query_returns_empty(self) -> None:
        assert search_companies("   ", fetch_json=lambda url: "[]") == []

    def test_bad_json_returns_empty(self) -> None:
        assert search_companies("x", fetch_json=lambda url: "not json") == []

    def test_network_error_returns_empty(self) -> None:
        def _boom(url: str) -> str:
            raise RuntimeError("offline")
        assert search_companies("x", fetch_json=_boom) == []


class TestMapToAnnualRecords:
    def test_maps_and_derives_fields(self) -> None:
        fin = parse_company_financials(_PAGE)
        records = map_to_annual_records(fin)
        assert [d.year for d, _ in records] == [2023, 2024]

        fy24 = dict(records)[date(2024, 3, 31)]
        assert fy24["revenue"] == pytest.approx(153670)
        # EBIT = Operating Profit − Depreciation = 36694 − 4678
        assert fy24["ebit"] == pytest.approx(32016)
        assert fy24["net_income"] == pytest.approx(26233)
        # equity = Equity Capital + Reserves = 2098 + 85912
        assert fy24["shareholders_equity"] == pytest.approx(88010)
        assert fy24["total_debt"] == pytest.approx(8359)
        assert fy24["eps"] == pytest.approx(63.4)

    def test_no_profit_loss_returns_empty(self) -> None:
        fin = parse_company_financials("<html><body><h1>X</h1></body></html>")
        assert map_to_annual_records(fin) == []


class TestCompanyDataService:
    def test_refresh_persists_and_reports_freshness(self, session: Session) -> None:
        service = CompanyDataService(session, fetch_page=lambda url: _PAGE)
        assert service.freshness("INFY") is None

        fin = service.refresh("INFY")
        assert fin.name == "Infosys Ltd"
        assert service.freshness("INFY") is not None

        name, rows = service.get_annual_records("INFY")
        assert name == "Infosys Ltd"
        assert [r.fiscal_year_end.year for r in rows] == [2023, 2024]
        assert rows[-1].revenue == pytest.approx(153670)

    def test_refresh_skips_persistence_when_fresh(self, session: Session) -> None:
        calls = {"n": 0}

        def _fetch(url: str) -> str:
            calls["n"] += 1
            return _PAGE

        service = CompanyDataService(session, fetch_page=_fetch)
        service.refresh("INFY")           # first persist
        rows_before = service.get_annual_records("INFY")[1]
        service.refresh("INFY")           # fresh → no new persistence
        rows_after = service.get_annual_records("INFY")[1]
        assert len(rows_before) == len(rows_after) == 2

    def test_force_refresh_repersists(self, session: Session) -> None:
        service = CompanyDataService(session, fetch_page=lambda url: _PAGE)
        service.refresh("INFY")
        # force should not raise and should keep a single row per year
        service.refresh("INFY", force=True)
        _name, rows = service.get_annual_records("INFY")
        assert len(rows) == 2

    def test_get_annual_records_unknown_symbol_triggers_fetch(self, session: Session) -> None:
        service = CompanyDataService(session, fetch_page=lambda url: _PAGE)
        name, rows = service.get_annual_records("INFY")
        assert name == "Infosys Ltd"
        assert len(rows) == 2

    def test_refresh_retains_last_html(self, session: Session) -> None:
        """The raw page must stay available for non-statement parsers (pledge)."""
        service = CompanyDataService(session, fetch_page=lambda url: _PAGE)
        assert service.last_html is None
        service.refresh("INFY")
        assert service.last_html == _PAGE
