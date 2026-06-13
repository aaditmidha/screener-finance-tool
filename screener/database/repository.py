"""Repository layer: CRUD helpers plus cache-aware freshness checks.

Wraps a SQLAlchemy session so callers never touch the ORM models or the cache
staleness rules directly.
"""

import logging
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from screener.database import cache
from screener.database.models import AnnualData, ARExtractedData, Company

logger = logging.getLogger(__name__)

# Columns on AnnualData a caller may set via upsert (excludes keys/timestamps).
_ANNUAL_FIELDS = {
    "revenue", "ebit", "net_income", "free_cash_flow",
    "total_assets", "total_debt", "shareholders_equity", "eps",
}

# Columns on ARExtractedData a caller may set via upsert.
_AR_FIELDS = {
    "revenue", "ebitda", "pat", "cfo", "capex", "trade_receivables",
    "inventory", "trade_payables", "total_assets", "total_equity",
    "total_debt", "cash", "depreciation", "interest_expense", "tax_expense",
    "guided_revenue_growth", "guided_margin", "guided_capex", "guidance_raw_text",
    "key_risks", "extraction_confidence", "pages_used", "unit",
}


class CompanyRepository:
    """Read/write access to :class:`Company` records with cache awareness."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to an active SQLAlchemy session.

        Args:
            session: An open SQLAlchemy session.
        """
        self._session = session

    def all(self) -> list[Company]:
        """Return every stored company, ordered by symbol.

        Returns:
            All :class:`Company` rows.
        """
        return list(self._session.scalars(select(Company).order_by(Company.symbol)))

    def get_by_symbol(self, symbol: str) -> Company | None:
        """Return the company with *symbol*, or None if not stored yet.

        Args:
            symbol: NSE/BSE ticker symbol.

        Returns:
            The matching :class:`Company` or ``None``.
        """
        stmt = select(Company).where(Company.symbol == symbol)
        return self._session.scalar(stmt)

    def needs_refresh(self, symbol: str) -> bool:
        """Return True if *symbol* is missing or its data is stale.

        Combines existence and the cache staleness rule so a caller can gate
        scraping with a single check.

        Args:
            symbol: NSE/BSE ticker symbol.

        Returns:
            True if the company should be (re-)scraped.
        """
        company = self.get_by_symbol(symbol)
        if company is None:
            logger.info("Cache miss for %s — needs scrape", symbol)
            return True
        stale = cache.is_stale(company.last_updated)
        if stale:
            logger.info("Cache stale for %s (last_updated=%s)", symbol, company.last_updated)
        return stale

    def upsert(self, symbol: str, name: str, sector: str | None = None,
               industry: str | None = None, data_quality: str | None = None,
               view_type: str | None = None, scrape_error: str | None = None) -> Company:
        """Insert or update a company master record.

        The ``last_updated`` timestamp is refreshed automatically by the ORM on
        any update, marking the cache fresh again. Provenance fields
        (``data_quality``/``view_type``/``scrape_error``) are only overwritten
        when a non-None value is supplied, so a lightweight peer upsert never
        clobbers a richer prior scrape.

        Args:
            symbol: NSE/BSE ticker symbol (unique key).
            name: Company display name.
            sector: Optional sector classification.
            industry: Optional industry classification.
            data_quality: Optional "full" | "partial" | "insufficient".
            view_type: Optional "consolidated" | "standalone".
            scrape_error: Optional last-error message (nullable).

        Returns:
            The persisted :class:`Company` instance.
        """
        company = self.get_by_symbol(symbol)
        if company is None:
            company = Company(symbol=symbol, name=name, sector=sector, industry=industry)
            self._session.add(company)
            logger.debug("Inserted new company %s", symbol)
        else:
            company.name = name
            if sector is not None:
                company.sector = sector
            if industry is not None:
                company.industry = industry
            logger.debug("Updated existing company %s", symbol)
        if data_quality is not None:
            company.data_quality = data_quality
        if view_type is not None:
            company.view_type = view_type
        # scrape_error is set unconditionally (including back to None) so a
        # successful re-scrape clears a stale error.
        company.scrape_error = scrape_error
        self._session.flush()
        return company


class AnnualDataRepository:
    """Read/write access to :class:`AnnualData` rows for a company."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to an active SQLAlchemy session.

        Args:
            session: An open SQLAlchemy session.
        """
        self._session = session

    def for_company(self, company_id: int) -> list[AnnualData]:
        """Return a company's annual rows, oldest fiscal year first.

        Args:
            company_id: Primary key of the owning company.

        Returns:
            Annual data rows ordered by ``fiscal_year_end`` ascending.
        """
        stmt = (
            select(AnnualData)
            .where(AnnualData.company_id == company_id)
            .order_by(AnnualData.fiscal_year_end)
        )
        return list(self._session.scalars(stmt))

    def upsert(self, company_id: int, fiscal_year_end: date, **metrics: Any) -> AnnualData:
        """Insert or update one fiscal year of annual data for a company.

        Args:
            company_id: Primary key of the owning company.
            fiscal_year_end: Period-end date identifying the year (unique per
                company).
            **metrics: Any of revenue, ebit, net_income, free_cash_flow,
                total_assets, total_debt, shareholders_equity, eps.

        Returns:
            The persisted :class:`AnnualData` row.

        Raises:
            ValueError: If an unknown metric name is supplied.
        """
        unknown = set(metrics) - _ANNUAL_FIELDS
        if unknown:
            raise ValueError(f"Unknown AnnualData field(s): {sorted(unknown)}")

        stmt = select(AnnualData).where(
            AnnualData.company_id == company_id,
            AnnualData.fiscal_year_end == fiscal_year_end,
        )
        row = self._session.scalar(stmt)
        if row is None:
            row = AnnualData(company_id=company_id, fiscal_year_end=fiscal_year_end, **metrics)
            self._session.add(row)
            logger.debug("Inserted annual data co=%s fy=%s", company_id, fiscal_year_end)
        else:
            for key, value in metrics.items():
                setattr(row, key, value)
            logger.debug("Updated annual data co=%s fy=%s", company_id, fiscal_year_end)
        self._session.flush()
        return row


class ARExtractedDataRepository:
    """Read/write access to :class:`ARExtractedData` rows (AR-derived figures)."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to an active SQLAlchemy session.

        Args:
            session: An open SQLAlchemy session.
        """
        self._session = session

    def for_company(self, company_id: int) -> list[ARExtractedData]:
        """Return a company's AR rows, oldest fiscal year first.

        Args:
            company_id: Primary key of the owning company.

        Returns:
            AR-extracted rows ordered by ``fiscal_year`` ascending.
        """
        stmt = (
            select(ARExtractedData)
            .where(ARExtractedData.company_id == company_id)
            .order_by(ARExtractedData.fiscal_year)
        )
        return list(self._session.scalars(stmt))

    def get(self, company_id: int, fiscal_year: int,
            source: str = "annual_report") -> ARExtractedData | None:
        """Return one AR row for a company/year/source, or None.

        Args:
            company_id: Owning company id.
            fiscal_year: Fiscal year.
            source: Document source key.

        Returns:
            The matching row or None.
        """
        stmt = select(ARExtractedData).where(
            ARExtractedData.company_id == company_id,
            ARExtractedData.fiscal_year == fiscal_year,
            ARExtractedData.source == source,
        )
        return self._session.scalar(stmt)

    def exists(self, company_id: int, fiscal_year: int,
               source: str = "annual_report") -> bool:
        """Return True if an extraction already exists for this company/year."""
        return self.get(company_id, fiscal_year, source) is not None

    def upsert(self, company_id: int, fiscal_year: int,
               source: str = "annual_report", **fields: Any) -> ARExtractedData:
        """Insert or update one AR extraction row.

        Args:
            company_id: Owning company id.
            fiscal_year: Fiscal year.
            source: Document source key (unique with company/year).
            **fields: Any of the AR figure/guidance/metadata columns.

        Returns:
            The persisted :class:`ARExtractedData` row.

        Raises:
            ValueError: If an unknown field name is supplied.
        """
        unknown = set(fields) - _AR_FIELDS
        if unknown:
            raise ValueError(f"Unknown ARExtractedData field(s): {sorted(unknown)}")

        row = self.get(company_id, fiscal_year, source)
        if row is None:
            row = ARExtractedData(company_id=company_id, fiscal_year=fiscal_year,
                                  source=source, **fields)
            self._session.add(row)
            logger.debug("Inserted AR data co=%s fy=%s", company_id, fiscal_year)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            logger.debug("Updated AR data co=%s fy=%s", company_id, fiscal_year)
        self._session.flush()
        return row
