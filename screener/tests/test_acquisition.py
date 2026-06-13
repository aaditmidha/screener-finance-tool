"""Tests for the acquisition orchestrator (fetch → parse → persist, search)."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database.models import Base
from screener.scraper.acquisition import (
    CompanyDataService,
    CompanySearchResult,
    assess_data_quality,
    extract_industry_url,
    has_financials,
    map_to_annual_records,
    period_to_date,
    search_companies,
)
from screener.scraper.exceptions import FetchError
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

    def test_records_view_type_and_quality(self, session: Session) -> None:
        service = CompanyDataService(session, fetch_page=lambda url: _PAGE)
        service.refresh("INFY")
        company = service._companies.get_by_symbol("INFY")
        assert company.view_type == "consolidated"
        # _PAGE has 2 annual years (< min 3) but a balance sheet → "partial".
        assert company.data_quality == "partial"
        assert company.scrape_error is None


# Consolidated page that loads but carries an EMPTY profit-loss table.
_EMPTY_CONSOLIDATED = """
<html><body><h1>Small Cap Ltd</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2024</th></tr></thead>
    <tbody></tbody>
  </table></section>
</body></html>
"""


class TestStandaloneFallback:
    def test_empty_consolidated_falls_back_to_standalone(self, session: Session) -> None:
        def fetch(url: str) -> str:
            return _EMPTY_CONSOLIDATED if "/consolidated/" in url else _PAGE

        service = CompanyDataService(session, fetch_page=fetch)
        fin = service.refresh("SMALLCAP")
        assert fin.profit_loss is not None and fin.profit_loss.rows
        company = service._companies.get_by_symbol("SMALLCAP")
        assert company.view_type == "standalone"

    def test_consolidated_404_falls_back_to_standalone(self, session: Session) -> None:
        def fetch(url: str) -> str:
            if "/consolidated/" in url:
                raise FetchError(url, "all 3 attempts failed (HTTP 404)")
            return _PAGE

        service = CompanyDataService(session, fetch_page=fetch)
        fin = service.refresh("SMALLCAP")
        assert fin.profit_loss is not None
        assert service._companies.get_by_symbol("SMALLCAP").view_type == "standalone"

    def test_total_fetch_failure_records_error_without_crashing(self, session: Session) -> None:
        def fetch(url: str) -> str:
            raise FetchError(url, "blocked")

        service = CompanyDataService(session, fetch_page=fetch)
        fin = service.refresh("BLOCKED")
        assert fin.profit_loss is None              # empty, not an exception
        company = service._companies.get_by_symbol("BLOCKED")
        assert company.data_quality == "insufficient"
        assert "blocked" in company.scrape_error

    def test_has_financials_helper(self) -> None:
        assert has_financials(parse_company_financials(_PAGE)) is True
        assert has_financials(parse_company_financials(_EMPTY_CONSOLIDATED)) is False

    def test_assess_quality_grades(self) -> None:
        assert assess_data_quality(parse_company_financials(_PAGE), min_years=3) == "partial"
        assert assess_data_quality(parse_company_financials(_PAGE), min_years=2) == "full"
        assert assess_data_quality(
            parse_company_financials(_EMPTY_CONSOLIDATED), min_years=3) == "insufficient"


# Company page carrying the #peers Industry breadcrumb link, plus the
# industry listing page that lists the true sector peers.
_PAGE_WITH_INDUSTRY = _PAGE.replace(
    "</body></html>",
    '<section id="peers"><a title="Industry" href="/market/IN07/IN0702/X/">'
    "Heavy Electrical Equipment</a></section></body></html>",
)
_INDUSTRY_HTML = """
  <table><tbody>
    <tr><td><a href="/company/INFY/">Infosys</a></td></tr>
    <tr><td><a href="/company/TCS/">TCS</a></td></tr>
    <tr><td><a href="/company/WIPRO/consolidated/">Wipro</a></td></tr>
  </tbody></table>
"""


class TestPeerDiscovery:
    def _service(self, session: Session, company_page=_PAGE_WITH_INDUSTRY):
        def fetch(url: str) -> str:
            return _INDUSTRY_HTML if "/market/" in url else company_page
        return CompanyDataService(session, fetch_page=fetch)

    def test_discover_peer_symbols_from_industry(self, session: Session) -> None:
        peers = self._service(session).discover_peer_symbols("INFY")
        assert peers == ["TCS", "WIPRO"]            # base INFY excluded

    def test_no_industry_link_returns_empty(self, session: Session) -> None:
        # _PAGE has no #peers / Industry breadcrumb.
        assert self._service(session, company_page=_PAGE).discover_peer_symbols("INFY") == []

    def test_industry_fetch_failure_returns_empty(self, session: Session) -> None:
        def fetch(url: str) -> str:
            if "/market/" in url:
                raise FetchError(url, "blocked")
            return _PAGE_WITH_INDUSTRY
        service = CompanyDataService(session, fetch_page=fetch)
        assert service.discover_peer_symbols("INFY") == []

    def test_get_annual_records_is_lightweight(self, session: Session) -> None:
        """Peer data fetch must skip schedule enrichment (no fetch_json calls)."""
        json_calls: list[str] = []

        def spy_json(url: str) -> str:
            json_calls.append(url)
            return "{}"

        service = CompanyDataService(
            session, fetch_page=lambda url: _PAGE_WITH_INDUSTRY, fetch_json=spy_json
        )
        service.get_annual_records("INFY")
        assert json_calls == []                      # enrichment skipped for peers


def test_extract_industry_url() -> None:
    assert extract_industry_url(_PAGE_WITH_INDUSTRY) == "/market/IN07/IN0702/X/"
    assert extract_industry_url(_PAGE) is None
