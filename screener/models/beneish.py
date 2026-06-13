"""Beneish M-Score earnings manipulation detector.

Computes all eight Beneish indices (DSRI, GMI, AQI, SGI, DEPI, SGAI, LVGI,
TATA) from two consecutive years of raw financial statement data, combines
them with the published Beneish (1999) coefficients, and classifies the result
against the thresholds in config.yaml.

The model coefficients are academic constants of the formula itself and live
in code; the *decision thresholds* (manipulator cutoff, grey zone) come from
config so they can be tuned without touching this module.

Edge-case policy: any index whose denominator is zero is undefined, so it is
set to the neutral value 1.0 (meaning "no year-over-year change") and a
warning is logged. TATA defaults to 0.0 in the same situation.
"""

import logging
from dataclasses import dataclass

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_thresholds = CONFIG["thresholds"]["beneish_m_score"]


@dataclass
class BeneishYear:
    """One fiscal year of raw inputs needed for the Beneish model.

    All monetary values must be in the same unit (e.g. ₹ crore).
    """

    revenue: float
    cogs: float
    receivables: float
    current_assets: float
    ppe: float                 # net property, plant & equipment
    total_assets: float
    depreciation: float
    sga: float                 # selling, general & administrative expense
    current_liabilities: float
    long_term_debt: float
    net_income: float
    cfo: float                 # cash flow from operations
    securities: float = 0.0    # investments counted as "hard" assets in AQI


@dataclass
class BeneishIndices:
    """The eight component indices feeding the M-Score formula."""

    dsri: float   # Days Sales in Receivables Index
    gmi: float    # Gross Margin Index
    aqi: float    # Asset Quality Index
    sgi: float    # Sales Growth Index
    depi: float   # Depreciation Index
    sgai: float   # SGA Expense Index
    lvgi: float   # Leverage Index
    tata: float   # Total Accruals to Total Assets


@dataclass
class BeneishResult:
    """Beneish M-Score result with component indices and verdict."""

    m_score: float
    indices: BeneishIndices
    verdict: str  # "manipulator" | "grey_zone" | "non_manipulator"


def _safe_div(numerator: float, denominator: float) -> float | None:
    """Return numerator / denominator, or None when the denominator is zero.

    Args:
        numerator: Dividend.
        denominator: Divisor.

    Returns:
        The quotient, or None if it is undefined.
    """
    if denominator == 0:
        return None
    return numerator / denominator


def _index(current: float | None, prior: float | None, name: str) -> float:
    """Form a year-over-year index current/prior, neutral (1.0) if undefined.

    Args:
        current: Current-year ratio (None if its denominator was zero).
        prior: Prior-year ratio (None if its denominator was zero).
        name: Index name, used only for the log message.

    Returns:
        current / prior, or 1.0 when either input is missing or prior is 0.
    """
    if current is None or prior is None or prior == 0:
        logger.debug("Beneish %s undefined (zero denominator); using neutral 1.0", name)
        return 1.0
    return current / prior


def compute_indices(current: BeneishYear, prior: BeneishYear) -> BeneishIndices:
    """Compute the eight Beneish indices from two consecutive years.

    Args:
        current: Raw financials for the year being tested (year t).
        prior: Raw financials for the preceding year (year t−1).

    Returns:
        BeneishIndices with every component populated (neutral defaults where
        a denominator was zero).
    """
    # DSRI — receivables growing faster than sales suggests inflated revenue.
    dsri = _index(
        _safe_div(current.receivables, current.revenue),
        _safe_div(prior.receivables, prior.revenue),
        "DSRI",
    )

    # GMI — prior margin over current margin; >1 means margins deteriorated.
    gmi = _index(
        _safe_div(prior.revenue - prior.cogs, prior.revenue),
        _safe_div(current.revenue - current.cogs, current.revenue),
        "GMI",
    )

    # AQI — growth in "soft" (non-current, non-PPE) assets that may hide
    # capitalised expenses.
    def _asset_quality(year: BeneishYear) -> float | None:
        soft = _safe_div(year.current_assets + year.ppe + year.securities, year.total_assets)
        return None if soft is None else 1.0 - soft

    aqi = _index(_asset_quality(current), _asset_quality(prior), "AQI")

    # SGI — sales growth itself is not manipulation, but high growth firms
    # face more pressure to manipulate.
    sgi = _index(current.revenue, prior.revenue, "SGI")

    # DEPI — a falling depreciation rate can indicate lives being stretched.
    depi = _index(
        _safe_div(prior.depreciation, prior.depreciation + prior.ppe),
        _safe_div(current.depreciation, current.depreciation + current.ppe),
        "DEPI",
    )

    # SGAI — SGA growing faster than sales signals declining efficiency.
    sgai = _index(
        _safe_div(current.sga, current.revenue),
        _safe_div(prior.sga, prior.revenue),
        "SGAI",
    )

    # LVGI — rising leverage tightens debt covenants, a manipulation incentive.
    lvgi = _index(
        _safe_div(current.long_term_debt + current.current_liabilities, current.total_assets),
        _safe_div(prior.long_term_debt + prior.current_liabilities, prior.total_assets),
        "LVGI",
    )

    # TATA — accruals (earnings not backed by cash) over total assets.
    tata_ratio = _safe_div(current.net_income - current.cfo, current.total_assets)
    if tata_ratio is None:
        logger.debug("Beneish TATA undefined (zero total assets); using 0.0")
        tata_ratio = 0.0

    return BeneishIndices(
        dsri=dsri, gmi=gmi, aqi=aqi, sgi=sgi,
        depi=depi, sgai=sgai, lvgi=lvgi, tata=tata_ratio,
    )


def calculate(indices: BeneishIndices) -> BeneishResult:
    """Combine pre-computed indices into the M-Score and classify it.

    Args:
        indices: The eight Beneish component indices.

    Returns:
        BeneishResult with M-Score, the input indices, and a verdict keyed to
        the thresholds in config.yaml.
    """
    m_score = (
        -4.840
        + 0.920 * indices.dsri
        + 0.528 * indices.gmi
        + 0.404 * indices.aqi
        + 0.892 * indices.sgi
        + 0.115 * indices.depi
        - 0.172 * indices.sgai
        + 4.679 * indices.tata
        - 0.327 * indices.lvgi
    )

    cutoff = _thresholds["manipulation_cutoff"]
    grey_lower = _thresholds["grey_zone_lower"]

    if m_score > cutoff:
        verdict = "manipulator"
    elif m_score > grey_lower:
        verdict = "grey_zone"
    else:
        verdict = "non_manipulator"

    logger.debug("Beneish M-Score: %.4f → %s", m_score, verdict)
    return BeneishResult(m_score=m_score, indices=indices, verdict=verdict)


def analyze(current: BeneishYear, prior: BeneishYear) -> BeneishResult:
    """End-to-end Beneish analysis from two years of raw financials.

    Args:
        current: Raw financials for the year being tested (year t).
        prior: Raw financials for the preceding year (year t−1).

    Returns:
        BeneishResult with M-Score, component indices, and verdict.
    """
    return calculate(compute_indices(current, prior))
