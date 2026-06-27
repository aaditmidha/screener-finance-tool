"""Focus charts — interactive Plotly trend visuals for the dashboard.

Pure builders that turn parsed financials into themed Plotly figures (revenue &
PAT, margins, return ratios, cash conversion). Each returns ``None`` when the
underlying data isn't available, so the UI can show a graceful message instead
of an empty chart. Free of Streamlit so they're unit-tested directly.
"""

import logging

from screener.scraper.parser import CompanyFinancials, FinancialTable

logger = logging.getLogger(__name__)

# Shared branded layout (kept local to avoid importing UI internals).
# Neutral slate font stays legible on both light and dark app themes.
_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"color": "#64748b", "family": "Inter, sans-serif"},
    "margin": {"l": 45, "r": 20, "t": 50, "b": 40},
    "height": 300,
    "legend": {"orientation": "h", "y": -0.2},
}
_TEAL, _RED, _AMBER, _BLUE = "#2dd4bf", "#c0392b", "#e67e22", "#6ea8fe"


def _row_by_period(table: FinancialTable | None, *needles: str) -> dict[str, float]:
    """Return {period: value} for the first matching row of *table*."""
    if table is None:
        return {}
    for needle in needles:
        row = table.row(needle)
        if row:
            return {p: v for p, v in zip(table.periods, row) if v is not None}
    return {}


def _aligned(periods: list[str], *maps: dict[str, float]):
    """Yield (period, values...) for periods present in every map."""
    for p in periods:
        if all(p in m for m in maps):
            yield (p, *[m[p] for m in maps])


def revenue_profit_chart(fin: CompanyFinancials) -> "object | None":
    """Bar (revenue) + line (PAT) trend, or None if unavailable."""
    import plotly.graph_objects as go

    pl = fin.profit_loss
    if pl is None:
        return None
    revenue = _row_by_period(pl, "sales", "revenue")
    pat = _row_by_period(pl, "net profit", "profit after tax")
    periods = [p for p in pl.periods if p in revenue or p in pat]
    if not periods:
        return None
    fig = go.Figure()
    fig.add_bar(x=periods, y=[revenue.get(p) for p in periods], name="Revenue", marker_color=_TEAL)
    fig.add_scatter(x=periods, y=[pat.get(p) for p in periods], name="PAT",
                    mode="lines+markers", line={"color": _RED, "width": 3})
    fig.update_layout(title="Revenue & PAT (₹ cr)", **_LAYOUT)
    return fig


def margins_chart(fin: CompanyFinancials) -> "object | None":
    """EBITDA / EBIT / PAT margin (%) lines, or None if unavailable."""
    import plotly.graph_objects as go

    pl = fin.profit_loss
    if pl is None:
        return None
    rev = _row_by_period(pl, "sales", "revenue")
    op = _row_by_period(pl, "operating profit")
    dep = _row_by_period(pl, "depreciation")
    pat = _row_by_period(pl, "net profit", "profit after tax")
    if not rev:
        return None
    periods = pl.periods
    fig = go.Figure()
    added = False
    if op:
        ys = [op[p] / rev[p] * 100 if p in op and p in rev and rev[p] else None for p in periods]
        fig.add_scatter(x=periods, y=ys, name="EBITDA margin", mode="lines+markers",
                        line={"color": _TEAL})
        added = True
        if dep:
            ebit_ys = [(op[p] - dep[p]) / rev[p] * 100 if p in op and p in dep and p in rev and rev[p]
                       else None for p in periods]
            fig.add_scatter(x=periods, y=ebit_ys, name="EBIT margin", mode="lines+markers",
                            line={"color": _BLUE})
    if pat:
        ys = [pat[p] / rev[p] * 100 if p in pat and p in rev and rev[p] else None for p in periods]
        fig.add_scatter(x=periods, y=ys, name="PAT margin", mode="lines+markers",
                        line={"color": _AMBER})
        added = True
    if not added:
        return None
    fig.update_layout(title="Margins (%)", **_LAYOUT)
    return fig


def returns_chart(fin: CompanyFinancials) -> "object | None":
    """ROCE & ROE (%) trend, or None if equity/earnings unavailable."""
    import plotly.graph_objects as go

    pl, bs = fin.profit_loss, fin.balance_sheet
    if pl is None or bs is None:
        return None
    op = _row_by_period(pl, "operating profit")
    dep = _row_by_period(pl, "depreciation")
    pat = _row_by_period(pl, "net profit", "profit after tax")
    eq_cap = _row_by_period(bs, "equity capital", "share capital")
    reserves = _row_by_period(bs, "reserves")
    debt = _row_by_period(bs, "borrowings", "debt")

    periods, roce, roe = [], [], []
    for p in pl.periods:
        equity = (eq_cap.get(p, 0) or 0) + (reserves.get(p, 0) or 0)
        if equity <= 0:
            continue
        periods.append(p)
        if p in op and p in dep and (equity + debt.get(p, 0)) > 0:
            roce.append((op[p] - dep[p]) / (equity + debt.get(p, 0)) * 100)
        else:
            roce.append(None)
        roe.append(pat[p] / equity * 100 if p in pat else None)
    if not periods or (not any(v is not None for v in roce) and not any(v is not None for v in roe)):
        return None
    fig = go.Figure()
    fig.add_scatter(x=periods, y=roce, name="ROCE", mode="lines+markers", line={"color": _TEAL})
    fig.add_scatter(x=periods, y=roe, name="ROE", mode="lines+markers", line={"color": _AMBER})
    fig.update_layout(title="Return ratios — ROCE vs ROE (%)", **_LAYOUT)
    return fig


def cash_conversion_chart(fin: CompanyFinancials) -> "object | None":
    """CFO / PAT (%) cash-conversion trend, or None if cash flow unavailable."""
    import plotly.graph_objects as go

    pl, cf = fin.profit_loss, fin.cash_flow
    if pl is None or cf is None:
        return None
    pat = _row_by_period(pl, "net profit", "profit after tax")
    cfo = _row_by_period(cf, "operating activity", "cash from operating")
    periods, ratios = [], []
    for p, pat_v, cfo_v in _aligned(pl.periods, pat, cfo):
        if pat_v:
            periods.append(p)
            ratios.append(cfo_v / pat_v * 100)
    if not periods:
        return None
    fig = go.Figure()
    fig.add_bar(x=periods, y=ratios, name="CFO / PAT", marker_color=_TEAL)
    fig.add_hline(y=100, line_dash="dash", line_color="#8b949e",
                  annotation_text="100% (full conversion)")
    fig.update_layout(title="Cash conversion — CFO / PAT (%)", **_LAYOUT)
    return fig


def focus_charts(fin: CompanyFinancials) -> list[tuple[str, "object"]]:
    """Return all available focus charts as (title, figure) pairs.

    Args:
        fin: Parsed company financials.

    Returns:
        Only charts whose data was available; empty list if none.
    """
    builders = [
        ("Revenue & PAT", revenue_profit_chart),
        ("Margins", margins_chart),
        ("Return ratios", returns_chart),
        ("Cash conversion", cash_conversion_chart),
    ]
    out = []
    for title, builder in builders:
        fig = builder(fin)
        if fig is not None:
            out.append((title, fig))
    logger.debug("Built %d focus chart(s) for %s", len(out), fin.symbol)
    return out
