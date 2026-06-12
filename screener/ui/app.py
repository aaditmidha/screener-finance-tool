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
import tempfile
from pathlib import Path
from typing import Any

from screener.config import CONFIG
from screener.database.engine import build_engine, get_session_factory
from screener.exporters import excel as excel_exporter
from screener.exporters.tearsheet import TearsheetInput, generate_tearsheet
from screener.models import custom_screener, working_capital as wc
from screener.models.peer_comparison import PeerComparison
from screener.scraper.acquisition import CompanyDataService, search_companies
from screener.scraper.parser import CompanyFinancials
from screener.ui import components

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Resource wiring (cached across reruns)
# --------------------------------------------------------------------------- #
def _build_service() -> CompanyDataService:
    """Construct a CompanyDataService backed by a fresh DB session."""
    engine = build_engine()
    from screener.database.models import Base
    Base.metadata.create_all(engine)
    session = get_session_factory(engine)()
    return CompanyDataService(session)


def _financials_to_excel_bytes(fin: CompanyFinancials) -> bytes:
    """Export the parsed statements to an in-memory Excel workbook.

    Args:
        fin: Parsed company financials.

    Returns:
        The workbook contents as bytes (for a Streamlit download button).
    """
    sheets: dict[str, list[dict[str, Any]]] = {}
    for name, table in (
        ("Annual PL", fin.profit_loss),
        ("Balance Sheet", fin.balance_sheet),
        ("Cash Flow", fin.cash_flow),
        ("Ratios", fin.ratios),
        ("Quarterly", fin.quarters),
    ):
        df = components.financial_table_to_df(table)
        if df.empty:
            continue
        sheets[name] = df.reset_index(names="Item").to_dict("records")

    with tempfile.TemporaryDirectory() as tmp:
        path = excel_exporter.export(sheets, "model.xlsx")
        # excel_exporter writes to its configured dir; relocate read into tmp-safe read
        data = Path(path).read_bytes()
    return data


# --------------------------------------------------------------------------- #
# Tab renderers
# --------------------------------------------------------------------------- #
def _render_statement_tab(st: Any, title: str, table: Any) -> None:
    """Render a single parsed statement as a table, or an info message."""
    df = components.financial_table_to_df(table)
    if df.empty:
        st.info(f"No {title} data available for this company.")
    else:
        st.dataframe(df, use_container_width=True)


def _render_peer_tab(st: Any, service: CompanyDataService, symbol: str) -> None:
    """Run and display the ranked peer comparison."""
    st.caption("Discovers sector peers from Screener and ranks them.")
    if not st.button("Run peer comparison", key="peer_btn"):
        return
    comparer = PeerComparison(
        company_repo=service._companies,           # reuse the service's repos
        annual_repo=service._annual,
        fetch_page=service._fetch_page,
        fetch_annual_data=service.get_annual_records,
    )
    url = service._company_url(symbol)
    try:
        with st.spinner("Comparing peers…"):
            ranked = comparer.compare(symbol, url)
        st.dataframe(ranked, use_container_width=True)
    except Exception as exc:  # surface, don't crash the app
        logger.exception("Peer comparison failed")
        st.error(f"Peer comparison failed: {exc}")


def _render_tearsheet_tab(
    st: Any, symbol: str, name: str, metrics: dict[str, Any]
) -> None:
    """Generate and display the LLM tearsheet (needs GROQ_API_KEY)."""
    st.caption("Generates a 1-page plain-English summary via the Groq API.")
    if not st.button("Generate tearsheet", key="ts_btn"):
        return
    data = TearsheetInput(symbol=symbol, name=name, metrics=metrics)
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


def _render_beneish(st: Any, fin: CompanyFinancials) -> None:
    """Render the Beneish M-Score metric with a red/green flag."""
    # Beneish needs granular inputs Screener often omits; degrade to N/A.
    emoji, colour, caption = components.beneish_flag(None)
    st.markdown(
        f"<span style='color:{colour};font-size:1.4rem'>{emoji} {caption}</span>",
        unsafe_allow_html=True,
    )


def _render_wc_heatmap(st: Any, fin: CompanyFinancials) -> None:
    """Render the Plotly working-capital heatmap, or an info message."""
    quarters = components.working_capital_quarters(fin)
    if not quarters:
        st.info("Granular working-capital data not available for this company.")
        return
    heatmap = wc.heatmap_data(quarters)
    st.plotly_chart(components.build_wc_heatmap_figure(heatmap), use_container_width=True)


# --------------------------------------------------------------------------- #
# Main app
# --------------------------------------------------------------------------- #
def main() -> None:
    """Render the full dashboard."""
    import streamlit as st

    from screener.logging_config import setup_logging
    setup_logging()

    st.set_page_config(page_title="Screener Finance Tool", layout="wide")
    st.title("Screener Finance Tool")

    service = st.cache_resource(_build_service)()

    # --- Search with autocomplete ----------------------------------------- #
    query = st.text_input("Search company", placeholder="e.g. Infosys, CG Power…")
    symbol: str | None = None
    name: str = ""
    if query:
        matches = search_companies(query)
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

    with st.spinner(f"Loading {symbol}…"):
        fin = service.refresh(symbol)

    # --- Headline: Beneish flag + WC heatmap ------------------------------ #
    left, right = st.columns([1, 2])
    with left:
        st.subheader("Earnings-manipulation check")
        _render_beneish(st, fin)
    with right:
        st.subheader("Working capital")
        _render_wc_heatmap(st, fin)

    # --- Excel download --------------------------------------------------- #
    st.download_button(
        "Download Excel model",
        data=_financials_to_excel_bytes(fin),
        file_name=f"{symbol}_model.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # --- Tabs ------------------------------------------------------------- #
    annual, quarterly, ratios, peers, tearsheet, screener_tab = st.tabs(
        ["Annual", "Quarterly", "Ratios", "Peer Compare", "Tearsheet", "Custom Screener"]
    )
    with annual:
        _render_statement_tab(st, "annual P&L", fin.profit_loss)
        _render_statement_tab(st, "balance sheet", fin.balance_sheet)
        _render_statement_tab(st, "cash flow", fin.cash_flow)
    with quarterly:
        _render_statement_tab(st, "quarterly", fin.quarters)
    with ratios:
        _render_statement_tab(st, "ratios", fin.ratios)
    with peers:
        _render_peer_tab(st, service, symbol)
    with tearsheet:
        _render_tearsheet_tab(st, symbol, name or fin.name, metrics={})
    with screener_tab:
        _render_screener_tab(st, service)


if __name__ == "__main__":
    main()
