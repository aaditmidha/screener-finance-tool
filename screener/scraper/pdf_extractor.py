"""Annual-report PDF text extraction (pdfplumber).

Pulls text (and tables) out of a downloaded AR PDF, identifies the financial-
statement pages by keyword, and builds a focused, length-bounded text blob to
feed the LLM extractor. This is part of the *local-only* acquisition pipeline —
the hosted app reads the resulting structured data from the DB and never runs
this.

The pdfplumber-touching call (:func:`extract_text`) is isolated; the page
selection and truncation helpers are pure and unit-tested without a real PDF.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["annual_report_extraction"]


@dataclass
class ExtractedPdf:
    """Text and tables pulled from a PDF, keyed by 1-based page number."""

    pages: dict[int, str] = field(default_factory=dict)
    tables: dict[int, list] = field(default_factory=dict)
    page_count: int = 0

    @property
    def full_text(self) -> str:
        """Return all page text concatenated in page order."""
        return "\n".join(self.pages[p] for p in sorted(self.pages))


def extract_text(pdf_path: str | Path) -> ExtractedPdf:
    """Extract per-page text and tables from *pdf_path* using pdfplumber.

    Args:
        pdf_path: Path to a local PDF file.

    Returns:
        An :class:`ExtractedPdf` with text/tables for every page that yielded
        text.

    Raises:
        ImportError: If pdfplumber is not installed (local-only dependency).
        FileNotFoundError: If the PDF does not exist.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    import pdfplumber  # local import — dev/local-only dependency

    result = ExtractedPdf()
    with pdfplumber.open(str(path)) as pdf:
        result.page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=3, y_tolerance=3)
            if text:
                result.pages[i] = text
            tables = page.extract_tables()
            if tables:
                result.tables[i] = tables
    logger.info("Extracted %d/%d text pages from %s", len(result.pages), result.page_count, path.name)
    return result


def find_financial_pages(extracted: ExtractedPdf) -> list[int]:
    """Return page numbers that look like financial-statement pages.

    Args:
        extracted: Output of :func:`extract_text`.

    Returns:
        Sorted 1-based page numbers whose text contains any configured
        financial-statement keyword.
    """
    keywords = [k.lower() for k in _cfg["financial_page_keywords"]]
    hits = [
        page_num
        for page_num, text in extracted.pages.items()
        if any(kw in text.lower() for kw in keywords)
    ]
    logger.debug("Identified %d financial page(s): %s", len(hits), sorted(hits))
    return sorted(hits)


def detect_unit(text: str) -> str:
    """Detect whether amounts are in Crores or Lakhs from the text.

    Args:
        text: Financial-statement text.

    Returns:
        "Cr" or "Lakhs"; defaults to "Cr" (the Indian large-cap norm) if no
        marker is found.
    """
    low = text.lower()
    for unit, markers in _cfg["unit_markers"].items():
        if any(m in low for m in markers):
            return unit
    return "Cr"


def truncate_for_llm(
    extracted: ExtractedPdf,
    target_pages: list[int],
    max_chars: int | None = None,
) -> str:
    """Build a length-bounded text blob from the financial pages.

    Args:
        extracted: Output of :func:`extract_text`.
        target_pages: Page numbers to include (from :func:`find_financial_pages`).
            If empty, falls back to the whole document.
        max_chars: Character cap. Defaults to config ``max_llm_chars``.

    Returns:
        Concatenated page text (each prefixed with its page number), truncated
        to *max_chars*.
    """
    cap = max_chars if max_chars is not None else _cfg["max_llm_chars"]
    pages = target_pages or sorted(extracted.pages)
    blob_parts = [f"[Page {p}]\n{extracted.pages[p]}" for p in pages if p in extracted.pages]
    blob = "\n\n".join(blob_parts)
    if len(blob) > cap:
        logger.debug("Truncating LLM blob from %d to %d chars", len(blob), cap)
        blob = blob[:cap]
    return blob
