"""Driver-based P&L forecast.

Projects the income statement forward (default three years) from a small set of
transparent, editable drivers — revenue growth, EBITDA margin, depreciation as a
% of revenue, finance costs, other income and the tax rate — in the spirit of
the reference models' ``FYxxE`` columns. Defaults are inferred from the
company's own history (recent revenue CAGR, latest margins, etc.) so a forecast
appears out of the box, but every driver is overridable from the UI.

Only the P&L is projected; a fully balancing forecast balance sheet and cash
flow (working-capital-day and capex driven) is deliberately out of scope here
and left to a follow-up. Nothing is fabricated: if there is no usable history
the projection is empty.
"""

import logging
import re
from dataclasses import dataclass

from screener.exporters import financial_model as fm
from screener.scraper.parser import CompanyFinancials

logger = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"(19|20)\d{2}")


@dataclass
class ForecastAssumptions:
    """The editable drivers behind the projection (rates as decimals)."""

    revenue_growth: float    # annual revenue growth, applied each forecast year
    ebitda_margin: float     # EBITDA / revenue, held flat
    depreciation_pct: float  # depreciation / revenue, held flat
    other_income: float      # absolute, held flat at the last actual
    interest: float          # absolute finance cost, held flat
    tax_rate: float          # tax / PBT
    shares: float            # share count (cr), held flat
    years: int = 3


@dataclass
class ForecastResult:
    """A projection: combined (historical + forecast) rows and the drivers used."""

    periods: list[str]            # historical + forecast period labels
    forecast_periods: list[str]   # just the forecast labels (e.g. "Mar 2026E")
    rows: list[fm.StatementRow]   # values span all periods
    assumptions: ForecastAssumptions
    n_history: int                # number of historical columns


def _last(values: list[float | None] | None) -> float | None:
    """Last non-None value of a series, or None."""
    if not values:
        return None
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _cagr(values: list[float | None] | None, years: int) -> float | None:
    """Revenue CAGR over the last *years* periods, or None if undefined."""
    if not values:
        return None
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    window = clean[-(years + 1):]
    first, last, n = window[0], window[-1], len(window) - 1
    if first <= 0 or last <= 0 or n < 1:
        return None
    return (last / first) ** (1 / n) - 1


def _series(fin: CompanyFinancials) -> dict[str, list[float | None]]:
    """Pluck the historical income-statement series by label."""
    return {r.label: r.values for r in fm.income_statement(fin)}


def default_assumptions(fin: CompanyFinancials, years: int = 3) -> ForecastAssumptions | None:
    """Infer sensible default drivers from the company's history.

    Args:
        fin: Parsed company financials (needs a P&L).
        years: Forecast horizon.

    Returns:
        Inferred :class:`ForecastAssumptions`, or None if there is no revenue.
    """
    s = _series(fin)
    revenue = s.get("Revenue from operations")
    last_rev = _last(revenue)
    if not last_rev:
        return None

    growth = _cagr(revenue, 3)
    if growth is None:
        growth = 0.10
    growth = max(-0.25, min(growth, 0.40))  # keep defaults sane

    ebitda = _last(s.get("EBITDA"))
    margin = (ebitda / last_rev) if ebitda is not None and last_rev else 0.15
    dep = _last(s.get("Depreciation & amortisation")) or 0.0
    dep_pct = (dep / last_rev) if last_rev else 0.03
    pat, pbt = _last(s.get("Profit after tax")), _last(s.get("Profit before tax"))
    tax_rate = (1 - pat / pbt) if pat is not None and pbt not in (None, 0) else 0.25
    tax_rate = max(0.0, min(tax_rate, 0.45))
    eps = _last(s.get("EPS (INR)"))
    shares = (pat / eps) if pat is not None and eps not in (None, 0) else 0.0

    return ForecastAssumptions(
        revenue_growth=round(growth, 4), ebitda_margin=round(margin, 4),
        depreciation_pct=round(dep_pct, 4), other_income=_last(s.get("Other income")) or 0.0,
        interest=_last(s.get("Interest / finance costs")) or 0.0, tax_rate=round(tax_rate, 4),
        shares=round(shares, 4), years=years,
    )


def _forecast_labels(periods: list[str], years: int) -> list[str]:
    """Derive forecast column labels from the last historical label."""
    if not periods:
        return [f"FY+{i}E" for i in range(1, years + 1)]
    last = periods[-1]
    match = _YEAR_RE.search(last)
    if not match:
        return [f"{last}+{i}E" for i in range(1, years + 1)]
    base = int(match.group())
    return [_YEAR_RE.sub(str(base + i), last) + "E" for i in range(1, years + 1)]


def project(fin: CompanyFinancials,
            assumptions: ForecastAssumptions | None = None) -> ForecastResult | None:
    """Project the income statement forward from the given (or default) drivers.

    Args:
        fin: Parsed company financials (needs a P&L with revenue).
        assumptions: Drivers to use; defaults inferred from history when omitted.

    Returns:
        A :class:`ForecastResult` whose rows span historical + forecast periods,
        or None if there is no usable history.
    """
    if fin.profit_loss is None:
        return None
    assumptions = assumptions or default_assumptions(fin)
    if assumptions is None:
        return None

    s = _series(fin)
    hist_periods = fin.profit_loss.periods
    n_hist = len(hist_periods)
    fcast_periods = _forecast_labels(hist_periods, assumptions.years)

    revenue = list(s.get("Revenue from operations") or [None] * n_hist)
    last_rev = _last(revenue)
    if not last_rev:
        return None

    a = assumptions
    rev_f, ebitda_f, dep_f, ebit_f, oi_f, int_f, pbt_f, tax_f, pat_f, eps_f = (
        [], [], [], [], [], [], [], [], [], [])
    running = last_rev
    for _ in range(a.years):
        running = running * (1 + a.revenue_growth)
        ebitda = running * a.ebitda_margin
        dep = running * a.depreciation_pct
        ebit = ebitda - dep
        pbt = ebit + a.other_income - a.interest
        tax = pbt * a.tax_rate
        pat = pbt - tax
        rev_f.append(running); ebitda_f.append(ebitda); dep_f.append(dep)
        ebit_f.append(ebit); oi_f.append(a.other_income); int_f.append(a.interest)
        pbt_f.append(pbt); tax_f.append(tax); pat_f.append(pat)
        eps_f.append(pat / a.shares if a.shares else None)

    def _combined(hist_key: str, forecast: list[float | None]) -> list[float | None]:
        return list(s.get(hist_key) or [None] * n_hist) + forecast

    def _growth_row(combined_values: list[float | None]) -> list[float | None]:
        out: list[float | None] = [None]
        for prev, curr in zip(combined_values, combined_values[1:]):
            out.append((curr / prev - 1) if prev not in (None, 0) and curr is not None else None)
        return out

    rev_combined = _combined("Revenue from operations", rev_f)
    ebitda_combined = _combined("EBITDA", ebitda_f)
    margin = [(e / r if e is not None and r else None)
              for e, r in zip(ebitda_combined, rev_combined)]
    rows = [
        fm.StatementRow("Revenue from operations", rev_combined, "num", True),
        fm.StatementRow("Revenue growth", _growth_row(rev_combined), "pct"),
        fm.StatementRow("EBITDA", ebitda_combined, "num", True),
        fm.StatementRow("EBITDA margin", margin, "pct"),
        fm.StatementRow("Depreciation & amortisation",
                        _combined("Depreciation & amortisation", dep_f), "num"),
        fm.StatementRow("EBIT", _combined("EBIT", ebit_f), "num", True),
        fm.StatementRow("Other income", _combined("Other income", oi_f), "num"),
        fm.StatementRow("Interest / finance costs",
                        _combined("Interest / finance costs", int_f), "num"),
        fm.StatementRow("Profit before tax", _combined("Profit before tax", pbt_f), "num", True),
        fm.StatementRow("Tax", _combined("Tax", tax_f), "num"),
        fm.StatementRow("Profit after tax", _combined("Profit after tax", pat_f), "num", True),
        fm.StatementRow("EPS (INR)", _combined("EPS (INR)", eps_f), "eps"),
    ]
    logger.info("Forecast for %s: %d hist + %d forecast periods (growth %.1f%%, margin %.1f%%)",
                fin.symbol, n_hist, a.years, a.revenue_growth * 100, a.ebitda_margin * 100)
    return ForecastResult(periods=list(hist_periods) + fcast_periods,
                          forecast_periods=fcast_periods, rows=rows,
                          assumptions=a, n_history=n_hist)
