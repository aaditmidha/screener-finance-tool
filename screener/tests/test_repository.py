"""Tests for the repositories (CRUD, cache-aware refresh, annual data)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database import cache
from screener.database.models import Base, Company
from screener.database.repository import AnnualDataRepository, CompanyRepository


@pytest.fixture()
def session() -> Session:
    """Provide an isolated in-memory SQLite session with tables created."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_needs_refresh_when_missing(session: Session) -> None:
    """An unknown symbol should need a refresh."""
    repo = CompanyRepository(session)
    assert repo.needs_refresh("UNKNOWN") is True


def test_upsert_inserts_then_found(session: Session) -> None:
    """upsert should create a record retrievable by symbol."""
    repo = CompanyRepository(session)
    repo.upsert("INFY", "Infosys Ltd", sector="IT")
    session.commit()

    found = repo.get_by_symbol("INFY")
    assert found is not None
    assert found.name == "Infosys Ltd"
    assert found.sector == "IT"


def test_fresh_record_does_not_need_refresh(session: Session) -> None:
    """A just-upserted record should be considered fresh."""
    repo = CompanyRepository(session)
    repo.upsert("TCS", "TCS Ltd")
    session.commit()
    assert repo.needs_refresh("TCS") is False


def test_stale_record_needs_refresh(session: Session) -> None:
    """A record whose last_updated is older than max_age should need refresh."""
    repo = CompanyRepository(session)
    company = Company(symbol="OLD", name="Old Co")
    session.add(company)
    session.commit()

    # Force the timestamp into the past, beyond the cache window.
    company.last_updated = datetime.now(timezone.utc) - cache.max_age() - timedelta(days=1)
    session.commit()

    assert repo.needs_refresh("OLD") is True


def test_upsert_updates_existing(session: Session) -> None:
    """A second upsert for the same symbol should update, not duplicate."""
    repo = CompanyRepository(session)
    repo.upsert("WIPRO", "Wipro")
    session.commit()
    repo.upsert("WIPRO", "Wipro Limited", sector="IT")
    session.commit()

    found = repo.get_by_symbol("WIPRO")
    assert found is not None
    assert found.name == "Wipro Limited"
    assert found.sector == "IT"


def test_all_returns_companies_ordered_by_symbol(session: Session) -> None:
    repo = CompanyRepository(session)
    repo.upsert("WIPRO", "Wipro")
    repo.upsert("INFY", "Infosys")
    repo.upsert("TCS", "TCS")
    session.commit()
    assert [c.symbol for c in repo.all()] == ["INFY", "TCS", "WIPRO"]


class TestAnnualDataRepository:
    """CRUD and ordering for AnnualData rows."""

    def _company_id(self, session: Session) -> int:
        company = CompanyRepository(session).upsert("INFY", "Infosys")
        session.commit()
        return company.id

    def test_upsert_inserts_then_reads_back(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = AnnualDataRepository(session)
        repo.upsert(cid, date(2024, 3, 31), revenue=153_670, net_income=26_233)
        session.commit()

        rows = repo.for_company(cid)
        assert len(rows) == 1
        assert rows[0].revenue == pytest.approx(153_670)
        assert rows[0].net_income == pytest.approx(26_233)

    def test_upsert_updates_same_year(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = AnnualDataRepository(session)
        repo.upsert(cid, date(2024, 3, 31), revenue=100)
        repo.upsert(cid, date(2024, 3, 31), revenue=153_670)
        session.commit()

        rows = repo.for_company(cid)
        assert len(rows) == 1            # not duplicated
        assert rows[0].revenue == pytest.approx(153_670)

    def test_for_company_orders_oldest_first(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = AnnualDataRepository(session)
        repo.upsert(cid, date(2024, 3, 31), revenue=153_670)
        repo.upsert(cid, date(2022, 3, 31), revenue=121_641)
        repo.upsert(cid, date(2023, 3, 31), revenue=146_767)
        session.commit()

        years = [r.fiscal_year_end.year for r in repo.for_company(cid)]
        assert years == [2022, 2023, 2024]

    def test_unknown_field_raises(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = AnnualDataRepository(session)
        with pytest.raises(ValueError):
            repo.upsert(cid, date(2024, 3, 31), bogus_metric=1.0)
