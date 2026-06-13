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

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from screener.config import CONFIG
from screener.database.models import AnnualData, ARExtractedData
from screener.database.repository import (
    AnnualDataRepository,
    ARExtractedDataRepository,
    CompanyRepository,
)
from screener.scraper import client, fetcher, schedules
from screener.scraper.exceptions import FetchError
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


def extract_industry_url(html: str) -> str | None:
    """Return the company's most-specific Industry page path from its page.

    Screener's ``#peers`` breadcrumb links the exact industry (e.g. "Heavy
    Electrical Equipment"); that industry page lists the true sector peers,
    unlike the bare peers API which isn't reliably industry-scoped.

    Args:
        html: Raw company-page HTML.

    Returns:
        A ``/market/...`` path, or None if no Industry link is present.
    """
    soup = BeautifulSoup(html, "lxml")
    scope = soup.find(id="peers") or soup
    link = scope.find("a", title="Industry")
    if link is None or not link.has_attr("href"):
        return None
    return link["href"]


def has_financials(fin: CompanyFinancials) -> bool:
    """Return True if the parsed financials have a non-empty profit-loss table.

    A page can load successfully yet carry an empty statement (a block page or
    a login-gated view); this distinguishes "loaded with data" from "loaded
    but empty".

    Args:
        fin: Parsed company financials.

    Returns:
        True when the profit-loss table exists and has at least one row.
    """
    return fin.profit_loss is not None and len(fin.profit_loss.rows) > 0


def assess_data_quality(fin: CompanyFinancials, min_years: int) -> str:
    """Classify how complete a company's parsed financials are.

    Args:
        fin: Parsed company financials.
        min_years: Minimum annual periods with revenue to count as "full".

    Returns:
        "full" (≥ min_years of revenue and a balance sheet), "partial"
        (some data but below that bar), or "insufficient" (no usable P&L).
    """
    pl = fin.profit_loss
    if pl is None or not pl.rows:
        return "insufficient"
    revenue = pl.row("sales") or pl.row("revenue") or []
    years = sum(1 for v in revenue if v is not None)
    has_bs = fin.balance_sheet is not None and bool(fin.balance_sheet.rows)
    if years >= min_years and has_bs:
        return "full"
    if years >= 1:
        return "partial"
    return "insufficient"


class CompanyDataService:
    """Fetch, persist and serve a company's financials with cache awareness."""

    def __init__(
        self,
        session: Session,
        fetch_page: Callable[[str], str] | None = None,
        fetch_json: Callable[[str], str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Wire the service to a DB session and a page fetcher.

        Args:
            session: An open SQLAlchemy session.
            fetch_page: ``url -> html`` callable. Defaults to the fetcher
                (HTTP → Playwright fallback); injectable for testing.
            fetch_json: ``url -> body`` callable for the schedules/notes API.
                Defaults to the HTTP client; pass None-returning stub in tests
                to skip enrichment.
            config: Override config; defaults to the global CONFIG.
        """
        self._session = session
        self._fetch_page = fetch_page or fetcher.fetch_page
        self._fetch_json = fetch_json or client.fetch
        self._cfg = (config or CONFIG)["scraper"]
        self._companies = CompanyRepository(session)
        self._annual = AnnualDataRepository(session)
        self._ar = ARExtractedDataRepository(session)
        #: Raw HTML of the most recently refreshed page (e.g. for the pledge
        #: parser, which reads sections outside the financial statements).
        self.last_html: str | None = None

    def latest_ar_pair(self, symbol: str) -> tuple[ARExtractedData | None, ARExtractedData | None]:
        """Return the (current, prior) AR-extracted rows for *symbol*, if any.

        Used to upgrade Beneish inputs with exact Annual-Report figures. Returns
        ``(None, None)`` unless at least two extracted years exist (e.g. the
        local AR pipeline has been run).

        Args:
            symbol: Company ticker.

        Returns:
            (latest_year_row, prior_year_row) or (None, None).
        """
        company = self._companies.get_by_symbol(symbol.upper())
        if company is None:
            return None, None
        rows = self._ar.for_company(company.id)
        if len(rows) < 2:
            return None, None
        return rows[-1], rows[-2]

    def _company_url(self, symbol: str) -> str:
        """Return the Screener consolidated company URL for *symbol*."""
        return self._cfg["company_url_template"].format(symbol=symbol.upper())

    def _standalone_url(self, symbol: str) -> str:
        """Return the bare (standalone/default) Screener URL for *symbol*."""
        return self._cfg["standalone_url_template"].format(symbol=symbol.upper())

    def _fetch_best_view(self, symbol: str) -> tuple[CompanyFinancials, str, str]:
        """Fetch consolidated, falling back to standalone, and return the best.

        Tries the consolidated page first. If it 404s/blocks, or loads but has
        an empty profit-loss table, retries the bare standalone URL.

        Args:
            symbol: Company ticker (upper-cased by the caller).

        Returns:
            A (financials, view_type, html) tuple where view_type is
            "consolidated" or "standalone".

        Raises:
            FetchError: If the standalone fallback also fails to fetch.
        """
        try:
            html = self._fetch_page(self._company_url(symbol))
            fin = parse_company_financials(html)
            if has_financials(fin):
                return fin, "consolidated", html
            logger.info("%s consolidated profit-loss empty — trying standalone", symbol)
        except FetchError as exc:
            logger.info("%s consolidated fetch failed (%s) — trying standalone", symbol, exc)

        html = self._fetch_page(self._standalone_url(symbol))
        return parse_company_financials(html), "standalone", html

    def freshness(self, symbol: str) -> datetime | None:
        """Return when *symbol* was last persisted, or None if never.

        Args:
            symbol: Company ticker.

        Returns:
            The stored ``last_updated`` timestamp, or None.
        """
        company = self._companies.get_by_symbol(symbol.upper())
        return company.last_updated if company else None

    def refresh(self, symbol: str, force: bool = False, enrich: bool = True) -> CompanyFinancials:
        """Fetch, parse and persist *symbol*'s financials (honouring the cache).

        Tries consolidated then standalone (FIX 2), records the view used and a
        data-quality grade, and stores any scrape error so the UI can surface
        it. A fetch failure is caught and recorded rather than raised, so a
        single bad company never crashes a batch (e.g. peer comparison).

        Args:
            symbol: Company ticker.
            force: Re-persist even if the cache is still fresh.
            enrich: Whether to fetch the expand-API notes. Skipped for peers
                (they only need headline figures) to avoid ~10 extra requests.

        Returns:
            The parsed :class:`CompanyFinancials` (possibly empty on failure).
        """
        symbol = symbol.upper()
        needs = force or self._companies.needs_refresh(symbol)

        try:
            fin, view_type, html = self._fetch_best_view(symbol)
        except FetchError as exc:
            logger.warning("Scrape failed for %s: %s", symbol, exc)
            existing = self._companies.get_by_symbol(symbol)
            self._companies.upsert(
                symbol=symbol, name=existing.name if existing else symbol,
                data_quality="insufficient", scrape_error=str(exc)[:500],
            )
            self._session.commit()
            return CompanyFinancials(name=symbol, symbol=symbol)

        self.last_html = html
        if enrich:
            schedules.enrich(fin, html, fetch_json=self._fetch_json)

        if not needs:
            logger.info("%s is cache-fresh; skipping persistence", symbol)
            return fin

        quality = assess_data_quality(fin, self._cfg["min_annual_years"])
        company = self._companies.upsert(
            symbol=symbol, name=fin.name or symbol,
            data_quality=quality, view_type=view_type, scrape_error=None,
        )
        count = 0
        for fy_date, fields in map_to_annual_records(fin):
            self._annual.upsert(company.id, fy_date, **fields)
            count += 1
        self._session.commit()
        logger.info("Persisted %d annual row(s) for %s [%s, %s]", count, symbol, view_type, quality)
        return fin

    def get_annual_records(self, symbol: str) -> tuple[str, list[AnnualData]]:
        """Return a company's name and persisted annual rows, refreshing if stale.

        Suitable as the ``fetch_annual_data`` callable for peer comparison; the
        refresh is lightweight (``enrich=False``) since peers only need headline
        figures (revenue, EBIT, net income, equity, debt) for the ranking.

        Args:
            symbol: Company ticker.

        Returns:
            A (company_name, [AnnualData]) tuple ordered oldest → newest.
        """
        symbol = symbol.upper()
        if self._companies.needs_refresh(symbol):
            self.refresh(symbol, enrich=False)
        company = self._companies.get_by_symbol(symbol)
        if company is None:
            return symbol, []
        return company.name, self._annual.for_company(company.id)

    def discover_peer_symbols(self, symbol: str, max_peers: int | None = None) -> list[str]:
        """Return *symbol*'s sector peers from its Screener Industry page.

        Reads the Industry breadcrumb link off the company page, fetches that
        industry's listing, and parses the company tickers — yielding genuine
        industry peers rather than the bare peers API's mis-scoped default set.

        Args:
            symbol: Base company ticker.
            max_peers: Cap on peers returned. Defaults to config ``max_peers``.

        Returns:
            Peer ticker symbols (excluding *symbol*); empty on any failure.
        """
        from screener.models.peer_comparison import discover_peers

        symbol = symbol.upper()
        html = self.last_html or self._fetch_page(self._company_url(symbol))
        industry_path = extract_industry_url(html)
        if industry_path is None:
            logger.warning("No industry link found for %s; cannot resolve peers", symbol)
            return []
        industry_url = self._cfg["base_url"] + industry_path
        try:
            industry_html = self._fetch_page(industry_url)
        except FetchError as exc:  # peers are optional; never crash the tab
            logger.warning("Industry page fetch failed for %s: %s", symbol, exc)
            return []
        return discover_peers(industry_html, symbol, max_peers)
