"""Upload-driven document ingestion — the user supplies the PDFs.

Scraping annual reports off exchange portals is unreliable (Cloudflare, blocks,
datacenter IPs), so instead the user uploads the source documents directly:
annual reports, quarterly filings and concall transcripts. Each PDF is parsed
(:mod:`pdf_extractor`) and structured-extracted (:mod:`ar_parser`, Groq with a
regex fallback) into ``ar_extracted_data``, keyed by ``source`` so the analysis
layer can distinguish AR / quarterly / concall data.

Concall transcripts go through guidance extraction
(:func:`screener.models.management_credibility.extract_guidance`) and are stored
as the ``guided_*`` columns, feeding the credibility tracker.

The PDF-text, parse and guidance functions are all injectable, so ingestion is
unit-tested without real PDFs or the Groq API.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from screener.database.repository import ARExtractedDataRepository, CompanyRepository
from screener.models import management_credibility
from screener.scraper import ar_parser, pdf_extractor

logger = logging.getLogger(__name__)

#: Recognised document kinds → the ``source`` value stored on each row.
ANNUAL = "annual_report"
QUARTERLY = "quarterly_filing"
CONCALL = "concall"
DOC_KINDS = (ANNUAL, QUARTERLY, CONCALL)

# Guidance metric name → ARExtractedData column (concall guidance storage).
_GUIDANCE_METRIC_COL = {
    "revenue_growth": "guided_revenue_growth",
    "revenue_growth_pct": "guided_revenue_growth",
    "margin": "guided_margin",
    "ebitda_margin": "guided_margin",
    "capex": "guided_capex",
}

_FULL_YEAR_RE = re.compile(r"(20\d{2})")
_FY_SHORT_RE = re.compile(r"FY\s*'?(\d{2})(?!\d)", re.IGNORECASE)


@dataclass
class UploadedDoc:
    """One user-uploaded document to ingest."""

    kind: str           # ANNUAL | QUARTERLY | CONCALL
    fiscal_year: int
    pdf_path: str
    name: str = ""      # original filename (for status reporting)


def infer_fiscal_year(filename: str, default: int | None = None) -> int | None:
    """Infer a fiscal year from a filename.

    Handles full years ("annual-report-2023.pdf" → 2023) and the Indian short
    form ("AR_FY24.pdf" / "Q1FY26.pdf" → 2024 / 2026).

    Args:
        filename: Uploaded file name.
        default: Value to return when no year is found.

    Returns:
        The four-digit year, or *default*.
    """
    name = filename or ""
    full = _FULL_YEAR_RE.search(name)
    if full:
        return int(full.group(1))
    short = _FY_SHORT_RE.search(name)
    if short:
        return 2000 + int(short.group(1))
    return default


def _default_text(pdf_path: str) -> str:
    """Extract a length-bounded text blob from *pdf_path* (all pages)."""
    extracted = pdf_extractor.extract_text(pdf_path)
    return pdf_extractor.truncate_for_llm(extracted, [])


def ingest(
    session: Session,
    symbol: str,
    docs: list[UploadedDoc],
    *,
    company_name: str | None = None,
    parse_fn: Callable[..., dict[str, Any]] | None = None,
    guidance_fn: Callable[..., list] | None = None,
    text_fn: Callable[[str], str] | None = None,
    client: object | None = None,
) -> list[dict[str, Any]]:
    """Ingest uploaded documents for *symbol* into the database.

    Args:
        session: Open SQLAlchemy session.
        symbol: Company ticker.
        docs: Uploaded documents to process.
        company_name: Display name to record if the company is new.
        parse_fn: ``(pdf_path, name, year) -> extraction dict`` for AR/quarterly
            (defaults to :func:`ar_parser.parse`).
        guidance_fn: ``(text, client=...) -> [GuidanceItem]`` for concalls
            (defaults to :func:`management_credibility.extract_guidance`).
        text_fn: ``pdf_path -> text`` for concalls (defaults to pdfplumber).
        client: Optional injected LLM client.

    Returns:
        A per-document status list ("ingested" | "failed" with details). Each
        document is best-effort — one failure never aborts the batch.
    """
    companies = CompanyRepository(session)
    ar = ARExtractedDataRepository(session)
    parse = parse_fn or ar_parser.parse
    extract_guidance = guidance_fn or management_credibility.extract_guidance
    read_text = text_fn or _default_text

    company = companies.get_by_symbol(symbol.upper()) or companies.upsert(
        symbol=symbol.upper(), name=company_name or symbol.upper()
    )

    results: list[dict[str, Any]] = []
    for doc in docs:
        entry: dict[str, Any] = {"name": doc.name or doc.pdf_path, "kind": doc.kind,
                                 "fiscal_year": doc.fiscal_year}
        try:
            if doc.kind == CONCALL:
                items = extract_guidance(read_text(doc.pdf_path), client=client)
                stored = 0
                for item in items:
                    col = _GUIDANCE_METRIC_COL.get(item.metric)
                    if col is not None:
                        ar.upsert(company.id, item.fiscal_year, source=CONCALL,
                                  **{col: item.guided})
                        stored += 1
                entry.update(status="ingested", guidance_items=stored)
            else:
                data = parse(doc.pdf_path, company.name, doc.fiscal_year, client=client)
                ar.upsert(company.id, doc.fiscal_year, source=doc.kind,
                          **ar_parser.flatten_extraction(data))
                entry.update(status="ingested", confidence=data.get("confidence"))
        except Exception as exc:  # best-effort per document
            logger.exception("Ingest failed for %s (%s)", doc.name, doc.kind)
            entry.update(status="failed", error=str(exc))
        results.append(entry)

    session.commit()
    ingested = sum(1 for r in results if r["status"] == "ingested")
    logger.info("Ingested %d/%d document(s) for %s", ingested, len(results), symbol)
    return results
