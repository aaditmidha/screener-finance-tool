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
from screener.ui import charts, components

logger = logging.getLogger(__name__)

# Brand accent (blue, per the template) and light/dark palettes.
_ACCENT = "#2f6bff"
_ACCENT_DARK = "#1d4ed8"
_PALETTES = {
    True: {"bg": "#0b1220", "surface": "#131c31", "surface2": "#1a2540",
           "text": "#e6edf3", "muted": "#94a3b8", "border": "rgba(255,255,255,0.08)"},
    False: {"bg": "#eef2f8", "surface": "#ffffff", "surface2": "#f6f9fd",
            "text": "#1e293b", "muted": "#64748b", "border": "rgba(15,23,42,0.10)"},
}
# Gradients for the summary "cards" (like the template's credit cards).
_CARD_GRADIENTS = [
    "linear-gradient(135deg,#2f6bff,#1d4ed8)",
    "linear-gradient(135deg,#7c3aed,#5b21b6)",
    "linear-gradient(135deg,#0ea5e9,#0369a1)",
    "linear-gradient(135deg,#f43f5e,#be123c)",
]


def _inject_theme(st: Any, dark: bool) -> None:
    """Inject the theme CSS for the chosen light/dark mode."""
    p = _PALETTES[dark]
    css = f"""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"], button, input, textarea {{ font-family: 'Inter', sans-serif; }}
#MainMenu, footer, [data-testid="stToolbar"] {{ visibility: hidden; }}
.stApp {{ background: {p['bg']}; }}
/* Streamlit's fixed header bar — match the app bg so it isn't a stray light strip in dark mode. */
[data-testid="stHeader"] {{ background: {p['bg']}; }}
.block-container {{ padding-top: 2.4rem; padding-bottom: 3rem; max-width: 1240px; }}
.stApp, .stApp p, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp label, [data-testid="stMarkdownContainer"] {{ color: {p['text']}; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {{ color: {p['muted']} !important; }}

/* Brand-blue sidebar (always) */
[data-testid="stSidebar"] {{ background: linear-gradient(180deg, {_ACCENT} 0%, {_ACCENT_DARK} 100%);
  border: none; }}
[data-testid="stSidebar"] * {{ color: #ffffff !important; }}
[data-testid="stSidebar"] [data-baseweb="input"], [data-testid="stSidebar"] [data-baseweb="select"] > div {{
  background: rgba(255,255,255,0.16) !important; border: none !important; border-radius: 10px; }}
[data-testid="stSidebar"] input {{ color: #fff !important; }}
[data-testid="stSidebar"] input::placeholder {{ color: rgba(255,255,255,0.75) !important; }}

/* Card-like bordered containers */
[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 16px;
  border: 1px solid {p['border']} !important; background: {p['surface']};
  box-shadow: 0 2px 10px rgba(2,12,40,0.08); }}

/* Metric tiles */
[data-testid="stMetric"] {{ background: {p['surface2']}; border: 1px solid {p['border']};
  border-radius: 14px; padding: 12px 16px; }}
[data-testid="stMetricValue"] {{ font-weight: 700; font-size: 1.45rem; color: {p['text']}; }}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {{ color: {p['muted']} !important; }}

/* Tabs */
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {p['border']}; }}
.stTabs [data-baseweb="tab"] {{ padding: 9px 15px; border-radius: 10px 10px 0 0; font-weight: 600;
  color: {p['muted']}; }}
.stTabs [aria-selected="true"] {{ background: rgba(47,107,255,0.14); color: {_ACCENT}; }}

/* Buttons */
.stButton > button, .stDownloadButton > button {{ border-radius: 10px; font-weight: 600;
  background: {_ACCENT}; color: #fff !important; border: none; }}
.stButton > button:hover, .stDownloadButton > button:hover {{ background: {_ACCENT_DARK}; }}
.stButton > button p, .stDownloadButton > button p {{ color: #fff !important; }}

/* Summary gradient cards */
.kpi-row {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 4px 0 16px; }}
.kpi-card {{ flex: 1; min-width: 150px; border-radius: 16px; padding: 16px 18px;
  box-shadow: 0 8px 18px rgba(2,12,40,0.18); }}
.kpi-card * {{ color: #fff !important; }}
.kpi-card .kpi-label {{ font-size: 0.76rem; opacity: 0.9; letter-spacing: 0.3px; }}
.kpi-card .kpi-value {{ font-size: 1.5rem; font-weight: 800; margin-top: 3px; }}

/* Brand block in the sidebar */
.brand {{ display:flex; align-items:center; gap:10px; padding: 2px 0 12px; }}
.brand .logo {{ font-size: 1.6rem; }}
.brand .name {{ font-weight: 800; font-size: 1.1rem; line-height:1.05; }}
.brand .tag {{ font-size: 0.68rem; opacity: 0.85; }}
.overview-title {{ font-size: 1.6rem; font-weight: 800; margin-bottom: 0.1rem; }}
</style>"""
    st.markdown(css, unsafe_allow_html=True)


def _render_kpi_cards(st: Any, kpis: list[tuple[str, str]]) -> None:
    """Render the headline KPIs as coloured gradient cards (template style)."""
    cards = "".join(
        f'<div class="kpi-card" style="background:{_CARD_GRADIENTS[i % len(_CARD_GRADIENTS)]}">'
        f'<div class="kpi-label">{label}</div><div class="kpi-value">{value}</div></div>'
        for i, (label, value) in enumerate(kpis)
    )
    st.markdown(f'<div class="kpi-row">{cards}</div>', unsafe_allow_html=True)


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


def _stage_uploads(ar_files: list, q_files: list, c_files: list) -> list:
    """Write uploaded files to a temp dir and build UploadedDoc records."""
    import os
    import tempfile
    from datetime import datetime

    from screener.scraper.document_ingest import (
        ANNUAL, CONCALL, QUARTERLY, UploadedDoc, infer_fiscal_year,
    )

    limits = CONFIG["uploads"]
    current_fy = datetime.now().year
    tmpdir = tempfile.mkdtemp(prefix="screener_upload_")
    docs: list = []
    plan = [
        (ar_files, ANNUAL, limits["max_annual_reports"]),
        (q_files, QUARTERLY, limits["max_quarterly"]),
        (c_files, CONCALL, limits["max_concalls"]),
    ]
    for files, kind, limit in plan:
        for f in (files or [])[:limit]:
            path = os.path.join(tmpdir, f.name)
            with open(path, "wb") as out:
                out.write(f.getbuffer())
            year = infer_fiscal_year(f.name, default=current_fy)
            docs.append(UploadedDoc(kind=kind, fiscal_year=year, pdf_path=path, name=f.name))
    return docs


def _render_upload_tab(st: Any, service: CompanyDataService, symbol: str, name: str) -> None:
    """Upload ARs / quarterlies / concalls; extract and update the analysis."""
    import pandas as pd

    from screener.scraper import document_ingest

    limits = CONFIG["uploads"]
    st.caption("Upload the company's own PDFs — they drive the AR-enhanced Beneish, "
               "the 🧾 Annual Reports and 🎙 Management tabs. Name files with the year "
               "(e.g. `AR_FY24.pdf`, `Q1FY26.pdf`) so they're tagged correctly. "
               "Extraction uses Groq (set `GROQ_API_KEY`) with a regex fallback.")

    # Show the result of a just-completed ingest (we re-ran the whole app after
    # ingesting so every other tab re-read the fresh AR data from the DB).
    last = st.session_state.get("ingest_results")
    if last and last.get("symbol") == symbol:
        ok = sum(1 for r in last["rows"] if r["status"] == "ingested")
        st.success(f"Extracted {ok}/{len(last['rows'])} document(s). The 🧾 Annual Reports / "
                   "🎙 Management tabs and the AR-upgraded Beneish now reflect this data.")
        st.dataframe(pd.DataFrame(last["rows"]), use_container_width=True)

    ar_files = st.file_uploader(f"📄 Annual reports (up to {limits['max_annual_reports']})",
                                type="pdf", accept_multiple_files=True, key="up_ar")
    q_files = st.file_uploader(f"📊 Quarterly filings (up to {limits['max_quarterly']})",
                               type="pdf", accept_multiple_files=True, key="up_q")
    c_files = st.file_uploader(f"🎙 Concall transcripts (up to {limits['max_concalls']})",
                               type="pdf", accept_multiple_files=True, key="up_c")

    if not st.button("Extract & update analysis", key="ingest_btn", type="primary"):
        return
    docs = _stage_uploads(ar_files, q_files, c_files)
    if not docs:
        st.warning("Upload at least one PDF first.")
        return

    with st.status(f"Extracting {len(docs)} document(s)…", expanded=True) as status:
        results = document_ingest.ingest(service._session, symbol, docs, company_name=name)
        for r in results:
            status.write(f"{r['kind']} · {r['name']} → {r['status']}")
        ok = sum(1 for r in results if r["status"] == "ingested")
        status.update(label=f"Extracted {ok}/{len(results)} document(s)", state="complete")

    # Re-run the whole script so EVERY tab re-reads the freshly-ingested AR data.
    # st.tabs renders all panels in a single run, so without a rerun the other
    # tabs keep showing their pre-ingest (empty) state until the next interaction.
    st.session_state["ingest_results"] = {"symbol": symbol, "rows": results}
    st.rerun()


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


def _render_research_note_tab(st: Any, fin: CompanyFinancials, symbol: str,
                              name: str, pledge_history: list) -> None:
    """Generate and download a one-page research note (.docx)."""
    from screener.exporters import research_note

    st.caption("Generates a one-page research note (Word .docx): thesis sections (Groq), "
               "a Key Financials table and focus charts. Set `GROQ_API_KEY` for the "
               "written sections; the tables and charts render without it.")
    if not st.button("Generate research note", key="note_btn", type="primary"):
        return
    score = forensic_score.compute(fin, pledge_history=pledge_history or None)
    metrics = {c.name: c.detail for c in score.components if c.available}
    metrics["Forensic score"] = f"{score.score:.0f}/100 ({score.verdict})"

    with st.spinner("Writing note…"):
        note = research_note.generate(fin, name, symbol, metrics=metrics)
        docx_bytes = research_note.to_docx(note)

    if note.sections:
        for section in note.sections:
            st.markdown(f"**{section.heading}**")
            st.write(section.body)
    else:
        st.info("LLM sections unavailable (no GROQ_API_KEY) — the note still includes "
                "the Key Financials table and focus charts.")
    st.download_button(
        "⬇ Download research note (.docx)", data=docx_bytes,
        file_name=f"{symbol}_research_note.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


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


def _render_charts_tab(st: Any, fin: CompanyFinancials) -> None:
    """Render the interactive focus charts in a two-column grid."""
    figs = charts.focus_charts(fin)
    if not figs:
        st.info("Not enough data to plot focus charts for this company.")
        return
    cols = st.columns(2)
    for i, (_title, fig) in enumerate(figs):
        with cols[i % 2]:
            st.plotly_chart(fig, use_container_width=True)


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

    dark = st.session_state.get("dark_mode", False)
    _inject_theme(st, dark)

    engine = st.cache_resource(_cached_engine)()
    service = CompanyDataService(get_session_factory(engine)())

    @st.cache_data(ttl=300, show_spinner=False)
    def _cached_search(q: str):
        """Cache autocomplete hits so tab-click reruns don't re-query Screener."""
        return search_companies(q)

    # --- Sidebar: brand, light/dark toggle, company search ---------------- #
    symbol: str | None = None
    name: str = ""
    with st.sidebar:
        st.markdown(
            '<div class="brand"><span class="logo">◆</span>'
            '<span><span class="name">Screener Forensic</span><br>'
            '<span class="tag">Intelligence for Indian equities</span></span></div>',
            unsafe_allow_html=True,
        )
        new_dark = st.toggle("🌙 Dark mode", value=dark, key="dark_toggle")
        if new_dark != dark:
            st.session_state["dark_mode"] = new_dark
            st.rerun()
        st.markdown("---")
        query = st.text_input("🔎 Search company", placeholder="Infosys, CG Power…")
        if query:
            matches = _cached_search(query)
            if matches:
                labels = [f"{m.name} ({m.symbol})" for m in matches]
                chosen = st.selectbox("Matches", labels)
                picked = matches[labels.index(chosen)]
                symbol, name = picked.symbol, picked.name
            else:
                st.warning("No matches found.")
        st.markdown("---")
        st.caption("Forensic score · Beneish · Reverse DCF · Earnings quality · "
                   "Peer ranking · Pledge · AI tearsheet · AR intelligence")

    if not symbol:
        st.markdown('<div class="overview-title">Welcome 👋</div>', unsafe_allow_html=True)
        st.caption("Search for an Indian listed company in the sidebar to begin your analysis.")
        st.stop()

    # --- Overview header --------------------------------------------------- #
    st.markdown(f'<div class="overview-title">{name or symbol} · Overview</div>',
                unsafe_allow_html=True)
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

    # --- KPI overview strip (gradient cards) ------------------------------ #
    kpis = components.headline_kpis(fin)
    if kpis:
        _render_kpi_cards(st, kpis)

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
    (upload, annual, quarterly, ratios, operational_tab, charts_tab, peers, tearsheet,
     note_tab, screener_tab, pledge, annual_reports, management) = st.tabs(
        ["📤 Upload & Analyze", "Annual", "Quarterly", "Ratios", "Operational Data",
         "📈 Charts", "Peer Compare", "Tearsheet", "📝 Research Note", "Custom Screener",
         "🚨 Pledge", "🧾 Annual Reports", "🎙 Management"]
    )
    with upload:
        _render_upload_tab(st, service, symbol, name or fin.name)
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
    with charts_tab:
        _render_charts_tab(st, fin)
    with peers:
        _render_peer_tab(st, service, symbol)
    with tearsheet:
        _render_tearsheet_tab(st, _build_tearsheet_input(symbol, name or fin.name, fin,
                                                          pledge_history, ar_pair))
    with note_tab:
        _render_research_note_tab(st, fin, symbol, name or fin.name, pledge_history)
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
