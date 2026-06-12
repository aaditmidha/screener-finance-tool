"""Acquisition orchestrator: fetch → parse → persist, with caching and search.

Ties the fetch layer (:mod:`screener.scraper.fetcher`) and parser
(:mod:`screener.scraper.parser`) to the database. :class:`CompanyDataService`
is the single entry point the UI and peer-comparison use to obtain a company's
financials: it respects the 7-day cache, persists annual rows through the
repositories, and reports data freshness.

The Screener page exposes aggregated statements; the mapper extracts the fields
the rest of the app needs (revenue, EBIT, net income, total assets/debt,
equity, EPS) and converts each period label (e.g. "Mar 2024") to a fiscal
year-end date. The HTTP fetch is injected, so the whole service is unit-tested
without a live Screener.
"""

import calendar
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from screener.config import CONFIG
from screener.database.models import AnnualData
from screener.database.repository import AnnualDataRepository, CompanyRepository
from screener.scraper import client, fetcher
from screener.scraper.parser import CompanyFinancials, FinancialTable, parse_company_financials

logger = logging.getLogger(__name__)

_scraper_cfg = CONFIG["scraper"]

# "Mar 2024" / "Mar2024" → month, year.
_PERIOD_RE = re.compile(r"([A-Za-z]{3})\s*'?\s*(\d{4})")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}

# Screener peer/search url → symbol.
_SYMBOL_RE = re.compile(r"/company/([A-Za-z0-9&._-]+)/")


@dataclass
class CompanySearchResult:
    """One autocomplete match from Screener's company search."""

    symbol: str
    name: str
    url: str


def period_to_date(label: str) -> date | None:
    """Convert a Screener period label to its month-end date.

    Args:
        label: Period header such as "Mar 2024".

    Returns:
        The last day of that month, or None if the label is not recognised.
    """
    match = _PERIOD_RE.search(label or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    year = int(match.group(2))
    if not month:
        return None
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def search_companies(
    query: str, fetch_json: Callable[[str], str] | None = None
) -> list[CompanySearchResult]:
    """Query Screener's autocomplete and return matching companies.

    Args:
        query: Free-text company-name fragment.
        fetch_json: ``url -> response text`` callable. Defaults to the HTTP
            client; injectable for testing.

    Returns:
        A list of :class:`CompanySearchResult`, empty if the query is blank or
        the response is unparseable.
    """
    if not query or not query.strip():
        return []
    fetch = fetch_json or client.fetch
    url = _scraper_cfg["search_url_template"].format(query=query.strip())
    try:
        raw = fetch(url)
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Search response for %r was not valid JSON: %s", query, exc)
        return []
    except Exception as exc:  # network/client errors shouldn't crash the UI
        logger.warning("Search failed for %r: %s", query, exc)
        return []

    results: list[CompanySearchResult] = []
    for entry in payload if isinstance(payload, list) else payload.get("results", []):
        page_url = entry.get("url", "")
        sym_match = _SYMBOL_RE.search(page_url)
        symbol = sym_match.group(1).upper() if sym_match else ""
        name = entry.get("name", "")
        if symbol and name:
            results.append(CompanySearchResult(symbol=symbol, name=name, url=page_url))
    logger.info("Search %r → %d result(s)", query, len(results))
    return results


def _columns_by_period(table: FinancialTable | None) -> dict[str, dict[str, float | None]]:
    """Pivot a FinancialTable to ``{period_label: {row_label: value}}``."""
    out: dict[str, dict[str, float | None]] = {}
    if table is None:
        return out
    for col_idx, period in enumerate(table.periods):
        column: dict[str, float | None] = {}
        for label, values in table.rows.items():
            column[label] = values[col_idx] if col_idx < len(values) else None
        out[period] = column
    return out


def _pick(column: dict[str, float | None], *needles: str) -> float | None:
    """Return the first value whose row label contains any of *needles*."""
    for needle in needles:
        low = needle.lower()
        for label, value in column.items():
            if low in label.lower():
                return value
    return None


def map_to_annual_records(fin: CompanyFinancials) -> list[tuple[date, dict[str, float]]]:
    """Map parsed statements to per-year (date, AnnualData-fields) tuples.

    EBIT is derived as Operating Profit − Depreciation; equity as Equity
    Capital + Reserves. Periods that don't resolve to a date are skipped.

    Args:
        fin: Parsed company financials.

    Returns:
        List of (fiscal_year_end, field-dict) sorted oldest → newest. Only
        non-None fields are included in each dict.
    """
    pl = fin.profit_loss
    if pl is None:
        return []

    pl_cols = _columns_by_period(pl)
    bs_cols = _columns_by_period(fin.balance_sheet)

    records: list[tuple[date, dict[str, float]]] = []
    for period in pl.periods:
        fy_date = period_to_date(period)
        if fy_date is None:
            continue
        plc = pl_cols.get(period, {})
        bsc = bs_cols.get(period, {})

        revenue = _pick(plc, "sales", "revenue", "income from operations")
        op_profit = _pick(plc, "operating profit", "ebitda")
        depreciation = _pick(plc, "depreciation")
        net_income = _pick(plc, "net profit", "profit after tax")
        eps = _pick(plc, "eps")

        ebit = None
        if op_profit is not None:
            ebit = op_profit - (depreciation or 0.0)

        equity_capital = _pick(bsc, "equity capital", "equity share capital", "share capital")
        reserves = _pick(bsc, "reserves")
        equity = None
        if equity_capital is not None or reserves is not None:
            equity = (equity_capital or 0.0) + (reserves or 0.0)

        fields: dict[str, float] = {}
        for key, value in (
            ("revenue", revenue),
            ("ebit", ebit),
            ("net_income", net_income),
            ("eps", eps),
            ("total_assets", _pick(bsc, "total assets")),
            ("total_debt", _pick(bsc, "borrowings", "debt")),
            ("shareholders_equity", equity),
        ):
            if value is not None:
                fields[key] = value

        if fields:
            records.append((fy_date, fields))

    records.sort(key=lambda t: t[0])
    return records


class CompanyDataService:
    """Fetch, persist and serve a company's financials with cache awareness."""

    def __init__(
        self,
        session: Session,
        fetch_page: Callable[[str], str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Wire the service to a DB session and a page fetcher.

        Args:
            session: An open SQLAlchemy session.
            fetch_page: ``url -> html`` callable. Defaults to the fetcher
                (HTTP → Playwright fallback); injectable for testing.
            config: Override config; defaults to the global CONFIG.
        """
        self._session = session
        self._fetch_page = fetch_page or fetcher.fetch_page
        self._cfg = (config or CONFIG)["scraper"]
        self._companies = CompanyRepository(session)
        self._annual = AnnualDataRepository(session)

    def _company_url(self, symbol: str) -> str:
        """Return the Screener company URL for *symbol*."""
        return self._cfg["company_url_template"].format(symbol=symbol.upper())

    def freshness(self, symbol: str) -> datetime | None:
        """Return when *symbol* was last persisted, or None if never.

        Args:
            symbol: Company ticker.

        Returns:
            The stored ``last_updated`` timestamp, or None.
        """
        company = self._companies.get_by_symbol(symbol.upper())
        return company.last_updated if company else None

    def refresh(self, symbol: str, force: bool = False) -> CompanyFinancials:
        """Fetch, parse and persist *symbol*'s financials (honouring the cache).

        If cached data is still fresh and *force* is False, the page is still
        fetched and parsed for display, but the parse is logged as a refresh
        only when data is written. (Persistence is skipped when fresh.)

        Args:
            symbol: Company ticker.
            force: Re-persist even if the cache is still fresh.

        Returns:
            The parsed :class:`CompanyFinancials`.
        """
        symbol = symbol.upper()
        needs = force or self._companies.needs_refresh(symbol)

        html = self._fetch_page(self._company_url(symbol))
        fin = parse_company_financials(html)

        if not needs:
            logger.info("%s is cache-fresh; skipping persistence", symbol)
            return fin

        company = self._companies.upsert(symbol=symbol, name=fin.name or symbol)
        count = 0
        for fy_date, fields in map_to_annual_records(fin):
            self._annual.upsert(company.id, fy_date, **fields)
            count += 1
        self._session.commit()
        logger.info("Persisted %d annual row(s) for %s", count, symbol)
        return fin

    def get_annual_records(self, symbol: str) -> tuple[str, list[AnnualData]]:
        """Return a company's name and persisted annual rows, refreshing if stale.

        Suitable as the ``fetch_annual_data`` callable for peer comparison.

        Args:
            symbol: Company ticker.

        Returns:
            A (company_name, [AnnualData]) tuple ordered oldest → newest.
        """
        symbol = symbol.upper()
        if self._companies.needs_refresh(symbol):
            self.refresh(symbol)
        company = self._companies.get_by_symbol(symbol)
        if company is None:
            return symbol, []
        return company.name, self._annual.for_company(company.id)
