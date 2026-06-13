"""Tests for the repositories (CRUD, cache-aware refresh, annual data)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database import cache
from screener.database.models import Base, Company
from screener.database.repository import (
    AnnualDataRepository,
    ARExtractedDataRepository,
    CompanyRepository,
)


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


class TestProvenanceFields:
    """data_quality / view_type / scrape_error handling on upsert."""

    def test_provenance_persisted(self, session: Session) -> None:
        repo = CompanyRepository(session)
        repo.upsert("INFY", "Infosys", data_quality="full", view_type="consolidated")
        session.commit()
        company = repo.get_by_symbol("INFY")
        assert company.data_quality == "full"
        assert company.view_type == "consolidated"

    def test_none_provenance_does_not_clobber(self, session: Session) -> None:
        """A later lightweight upsert (no quality args) keeps the prior grade."""
        repo = CompanyRepository(session)
        repo.upsert("INFY", "Infosys", data_quality="full", view_type="consolidated")
        repo.upsert("INFY", "Infosys")          # peer-style upsert, no provenance
        session.commit()
        company = repo.get_by_symbol("INFY")
        assert company.data_quality == "full"   # preserved
        assert company.view_type == "consolidated"

    def test_scrape_error_cleared_on_success(self, session: Session) -> None:
        repo = CompanyRepository(session)
        repo.upsert("INFY", "Infosys", scrape_error="blocked")
        repo.upsert("INFY", "Infosys", data_quality="full")   # success path
        session.commit()
        assert repo.get_by_symbol("INFY").scrape_error is None


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


class TestARExtractedDataRepository:
    """CRUD + cache checks for AR-extracted figures."""

    def _company_id(self, session: Session) -> int:
        company = CompanyRepository(session).upsert("CGPOWER", "CG Power")
        session.commit()
        return company.id

    def test_upsert_and_get(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = ARExtractedDataRepository(session)
        repo.upsert(cid, 2026, revenue=9000, trade_receivables=1600,
                    extraction_confidence="high", unit="Cr")
        session.commit()
        row = repo.get(cid, 2026)
        assert row.revenue == 9000
        assert row.trade_receivables == 1600
        assert row.extraction_confidence == "high"

    def test_exists(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = ARExtractedDataRepository(session)
        assert repo.exists(cid, 2026) is False
        repo.upsert(cid, 2026, revenue=9000)
        session.commit()
        assert repo.exists(cid, 2026) is True

    def test_upsert_updates_same_year(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = ARExtractedDataRepository(session)
        repo.upsert(cid, 2026, revenue=100)
        repo.upsert(cid, 2026, revenue=9000)
        session.commit()
        assert len(repo.for_company(cid)) == 1
        assert repo.get(cid, 2026).revenue == 9000

    def test_for_company_ordered(self, session: Session) -> None:
        cid = self._company_id(session)
        repo = ARExtractedDataRepository(session)
        repo.upsert(cid, 2026, revenue=3)
        repo.upsert(cid, 2024, revenue=1)
        repo.upsert(cid, 2025, revenue=2)
        session.commit()
        assert [r.fiscal_year for r in repo.for_company(cid)] == [2024, 2025, 2026]

    def test_unknown_field_raises(self, session: Session) -> None:
        cid = self._company_id(session)
        with pytest.raises(ValueError):
            ARExtractedDataRepository(session).upsert(cid, 2026, bogus=1.0)
