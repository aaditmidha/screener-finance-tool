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
from screener.database.models import AnnualData, Company

logger = logging.getLogger(__name__)

# Columns on AnnualData a caller may set via upsert (excludes keys/timestamps).
_ANNUAL_FIELDS = {
    "revenue", "ebit", "net_income", "free_cash_flow",
    "total_assets", "total_debt", "shareholders_equity", "eps",
}


class CompanyRepository:
    """Read/write access to :class:`Company` records with cache awareness."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to an active SQLAlchemy session.

        Args:
            session: An open SQLAlchemy session.
        """
        self._session = session

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
               industry: str | None = None) -> Company:
        """Insert or update a company master record.

        The ``last_updated`` timestamp is refreshed automatically by the ORM on
        any update, marking the cache fresh again.

        Args:
            symbol: NSE/BSE ticker symbol (unique key).
            name: Company display name.
            sector: Optional sector classification.
            industry: Optional industry classification.

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
            company.sector = sector
            company.industry = industry
            logger.debug("Updated existing company %s", symbol)
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
