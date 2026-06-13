"""Tests for the AR pipeline orchestration (downloader + parser mocked)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from screener.database.models import Base
from screener.database.repository import ARExtractedDataRepository, CompanyRepository
from screener.scraper import ar_pipeline
from screener.scraper.ar_pipeline import ARPipeline, flatten_extraction, recent_years
from screener.scraper.exceptions import DownloadError


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _extraction(revenue=9000, receivables=1600, confidence="high"):
    return {
        "unit": "Cr", "confidence": confidence, "pages_used": [142],
        "financials": {"revenue": revenue, "pat": 900, "cfo": 1100,
                       "trade_receivables": receivables, "total_assets": 12000},
        "guidance": {"revenue_growth_pct": 0.15, "margin_pct": None,
                     "capex_amount": None, "raw_text": "target 15%"},
        "key_risks": ["cost inflation"],
    }


class _FakeDownloader:
    """Returns a DownloadResult-like for given years; fails for others."""

    def __init__(self, available: set[int]) -> None:
        self._available = available
        self.calls: list[int] = []

    def download_report(self, symbol: str, year: int, ir_url=None):
        self.calls.append(year)
        if year not in self._available:
            raise DownloadError(symbol, year, ["ir", "nse", "bse"])
        return SimpleNamespace(path=Path(f"/tmp/{symbol}_{year}.pdf"),
                               source="ir_page", from_cache=False)


class TestHelpers:
    def test_recent_years(self) -> None:
        assert recent_years(3, 2026) == [2024, 2025, 2026]

    def test_flatten_extraction(self) -> None:
        flat = flatten_extraction(_extraction())
        assert flat["revenue"] == 9000
        assert flat["trade_receivables"] == 1600
        assert flat["guided_revenue_growth"] == 0.15      # nested guidance mapped
        assert flat["extraction_confidence"] == "high"
        import json
        assert json.loads(flat["key_risks"]) == ["cost inflation"]
        assert json.loads(flat["pages_used"]) == [142]

    def test_flatten_omits_nulls(self) -> None:
        flat = flatten_extraction(_extraction())
        assert "guided_margin" not in flat                # None guidance dropped


class TestProcess:
    def _pipeline(self, session, downloader, extraction=None):
        return ARPipeline(
            session,
            downloader=downloader,
            parse_fn=lambda path, name, year: extraction or _extraction(),
        )

    def test_extracts_and_persists(self, session: Session) -> None:
        dl = _FakeDownloader({2024, 2025, 2026})
        pipe = self._pipeline(session, dl)
        results = pipe.process("CGPOWER", [2024, 2025, 2026])

        assert [r["status"] for r in results] == ["extracted"] * 3
        company = CompanyRepository(session).get_by_symbol("CGPOWER")
        rows = ARExtractedDataRepository(session).for_company(company.id)
        assert [r.fiscal_year for r in rows] == [2024, 2025, 2026]
        assert rows[-1].trade_receivables == 1600
        assert rows[-1].guided_revenue_growth == 0.15

    def test_cached_years_skip_download(self, session: Session) -> None:
        dl = _FakeDownloader({2026})
        pipe = self._pipeline(session, dl)
        pipe.process("CGPOWER", [2026])            # first extraction
        dl.calls.clear()
        results = pipe.process("CGPOWER", [2026])  # second run → cached
        assert results == [{"year": 2026, "status": "cached"}]
        assert dl.calls == []                       # downloader not consulted

    def test_force_reextracts(self, session: Session) -> None:
        dl = _FakeDownloader({2026})
        pipe = self._pipeline(session, dl)
        pipe.process("CGPOWER", [2026])
        results = pipe.process("CGPOWER", [2026], force=True)
        assert results[0]["status"] == "extracted"

    def test_missing_pdf_recorded_not_fatal(self, session: Session) -> None:
        dl = _FakeDownloader({2026})               # 2024 unavailable
        pipe = self._pipeline(session, dl)
        results = pipe.process("CGPOWER", [2024, 2026])
        statuses = {r["year"]: r["status"] for r in results}
        assert statuses == {2024: "no_pdf", 2026: "extracted"}

    def test_extraction_failure_recorded_not_fatal(self, session: Session) -> None:
        dl = _FakeDownloader({2026})

        def _boom(path, name, year):
            raise RuntimeError("pdf corrupt")

        pipe = ARPipeline(session, downloader=dl, parse_fn=_boom)
        results = pipe.process("CGPOWER", [2026])
        assert results[0]["status"] == "failed"
        assert "corrupt" in results[0]["error"]
