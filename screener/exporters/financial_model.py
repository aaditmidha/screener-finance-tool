"""Canonical analyst-style financial derivation shared by the Excel model and
the research note.

Screener exposes aggregated statements (P&L, balance sheet, cash flow, ratios,
quarters) plus one level of note expansion. This module turns those into the
**standard sell-side view** used in reference models (e.g. the POCL/Paramount
workbooks and the Nuvama meet note): a derived income statement, common-size
(% of revenue), YoY growth, a mapped balance sheet and cash flow, and a block
of profitability / returns / leverage / efficiency / per-share ratios.

Everything is computed from the parsed figures — rows whose inputs Screener
doesn't provide are simply omitted, so the output never shows fabricated zeros.
Both :mod:`screener.exporters.model_workbook` and
:mod:`screener.exporters.research_note` consume these rows so the workbook and
the note always agree.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from screener.models import operational
from screener.scraper.parser import CompanyFinancials, FinancialTable

logger = logging.getLogger(__name__)

# Row kinds → how a renderer should format the values.
#   num   plain number (INR cr)        pct   ratio rendered as a percentage
#   x     multiple, e.g. "1.8x"        days  whole days
#   eps   2-dp rupee figure            ratio 2-dp number
#   header section sub-heading (label only, no values)
_Number = float | None


@dataclass
class StatementRow:
    """One derived line: a label, per-period values and a formatting kind."""

    label: str
    values: list[_Number] = field(default_factory=list)
    kind: str = "num"
    bold: bool = False


@dataclass
class Section:
    """A titled block of rows (e.g. "Income statement")."""

    title: str
    rows: list[StatementRow] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Series helpers
# --------------------------------------------------------------------------- #
def _series(table: FinancialTable | None, *needles: str) -> list[_Number] | None:
    """First row across *needles*, or None."""
    if table is None:
        return None
    for needle in needles:
        row = table.row(needle)
        if row:
            return row
    return None


def _combine(op: Callable[[float, float], float],
             a: list[_Number] | None, b: list[_Number] | None) -> list[_Number] | None:
    """Element-wise combine, None where either side is None."""
    if a is None or b is None:
        return None
    return [op(x, y) if x is not None and y is not None else None for x, y in zip(a, b)]


def _ratio(num: list[_Number] | None, den: list[_Number] | None) -> list[_Number] | None:
    """Element-wise num/den, None on a zero/None denominator."""
    if num is None or den is None:
        return None
    return [(n / d) if n is not None and d not in (None, 0) else None
            for n, d in zip(num, den)]


def _yoy(series: list[_Number] | None) -> list[_Number] | None:
    """Year-over-year growth of a series (first period None)."""
    if series is None:
        return None
    out: list[_Number] = [None]
    for prev, curr in zip(series, series[1:]):
        out.append((curr / prev - 1) if prev not in (None, 0) and curr is not None else None)
    return out


def _any(values: list[_Number] | None) -> bool:
    return bool(values) and any(v is not None for v in values)


def periods(fin: CompanyFinancials) -> list[str]:
    """Return the model's period labels (P&L preferred, else balance sheet)."""
    if fin.profit_loss:
        return fin.profit_loss.periods
    if fin.balance_sheet:
        return fin.balance_sheet.periods
    return []


# --------------------------------------------------------------------------- #
# Derived figures (used across statements/ratios)
# --------------------------------------------------------------------------- #
def _core(fin: CompanyFinancials) -> dict[str, list[_Number] | None]:
    """Compute the core derived series once for reuse across sections."""
    pl, bs = fin.profit_loss, fin.balance_sheet
    revenue = _series(pl, "sales", "revenue")
    expenses = _series(pl, "expenses")
    ebitda = _series(pl, "operating profit")
    dep = _series(pl, "depreciation")
    interest = _series(pl, "interest")
    other_income = _series(pl, "other income")
    pbt = _series(pl, "profit before tax")
    tax_pct = _series(pl, "tax %", "tax")
    pat = _series(pl, "net profit", "profit after tax")
    eps = _series(pl, "eps")
    ebit = _combine(lambda o, d: o - d, ebitda, dep)
    tax = _combine(lambda p, t: p * t / 100.0, pbt, tax_pct)

    equity_cap = _series(bs, "equity capital", "share capital")
    reserves = _series(bs, "reserves")
    networth = _combine(lambda a, b: a + b, equity_cap, reserves)
    borrowings = _series(bs, "borrowings", "debt")
    total_assets = _series(bs, "total assets")
    capital_employed = _combine(lambda e, d: e + d, networth, borrowings)
    # Implied share count from PAT / EPS (Screener gives no share count directly).
    shares = _ratio(pat, eps)
    return dict(revenue=revenue, expenses=expenses, ebitda=ebitda, dep=dep,
                interest=interest, other_income=other_income, pbt=pbt, tax=tax,
                tax_pct=tax_pct, pat=pat, eps=eps, ebit=ebit, equity_cap=equity_cap,
                reserves=reserves, networth=networth, borrowings=borrowings,
                total_assets=total_assets, capital_employed=capital_employed,
                shares=shares)


# --------------------------------------------------------------------------- #
# Statement builders
# --------------------------------------------------------------------------- #
def income_statement(fin: CompanyFinancials) -> list[StatementRow]:
    """Derived income statement: revenue → PAT → EPS, reference-model order."""
    c = _core(fin)
    spec: list[tuple[str, list[_Number] | None, str, bool]] = [
        ("Revenue from operations", c["revenue"], "num", True),
        ("Total expenses", c["expenses"], "num", False),
        ("EBITDA", c["ebitda"], "num", True),
        ("Depreciation & amortisation", c["dep"], "num", False),
        ("EBIT", c["ebit"], "num", True),
        ("Other income", c["other_income"], "num", False),
        ("Interest / finance costs", c["interest"], "num", False),
        ("Profit before tax", c["pbt"], "num", True),
        ("Tax", c["tax"], "num", False),
        ("Profit after tax", c["pat"], "num", True),
        ("EPS (INR)", c["eps"], "eps", False),
    ]
    return [StatementRow(lbl, list(v), k, b) for lbl, v, k, b in spec if _any(v)]


def common_size(fin: CompanyFinancials) -> list[StatementRow]:
    """Income-statement lines as a % of revenue (operating leverage view)."""
    c = _core(fin)
    rev = c["revenue"]
    spec = [
        ("Total expenses / revenue", _ratio(c["expenses"], rev)),
        ("EBITDA margin", _ratio(c["ebitda"], rev)),
        ("Depreciation / revenue", _ratio(c["dep"], rev)),
        ("EBIT margin", _ratio(c["ebit"], rev)),
        ("Interest / revenue", _ratio(c["interest"], rev)),
        ("PBT margin", _ratio(c["pbt"], rev)),
        ("PAT margin", _ratio(c["pat"], rev)),
    ]
    return [StatementRow(lbl, list(v), "pct") for lbl, v in spec if _any(v)]


def growth(fin: CompanyFinancials) -> list[StatementRow]:
    """Year-over-year growth of the headline lines."""
    c = _core(fin)
    spec = [
        ("Revenue growth", _yoy(c["revenue"])),
        ("EBITDA growth", _yoy(c["ebitda"])),
        ("EBIT growth", _yoy(c["ebit"])),
        ("PBT growth", _yoy(c["pbt"])),
        ("PAT growth", _yoy(c["pat"])),
        ("EPS growth", _yoy(c["eps"])),
    ]
    return [StatementRow(lbl, list(v), "pct") for lbl, v in spec if _any(v)]


def balance_sheet(fin: CompanyFinancials) -> list[StatementRow]:
    """Mapped balance sheet: equity & liabilities, then assets."""
    c = _core(fin)
    bs = fin.balance_sheet
    spec: list[tuple[str, list[_Number] | None, str, bool]] = [
        ("Equity share capital", c["equity_cap"], "num", False),
        ("Reserves & surplus", c["reserves"], "num", False),
        ("Shareholders' funds", c["networth"], "num", True),
        ("Borrowings", c["borrowings"], "num", False),
        ("Other liabilities", _series(bs, "other liabilities"), "num", False),
        ("Total liabilities", _series(bs, "total liabilities"), "num", True),
        ("Fixed assets (net block)", _series(bs, "fixed assets"), "num", False),
        ("Capital work in progress", _series(bs, "cwip", "capital work"), "num", False),
        ("Investments", _series(bs, "investments"), "num", False),
        ("Other assets", _series(bs, "other assets"), "num", False),
        ("Total assets", c["total_assets"], "num", True),
    ]
    return [StatementRow(lbl, list(v), k, b) for lbl, v, k, b in spec if _any(v)]


def cash_flow(fin: CompanyFinancials) -> list[StatementRow]:
    """Mapped cash-flow statement: operating, investing, financing, net."""
    cf = fin.cash_flow
    spec: list[tuple[str, list[_Number] | None, bool]] = [
        ("Cash from operating activity", _series(cf, "cash from operating", "operating activ"), True),
        ("Cash from investing activity", _series(cf, "cash from investing", "investing activ"), False),
        ("Cash from financing activity", _series(cf, "cash from financing", "financing activ"), False),
        ("Net cash flow", _series(cf, "net cash flow"), True),
    ]
    return [StatementRow(lbl, list(v), "num", b) for lbl, v, b in spec if _any(v)]


def ratios(fin: CompanyFinancials) -> list[StatementRow]:
    """Profitability, returns, leverage, efficiency and per-share ratios."""
    c = _core(fin)
    rev = c["revenue"]
    # Efficiency metrics reuse the operational model (days / turnover / CCC).
    op_by_label = {m.label: (m.values, m.fmt) for m in operational.compute(fin).metrics}

    rows: list[StatementRow] = []

    def add(label: str, values: list[_Number] | None, kind: str, bold: bool = False) -> None:
        if _any(values):
            rows.append(StatementRow(label, list(values), kind, bold))

    def add_op(label: str, kind_override: str | None = None) -> None:
        if label in op_by_label:
            values, fmt = op_by_label[label]
            add(label, values, kind_override or fmt)

    rows.append(StatementRow("Profitability", [], "header", True))
    add("EBITDA margin", _ratio(c["ebitda"], rev), "pct")
    add("EBIT margin", _ratio(c["ebit"], rev), "pct")
    add("PAT margin", _ratio(c["pat"], rev), "pct")

    rows.append(StatementRow("Returns", [], "header", True))
    # Prefer Screener's reported ROCE if present, else EBIT / capital employed.
    screener_roce = _series(fin.ratios, "roce")
    add("ROCE", [v / 100.0 if v is not None else None for v in screener_roce] if screener_roce
        else _ratio(c["ebit"], c["capital_employed"]), "pct")
    add("ROE", _ratio(c["pat"], c["networth"]), "pct")
    add("ROA", _ratio(c["pat"], c["total_assets"]), "pct")

    rows.append(StatementRow("Leverage & coverage", [], "header", True))
    add("Debt / equity", _ratio(c["borrowings"], c["networth"]), "ratio")
    add("Interest coverage", _ratio(c["ebit"], c["interest"]), "x")

    rows.append(StatementRow("Efficiency", [], "header", True))
    add("Asset turnover", _ratio(rev, c["total_assets"]), "x")
    add_op("Receivable days")
    add_op("Inventory days")
    add_op("Payable days")
    add_op("Cash conversion cycle (days)")
    add_op("CFO / PAT")

    rows.append(StatementRow("Per share & book value", [], "header", True))
    add("EPS (INR)", c["eps"], "eps")
    add("Book value per share (INR)", _ratio(c["networth"], c["shares"]), "eps")

    # Drop a header with no rows beneath it.
    return _prune_empty_headers(rows)


def _prune_empty_headers(rows: list[StatementRow]) -> list[StatementRow]:
    """Remove header rows that are immediately followed by another header/end."""
    out: list[StatementRow] = []
    for i, row in enumerate(rows):
        if row.kind == "header":
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if nxt is None or nxt.kind == "header":
                continue
        out.append(row)
    return out


def summary_sections(fin: CompanyFinancials) -> list[Section]:
    """All derived sections, in reference-model order, for the Output sheet/note.

    Args:
        fin: Parsed company financials.

    Returns:
        Ordered :class:`Section` list; sections with no rows are omitted.
    """
    builders: list[tuple[str, Callable[[CompanyFinancials], list[StatementRow]]]] = [
        ("Income statement", income_statement),
        ("Common-size (% of revenue)", common_size),
        ("Growth (% YoY)", growth),
        ("Balance sheet", balance_sheet),
        ("Cash flow", cash_flow),
        ("Ratios & returns", ratios),
    ]
    sections = [Section(title, builder(fin)) for title, builder in builders]
    return [s for s in sections if s.rows]
