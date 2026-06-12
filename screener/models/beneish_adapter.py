"""Maps parsed Screener statements to Beneish M-Score inputs.

Screener's public company page is aggregated, so several Beneish inputs are
approximated (the full mapping and rationale live in DECISIONS.md §7.1):

==============  ====================================  ==============
Beneish input   Screener source                       Status
==============  ====================================  ==============
revenue         Sales                                 exact
cogs            Raw Material Cost, else Expenses      approximated*
receivables     Trade Receivables / Debtors           often missing
current_assets  Current Assets, else Other Assets     approximated
ppe             Fixed Assets (+ CWIP)                 exact
securities      Investments                           exact
total_assets    Total Assets                          exact
depreciation    Depreciation (P&L)                    exact
sga             Other Expenses                        often missing
curr. liab.     Current Liab., else Other Liabilities approximated
long-term debt  Borrowings (mixes ST + LT)            approximated
net_income      Net Profit                            exact
cfo             Cash from Operating Activity          exact
==============  ====================================  ==============

\\* When only the total "Expenses" line exists, gross margin collapses to
operating margin, so GMI becomes an OPM index — directionally informative.

Missing fields degrade to values that neutralise only their own index (the
core model maps zero-denominator indices to 1.0), and every approximation or
gap is reported in :class:`BeneishSourcing.approximated` /
:class:`BeneishSourcing.missing` so the UI can disclose data quality honestly.
Beneish is a probabilistic flag, not a precise measurement — documented
approximations are acceptable; silent ones are not.
"""

import logging
from dataclasses import dataclass, field

from screener.models.beneish import BeneishResult, BeneishYear, analyze
from screener.scraper.parser import CompanyFinancials, FinancialTable

logger = logging.getLogger(__name__)


@dataclass
class BeneishSourcing:
    """A computed M-Score plus full disclosure of where inputs came from."""

    result: BeneishResult
    periods: tuple[str, str]              # (prior, current) period labels
    approximated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _col(table: FinancialTable | None, label_contains: str, index: int) -> float | None:
    """Return one row's value at column *index* (negative ok), or None."""
    if table is None:
        return None
    values = table.row(label_contains)
    if not values or abs(index) > len(values):
        return None
    return values[index]


def _first(table: FinancialTable | None, index: int, *needles: str) -> float | None:
    """Return the first matching row's value at *index* across *needles*."""
    for needle in needles:
        value = _col(table, needle, index)
        if value is not None:
            return value
    return None


def from_financials(fin: CompanyFinancials) -> BeneishSourcing | None:
    """Compute the Beneish M-Score from parsed Screener statements.

    Uses the two most recent annual periods. Exact fields are used where the
    page provides them; otherwise documented approximations apply and are
    reported in the result. Fields absent outright are zeroed, which the core
    model turns into a neutral (1.0) contribution for that index only.

    Args:
        fin: Parsed company financials (annual P&L required).

    Returns:
        A BeneishSourcing with the score and data-quality notes, or None when
        fewer than two annual periods with revenue and total assets exist.
    """
    pl, bs, cf = fin.profit_loss, fin.balance_sheet, fin.cash_flow
    if pl is None or bs is None or len(pl.periods) < 2 or len(bs.periods) < 2:
        logger.info("Beneish unavailable for %s: need 2+ annual periods", fin.symbol)
        return None

    approximated: list[str] = []
    missing: list[str] = []

    def build_year(idx: int) -> BeneishYear | None:
        """Assemble one BeneishYear from column *idx* (-1 current, -2 prior)."""
        revenue = _first(pl, idx, "sales", "revenue")
        total_assets = _first(bs, idx, "total assets")
        if revenue is None or total_assets is None or total_assets == 0:
            return None

        # COGS: prefer the granular materials line; else the aggregate
        # Expenses line (gross margin then equals operating margin).
        cogs = _first(pl, idx, "raw material", "cost of materials")
        if cogs is None:
            cogs = _first(pl, idx, "expenses")
            if cogs is not None and idx == -1:
                approximated.append("COGS ≈ Total Expenses (GMI tracks operating margin)")
        if cogs is None:
            cogs = 0.0
            if idx == -1:
                missing.append("expenses (GMI neutral)")

        receivables = _first(bs, idx, "receivable", "debtor")
        if receivables is None:
            receivables = 0.0
            if idx == -1:
                missing.append("trade receivables (DSRI neutral)")

        current_assets = _first(bs, idx, "current assets")
        if current_assets is None:
            current_assets = _first(bs, idx, "other assets")
            if current_assets is not None and idx == -1:
                approximated.append("Current assets ≈ Other Assets")
        current_assets = current_assets or 0.0

        ppe = (_first(bs, idx, "fixed assets") or 0.0) + (_first(bs, idx, "cwip") or 0.0)
        securities = _first(bs, idx, "investments") or 0.0
        depreciation = _first(pl, idx, "depreciation") or 0.0

        sga = _first(pl, idx, "other expenses")
        if sga is None:
            sga = 0.0
            if idx == -1:
                missing.append("SGA / other expenses (SGAI neutral)")

        current_liabilities = _first(bs, idx, "current liabilities")
        if current_liabilities is None:
            current_liabilities = _first(bs, idx, "other liabilities")
            if current_liabilities is not None and idx == -1:
                approximated.append("Current liabilities ≈ Other Liabilities")
        current_liabilities = current_liabilities or 0.0

        long_term_debt = _first(bs, idx, "borrowings", "debt") or 0.0
        if long_term_debt and idx == -1:
            approximated.append("LT debt ≈ total Borrowings (incl. short-term)")

        net_income = _first(pl, idx, "net profit", "profit after tax") or 0.0

        cfo = _first(cf, idx, "operating activity", "cash from operating")
        if cfo is None:
            cfo = net_income  # zeroes TATA → neutral accruals signal
            if idx == -1:
                missing.append("CFO (TATA neutral)")

        return BeneishYear(
            revenue=revenue, cogs=cogs, receivables=receivables,
            current_assets=current_assets, ppe=ppe, total_assets=total_assets,
            depreciation=depreciation, sga=sga,
            current_liabilities=current_liabilities, long_term_debt=long_term_debt,
            net_income=net_income, cfo=cfo, securities=securities,
        )

    current = build_year(-1)
    prior = build_year(-2)
    if current is None or prior is None:
        logger.info("Beneish unavailable for %s: revenue/assets missing", fin.symbol)
        return None

    result = analyze(current, prior)
    sourcing = BeneishSourcing(
        result=result,
        periods=(pl.periods[-2], pl.periods[-1]),
        approximated=sorted(set(approximated)),
        missing=sorted(set(missing)),
    )
    logger.info(
        "Beneish for %s [%s→%s]: M=%.2f (%s); approx=%d missing=%d",
        fin.symbol, sourcing.periods[0], sourcing.periods[1],
        result.m_score, result.verdict, len(sourcing.approximated), len(sourcing.missing),
    )
    return sourcing
