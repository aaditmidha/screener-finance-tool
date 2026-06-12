"""SQLAlchemy ORM models for persisted financial data.

Every persisted row carries a ``last_updated`` timestamp (set on insert and
refreshed on update) so the cache layer can decide whether data is stale.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class TimestampMixin:
    """Adds a ``last_updated`` column refreshed on every insert and update."""

    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        nullable=False,
    )


class Company(TimestampMixin, Base):
    """Master record for a listed company."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))

    annual_data: Mapped[list["AnnualData"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    quarterly_data: Mapped[list["QuarterlyData"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class AnnualData(TimestampMixin, Base):
    """Annual P&L / balance-sheet snapshot for a company."""

    __tablename__ = "annual_data"
    __table_args__ = (UniqueConstraint("company_id", "fiscal_year_end", name="uq_annual_company_year"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    fiscal_year_end: Mapped["Date"] = mapped_column(Date, nullable=False)

    revenue: Mapped[float | None] = mapped_column(Float)
    ebit: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    total_assets: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    shareholders_equity: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)

    company: Mapped["Company"] = relationship(back_populates="annual_data")


class QuarterlyData(TimestampMixin, Base):
    """Quarterly results snapshot for a company."""

    __tablename__ = "quarterly_data"
    __table_args__ = (UniqueConstraint("company_id", "quarter_end", name="uq_quarterly_company_quarter"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    quarter_end: Mapped["Date"] = mapped_column(Date, nullable=False)

    revenue: Mapped[float | None] = mapped_column(Float)
    ebit: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)

    company: Mapped["Company"] = relationship(back_populates="quarterly_data")
