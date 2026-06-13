"""Cross-year insights derived from Annual-Report extracted data.

Three analyst-grade views over the ``ar_extracted_data`` rows:

* **Discrepancy** — Screener-scraped figures vs AR-extracted figures per metric
  and year, flagged moderate (>5%) / large (>20%). Large gaps often mean a
  restatement, an accounting-policy change, or an extraction error worth a
  manual check.
* **Risk timeline** — the key risks named across years, with first/last
  mention and frequency. Almost no tool tracks risk-factor evolution.
* **Guidance scorecard** — management's guided revenue growth vs what was
  actually delivered the following year, scored by the credibility engine.

All inputs are duck-typed (``ARExtractedData``-like / ``AnnualData``-like), so
this module stays free of DB and scraper dependencies and is unit-tested with
plain stubs. Thresholds come from the ``discrepancy`` config block.
"""

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from screener.config import CONFIG
from screener.models.management_credibility import (
    CredibilityResult,
    GuidanceItem,
    evaluate,
)

logger = logging.getLogger(__name__)

_disc_cfg = CONFIG["discrepancy"]

# (display label, ARExtractedData attr, AnnualData attr) for comparable metrics.
_COMPARABLE = [
    ("Revenue", "revenue", "revenue"),
    ("Net profit / PAT", "pat", "net_income"),
    ("Total assets", "total_assets", "total_assets"),
    ("Total debt", "total_debt", "total_debt"),
    ("Total equity", "total_equity", "shareholders_equity"),
]


@dataclass
class DiscrepancyCell:
    """One metric/year Screener-vs-AR comparison."""

    metric: str
    year: int
    screener: float | None
    ar: float | None
    diff_pct: float | None       # (ar − screener) / screener
    severity: str                # "ok" | "moderate" | "large" | "n/a"


@dataclass
class RiskEntry:
    """A risk factor and its presence across the analysed years."""

    risk: str
    first_year: int
    last_year: int
    frequency: int


def _severity(diff_pct: float | None) -> str:
    """Classify a percentage difference into ok / moderate / large / n/a."""
    if diff_pct is None:
        return "n/a"
    mag = abs(diff_pct)
    if mag > _disc_cfg["large_pct"]:
        return "large"
    if mag > _disc_cfg["moderate_pct"]:
        return "moderate"
    return "ok"


def discrepancies(ar_rows: list[Any], annual_rows: list[Any]) -> list[DiscrepancyCell]:
    """Compare Screener annual figures against AR-extracted figures.

    Args:
        ar_rows: ARExtractedData-like rows (``.fiscal_year`` + metric attrs).
        annual_rows: AnnualData-like rows (``.fiscal_year_end`` + metric attrs).

    Returns:
        DiscrepancyCells for every comparable metric/year where both sides have
        a value, ordered by metric then year.
    """
    screener_by_year = {row.fiscal_year_end.year: row for row in annual_rows}
    cells: list[DiscrepancyCell] = []
    for label, ar_attr, annual_attr in _COMPARABLE:
        for ar_row in ar_rows:
            year = ar_row.fiscal_year
            ar_val = getattr(ar_row, ar_attr, None)
            screener_row = screener_by_year.get(year)
            scr_val = getattr(screener_row, annual_attr, None) if screener_row else None
            if ar_val is None or scr_val is None:
                continue
            diff_pct = (ar_val - scr_val) / scr_val if scr_val else None
            cells.append(DiscrepancyCell(
                metric=label, year=year, screener=scr_val, ar=ar_val,
                diff_pct=diff_pct, severity=_severity(diff_pct),
            ))
    return cells


def worst_discrepancies(cells: list[DiscrepancyCell], top: int = 3) -> list[DiscrepancyCell]:
    """Return the *top* cells with the largest absolute percentage difference.

    Args:
        cells: Output of :func:`discrepancies`.
        top: How many to return.

    Returns:
        The most divergent cells (largest |diff_pct| first).
    """
    ranked = sorted(
        (c for c in cells if c.diff_pct is not None),
        key=lambda c: abs(c.diff_pct), reverse=True,
    )
    return ranked[:top]


def _parse_risks(value: Any) -> list[str]:
    """Coerce a key_risks value (JSON string or list) into a list of strings."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def risk_timeline(ar_rows: list[Any]) -> list[RiskEntry]:
    """Aggregate key risks across years into a frequency-ranked timeline.

    Risks are matched case-insensitively on trimmed text. The displayed text is
    the first-seen spelling.

    Args:
        ar_rows: ARExtractedData-like rows with ``.fiscal_year`` and
            ``.key_risks`` (JSON string or list).

    Returns:
        RiskEntry list sorted by frequency (desc), then earliest first mention.
    """
    agg: "OrderedDict[str, dict]" = OrderedDict()
    for row in sorted(ar_rows, key=lambda r: r.fiscal_year):
        for risk in _parse_risks(getattr(row, "key_risks", None)):
            key = risk.strip().lower()
            if not key:
                continue
            if key not in agg:
                agg[key] = {"text": risk.strip(), "first": row.fiscal_year,
                            "last": row.fiscal_year, "count": 0}
            agg[key]["last"] = row.fiscal_year
            agg[key]["count"] += 1
    entries = [
        RiskEntry(risk=v["text"], first_year=v["first"], last_year=v["last"], frequency=v["count"])
        for v in agg.values()
    ]
    entries.sort(key=lambda e: (-e.frequency, e.first_year))
    return entries


def guidance_scorecard(
    ar_rows: list[Any], revenue_by_year: dict[int, float]
) -> CredibilityResult | None:
    """Score management's revenue-growth guidance against delivery.

    Guidance extracted from FY *N*'s report targets FY *N+1*; the actual growth
    is computed from revenue in N and N+1.

    Args:
        ar_rows: ARExtractedData-like rows with ``.guided_revenue_growth``.
        revenue_by_year: Map of fiscal year → revenue (Screener or AR).

    Returns:
        A CredibilityResult, or None if no guidance could be paired with an
        actual outcome.
    """
    items: list[GuidanceItem] = []
    for row in ar_rows:
        guided = getattr(row, "guided_revenue_growth", None)
        if guided is None:
            continue
        target = row.fiscal_year + 1
        prev_rev, cur_rev = revenue_by_year.get(target - 1), revenue_by_year.get(target)
        actual = (cur_rev / prev_rev - 1) if prev_rev and cur_rev and prev_rev > 0 else None
        items.append(GuidanceItem(fiscal_year=target, metric="revenue_growth",
                                  guided=guided, actual=actual))
    if not items:
        return None
    try:
        return evaluate(items)
    except ValueError:
        logger.info("No evaluable guidance (no actuals yet)")
        return None
