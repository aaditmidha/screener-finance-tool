"""HTML parsing helpers for Screener.in company pages.

Screener renders each statement as a ``<section>`` (ids ``profit-loss``,
``balance-sheet``, ``cash-flow``, ``ratios``, ``quarters``) containing a
``table.data-table``: the header row carries the period labels (e.g. "Mar
2024") and each body row starts with a line-item label followed by one value
per period. :func:`parse_company_financials` turns those tables into typed
:class:`FinancialTable` objects with numbers cleaned of Indian-format commas,
percent signs and footnote markers.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from screener.scraper.exceptions import ParseError

logger = logging.getLogger(__name__)

# Screener section anchor → friendly statement name.
_SECTIONS: dict[str, str] = {
    "profit-loss": "profit_loss",
    "balance-sheet": "balance_sheet",
    "cash-flow": "cash_flow",
    "ratios": "ratios",
    "quarters": "quarters",
}

# Strip everything except digits, sign, decimal point (drops commas, %, ₹, +).
_NUM_CLEAN = re.compile(r"[^\d.\-]")


@dataclass
class FinancialTable:
    """One parsed Screener statement: period headers and labelled rows."""

    section: str
    periods: list[str]
    rows: dict[str, list[float | None]] = field(default_factory=dict)

    def row(self, label_contains: str) -> list[float | None] | None:
        """Return the first row whose label contains *label_contains*.

        Args:
            label_contains: Case-insensitive substring to match against row
                labels (e.g. "sales", "net profit").

        Returns:
            The matching row's values, or None if no label matches.
        """
        needle = label_contains.lower()
        for label, values in self.rows.items():
            if needle in label.lower():
                return values
        return None

    def latest(self, label_contains: str) -> float | None:
        """Return the most recent non-None value of a matching row.

        Args:
            label_contains: Case-insensitive substring to match a row label.

        Returns:
            The last non-None value in the matched row, or None.
        """
        values = self.row(label_contains)
        if not values:
            return None
        for value in reversed(values):
            if value is not None:
                return value
        return None


@dataclass
class CompanyFinancials:
    """All statements parsed from a single Screener company page."""

    name: str
    symbol: str
    profit_loss: FinancialTable | None = None
    balance_sheet: FinancialTable | None = None
    cash_flow: FinancialTable | None = None
    ratios: FinancialTable | None = None
    quarters: FinancialTable | None = None


def clean_number(text: str | None) -> float | None:
    """Parse a Screener cell into a float, or None when it is not numeric.

    Handles Indian-format thousands separators ("1,23,456"), percent signs,
    currency symbols, trailing "+/-" expand markers, and en-dash placeholders.

    Args:
        text: Raw cell text.

    Returns:
        The numeric value, or None for blank / non-numeric cells.
    """
    if text is None:
        return None
    raw = text.strip().replace("–", "-").replace("—", "-")  # en/em dash → minus
    if raw in ("", "-", "—"):
        return None
    negative = raw.startswith("-")
    cleaned = _NUM_CLEAN.sub("", raw)
    # A lone sign or stray dot is not a number.
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        logger.debug("Could not parse number from %r", text)
        return None
    return -value if negative and value > 0 else value


def parse_table(soup: BeautifulSoup, section_id: str) -> FinancialTable | None:
    """Parse one Screener statement section into a FinancialTable.

    Args:
        soup: Parsed page.
        section_id: Screener section anchor id (e.g. "profit-loss").

    Returns:
        A populated FinancialTable, or None if the section/table is absent.

    Raises:
        ParseError: If the section exists but has no parseable header row.
    """
    section = soup.find("section", id=section_id) or soup.find(id=section_id)
    if section is None:
        logger.debug("Section %r not found", section_id)
        return None
    table = section.find("table")
    if table is None:
        logger.debug("Section %r has no table", section_id)
        return None

    header_cells = _header_cells(table)
    if not header_cells:
        raise ParseError(section_id, "header row")
    # First header cell is the (empty) label column; the rest are periods.
    periods = [c.get_text(strip=True) for c in header_cells[1:]]

    name = _SECTIONS.get(section_id, section_id)
    result = FinancialTable(section=name, periods=periods)

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = _clean_label(cells[0].get_text(strip=True))
        if not label:
            continue
        values = [clean_number(c.get_text(strip=True)) for c in cells[1:]]
        result.rows[label] = values

    logger.debug("Parsed %s: %d periods, %d rows", section_id, len(periods), len(result.rows))
    return result


def parse_company_financials(html: str) -> CompanyFinancials:
    """Parse every statement from a Screener company page.

    Args:
        html: Raw HTML of the company page.

    Returns:
        A CompanyFinancials with whichever statements were present populated.
    """
    soup = BeautifulSoup(html, "lxml")
    financials = CompanyFinancials(
        name=_extract_company_name(soup),
        symbol=_extract_symbol(soup),
    )
    financials.profit_loss = parse_table(soup, "profit-loss")
    financials.balance_sheet = parse_table(soup, "balance-sheet")
    financials.cash_flow = parse_table(soup, "cash-flow")
    financials.ratios = parse_table(soup, "ratios")
    financials.quarters = parse_table(soup, "quarters")
    logger.info("Parsed financials for %s (%s)", financials.name, financials.symbol)
    return financials


def _header_cells(table: Tag) -> list[Tag]:
    """Return the header row's cells (from <thead> or the first <tr>)."""
    thead = table.find("thead")
    header_row = thead.find("tr") if thead else table.find("tr")
    return header_row.find_all(["th", "td"]) if header_row else []


def _clean_label(label: str) -> str:
    """Strip Screener's trailing expand markers ("+", "-") from a row label."""
    return label.rstrip(" +- ").strip()


# ---------------------------------------------------------------------------- #
# Legacy/compatibility helpers
# ---------------------------------------------------------------------------- #
def parse_company_page(html: str) -> dict[str, Any]:
    """Backwards-compatible summary dict (name/symbol/financials/ratios).

    Args:
        html: Raw HTML of the company page.

    Returns:
        Dict with 'name', 'symbol', and the parsed CompanyFinancials object
        under 'financials'.
    """
    financials = parse_company_financials(html)
    return {
        "name": financials.name,
        "symbol": financials.symbol,
        "financials": financials,
        "ratios": financials.ratios,
    }


def _extract_company_name(soup: BeautifulSoup) -> str:
    """Return company name from page <h1> or empty string if absent."""
    tag = soup.find("h1")
    return tag.get_text(strip=True) if tag else ""


def _extract_symbol(soup: BeautifulSoup) -> str:
    """Return BSE/NSE ticker symbol from page metadata or empty string."""
    tag = soup.find("div", class_="company-ratios")
    if tag:
        symbol_tag = tag.find("span", id="nse-ticker") or tag.find("span", id="bse-ticker")
        if symbol_tag:
            return symbol_tag.get_text(strip=True)
    return ""
