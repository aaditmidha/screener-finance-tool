"""Capital allocation analysis: ROIC vs WACC trend and a 0–10 score.

A company creates value only when its return on invested capital (ROIC)
exceeds its cost of capital (WACC). This module builds the year-by-year
spread, then scores capital allocation on three configurable components:

* **avg_spread** — how wide the average ROIC−WACC spread is.
* **consistency** — the fraction of years with a positive spread.
* **trend** — whether the spread widened from first to last year.

Component weights, the "excellent" spread benchmark, and rating cutoffs all
come from ``thresholds.capital_allocation`` in config.yaml.
"""

import logging
from dataclasses import dataclass

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["thresholds"]["capital_allocation"]


@dataclass
class YearSpread:
    """ROIC, WACC, and their spread for one fiscal year."""

    year: int
    roic: float
    wacc: float

    @property
    def spread(self) -> float:
        """Return the value-creation spread (ROIC − WACC)."""
        return self.roic - self.wacc


@dataclass
class CapitalAllocationResult:
    """Aggregate capital allocation assessment across the analysed years."""

    avg_roic: float
    avg_wacc: float
    avg_spread: float
    score: float    # 0–10
    rating: str     # "excellent" | "good" | "mediocre" | "poor"


def nopat(ebit: float, tax_rate: float) -> float:
    """Return net operating profit after tax (EBIT × (1 − tax rate)).

    Args:
        ebit: Earnings before interest and taxes.
        tax_rate: Effective tax rate as a decimal (e.g. 0.25).

    Returns:
        NOPAT in the same unit as EBIT.
    """
    return ebit * (1 - tax_rate)


def roic(nopat_value: float, invested_capital: float) -> float:
    """Return Return on Invested Capital (NOPAT / invested capital).

    Args:
        nopat_value: Net operating profit after tax.
        invested_capital: Equity + debt − non-operating cash.

    Returns:
        ROIC as a decimal, or 0.0 (with a warning) if invested capital is
        zero or negative.
    """
    if invested_capital <= 0:
        logger.warning(
            "ROIC undefined (invested capital %.2f ≤ 0); returning 0.0", invested_capital
        )
        return 0.0
    return nopat_value / invested_capital


def wacc(
    equity_value: float,
    debt_value: float,
    cost_of_equity: float,
    cost_of_debt: float,
    tax_rate: float,
) -> float:
    """Return the weighted average cost of capital.

    Args:
        equity_value: Market (or book) value of equity.
        debt_value: Value of interest-bearing debt.
        cost_of_equity: Required return on equity as a decimal.
        cost_of_debt: Pre-tax cost of debt as a decimal.
        tax_rate: Effective tax rate as a decimal (debt interest is
            tax-deductible).

    Returns:
        WACC as a decimal, or 0.0 (with a warning) if total capital is zero.
    """
    total = equity_value + debt_value
    if total == 0:
        logger.warning("WACC undefined (zero total capital); returning 0.0")
        return 0.0
    weight_equity = equity_value / total
    weight_debt = debt_value / total
    return weight_equity * cost_of_equity + weight_debt * cost_of_debt * (1 - tax_rate)


def build_trend(years: list[tuple[int, float, float]]) -> list[YearSpread]:
    """Build YearSpread records from (year, roic, wacc) tuples.

    Args:
        years: Tuples of (fiscal year, ROIC, WACC), ordered oldest → newest.

    Returns:
        List of YearSpread in the same order.
    """
    return [YearSpread(year=y, roic=r, wacc=w) for y, r, w in years]


def _clamp01(value: float) -> float:
    """Clamp *value* into the closed interval [0, 1]."""
    return max(0.0, min(1.0, value))


def score_trend(trend: list[YearSpread]) -> CapitalAllocationResult:
    """Score capital allocation quality from a multi-year ROIC/WACC trend.

    Args:
        trend: At least one YearSpread, ordered oldest → newest.

    Returns:
        CapitalAllocationResult with averages, a 0–10 score, and a rating
        derived from the config cutoffs.

    Raises:
        ValueError: If *trend* is empty.
    """
    if not trend:
        raise ValueError("Capital allocation scoring needs at least one year")

    excellent = _cfg["excellent_spread"]
    weights = _cfg["weights"]

    n = len(trend)
    avg_roic = sum(y.roic for y in trend) / n
    avg_wacc = sum(y.wacc for y in trend) / n
    avg_spread = sum(y.spread for y in trend) / n

    # Component 1: how wide is the average spread, relative to the benchmark.
    spread_pts = _clamp01(avg_spread / excellent) * weights["avg_spread"]

    # Component 2: how consistently was the spread positive.
    positive_years = sum(1 for y in trend if y.spread > 0)
    consistency_pts = (positive_years / n) * weights["consistency"]

    # Component 3: did the spread widen over the period. A single year has no
    # trend, so it scores neutral half-marks.
    if n == 1:
        trend_pts = 0.5 * weights["trend"]
    else:
        improvement = trend[-1].spread - trend[0].spread
        trend_pts = _clamp01(improvement / excellent) * weights["trend"]

    score = spread_pts + consistency_pts + trend_pts

    ratings = _cfg["ratings"]
    if score >= ratings["excellent_min"]:
        rating = "excellent"
    elif score >= ratings["good_min"]:
        rating = "good"
    elif score >= ratings["mediocre_min"]:
        rating = "mediocre"
    else:
        rating = "poor"

    logger.debug(
        "Capital allocation: avg_spread=%.3f score=%.2f rating=%s",
        avg_spread, score, rating,
    )
    return CapitalAllocationResult(
        avg_roic=avg_roic,
        avg_wacc=avg_wacc,
        avg_spread=avg_spread,
        score=score,
        rating=rating,
    )
