"""Streamlit dashboard for the Screener Finance Tool.

Run with:
    streamlit run screener/ui/app.py

Features: company-name search with autocomplete, a data-freshness indicator,
tabs for Annual / Quarterly / Ratios / Peer Compare / Tearsheet, the Beneish
M-Score with a red/green flag, a Plotly working-capital heatmap, and an Excel
download button. All heavy logic lives in :mod:`screener.ui.components`,
:mod:`screener.scraper.acquisition`, and the model/exporter packages; this
module is the thin Streamlit shell that wires them together.
"""

import logging
from pathlib import Path
from typing import Any

from screener.config import CONFIG
from screener.database.engine import build_engine, ensure_schema, get_session_factory
from screener.exporters import model_workbook
from screener.exporters.tearsheet import TearsheetInput, generate_tearsheet
from screener.models import (
    beneish_adapter,
    custom_screener,
    forensic_score,
    operational,
    pledge_monitor,
    working_capital as wc,
)
from screener.models.peer_comparison import PeerComparison
from screener.scraper.acquisition import CompanyDataService, search_companies
from screener.scraper.parser import CompanyFinancials
from screener.ui import components

logger = logging.getLogger(__name__)

# Custom theme/CSS — gives the dashboard a polished fintech look rather than
# the default Streamlit chrome. Injected once per run.
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"], button, input, textarea { font-family: 'Inter', sans-serif; }

/* Hide Streamlit chrome for a product feel */
#MainMenu, footer { visibility: hidden; }
[data-testid="stToolbar"] { right: 1rem; }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1180px; }

/* Branded header */
.app-header { margin-bottom: 0.4rem; }
.app-title { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.5px; line-height: 1.1;
  background: linear-gradient(90deg, #e6edf3 0%, #2dd4bf 120%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.app-title .mark { -webkit-text-fill-color: #2dd4bf; }
.app-sub { color: #8b949e; font-size: 0.95rem; margin-top: 0.15rem; }
.app-pills { margin-top: 0.7rem; display: flex; flex-wrap: wrap; gap: 6px; }
.pill { background: rgba(45,212,191,0.10); color: #2dd4bf; border: 1px solid rgba(45,212,191,0.30);
  border-radius: 999px; padding: 3px 11px; font-size: 0.72rem; font-weight: 600; }

/* Card-like bordered containers */
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 16px;
  border-color: rgba(255,255,255,0.07) !important;
  background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
  box-shadow: 0 1px 3px rgba(0,0,0,0.25); }

/* Metric tiles */
[data-testid="stMetric"] { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06);
  border-radius: 12px; padding: 10px 14px; }
[data-testid="stMetricValue"] { font-weight: 700; font-size: 1.4rem; }
[data-testid="stMetricLabel"] { color: #8b949e; }

/* Tabs as pills */
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid rgba(255,255,255,0.06); }
.stTabs [data-baseweb="tab"] { padding: 9px 15px; border-radius: 9px 9px 0 0; font-weight: 600; }
.stTabs [aria-selected="true"] { background: rgba(45,212,191,0.12); color: #2dd4bf; }

/* Buttons */
.stButton > button, .stDownloadButton > button { border-radius: 10px; font-weight: 600;
  border: 1px solid rgba(45,212,191,0.35); }
.stDataFrame { border-radius: 10px; overflow: hidden; }
</style>
"""


def _inject_css(st: Any) -> None:
    """Inject the custom theme CSS once."""
    st.markdown(_CSS, unsafe_allow_html=True)


def _render_header(st: Any) -> None:
    """Render the branded gradient header with capability pills."""
    pills = ["Forensic score", "Beneish M-Score", "Reverse DCF", "Earnings quality",
             "Peer ranking", "Pledge monitor", "AI tearsheet", "Annual-report intelligence"]
    pill_html = "".join(f'<span class="pill">{p}</span>' for p in pills)
    st.markdown(
        f"""
        <div class="app-header">
          <div class="app-title"><span class="mark">◆</span> Screener Forensic <span style="font-weight:600;">Intelligence</span></div>
          <div class="app-sub">Beyond the numbers — forensic accounting, valuation and AR intelligence for Indian equities.</div>
          <div class="app-pills">{pill_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Resource wiring (cached across reruns)
# --------------------------------------------------------------------------- #
def _cached_engine():
    """Build and migrate the engine once (safe to share across sessions).

    The engine is cached as a Streamlit resource; a *fresh session* is created
    per run (SQLAlchemy Sessions are not thread-safe and must not be shared
    across Streamlit's per-user threads).
    """
    engine = build_engine()
    ensure_schema(engine)
    return engine


def _financials_to_excel_bytes(fin: CompanyFinancials, ar_rows: list, annual_rows: list) -> bytes:
    """Export the template-style model workbook as in-memory bytes.

    Args:
        fin: Parsed (and notes-enriched) company financials.
        ar_rows: AR-extracted rows (adds AR sheets when present).
        annual_rows: Stored annual rows (enables the discrepancy sheet).

    Returns:
        The workbook contents as bytes (for a Streamlit download button).
    """
    return model_workbook.to_bytes(fin, ar_rows=ar_rows or None, annual_rows=annual_rows or None)


# --------------------------------------------------------------------------- #
# Tab renderers
# --------------------------------------------------------------------------- #
def _render_statement_tab(st: Any, title: str, table: Any) -> None:
    """Render a single parsed statement as a formatted table, or an info message."""
    df = components.financial_table_to_df(table)
    if df.empty:
        st.info(f"No {title} data available for this company.")
    else:
        st.dataframe(components.style_statement_df(df), use_container_width=True)


def _render_peer_tab(st: Any, service: CompanyDataService, symbol: str) -> None:
    """Run and display the ranked peer comparison."""
    st.caption("Discovers sector peers from Screener's peer table and ranks them "
               "by ROCE, ROE, revenue growth and a composite score.")
    if not st.button("Run peer comparison", key="peer_btn"):
        return
    comparer = PeerComparison(
        company_repo=service._companies,           # reuse the service's repos
        annual_repo=service._annual,
        discover_peers=service.discover_peer_symbols,
        fetch_annual_data=service.get_annual_records,
    )
    progress = st.progress(0.0, text="Discovering peers…")

    def _on_progress(sym: str, index: int, total: int) -> None:
        progress.progress(index / total, text=f"Fetching {sym} ({index}/{total})…")

    try:
        ranked = comparer.compare(symbol, on_progress=_on_progress)
        progress.empty()
        st.dataframe(ranked, use_container_width=True)
    except ValueError as exc:                       # no comparable data gathered
        progress.empty()
        st.warning(f"No peer data available: {exc}")
    except Exception as exc:  # surface, don't crash the app
        progress.empty()
        logger.exception("Peer comparison failed")
        st.error(f"Peer comparison failed: {exc}")


def _render_annual_reports_tab(st: Any, service: CompanyDataService, symbol: str) -> None:
    """Surface AR-extracted data: exact figures, discrepancy, guidance, risks."""
    import pandas as pd

    from screener.models import ar_insights

    company = service._companies.get_by_symbol(symbol)
    ar_rows = service._ar.for_company(company.id) if company else []
    if not ar_rows:
        st.info(
            "No Annual-Report data extracted yet. The AR pipeline runs **locally** "
            "(Playwright + Groq) — run it to populate exact figures, then they "
            "appear here and upgrade the Beneish score. See the README."
        )
        return

    annual_rows = service._annual.for_company(company.id)
    revenue_by_year = {r.fiscal_year_end.year: r.revenue for r in annual_rows if r.revenue}

    st.caption("Exact figures parsed from the company's annual reports.")
    exact = {
        label: [getattr(r, attr, None) for r in ar_rows]
        for label, attr in [("Revenue", "revenue"), ("PAT", "pat"), ("CFO", "cfo"),
                            ("Trade receivables", "trade_receivables"),
                            ("Total assets", "total_assets"), ("Total debt", "total_debt")]
    }
    years = [str(r.fiscal_year) for r in ar_rows]
    st.dataframe(pd.DataFrame(exact, index=years).T, use_container_width=True)

    cells = ar_insights.discrepancies(ar_rows, annual_rows)
    worst = ar_insights.worst_discrepancies(cells)
    if any(c.severity == "large" for c in worst):
        st.error("⚠️ Large Screener-vs-AR discrepancy on a key metric — verify before relying on it.")
    if cells:
        st.subheader("Screener vs Annual Report")
        st.dataframe(pd.DataFrame(
            [{"Metric": c.metric, "Year": c.year, "Screener": c.screener,
              "AR": c.ar, "Diff %": None if c.diff_pct is None else round(c.diff_pct * 100, 1),
              "Severity": c.severity} for c in cells]
        ), use_container_width=True)

    scorecard = ar_insights.guidance_scorecard(ar_rows, revenue_by_year)
    if scorecard is not None:
        st.subheader("Management guidance vs delivery")
        st.metric("Credibility score", f"{scorecard.score:.1f}/10",
                  help=f"hit rate {scorecard.hit_rate:.0%}, bias {scorecard.bias:+.0%}")

    timeline = ar_insights.risk_timeline(ar_rows)
    if timeline:
        st.subheader("Key-risk timeline")
        st.dataframe(pd.DataFrame(
            [{"Risk": e.risk, "First": e.first_year, "Last": e.last_year,
              "Times mentioned": e.frequency} for e in timeline]
        ), use_container_width=True)


def _render_management_tab(st: Any, service: CompanyDataService, symbol: str,
                           ar_rows: list, annual_rows: list) -> None:
    """Render the management-credibility scorecard (guidance vs delivery)."""
    from screener.models import ar_insights

    st.caption("Scores management's guidance against what was actually delivered, "
               "from Annual-Report data.")
    if not ar_rows:
        st.info("No Annual-Report data yet. Run the AR pipeline locally to extract "
                "management guidance, then credibility scoring appears here.")
        return

    revenue_by_year = {r.fiscal_year_end.year: r.revenue for r in annual_rows if r.revenue}
    scorecard = ar_insights.guidance_scorecard(ar_rows, revenue_by_year)
    if scorecard is None:
        st.info("No quantified guidance could be paired with actuals yet "
                "(needs guidance from one year and the following year's result).")
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("Credibility", f"{scorecard.score:.1f}/10", scorecard.rating)
    col2.metric("Hit rate", f"{scorecard.hit_rate:.0%}")
    col3.metric("Bias", f"{scorecard.bias:+.0%}",
                help="positive = under-promises/over-delivers")
    st.caption(f"Based on {scorecard.evaluated} guidance item(s) with known outcomes.")


def _render_operational_tab(st: Any, fin: CompanyFinancials) -> None:
    """Render derived operational-efficiency metrics, or an info message."""
    st.caption("Operating-efficiency metrics derived from the statements: margins, "
               "turnover ratios, working-capital days and cash conversion.")
    op = operational.compute(fin)
    df = components.operational_to_df(op)
    if df.empty:
        st.info("Not enough data to derive operational metrics for this company.")
    else:
        st.dataframe(df, use_container_width=True)


def _build_tearsheet_input(
    symbol: str, name: str, fin: CompanyFinancials, pledge_history: list, ar_pair: tuple
) -> TearsheetInput:
    """Assemble a rich tearsheet input from the computed forensic signals."""
    score = forensic_score.compute(fin, pledge_history=pledge_history or None)
    metrics: dict[str, Any] = {
        "forensic_score": f"{score.score:.0f}/100 ({score.verdict})",
    }
    for comp in score.components:
        if comp.available:
            metrics[comp.name] = comp.detail

    ar_context: dict[str, Any] = {}
    ar_current, _ar_prior = ar_pair
    if ar_current is not None:
        import json
        for label, attr in [("Revenue", "revenue"), ("PAT", "pat"), ("CFO", "cfo"),
                            ("Trade receivables", "trade_receivables")]:
            val = getattr(ar_current, attr, None)
            if val is not None:
                ar_context[f"{label} (AR FY{ar_current.fiscal_year})"] = val
        if getattr(ar_current, "guidance_raw_text", None):
            ar_context["Management guidance"] = ar_current.guidance_raw_text
        risks = getattr(ar_current, "key_risks", None)
        if risks:
            try:
                ar_context["Key risks"] = ", ".join(json.loads(risks)[:3])
            except (json.JSONDecodeError, TypeError):
                pass
    return TearsheetInput(symbol=symbol, name=name, metrics=metrics, ar_context=ar_context)


def _render_tearsheet_tab(
    st: Any, data: TearsheetInput
) -> None:
    """Generate and display the LLM tearsheet (needs GROQ_API_KEY)."""
    st.caption("Generates a 1-page plain-English summary via the Groq API.")
    st.caption("✅ Enhanced with Annual Report data." if data.ar_enhanced
               else "Using Screener data only (run the AR pipeline locally to enrich).")
    if not st.button("Generate tearsheet", key="ts_btn"):
        return
    try:
        with st.spinner("Writing tearsheet…"):
            sheet = generate_tearsheet(data, make_pdf=True)
        st.markdown(sheet.summary)
        if sheet.pdf_path:
            st.download_button(
                "Download tearsheet PDF",
                data=Path(sheet.pdf_path).read_bytes(),
                file_name=Path(sheet.pdf_path).name,
                mime="application/pdf",
            )
    except Exception as exc:
        logger.exception("Tearsheet generation failed")
        st.error(f"Tearsheet generation failed: {exc}")


def _render_screener_tab(st: Any, service: CompanyDataService) -> None:
    """Render the custom-formula screener over all persisted companies."""
    st.caption(
        "Rank every downloaded company by your own formula, e.g. "
        "`(pat / revenue) * revenue_growth_3yr`. Variables: revenue, pat, ebit, "
        "equity, debt, eps, total_assets, roe, roce, debt_to_equity, pat_margin, "
        "ebit_margin, revenue_growth_3yr, pat_growth_3yr."
    )
    formula = st.text_input("Formula", value="roce * revenue_growth_3yr", key="formula")
    if not st.button("Run screen", key="screen_btn"):
        return
    companies = {
        c.symbol: service._annual.for_company(c.id) for c in service._companies.all()
    }
    companies = {sym: rows for sym, rows in companies.items() if rows}
    if not companies:
        st.info("No companies in the database yet — analyse a few first.")
        return
    try:
        ranked = custom_screener.screen(companies, formula)
        st.dataframe(ranked, use_container_width=True)
    except (custom_screener.FormulaError, ValueError) as exc:
        st.error(str(exc))


def _render_forensic(st: Any, fin: CompanyFinancials, pledge_history: list) -> None:
    """Render the composite forensic health score as a gauge + component tiles."""
    score = forensic_score.compute(fin, pledge_history=pledge_history or None)
    with st.container(border=True):
        gauge_col, comp_col = st.columns([1, 1.3])
        with gauge_col:
            st.markdown("##### 🚦 Forensic health")
            st.plotly_chart(components.build_forensic_gauge(score), use_container_width=True)
        with comp_col:
            st.markdown("##### Component breakdown")
            grid = st.columns(2)
            for i, comp in enumerate(score.components):
                with grid[i % 2]:
                    value = f"{comp.score:.0f}/100" if comp.available else "—"
                    st.metric(comp.name.split(" (")[0], value, help=comp.detail)


def _render_beneish(st: Any, fin: CompanyFinancials,
                    ar_pair: tuple = (None, None)) -> None:
    """Render the Beneish M-Score with a red/green flag and data disclosure."""
    ar_current, ar_prior = ar_pair
    sourcing = beneish_adapter.from_financials(fin, ar_current=ar_current, ar_prior=ar_prior)
    result = sourcing.result if sourcing else None
    emoji, colour, caption = components.beneish_flag(result)
    st.markdown(
        f"<span style='color:{colour};font-size:1.4rem'>{emoji} {caption}</span>",
        unsafe_allow_html=True,
    )
    if sourcing:
        st.caption(f"Computed on {sourcing.periods[0]} → {sourcing.periods[1]} annuals.")
        if sourcing.exact_ar:
            st.caption(f"✅ Exact from Annual Report: {', '.join(sourcing.exact_ar)}.")
        note = components.data_quality_note(sourcing.approximated, sourcing.missing)
        if note:
            st.caption(f"ℹ️ {note}. Results directionally accurate — see DECISIONS.md §7.1.")


def _render_pledge_tab(st: Any, service: CompanyDataService) -> None:
    """Render the promoter pledge monitor from the last fetched page."""
    if not service.last_html:
        st.info("Load a company first.")
        return
    history = pledge_monitor.parse_pledge_history(service.last_html)
    if not history:
        st.info("No promoter pledge data available for this company on Screener.")
        return

    result = pledge_monitor.analyze(history)
    emoji, colour, caption = components.pledge_badge(result)
    st.markdown(
        f"<span style='color:{colour};font-size:1.4rem'>{emoji} {caption}</span>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(components.build_pledge_figure(history), use_container_width=True)

    for period, threshold in result.crossings:
        message = f"Pledge crossed {threshold:.0f}% in {period}"
        if threshold >= pledge_monitor._cfg["critical_pct"]:
            st.error(message)
        else:
            st.warning(message)
    if not result.crossings:
        st.success("No threshold crossings in the available history.")


def _render_wc_heatmap(st: Any, fin: CompanyFinancials) -> None:
    """Render the Plotly working-capital heatmap, or an info message."""
    quarters = components.working_capital_quarters(fin)
    if not quarters:
        st.info("Granular working-capital data not available for this company.")
        return
    # Periods here are fiscal years parsed from the annual BS → annual days.
    heatmap = wc.heatmap_data(quarters, days=CONFIG["working_capital"]["days_per_year"])
    st.plotly_chart(components.build_wc_heatmap_figure(heatmap), use_container_width=True)


# --------------------------------------------------------------------------- #
# Main app
# --------------------------------------------------------------------------- #
def main() -> None:
    """Render the full dashboard."""
    import streamlit as st

    from screener.logging_config import setup_logging
    setup_logging()

    st.set_page_config(page_title="Screener Forensic Intelligence", layout="wide", page_icon="◆")
    _inject_css(st)
    _render_header(st)

    engine = st.cache_resource(_cached_engine)()
    service = CompanyDataService(get_session_factory(engine)())

    # --- Search with autocomplete ----------------------------------------- #
    @st.cache_data(ttl=300, show_spinner=False)
    def _cached_search(q: str):
        """Cache autocomplete hits so tab-click reruns don't re-query Screener."""
        return search_companies(q)

    query = st.text_input("Search company", placeholder="e.g. Infosys, CG Power…")
    symbol: str | None = None
    name: str = ""
    if query:
        matches = _cached_search(query)
        if matches:
            labels = [f"{m.name} ({m.symbol})" for m in matches]
            chosen = st.selectbox("Matches", labels)
            picked = matches[labels.index(chosen)]
            symbol, name = picked.symbol, picked.name
        else:
            st.warning("No matches found.")

    if not symbol:
        st.stop()

    # --- Freshness indicator ---------------------------------------------- #
    st.caption(f"Data freshness: {components.format_freshness(service.freshness(symbol))}")

    # Refresh once per selected symbol; tab clicks rerun the script but must
    # not hammer Screener with repeat fetches. The service is rebuilt each run
    # (fresh session), so the page HTML is cached in session_state and
    # re-hydrated onto the service for the pledge/peer features.
    if st.session_state.get("loaded_symbol") != symbol:
        with st.spinner(f"Loading {symbol}…"):
            st.session_state["fin"] = service.refresh(symbol)
            st.session_state["last_html"] = service.last_html
            st.session_state["loaded_symbol"] = symbol
    fin = st.session_state["fin"]
    service.last_html = st.session_state.get("last_html")
    pledge_history = pledge_monitor.parse_pledge_history(service.last_html or "")
    ar_pair = service.latest_ar_pair(symbol)

    # --- KPI overview strip ----------------------------------------------- #
    kpis = components.headline_kpis(fin)
    if kpis:
        cols = st.columns(len(kpis))
        for col, (label, value) in zip(cols, kpis):
            col.metric(label, value)

    # --- Headline: composite forensic score ------------------------------- #
    _render_forensic(st, fin, pledge_history)

    # --- Beneish flag + WC heatmap ---------------------------------------- #
    left, right = st.columns([1, 2])
    with left:
        with st.container(border=True):
            st.markdown("##### 🔎 Earnings-manipulation check")
            _render_beneish(st, fin, ar_pair=ar_pair)
    with right:
        with st.container(border=True):
            st.markdown("##### 💧 Working capital")
            _render_wc_heatmap(st, fin)

    # --- Excel download --------------------------------------------------- #
    company = service._companies.get_by_symbol(symbol)
    ar_rows = service._ar.for_company(company.id) if company else []
    annual_rows = service._annual.for_company(company.id) if company else []
    st.download_button(
        "Download Excel model",
        data=_financials_to_excel_bytes(fin, ar_rows, annual_rows),
        file_name=f"{symbol}_model.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # --- Tabs ------------------------------------------------------------- #
    (annual, quarterly, ratios, operational_tab, peers, tearsheet,
     screener_tab, pledge, annual_reports, management) = st.tabs(
        ["Annual", "Quarterly", "Ratios", "Operational Data", "Peer Compare",
         "Tearsheet", "Custom Screener", "🚨 Pledge", "🧾 Annual Reports", "🎙 Management"]
    )
    with annual:
        _render_statement_tab(st, "annual P&L", fin.profit_loss)
        _render_statement_tab(st, "balance sheet", fin.balance_sheet)
        _render_statement_tab(st, "cash flow", fin.cash_flow)
    with quarterly:
        _render_statement_tab(st, "quarterly", fin.quarters)
    with ratios:
        _render_statement_tab(st, "ratios", fin.ratios)
    with operational_tab:
        _render_operational_tab(st, fin)
    with peers:
        _render_peer_tab(st, service, symbol)
    with tearsheet:
        _render_tearsheet_tab(st, _build_tearsheet_input(symbol, name or fin.name, fin,
                                                          pledge_history, ar_pair))
    with screener_tab:
        _render_screener_tab(st, service)
    with pledge:
        _render_pledge_tab(st, service)
    with annual_reports:
        _render_annual_reports_tab(st, service, symbol)
    with management:
        _render_management_tab(st, service, symbol, ar_rows, annual_rows)


if __name__ == "__main__":
    main()
