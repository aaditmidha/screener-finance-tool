"""Earnings quality analysis: cash conversion and accrual checks.

Profit is an opinion; cash is a fact. This module compares reported profit
(PAT) against operating cash flow (CFO) over several years and flags the
classic warning signs:

* **Low CFO/PAT** — profits chronically not converting to cash.
* **High accruals** — a large gap between profit and cash relative to assets.
* **PAT/CFO divergence** — reported profit growing much faster than cash flow.

All flag thresholds come from ``thresholds.earnings_quality`` in config.yaml.
"""

import logging
import math
from dataclasses import dataclass, field

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_thresholds = CONFIG["thresholds"]["earnings_quality"]

# Flag identifiers surfaced in EarningsQualityResult.flags
FLAG_LOW_CFO_PAT = "low_cfo_pat"
FLAG_HIGH_ACCRUALS = "high_accruals"
FLAG_PAT_CFO_DIVERGENCE = "pat_cfo_divergence"


@dataclass
class YearFinancials:
    """One fiscal year of inputs for earnings quality analysis.

    All monetary values must be in the same unit (e.g. ₹ crore).
    """

    year: int            # fiscal year label, e.g. 2024
    pat: float           # profit after tax
    cfo: float           # cash flow from operations
    total_assets: float


@dataclass
class EarningsQualityResult:
    """Earnings quality summary across the analysed years."""

    avg_cfo_pat: float            # mean CFO/PAT over years with positive PAT
    latest_accrual_ratio: float   # (PAT − CFO) / total assets, latest year
    pat_cagr: float | None        # None if CAGR is undefined (non-positive endpoint)
    cfo_cagr: float | None
    flags: list[str] = field(default_factory=list)
    verdict: str = "healthy"      # "healthy" | "caution" | "red_flag"


def cfo_pat_ratio(cfo: float, pat: float) -> float:
    """Return the CFO/PAT cash conversion ratio for one year.

    Args:
        cfo: Cash flow from operations.
        pat: Profit after tax.

    Returns:
        CFO divided by PAT. When PAT is zero the ratio is undefined: returns
        ``inf`` if CFO is positive (cash with no profit) and 0.0 otherwise.
    """
    if pat == 0:
        return math.inf if cfo > 0 else 0.0
    return cfo / pat


def accrual_ratio(pat: float, cfo: float, total_assets: float) -> float:
    """Return the accrual ratio (PAT − CFO) / total assets for one year.

    A persistently high positive value means earnings are built on accruals
    rather than cash, a classic earnings-management signature.

    Args:
        pat: Profit after tax.
        cfo: Cash flow from operations.
        total_assets: Total assets at year end.

    Returns:
        The accrual ratio, or 0.0 (with a warning) if total_assets is zero.
    """
    if total_assets == 0:
        logger.warning("Accrual ratio undefined (zero total assets); returning 0.0")
        return 0.0
    return (pat - cfo) / total_assets


def _cagr(first: float, last: float, periods: int) -> float | None:
    """Return compound annual growth rate, or None when undefined.

    Args:
        first: Value in the first year.
        last: Value in the last year.
        periods: Number of yearly intervals between them (≥ 1).

    Returns:
        CAGR as a decimal, or None if either endpoint is non-positive (CAGR
        has no meaningful value across a sign change or from zero).
    """
    if first <= 0 or last <= 0 or periods < 1:
        return None
    return (last / first) ** (1 / periods) - 1


def analyze(years: list[YearFinancials]) -> EarningsQualityResult:
    """Run the full earnings quality analysis over consecutive years.

    Args:
        years: At least two years of financials, ordered oldest → newest.

    Returns:
        EarningsQualityResult with averages, growth rates, raised flags, and
        an overall verdict (0 flags → healthy, 1 → caution, 2+ → red_flag).

    Raises:
        ValueError: If fewer than two years are supplied.
    """
    if len(years) < 2:
        raise ValueError(f"Earnings quality analysis needs ≥ 2 years, got {len(years)}")

    ratios = [cfo_pat_ratio(y.cfo, y.pat) for y in years if y.pat > 0]
    if ratios:
        avg_cfo_pat = sum(ratios) / len(ratios)
    else:
        logger.warning("No year with positive PAT; CFO/PAT average set to 0.0")
        avg_cfo_pat = 0.0

    latest = years[-1]
    latest_accrual = accrual_ratio(latest.pat, latest.cfo, latest.total_assets)

    intervals = len(years) - 1
    pat_cagr = _cagr(years[0].pat, latest.pat, intervals)
    cfo_cagr = _cagr(years[0].cfo, latest.cfo, intervals)

    flags: list[str] = []
    if avg_cfo_pat < _thresholds["cfo_pat_min"]:
        flags.append(FLAG_LOW_CFO_PAT)
        logger.info("Flag: avg CFO/PAT %.2f below %.2f", avg_cfo_pat, _thresholds["cfo_pat_min"])
    if latest_accrual > _thresholds["accrual_ratio_max"]:
        flags.append(FLAG_HIGH_ACCRUALS)
        logger.info(
            "Flag: accrual ratio %.3f above %.3f", latest_accrual, _thresholds["accrual_ratio_max"]
        )
    if (
        pat_cagr is not None
        and cfo_cagr is not None
        and pat_cagr - cfo_cagr > _thresholds["divergence_growth_gap"]
    ):
        flags.append(FLAG_PAT_CFO_DIVERGENCE)
        logger.info(
            "Flag: PAT CAGR %.1f%% diverges from CFO CAGR %.1f%%",
            pat_cagr * 100, cfo_cagr * 100,
        )

    if not flags:
        verdict = "healthy"
    elif len(flags) == 1:
        verdict = "caution"
    else:
        verdict = "red_flag"

    return EarningsQualityResult(
        avg_cfo_pat=avg_cfo_pat,
        latest_accrual_ratio=latest_accrual,
        pat_cagr=pat_cagr,
        cfo_cagr=cfo_cagr,
        flags=flags,
        verdict=verdict,
    )
