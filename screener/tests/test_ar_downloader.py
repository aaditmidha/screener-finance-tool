"""Tests for the annual-report downloader.

The Playwright-backed primitives (_render_page, _fetch_bytes) are monkeypatched
so no real browser launches; the focus is cache behaviour, source-priority
orchestration, delay configuration, and parsing helpers.
"""

import copy

import pytest

from screener.config import CONFIG
from screener.scraper import ar_downloader
from screener.scraper.ar_downloader import AnnualReportDownloader, DownloadResult, extract_year
from screener.scraper.exceptions import DownloadError

_PDF_BYTES = b"%PDF-1.7\n%stub annual report\n"


@pytest.fixture()
def cfg(tmp_path) -> dict:
    """Config dict with storage redirected into a temp dir."""
    c = copy.deepcopy({"annual_reports": CONFIG["annual_reports"]})
    c["annual_reports"]["storage_dir"] = str(tmp_path / "annual_reports")
    return c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually sleep during delay calls."""
    monkeypatch.setattr(ar_downloader.time, "sleep", lambda _s: None)


@pytest.fixture()
def downloader(cfg: dict, monkeypatch: pytest.MonkeyPatch) -> AnnualReportDownloader:
    """A downloader whose network fetch returns a stub PDF."""
    d = AnnualReportDownloader(config=cfg)
    monkeypatch.setattr(d, "_fetch_bytes", lambda url: _PDF_BYTES)
    return d


class TestExtractYear:
    @pytest.mark.parametrize("text,expected", [
        ("Annual Report FY2024", 2024),
        ("annual-report-2023-24.pdf", 2023),
        ("FY 2022-23", 2022),
        ("Integrated Report 2021", 2021),
        ("https://x.test/reports/AR_2020.pdf", 2020),
        ("no year here", None),
        ("", None),
    ])
    def test_extraction(self, text: str, expected: int | None) -> None:
        assert extract_year(text) == expected


class TestCache:
    def test_cache_path_structure(self, downloader: AnnualReportDownloader) -> None:
        path = downloader.cache_path("infy", 2024, "AR.pdf")
        # <root>/INFY/2024/AR.pdf  — symbol upper-cased, year as dir
        assert path.parts[-3:] == ("INFY", "2024", "AR.pdf")

    def test_find_cached_returns_none_when_absent(self, downloader: AnnualReportDownloader) -> None:
        assert downloader.find_cached("INFY", 2024) is None

    def test_find_cached_finds_saved_pdf(self, downloader: AnnualReportDownloader) -> None:
        dest = downloader.cache_path("INFY", 2024, "AR.pdf")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(_PDF_BYTES)
        assert downloader.find_cached("INFY", 2024) == dest

    def test_find_cached_ignores_empty_files(self, downloader: AnnualReportDownloader) -> None:
        dest = downloader.cache_path("INFY", 2024, "AR.pdf")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"")
        assert downloader.find_cached("INFY", 2024) is None


class TestDelays:
    def test_bse_delay_is_at_least_15s(self, downloader: AnnualReportDownloader) -> None:
        """BSE is the aggressive blocker — its min delay must be ≥ 15s."""
        assert downloader._sources["bse"]["min_delay_seconds"] >= 15

    def test_sleep_uses_configured_bounds(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, float] = {}
        monkeypatch.setattr(
            ar_downloader.random, "uniform",
            lambda lo, hi: captured.update(lo=lo, hi=hi) or lo,
        )
        downloader._sleep_for_source("bse")
        assert captured["lo"] == downloader._sources["bse"]["min_delay_seconds"]
        assert captured["hi"] == downloader._sources["bse"]["max_delay_seconds"]


class TestOrchestration:
    def test_cache_hit_short_circuits(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached file must be returned without consulting any resolver."""
        dest = downloader.cache_path("INFY", 2024, "AR.pdf")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(_PDF_BYTES)

        def _boom(*a, **k):
            raise AssertionError("resolver must not run on cache hit")

        monkeypatch.setattr(downloader, "resolve_nse", _boom)
        result = downloader.download_report("INFY", 2024)
        assert result.from_cache is True
        assert result.source == "cache"

    def test_priority_ir_before_nse(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IR page must be tried first; NSE only if IR has no match."""
        order: list[str] = []
        monkeypatch.setattr(downloader, "resolve_ir_page",
                            lambda s, u: order.append("ir") or {})
        monkeypatch.setattr(downloader, "resolve_nse",
                            lambda s: order.append("nse") or {2024: "https://x.test/AR_2024.pdf"})
        monkeypatch.setattr(downloader, "resolve_bse",
                            lambda s: order.append("bse") or {})

        result = downloader.download_report("INFY", 2024, ir_url="https://infy.test/ir")
        assert order == ["ir", "nse"]      # bse never reached
        assert result.source == "nse"
        assert result.from_cache is False
        assert result.path.exists()

    def test_skips_ir_when_no_url(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no IR URL, the chain must start at NSE."""
        order: list[str] = []
        monkeypatch.setattr(downloader, "resolve_nse",
                            lambda s: order.append("nse") or {})
        monkeypatch.setattr(downloader, "resolve_bse",
                            lambda s: order.append("bse") or {2024: "https://x.test/AR_2024.pdf"})

        result = downloader.download_report("INFY", 2024)
        assert order == ["nse", "bse"]
        assert result.source == "bse"

    def test_resolver_error_falls_through(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolver raising DownloadError must not abort the chain."""
        def _fail(_s):
            raise DownloadError("INFY", 2024, ["render"])

        monkeypatch.setattr(downloader, "resolve_nse", _fail)
        monkeypatch.setattr(downloader, "resolve_bse",
                            lambda s: {2024: "https://x.test/AR_2024.pdf"})
        result = downloader.download_report("INFY", 2024)
        assert result.source == "bse"

    def test_all_sources_exhausted_raises(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloader, "resolve_nse", lambda s: {})
        monkeypatch.setattr(downloader, "resolve_bse", lambda s: {})
        with pytest.raises(DownloadError) as exc_info:
            downloader.download_report("INFY", 2024)
        assert exc_info.value.sources_tried == ["nse", "bse"]

    def test_returns_download_result_type(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloader, "resolve_nse",
                            lambda s: {2024: "https://x.test/AR_2024.pdf"})
        assert isinstance(downloader.download_report("INFY", 2024), DownloadResult)


class TestParsingHelpers:
    def test_extract_pdf_links_filters_and_resolves(self) -> None:
        html = """
        <a href="/files/annual-report-2024.pdf">Annual Report 2024</a>
        <a href="https://cdn.test/AR_2023.PDF">FY2023 Report</a>
        <a href="/about.html">About us</a>
        """
        links = AnnualReportDownloader._extract_pdf_links(html, base_url="https://co.test/ir")
        urls = [u for _t, u in links]
        assert "https://co.test/files/annual-report-2024.pdf" in urls
        assert "https://cdn.test/AR_2023.PDF" in urls
        assert all(".pdf" in u.lower() for u in urls)
        assert len(links) == 2

    def test_resolve_ir_page_matches_keywords_and_years(
        self, downloader: AnnualReportDownloader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        html = """
        <a href="/ar-2024.pdf">Annual Report FY2024</a>
        <a href="/ar-2023.pdf">Annual Report FY2023</a>
        <a href="/results-q1.pdf">Q1 Results 2024</a>
        """
        monkeypatch.setattr(downloader, "_render_page", lambda url: html)
        found = downloader.resolve_ir_page("INFY", "https://infy.test/ir")
        assert found == {
            2024: "https://infy.test/ar-2024.pdf",
            2023: "https://infy.test/ar-2023.pdf",
        }  # Q1 results excluded — no "annual report" keyword

    def test_save_pdf_rejects_non_pdf(self, downloader: AnnualReportDownloader) -> None:
        dest = downloader.cache_path("INFY", 2024, "bad.pdf")
        with pytest.raises(DownloadError):
            downloader._save_pdf(b"<html>not a pdf</html>", dest)
        assert not dest.exists()
