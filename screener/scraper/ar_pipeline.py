"""Annual-report pipeline: download → extract → store exact figures.

Orchestrates the local-only acquisition flow: for each fiscal year, ensure the
AR PDF is downloaded (cache-first), extract structured data from it
(:mod:`ar_parser`), and persist it to ``ar_extracted_data``. Each year is
best-effort — a missing PDF or a failed extraction is recorded and the batch
continues. Already-extracted years are skipped unless ``force=True``.

The downloader and parser are injected, so the orchestration is unit-tested
without real PDFs, a browser, or the Groq API.
"""

import logging
from collections.abc import Iterable
from typing import Any, Callable

from sqlalchemy.orm import Session

from screener.config import CONFIG
from screener.database.repository import ARExtractedDataRepository, CompanyRepository
from screener.scraper import ar_parser
from screener.scraper.ar_downloader import AnnualReportDownloader
from screener.scraper.exceptions import DownloadError

logger = logging.getLogger(__name__)

_cfg = CONFIG["annual_report_extraction"]

# Re-exported from ar_parser (kept here for backwards-compatible imports).
flatten_extraction = ar_parser.flatten_extraction


def recent_years(count: int, end_year: int) -> list[int]:
    """Return the *count* fiscal years ending at *end_year* (descending input,
    ascending output).

    Args:
        count: Number of years.
        end_year: Most recent fiscal year.

    Returns:
        Ascending list of years, e.g. recent_years(3, 2026) → [2024, 2025, 2026].
    """
    return list(range(end_year - count + 1, end_year + 1))


class ARPipeline:
    """Downloads, extracts and stores annual-report data for a company."""

    def __init__(
        self,
        session: Session,
        downloader: AnnualReportDownloader | None = None,
        parse_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        """Wire the pipeline to a DB session, a downloader and a parser.

        Args:
            session: An open SQLAlchemy session.
            downloader: AR downloader (defaults to a real one — local-only).
            parse_fn: ``(pdf_path, company_name, year) -> dict`` extractor;
                defaults to :func:`ar_parser.parse`.
        """
        self._session = session
        self._companies = CompanyRepository(session)
        self._ar = ARExtractedDataRepository(session)
        self._downloader = downloader or AnnualReportDownloader()
        self._parse = parse_fn or ar_parser.parse

    def _store(self, company_id: int, year: int, data: dict[str, Any]) -> None:
        """Flatten and persist one year's extraction."""
        self._ar.upsert(company_id, year, **flatten_extraction(data))

    def process(
        self,
        symbol: str,
        years: Iterable[int],
        ir_url: str | None = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Run download → extract → store for *symbol* across *years*.

        Args:
            symbol: Company ticker.
            years: Fiscal years to process.
            ir_url: Optional investor-relations URL to prioritise downloads.
            force: Re-extract even if a row already exists.

        Returns:
            A per-year status list, each item a dict with ``year`` and
            ``status`` ("cached" | "extracted" | "no_pdf" | "failed").
        """
        symbol = symbol.upper()
        company = self._companies.get_by_symbol(symbol) or self._companies.upsert(symbol, symbol)
        results: list[dict[str, Any]] = []

        for year in years:
            if not force and self._ar.exists(company.id, year):
                logger.info("%s FY%s: already extracted, skipping", symbol, year)
                results.append({"year": year, "status": "cached"})
                continue
            try:
                download = self._downloader.download_report(symbol, year, ir_url=ir_url)
            except DownloadError as exc:
                logger.warning("%s FY%s: no PDF (%s)", symbol, year, exc)
                results.append({"year": year, "status": "no_pdf", "error": str(exc)})
                continue
            try:
                data = self._parse(str(download.path), company.name, year)
                self._store(company.id, year, data)
                results.append({
                    "year": year, "status": "extracted",
                    "confidence": data.get("confidence"), "source": download.source,
                })
            except Exception as exc:  # extraction is best-effort per year
                logger.exception("%s FY%s extraction failed", symbol, year)
                results.append({"year": year, "status": "failed", "error": str(exc)})

        self._session.commit()
        extracted = sum(1 for r in results if r["status"] == "extracted")
        logger.info("AR pipeline for %s: %d extracted, %d total", symbol, extracted, len(results))
        return results
