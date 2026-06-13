"""Forensic Red-Flag composite score — one health number from many signals.

Aggregates the project's forensic models into a single 0–100 score (higher =
healthier) with a per-component breakdown:

* **Manipulation** — Beneish M-Score verdict.
* **Earnings quality** — CFO/PAT conversion and accrual flags.
* **Pledge** — promoter pledge risk (India-specific).
* **Leverage** — debt-to-equity.

No free Indian tool publishes an aggregated forensic score. Components with no
available data are excluded and the weights renormalised over the rest, so the
score is always computed on what's known — and each component reports whether
it contributed and why. All weights/bands come from ``forensic_score`` config.
"""

import logging
from dataclasses import dataclass, field

from screener.config import CONFIG
from screener.models import beneish_adapter, earnings_quality, pledge_monitor
from screener.scraper.parser import CompanyFinancials

logger = logging.getLogger(__name__)

_cfg = CONFIG["forensic_score"]

# Verdict → component sub-score (0–100, higher = healthier).
_BENEISH_SCORE = {"non_manipulator": 100.0, "grey_zone": 50.0, "manipulator": 0.0}
_EQ_SCORE = {"healthy": 100.0, "caution": 60.0, "red_flag": 20.0}
_PLEDGE_SCORE = {"none": 100.0, "low": 100.0, "elevated": 50.0, "high": 0.0}


@dataclass
class Component:
    """One forensic sub-score and its supporting detail."""

    name: str
    available: bool
    score: float | None      # 0–100, or None when unavailable
    detail: str


@dataclass
class ForensicScore:
    """Composite forensic health assessment."""

    score: float                       # 0–100
    verdict: str                       # "healthy" | "watch" | "high_risk"
    components: list[Component] = field(default_factory=list)


def _col_series(table, *needles: str) -> dict[str, float]:
    """Return {period: value} for the first matching row of *table*."""
    if table is None:
        return {}
    for needle in needles:
        row = table.row(needle)
        if row:
            return {p: v for p, v in zip(table.periods, row) if v is not None}
    return {}


def _manipulation(fin: CompanyFinancials) -> Component:
    """Beneish-based manipulation component."""
    sourcing = beneish_adapter.from_financials(fin)
    if sourcing is None:
        return Component("Manipulation (Beneish)", False, None, "insufficient data")
    score = _BENEISH_SCORE.get(sourcing.result.verdict, 50.0)
    detail = f"M-Score {sourcing.result.m_score:.2f} — {sourcing.result.verdict.replace('_', ' ')}"
    return Component("Manipulation (Beneish)", True, score, detail)


def _earnings_quality(fin: CompanyFinancials) -> Component:
    """CFO/PAT + accruals component, built from PL/CF/BS periods."""
    pat = _col_series(fin.profit_loss, "net profit", "profit after tax")
    cfo = _col_series(fin.cash_flow, "operating activity", "cash from operating")
    assets = _col_series(fin.balance_sheet, "total assets")
    periods = [p for p in pat if p in cfo and p in assets]
    if len(periods) < 2:
        return Component("Earnings quality", False, None, "no cash-flow / PAT history")

    years = [
        earnings_quality.YearFinancials(year=i, pat=pat[p], cfo=cfo[p], total_assets=assets[p])
        for i, p in enumerate(periods)
    ]
    try:
        result = earnings_quality.analyze(years)
    except ValueError as exc:
        return Component("Earnings quality", False, None, str(exc))
    score = _EQ_SCORE.get(result.verdict, 60.0)
    detail = f"avg CFO/PAT {result.avg_cfo_pat:.2f}, {len(result.flags)} flag(s) — {result.verdict}"
    return Component("Earnings quality", True, score, detail)


def _pledge(history: list[pledge_monitor.PledgePoint] | None) -> Component:
    """Promoter-pledge risk component."""
    if not history:
        return Component("Promoter pledge", False, None, "no pledge data")
    result = pledge_monitor.analyze(history)
    score = _PLEDGE_SCORE.get(result.risk_level, 50.0)
    detail = f"{result.latest_pct:.1f}% pledged — {result.risk_level} risk"
    return Component("Promoter pledge", True, score, detail)


def _leverage(fin: CompanyFinancials) -> Component:
    """Debt-to-equity component (linear between safe and risky bands)."""
    bs = fin.balance_sheet
    if bs is None:
        return Component("Leverage (D/E)", False, None, "no balance sheet")
    debt = bs.latest("borrowings") or bs.latest("debt")
    equity_cap = bs.latest("equity capital") or bs.latest("share capital") or 0.0
    reserves = bs.latest("reserves") or 0.0
    equity = equity_cap + reserves
    if debt is None or equity <= 0:
        return Component("Leverage (D/E)", False, None, "debt/equity not parseable")

    de = debt / equity
    safe, risky = _cfg["leverage"]["de_safe"], _cfg["leverage"]["de_risky"]
    if de <= safe:
        score = 100.0
    elif de >= risky:
        score = 0.0
    else:
        score = 100.0 * (risky - de) / (risky - safe)
    return Component("Leverage (D/E)", True, score, f"D/E {de:.2f}")


def compute(
    fin: CompanyFinancials,
    pledge_history: list[pledge_monitor.PledgePoint] | None = None,
) -> ForensicScore:
    """Compute the composite forensic health score for a company.

    Args:
        fin: Parsed company financials.
        pledge_history: Optional promoter-pledge history (parsed from the page).

    Returns:
        A :class:`ForensicScore`. The composite is the weighted average over
        components that had data; if none did, the score is 0.0 with verdict
        "high_risk" and every component flagged unavailable.
    """
    weights = _cfg["weights"]
    components = {
        "manipulation": _manipulation(fin),
        "earnings_quality": _earnings_quality(fin),
        "pledge": _pledge(pledge_history),
        "leverage": _leverage(fin),
    }

    total_weight = sum(weights[key] for key, comp in components.items() if comp.available)
    if total_weight > 0:
        composite = sum(
            weights[key] * comp.score
            for key, comp in components.items()
            if comp.available and comp.score is not None
        ) / total_weight
    else:
        composite = 0.0

    bands = _cfg["bands"]
    if total_weight == 0:
        verdict = "high_risk"
    elif composite >= bands["healthy_min"]:
        verdict = "healthy"
    elif composite >= bands["watch_min"]:
        verdict = "watch"
    else:
        verdict = "high_risk"

    logger.info("Forensic score for %s: %.0f/100 (%s)", fin.symbol, composite, verdict)
    return ForensicScore(
        score=round(composite, 1), verdict=verdict, components=list(components.values())
    )
