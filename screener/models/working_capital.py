"""Working capital analysis: cash conversion cycle and heatmap data.

The cash conversion cycle (CCC) measures how many days cash is locked up in
operations:

    CCC = DSO + DIO − DPO

* **DSO** (days sales outstanding) — receivables / revenue × days.
* **DIO** (days inventory outstanding) — inventory / COGS × days.
* **DPO** (days payables outstanding) — payables / COGS × days.

:func:`heatmap_data` turns a list of quarters into metric-per-quarter series
shaped for a Streamlit/Plotly heatmap (metrics as rows, quarters as columns).

The day-count conventions come from ``working_capital`` in config.yaml.
"""

import logging
from dataclasses import dataclass

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["working_capital"]


@dataclass
class QuarterFinancials:
    """One quarter of inputs for working capital analysis.

    All monetary values must be in the same unit (e.g. ₹ crore) and reflect
    the same quarter (flows) or its end date (balances).
    """

    label: str          # e.g. "FY24 Q1"
    revenue: float      # quarterly revenue (flow)
    cogs: float         # quarterly cost of goods sold (flow)
    receivables: float  # trade receivables at quarter end
    inventory: float    # inventory at quarter end
    payables: float     # trade payables at quarter end


def dso(receivables: float, revenue: float, days: float | None = None) -> float:
    """Return days sales outstanding for one period.

    Args:
        receivables: Trade receivables at period end.
        revenue: Revenue for the period.
        days: Day-count for the period. Defaults to the configured
            days_per_quarter.

    Returns:
        DSO in days, or 0.0 (with a warning) if revenue is zero.
    """
    period = days if days is not None else _cfg["days_per_quarter"]
    if revenue == 0:
        logger.warning("DSO undefined (zero revenue); returning 0.0")
        return 0.0
    return receivables / revenue * period


def dio(inventory: float, cogs: float, days: float | None = None) -> float:
    """Return days inventory outstanding for one period.

    Args:
        inventory: Inventory at period end.
        cogs: Cost of goods sold for the period.
        days: Day-count for the period. Defaults to the configured
            days_per_quarter.

    Returns:
        DIO in days, or 0.0 (with a warning) if COGS is zero.
    """
    period = days if days is not None else _cfg["days_per_quarter"]
    if cogs == 0:
        logger.warning("DIO undefined (zero COGS); returning 0.0")
        return 0.0
    return inventory / cogs * period


def dpo(payables: float, cogs: float, days: float | None = None) -> float:
    """Return days payables outstanding for one period.

    Args:
        payables: Trade payables at period end.
        cogs: Cost of goods sold for the period.
        days: Day-count for the period. Defaults to the configured
            days_per_quarter.

    Returns:
        DPO in days, or 0.0 (with a warning) if COGS is zero.
    """
    period = days if days is not None else _cfg["days_per_quarter"]
    if cogs == 0:
        logger.warning("DPO undefined (zero COGS); returning 0.0")
        return 0.0
    return payables / cogs * period


def ccc(dso_days: float, dio_days: float, dpo_days: float) -> float:
    """Return the cash conversion cycle (DSO + DIO − DPO).

    Args:
        dso_days: Days sales outstanding.
        dio_days: Days inventory outstanding.
        dpo_days: Days payables outstanding.

    Returns:
        CCC in days. Negative means suppliers finance the operating cycle.
    """
    return dso_days + dio_days - dpo_days


def heatmap_data(
    quarters: list[QuarterFinancials], days: float | None = None
) -> dict[str, list]:
    """Build CCC heatmap series from period financials.

    Args:
        quarters: Period records ordered oldest → newest (quarterly or annual).
        days: Day-count convention for every period. Defaults to the
            configured days_per_quarter; pass days_per_year for annual data.

    Returns:
        Dict with parallel lists, ready for a metrics-by-periods heatmap:
        ``quarters`` (labels), ``dso``, ``dio``, ``dpo``, ``ccc``.

    Raises:
        ValueError: If *quarters* is empty.
    """
    if not quarters:
        raise ValueError("Heatmap needs at least one quarter of data")

    labels: list[str] = []
    dso_series: list[float] = []
    dio_series: list[float] = []
    dpo_series: list[float] = []
    ccc_series: list[float] = []

    for q in quarters:
        q_dso = dso(q.receivables, q.revenue, days=days)
        q_dio = dio(q.inventory, q.cogs, days=days)
        q_dpo = dpo(q.payables, q.cogs, days=days)
        labels.append(q.label)
        dso_series.append(q_dso)
        dio_series.append(q_dio)
        dpo_series.append(q_dpo)
        ccc_series.append(ccc(q_dso, q_dio, q_dpo))

    logger.debug("Heatmap built for %d quarters", len(quarters))
    return {
        "quarters": labels,
        "dso": dso_series,
        "dio": dio_series,
        "dpo": dpo_series,
        "ccc": ccc_series,
    }
