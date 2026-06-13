"""Operational efficiency metrics derived from the financial statements.

Screener doesn't publish a generic "operational data" section, but the
reference models' Operational Data sheets are operating-efficiency ratios —
all derivable from the P&L, balance sheet, cash flow and the expand-API notes:
margins, turnover ratios, working-capital days (DSO/DIO/DPO/CCC) and cash
conversion. This module computes whatever the available data supports and
omits metrics whose inputs are missing, so nothing is fabricated.

COGS is approximated as Sales − Operating Profit where a granular cost line
isn't published (consistent with the working-capital model).
"""

import logging
from dataclasses import dataclass, field

from screener.config import CONFIG
from screener.scraper.parser import CompanyFinancials, FinancialTable

logger = logging.getLogger(__name__)

_DAYS = CONFIG["working_capital"]["days_per_year"]


@dataclass
class OperationalMetric:
    """One operational metric across the reporting periods.

    Attributes:
        label: Display label.
        values: One value per period (None where uncomputable).
        fmt: Render hint — "pct" (ratio shown as %), "x" (turnover), or
            "days".
    """

    label: str
    values: list[float | None]
    fmt: str


@dataclass
class OperationalData:
    """Operational metrics aligned to a company's reporting periods."""

    periods: list[str]
    metrics: list[OperationalMetric] = field(default_factory=list)


def _col_value(table: FinancialTable | None, period: str, *needles: str) -> float | None:
    """Return a row's value in *period*, matching the first label substring.

    Args:
        table: Statement to read (or None).
        period: Period label to align on (e.g. "Mar 2024").
        *needles: Case-insensitive row-label substrings, tried in order.

    Returns:
        The cell value, or None if the table/period/row is absent.
    """
    if table is None or period not in table.periods:
        return None
    idx = table.periods.index(period)
    for needle in needles:
        row = table.row(needle)
        if row and idx < len(row) and row[idx] is not None:
            return row[idx]
    return None


def _ratio(num: float | None, den: float | None) -> float | None:
    """Return num/den, or None if either is missing or the denominator is 0."""
    if num is None or den in (None, 0):
        return None
    return num / den


def _days(stock: float | None, flow: float | None) -> float | None:
    """Return stock/flow expressed in days over the annual period, or None."""
    ratio = _ratio(stock, flow)
    return ratio * _DAYS if ratio is not None else None


def compute(fin: CompanyFinancials) -> OperationalData:
    """Compute operational metrics from parsed (ideally notes-enriched) data.

    Args:
        fin: Parsed company financials.

    Returns:
        OperationalData over the P&L periods. Metrics with no computable value
        in any period are omitted entirely.
    """
    pl = fin.profit_loss
    if pl is None or not pl.periods:
        return OperationalData(periods=[], metrics=[])

    periods = pl.periods
    bs, cf, notes_bs = fin.balance_sheet, fin.cash_flow, fin.notes_bs

    # Per-period raw inputs.
    sales, op_profit, dep, pat, cogs = [], [], [], [], []
    total_assets, fixed_assets = [], []
    receivables, inventory, payables, cfo = [], [], [], []
    for p in periods:
        s = _col_value(pl, p, "sales", "revenue")
        op = _col_value(pl, p, "operating profit")
        sales.append(s)
        op_profit.append(op)
        dep.append(_col_value(pl, p, "depreciation"))
        pat.append(_col_value(pl, p, "net profit", "profit after tax"))
        cogs.append(s - op if s is not None and op is not None else None)
        total_assets.append(_col_value(bs, p, "total assets"))
        fixed_assets.append(_col_value(bs, p, "fixed assets"))
        receivables.append(_col_value(bs, p, "receivable", "debtor")
                           or _col_value(notes_bs, p, "receivable", "debtor"))
        inventory.append(_col_value(bs, p, "inventor")
                        or _col_value(notes_bs, p, "inventor"))
        payables.append(_col_value(bs, p, "payable")
                       or _col_value(notes_bs, p, "payable"))
        cfo.append(_col_value(cf, p, "operating activity", "cash from operating"))

    def yoy(series: list[float | None]) -> list[float | None]:
        out: list[float | None] = [None]
        for prev, curr in zip(series, series[1:]):
            out.append((curr / prev - 1) if prev not in (None, 0) and curr is not None else None)
        return out

    def per(fn) -> list[float | None]:
        return [fn(i) for i in range(len(periods))]

    candidates = [
        OperationalMetric("Revenue growth %", yoy(sales), "pct"),
        OperationalMetric("EBITDA margin %", per(lambda i: _ratio(op_profit[i], sales[i])), "pct"),
        OperationalMetric(
            "EBIT margin %",
            per(lambda i: _ratio(
                (op_profit[i] - dep[i]) if op_profit[i] is not None and dep[i] is not None else None,
                sales[i])),
            "pct"),
        OperationalMetric("PAT margin %", per(lambda i: _ratio(pat[i], sales[i])), "pct"),
        OperationalMetric("Asset turnover", per(lambda i: _ratio(sales[i], total_assets[i])), "x"),
        OperationalMetric("Fixed-asset turnover", per(lambda i: _ratio(sales[i], fixed_assets[i])), "x"),
        OperationalMetric("Inventory turnover", per(lambda i: _ratio(cogs[i], inventory[i])), "x"),
        OperationalMetric("Receivable days", per(lambda i: _days(receivables[i], sales[i])), "days"),
        OperationalMetric("Inventory days", per(lambda i: _days(inventory[i], cogs[i])), "days"),
        OperationalMetric("Payable days", per(lambda i: _days(payables[i], cogs[i])), "days"),
        OperationalMetric("CFO / EBITDA", per(lambda i: _ratio(cfo[i], op_profit[i])), "pct"),
        OperationalMetric("CFO / PAT", per(lambda i: _ratio(cfo[i], pat[i])), "pct"),
    ]

    # Cash conversion cycle = receivable + inventory − payable days.
    by_label = {m.label: m.values for m in candidates}
    ccc = []
    for i in range(len(periods)):
        rd, idd, pd_ = (by_label["Receivable days"][i], by_label["Inventory days"][i],
                        by_label["Payable days"][i])
        ccc.append(rd + idd - pd_ if None not in (rd, idd, pd_) else None)
    candidates.append(OperationalMetric("Cash conversion cycle (days)", ccc, "days"))

    metrics = [m for m in candidates if any(v is not None for v in m.values)]
    logger.debug("Computed %d operational metric(s) for %s", len(metrics), fin.symbol)
    return OperationalData(periods=periods, metrics=metrics)
