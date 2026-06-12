"""Promoter pledge risk monitor — an India-specific red-flag detector.

Promoters pledging their shares as loan collateral is a classic precursor to
distress in Indian markets: a falling stock triggers margin calls, forced
selling, and a spiral. This module:

* parses promoter-pledge history out of a Screener-style shareholding table;
* flags threshold crossings (default >20% warning, >40% critical);
* cross-references crossings with subsequent stock-price drops.

Thresholds come from ``thresholds.pledge`` in config.yaml.
"""

import logging
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from screener.config import CONFIG
from screener.scraper.parser import parse_table

logger = logging.getLogger(__name__)

_cfg = CONFIG["thresholds"]["pledge"]


@dataclass
class PledgePoint:
    """Promoter pledge level at one reporting period."""

    period: str       # e.g. "Mar 2024"
    pledge_pct: float  # percent of promoter holding pledged (0–100)


@dataclass
class PledgePriceEvent:
    """A pledge threshold crossing followed by a material price drop."""

    period: str
    threshold_pct: float
    price_drop: float   # most negative subsequent return, e.g. -0.22


@dataclass
class PledgeResult:
    """Aggregate pledge risk assessment."""

    latest_pct: float
    max_pct: float
    crossings: list[tuple[str, float]] = field(default_factory=list)
    rising: bool = False
    risk_level: str = "none"   # "none" | "low" | "elevated" | "high"
    price_events: list[PledgePriceEvent] = field(default_factory=list)


def parse_pledge_history(html: str) -> list[PledgePoint]:
    """Extract promoter pledge history from a Screener shareholding table.

    Looks for a row whose label mentions "pledge" inside the ``shareholding``
    section of a company page.

    Args:
        html: Raw company-page HTML.

    Returns:
        Chronological pledge points; empty if the page has no pledge row.
    """
    soup = BeautifulSoup(html, "lxml")
    table = parse_table(soup, "shareholding")
    if table is None:
        logger.info("No shareholding section found")
        return []
    values = table.row("pledge")
    if values is None:
        logger.info("Shareholding table has no pledge row")
        return []
    points = [
        PledgePoint(period=p, pledge_pct=v)
        for p, v in zip(table.periods, values)
        if v is not None
    ]
    logger.debug("Parsed %d pledge point(s)", len(points))
    return points


def _find_crossings(history: list[PledgePoint], threshold: float) -> list[str]:
    """Return periods where the pledge level first rises above *threshold*."""
    crossings: list[str] = []
    previous = 0.0
    for point in history:
        if previous <= threshold < point.pledge_pct:
            crossings.append(point.period)
        previous = point.pledge_pct
    return crossings


def _is_rising(history: list[PledgePoint], lookback: int = 3) -> bool:
    """True if the pledge level increased monotonically over the last periods."""
    tail = history[-lookback:]
    if len(tail) < 2:
        return False
    return all(b.pledge_pct > a.pledge_pct for a, b in zip(tail, tail[1:]))


def _price_events(
    history: list[PledgePoint],
    crossings: list[tuple[str, float]],
    prices: dict[str, float],
) -> list[PledgePriceEvent]:
    """Match crossings with subsequent price drops beyond the config threshold."""
    lookahead = _cfg["price_lookahead_periods"]
    drop_threshold = _cfg["price_drop_pct"]
    periods = [p.period for p in history]

    events: list[PledgePriceEvent] = []
    for period, threshold in crossings:
        if period not in periods or period not in prices:
            continue
        idx = periods.index(period)
        base = prices[period]
        if base <= 0:
            continue
        window = [
            prices[periods[i]]
            for i in range(idx + 1, min(idx + 1 + lookahead, len(periods)))
            if periods[i] in prices
        ]
        if not window:
            continue
        worst_return = min(window) / base - 1
        if worst_return <= -drop_threshold:
            events.append(
                PledgePriceEvent(period=period, threshold_pct=threshold, price_drop=worst_return)
            )
            logger.info(
                "Pledge crossing at %s (>%.0f%%) followed by %.0f%% price drop",
                period, threshold, worst_return * 100,
            )
    return events


def analyze(
    history: list[PledgePoint], prices: dict[str, float] | None = None
) -> PledgeResult:
    """Assess promoter pledge risk from a pledge history.

    Args:
        history: Chronological pledge points (oldest → newest).
        prices: Optional period → closing-price mapping for the same periods,
            used to flag pledge crossings followed by price drops.

    Returns:
        A PledgeResult with crossings, trend, risk level and price events.

    Raises:
        ValueError: If *history* is empty.
    """
    if not history:
        raise ValueError("Pledge analysis needs at least one data point")

    warning = _cfg["warning_pct"]
    critical = _cfg["critical_pct"]

    latest = history[-1].pledge_pct
    peak = max(p.pledge_pct for p in history)
    crossings: list[tuple[str, float]] = [
        (period, warning) for period in _find_crossings(history, warning)
    ] + [
        (period, critical) for period in _find_crossings(history, critical)
    ]
    rising = _is_rising(history)

    if latest <= 0:
        risk = "none"
    elif latest > critical or (latest > warning and rising):
        risk = "high"
    elif latest > warning:
        risk = "elevated"
    else:
        risk = "low"

    events = _price_events(history, crossings, prices) if prices else []

    logger.info("Pledge risk for latest=%.1f%%: %s", latest, risk)
    return PledgeResult(
        latest_pct=latest,
        max_pct=peak,
        crossings=sorted(crossings, key=lambda c: c[1]),
        rising=rising,
        risk_level=risk,
        price_events=events,
    )
