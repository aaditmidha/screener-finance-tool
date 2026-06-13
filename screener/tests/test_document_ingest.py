"""Tests for upload-driven document ingestion (PDFs supplied by the user)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database.models import Base
from screener.database.repository import ARExtractedDataRepository, CompanyRepository
from screener.models.management_credibility import GuidanceItem
from screener.scraper import document_ingest
from screener.scraper.document_ingest import ANNUAL, CONCALL, QUARTERLY, UploadedDoc, infer_fiscal_year


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _extraction(revenue=9000, confidence="high"):
    return {
        "unit": "Cr", "confidence": confidence, "pages_used": [1],
        "financials": {"revenue": revenue, "pat": 900, "trade_receivables": 1600},
        "guidance": {"revenue_growth_pct": None, "margin_pct": None,
                     "capex_amount": None, "raw_text": None},
        "key_risks": ["cost inflation"],
    }


class TestInferFiscalYear:
    @pytest.mark.parametrize("name,expected", [
        ("AR_FY24.pdf", 2024),
        ("annual-report-2023.pdf", 2023),
        ("Q1FY26_results.pdf", 2026),
        ("concall 2022-23.pdf", 2022),
        ("no_year.pdf", None),
    ])
    def test_inference(self, name, expected) -> None:
        assert infer_fiscal_year(name) == expected

    def test_default_when_missing(self) -> None:
        assert infer_fiscal_year("x.pdf", default=2026) == 2026


class TestIngest:
    def _ingest(self, session, docs, **kw):
        return document_ingest.ingest(
            session, "CGPOWER", docs, company_name="CG Power",
            parse_fn=lambda path, name, year, client=None: _extraction(),
            guidance_fn=lambda text, client=None: [
                GuidanceItem(fiscal_year=2025, metric="revenue_growth", guided=0.15)],
            text_fn=lambda path: "transcript text",
            **kw,
        )

    def test_annual_stored_with_source(self, session: Session) -> None:
        results = self._ingest(session, [UploadedDoc(ANNUAL, 2024, "/x/ar.pdf", "ar.pdf")])
        assert results[0]["status"] == "ingested"
        cid = CompanyRepository(session).get_by_symbol("CGPOWER").id
        row = ARExtractedDataRepository(session).get(cid, 2024, source=ANNUAL)
        assert row.revenue == 9000
        assert row.trade_receivables == 1600

    def test_quarterly_source_distinct(self, session: Session) -> None:
        self._ingest(session, [UploadedDoc(QUARTERLY, 2026, "/x/q.pdf", "q.pdf")])
        cid = CompanyRepository(session).get_by_symbol("CGPOWER").id
        assert ARExtractedDataRepository(session).get(cid, 2026, source=QUARTERLY) is not None
        assert ARExtractedDataRepository(session).get(cid, 2026, source=ANNUAL) is None

    def test_concall_stores_guidance(self, session: Session) -> None:
        results = self._ingest(session, [UploadedDoc(CONCALL, 2024, "/x/c.pdf", "c.pdf")])
        assert results[0]["guidance_items"] == 1
        cid = CompanyRepository(session).get_by_symbol("CGPOWER").id
        row = ARExtractedDataRepository(session).get(cid, 2025, source=CONCALL)
        assert row.guided_revenue_growth == 0.15

    def test_mixed_batch(self, session: Session) -> None:
        docs = [
            UploadedDoc(ANNUAL, 2024, "/x/ar.pdf", "ar.pdf"),
            UploadedDoc(QUARTERLY, 2026, "/x/q.pdf", "q.pdf"),
            UploadedDoc(CONCALL, 2024, "/x/c.pdf", "c.pdf"),
        ]
        results = self._ingest(session, docs)
        assert [r["status"] for r in results] == ["ingested"] * 3

    def test_failure_recorded_not_fatal(self, session: Session) -> None:
        def _boom(path, name, year, client=None):
            raise RuntimeError("corrupt pdf")
        results = document_ingest.ingest(
            session, "CGPOWER", [UploadedDoc(ANNUAL, 2024, "/x/ar.pdf", "ar.pdf")],
            company_name="CG Power", parse_fn=_boom,
        )
        assert results[0]["status"] == "failed"
        assert "corrupt" in results[0]["error"]

    def test_creates_company_if_absent(self, session: Session) -> None:
        self._ingest(session, [UploadedDoc(ANNUAL, 2024, "/x/ar.pdf", "ar.pdf")])
        assert CompanyRepository(session).get_by_symbol("CGPOWER").name == "CG Power"
