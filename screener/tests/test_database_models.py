"""Tests for the ORM models and last_updated timestamp behaviour."""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database.models import AnnualData, Base, Company, QuarterlyData


@pytest.fixture()
def session() -> Session:
    """Provide an isolated in-memory SQLite session with tables created."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_company_gets_last_updated_on_insert(session: Session) -> None:
    """A newly inserted company must receive a last_updated timestamp."""
    company = Company(symbol="INFY", name="Infosys Ltd")
    session.add(company)
    session.commit()
    assert isinstance(company.last_updated, datetime)


def test_last_updated_changes_on_update(session: Session) -> None:
    """Updating a company must refresh its last_updated timestamp."""
    company = Company(symbol="TCS", name="TCS Ltd")
    session.add(company)
    session.commit()
    first = company.last_updated

    company.name = "Tata Consultancy Services"
    session.commit()
    assert company.last_updated >= first


def test_annual_and_quarterly_relationships(session: Session) -> None:
    """Annual and quarterly rows must link back to their company."""
    company = Company(symbol="RELIANCE", name="Reliance Industries")
    company.annual_data.append(AnnualData(fiscal_year_end=date(2024, 3, 31), revenue=1000.0))
    company.quarterly_data.append(QuarterlyData(quarter_end=date(2024, 6, 30), revenue=260.0))
    session.add(company)
    session.commit()

    assert company.annual_data[0].company is company
    assert company.quarterly_data[0].company is company
    assert company.annual_data[0].last_updated is not None
