"""Pure, testable helpers behind the Streamlit dashboard.

Everything here is free of Streamlit calls so it can be unit-tested directly:
DataFrame conversion, the Beneish red/green flag, the Plotly working-capital
heatmap, freshness formatting, and the adapters that turn parsed financials
into model inputs (degrading gracefully when Screener omits granular rows).
"""

import logging
from datetime import datetime, timezone

import pandas as pd

from screener.database import cache
from screener.models import working_capital as wc
from screener.models.beneish import BeneishResult
from screener.scraper.parser import CompanyFinancials, FinancialTable

logger = logging.getLogger(__name__)

# Verdict → (emoji flag, hex colour) for the Beneish display.
_BENEISH_STYLE = {
    "manipulator": ("🔴", "#c0392b"),
    "grey_zone": ("🟠", "#e67e22"),
    "non_manipulator": ("🟢", "#27ae60"),
}


def financial_table_to_df(table: FinancialTable | None) -> pd.DataFrame:
    """Convert a FinancialTable to a periods-as-columns DataFrame.

    Args:
        table: Parsed statement, or None.

    Returns:
        DataFrame indexed by line-item label with one column per period; empty
        if *table* is None.
    """
    if table is None:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(table.rows, orient="index", columns=table.periods)


def beneish_flag(result: BeneishResult | None) -> tuple[str, str, str]:
    """Return (emoji, colour, caption) for a Beneish result.

    Args:
        result: A computed BeneishResult, or None when inputs were unavailable.

    Returns:
        Tuple of (flag emoji, hex colour, human caption). For None, a neutral
        grey "not available" trio.
    """
    if result is None:
        return ("⚪", "#7f8c8d", "M-Score unavailable (insufficient data)")
    emoji, colour = _BENEISH_STYLE.get(result.verdict, ("⚪", "#7f8c8d"))
    caption = f"M-Score {result.m_score:.2f} — {result.verdict.replace('_', ' ')}"
    return (emoji, colour, caption)


def format_freshness(last_updated: datetime | None, now: datetime | None = None) -> str:
    """Render a human freshness string from a last-updated timestamp.

    Args:
        last_updated: When the data was last scraped, or None if never.
        now: Current time, injectable for testing. Defaults to UTC now.

    Returns:
        A short status string, e.g. "Updated 2h ago (fresh)" or "Never scraped".
    """
    if last_updated is None:
        return "Never scraped"

    current = now or datetime.now(timezone.utc)
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    delta = current - last_updated
    hours = delta.total_seconds() / 3600
    if hours < 1:
        age = f"{int(delta.total_seconds() // 60)}m ago"
    elif hours < 24:
        age = f"{int(hours)}h ago"
    else:
        age = f"{delta.days}d ago"

    state = "stale" if cache.is_stale(last_updated, now=current) else "fresh"
    return f"Updated {age} ({state})"


def working_capital_quarters(fin: CompanyFinancials) -> list[wc.QuarterFinancials]:
    """Build working-capital period records from parsed financials.

    Screener's aggregated balance sheet often omits receivables/inventory/
    payables; when any are missing this returns an empty list so the caller can
    show a "not available" message rather than a misleading chart.

    Args:
        fin: Parsed company financials.

    Returns:
        A QuarterFinancials per period (here, per fiscal year), or [] if the
        required granular rows are absent.
    """
    pl, bs = fin.profit_loss, fin.balance_sheet
    if pl is None or bs is None:
        return []

    receivables = bs.row("receivable") or bs.row("debtor")
    inventory = bs.row("inventor")
    payables = bs.row("payable")
    sales = pl.row("sales") or pl.row("revenue")
    op_profit = pl.row("operating profit")
    if not all([receivables, inventory, payables, sales, op_profit]):
        logger.info("Granular working-capital rows unavailable for %s", fin.symbol)
        return []

    quarters: list[wc.QuarterFinancials] = []
    for i, period in enumerate(bs.periods):
        # COGS ≈ Sales − Operating Profit when a COGS line isn't published.
        rev = _at(sales, i)
        cogs = None
        if rev is not None and _at(op_profit, i) is not None:
            cogs = rev - _at(op_profit, i)
        if None in (rev, cogs, _at(receivables, i), _at(inventory, i), _at(payables, i)):
            continue
        quarters.append(
            wc.QuarterFinancials(
                label=period,
                revenue=rev,
                cogs=cogs,
                receivables=_at(receivables, i),
                inventory=_at(inventory, i),
                payables=_at(payables, i),
            )
        )
    return quarters


def build_wc_heatmap_figure(heatmap: dict[str, list]) -> "object":
    """Build a Plotly heatmap figure of DSO/DIO/DPO/CCC by period.

    Args:
        heatmap: Output of :func:`screener.models.working_capital.heatmap_data`.

    Returns:
        A ``plotly.graph_objects.Figure`` with metrics as rows and periods as
        columns.
    """
    import plotly.graph_objects as go

    metrics = ["dso", "dio", "dpo", "ccc"]
    z = [heatmap[m] for m in metrics]
    figure = go.Figure(
        data=go.Heatmap(
            z=z,
            x=heatmap["quarters"],
            y=["DSO", "DIO", "DPO", "CCC"],
            colorscale="RdYlGn_r",
            colorbar={"title": "Days"},
        )
    )
    figure.update_layout(
        title="Working Capital Cycle (days)",
        xaxis_title="Period",
        yaxis_title="Metric",
    )
    return figure


def _at(row: list[float | None], index: int) -> float | None:
    """Return ``row[index]`` or None if out of range."""
    return row[index] if 0 <= index < len(row) else None
