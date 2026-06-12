"""Screener row-expand ("schedules") API: notes-level financial detail.

The public company page shows aggregated statements, but every row marked
"+" hides child rows behind ``/api/company/{id}/schedules/`` — trade
receivables, inventories, payables, cost splits, working-capital movements.
This module fetches those children and attaches them to a parsed
:class:`CompanyFinancials` as ``notes_pl`` / ``notes_bs`` / ``notes_cf``
tables, which power the Notes sheets in the Excel model and upgrade the
Beneish / working-capital inputs from "missing" to real values.

Each expansion is one rate-limited request; failures are logged and skipped so
a flaky schedule never breaks a company load.
"""

import json
import logging
import re
from typing import Any, Callable
from urllib.parse import quote

from screener.config import CONFIG
from screener.scraper import client
from screener.scraper.parser import CompanyFinancials, FinancialTable, clean_number

logger = logging.getLogger(__name__)

_cfg = CONFIG["scraper"]

_COMPANY_ID_RE = re.compile(r"/api/company/(\d+)/")

# Keys in schedule responses that are API metadata, not period columns.
_META_KEYS = {"isExpandable", "setAttributes"}

# Section anchor → CompanyFinancials notes attribute.
_NOTES_ATTR = {
    "profit-loss": "notes_pl",
    "balance-sheet": "notes_bs",
    "cash-flow": "notes_cf",
}


def extract_company_id(html: str) -> str | None:
    """Pull Screener's numeric company id out of a company page.

    Args:
        html: Raw company-page HTML.

    Returns:
        The id as a string, or None if the page embeds no API links.
    """
    match = _COMPANY_ID_RE.search(html or "")
    return match.group(1) if match else None


def fetch_schedule(
    company_id: str,
    parent: str,
    section: str,
    fetch_json: Callable[[str], str] | None = None,
) -> dict[str, dict[str, float | None]]:
    """Fetch one parent row's children from the schedules API.

    Args:
        company_id: Screener numeric company id.
        parent: Parent row label exactly as shown on the page (e.g.
            "Other Assets").
        section: Statement anchor ("profit-loss", "balance-sheet", "cash-flow").
        fetch_json: ``url -> body`` callable; defaults to the HTTP client.

    Returns:
        Mapping of child label → {period: numeric value}, metadata keys
        stripped and values cleaned. Empty on any failure.
    """
    fetch = fetch_json or client.fetch
    url = _cfg["schedules_url_template"].format(
        company_id=company_id, parent=quote(parent), section=section
    )
    try:
        payload = json.loads(fetch(url))
    except Exception as exc:  # one bad schedule must not sink the load
        logger.warning("Schedule %r/%s failed for company %s: %s", parent, section, company_id, exc)
        return {}
    if not isinstance(payload, dict):
        logger.warning("Schedule %r/%s returned non-dict payload", parent, section)
        return {}

    out: dict[str, dict[str, float | None]] = {}
    for label, row in payload.items():
        if not isinstance(row, dict):
            continue
        out[label] = {
            period: clean_number(str(value))
            for period, value in row.items()
            if period not in _META_KEYS
        }
    return out


def _build_notes_table(
    section: str,
    periods: list[str],
    children_by_parent: dict[str, dict[str, dict[str, float | None]]],
) -> FinancialTable | None:
    """Assemble fetched children into a notes FinancialTable.

    Row labels are ``"<parent> · <child>"`` so substring lookups (e.g.
    ``row("receivable")``) keep working while exporters can re-group rows by
    parent for display.

    Args:
        section: Statement anchor the notes belong to.
        periods: Period labels of the parent statement (column order).
        children_by_parent: parent label → child label → {period: value}.

    Returns:
        A FinancialTable, or None when no children were fetched.
    """
    rows: dict[str, list[float | None]] = {}
    for parent, children in children_by_parent.items():
        for child, values in children.items():
            rows[f"{parent} · {child}"] = [values.get(p) for p in periods]
    if not rows:
        return None
    return FinancialTable(section=f"{section}-notes", periods=list(periods), rows=rows)


def enrich(
    fin: CompanyFinancials,
    html: str,
    fetch_json: Callable[[str], str] | None = None,
    config: dict[str, Any] | None = None,
) -> CompanyFinancials:
    """Attach notes tables to *fin* by expanding configured parent rows.

    Args:
        fin: Parsed company financials (statements must already be populated).
        html: The raw page HTML (source of the company id).
        fetch_json: ``url -> body`` callable; defaults to the HTTP client.
        config: Override config; defaults to global CONFIG.

    Returns:
        The same *fin* instance with ``notes_pl`` / ``notes_bs`` / ``notes_cf``
        populated where data exists (unchanged on any failure).
    """
    company_id = extract_company_id(html)
    if company_id is None:
        logger.info("No company id in page for %s; skipping notes enrichment", fin.symbol)
        return fin

    parents_cfg = ((config or CONFIG)["scraper"])["schedule_parents"]
    statements = {
        "profit-loss": fin.profit_loss,
        "balance-sheet": fin.balance_sheet,
        "cash-flow": fin.cash_flow,
    }

    for section, parents in parents_cfg.items():
        statement = statements.get(section)
        if statement is None:
            continue
        children_by_parent: dict[str, dict[str, dict[str, float | None]]] = {}
        for parent in parents:
            children = fetch_schedule(company_id, parent, section, fetch_json=fetch_json)
            if children:
                children_by_parent[parent] = children
        table = _build_notes_table(section, statement.periods, children_by_parent)
        if table is not None:
            setattr(fin, _NOTES_ATTR[section], table)
            logger.info(
                "Enriched %s %s with %d note rows", fin.symbol, section, len(table.rows)
            )
    return fin
