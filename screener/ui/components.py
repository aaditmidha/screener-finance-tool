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

    def _bs_row(*needles: str) -> list[float | None] | None:
        """Look a row up on the balance sheet, falling back to its notes."""
        for needle in needles:
            found = bs.row(needle) or (fin.notes_bs.row(needle) if fin.notes_bs else None)
            if found:
                return found
        return None

    receivables = _bs_row("receivable", "debtor")
    inventory = _bs_row("inventor")
    payables = _bs_row("payable")
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


# Forensic verdict → (emoji, hex colour).
_FORENSIC_STYLE = {
    "healthy": ("🟢", "#27ae60"),
    "watch": ("🟠", "#e67e22"),
    "high_risk": ("🔴", "#c0392b"),
}


def forensic_badge(result: "object") -> tuple[str, str, str]:
    """Return (emoji, colour, caption) for a ForensicScore.

    Args:
        result: A :class:`screener.models.forensic_score.ForensicScore`.

    Returns:
        Tuple of (badge emoji, hex colour, caption with score and verdict).
    """
    emoji, colour = _FORENSIC_STYLE.get(result.verdict, ("⚪", "#7f8c8d"))
    caption = f"Forensic health {result.score:.0f}/100 — {result.verdict.replace('_', ' ')}"
    return (emoji, colour, caption)


def build_forensic_gauge(result: "object") -> "object":
    """Build a Plotly gauge for the 0–100 forensic health score.

    Args:
        result: A :class:`screener.models.forensic_score.ForensicScore`.

    Returns:
        A ``plotly.graph_objects.Figure`` indicator gauge with red/amber/green
        zones and the verdict-coloured needle.
    """
    import plotly.graph_objects as go

    colour = _FORENSIC_STYLE.get(result.verdict, ("", "#7f8c8d"))[1]
    figure = go.Figure(go.Indicator(
        mode="gauge+number",
        value=result.score,
        number={"suffix": "/100", "font": {"size": 40, "color": colour}},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#8b949e"},
            "bar": {"color": colour, "thickness": 0.28},
            "bgcolor": "rgba(0,0,0,0)",
            "borderwidth": 0,
            "steps": [
                {"range": [0, 50], "color": "rgba(192,57,43,0.22)"},
                {"range": [50, 75], "color": "rgba(230,126,34,0.22)"},
                {"range": [75, 100], "color": "rgba(39,174,96,0.22)"},
            ],
            "threshold": {"line": {"color": colour, "width": 4}, "thickness": 0.8,
                          "value": result.score},
        },
    ))
    figure.update_layout(
        height=220, margin={"l": 24, "r": 24, "t": 16, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)", font={"color": "#e6edf3", "family": "Inter, sans-serif"},
    )
    return figure


def _format_operational(value: float | None, fmt: str) -> str:
    """Format one operational metric value for display per its unit hint."""
    if value is None:
        return "—"
    if fmt == "pct":
        return f"{value * 100:.1f}%"
    if fmt == "x":
        return f"{value:.2f}x"
    if fmt == "days":
        return f"{value:.0f}"
    return f"{value:.2f}"


def operational_to_df(op: "object") -> pd.DataFrame:
    """Turn an OperationalData into a display DataFrame (metrics × periods).

    Args:
        op: A :class:`screener.models.operational.OperationalData`.

    Returns:
        DataFrame indexed by metric label with formatted string cells; empty
        when no metrics were computable.
    """
    if not op.metrics:
        return pd.DataFrame()
    rows = {
        m.label: [_format_operational(v, m.fmt) for v in m.values]
        for m in op.metrics
    }
    return pd.DataFrame.from_dict(rows, orient="index", columns=op.periods)


def data_quality_note(approximated: list[str], missing: list[str]) -> str:
    """Render data-sourcing disclosures as one human-readable line.

    Args:
        approximated: Descriptions of approximated inputs.
        missing: Descriptions of unavailable inputs (neutralised in the model).

    Returns:
        A disclosure string, empty when everything was exact.
    """
    parts: list[str] = []
    if approximated:
        parts.append("Approximated: " + "; ".join(approximated))
    if missing:
        parts.append("Unavailable (index neutralised): " + "; ".join(missing))
    return ". ".join(parts)


# Risk level → (emoji, hex colour) for the pledge badge.
_PLEDGE_STYLE = {
    "none": ("🟢", "#27ae60"),
    "low": ("🟢", "#27ae60"),
    "elevated": ("🟠", "#e67e22"),
    "high": ("🔴", "#c0392b"),
}


def pledge_badge(result: "object") -> tuple[str, str, str]:
    """Return (emoji, colour, caption) for a PledgeResult.

    Args:
        result: A :class:`screener.models.pledge_monitor.PledgeResult`.

    Returns:
        Tuple of (badge emoji, hex colour, caption text).
    """
    emoji, colour = _PLEDGE_STYLE.get(result.risk_level, ("⚪", "#7f8c8d"))
    caption = f"Promoter pledge {result.latest_pct:.1f}% — {result.risk_level} risk"
    if result.rising:
        caption += " (rising)"
    return (emoji, colour, caption)


def build_pledge_figure(history: list) -> "object":
    """Build a Plotly line chart of pledge % over time with threshold bands.

    Args:
        history: List of :class:`PledgePoint` (period, pledge_pct).

    Returns:
        A ``plotly.graph_objects.Figure``.
    """
    import plotly.graph_objects as go

    from screener.config import CONFIG

    cfg = CONFIG["thresholds"]["pledge"]
    figure = go.Figure(
        go.Scatter(
            x=[p.period for p in history],
            y=[p.pledge_pct for p in history],
            mode="lines+markers",
            name="Pledged %",
        )
    )
    figure.add_hline(y=cfg["warning_pct"], line_dash="dash", line_color="#e67e22",
                     annotation_text=f"warning {cfg['warning_pct']:.0f}%")
    figure.add_hline(y=cfg["critical_pct"], line_dash="dash", line_color="#c0392b",
                     annotation_text=f"critical {cfg['critical_pct']:.0f}%")
    figure.update_layout(
        title="Promoter pledge history",
        yaxis_title="% of promoter holding pledged",
        yaxis_rangemode="tozero",
    )
    return figure


def _at(row: list[float | None], index: int) -> float | None:
    """Return ``row[index]`` or None if out of range."""
    return row[index] if 0 <= index < len(row) else None
