"""One-page research note generator (Word .docx), matching the meet-note format.

Mirrors the reference analyst notes (e.g. the POCL meet note): a title, a few
thematic thesis sections (heading + paragraph), a **Key Financials** table, a
**peer comparison** table, and embedded **focus charts** (revenue/PAT trend and
margins). The thesis prose is written by the LLM (Groq, via
:mod:`screener.llm`) from the company's own figures; everything else is computed
and never fabricated.

The LLM client is injectable, so section generation is unit-tested without a
key, and the chart/table/docx builders are pure.
"""

import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from screener.llm import ChatClient, LLMError, chat
from screener.scraper.parser import CompanyFinancials, FinancialTable

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_SECTIONS_SYSTEM = """You are a buy-side equity analyst writing a concise one-page \
research / meet note for a portfolio manager. Using ONLY the data provided, \
return a JSON array of 4-6 sections, each {"heading": "...", "body": "..."}. \
Cover, in order: the business and capacity, growth drivers, margins and returns, \
key risks, and a final "Valuation & View" section. Each body is 50-90 words, \
specific with the numbers given, plain English, no markdown, no preamble. Never \
invent data not provided."""

_SECTIONS_USER = """Write the note for {name} ({symbol}).

{context}

Return ONLY the JSON array of sections."""


@dataclass
class NoteSection:
    """One thesis section of the note."""

    heading: str
    body: str


@dataclass
class ResearchNote:
    """The assembled note content (before rendering)."""

    symbol: str
    name: str
    sections: list[NoteSection] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    key_financials: list[tuple[str, list[float | None], str]] = field(default_factory=list)
    peer_columns: list[str] = field(default_factory=list)
    peer_rows: list[list[Any]] = field(default_factory=list)


def _latest_row(table: FinancialTable | None, *needles: str) -> list[float | None] | None:
    if table is None:
        return None
    for needle in needles:
        row = table.row(needle)
        if row:
            return row
    return None


def _ratio_series(num: list[float | None] | None,
                  den: list[float | None] | None) -> list[float | None]:
    if num is None or den is None:
        return []
    return [
        (n / d) if n is not None and d not in (None, 0) else None
        for n, d in zip(num, den)
    ]


def key_financials(fin: CompanyFinancials) -> tuple[list[str], list[tuple[str, list, str]]]:
    """Compute the Key Financials table (label, per-period values, fmt).

    Args:
        fin: Parsed company financials.

    Returns:
        (period labels, rows) where each row is (label, values, fmt) with fmt in
        {"num", "pct"}; empty when there is no P&L.
    """
    pl, bs = fin.profit_loss, fin.balance_sheet
    if pl is None:
        return [], []
    periods = pl.periods
    revenue = _latest_row(pl, "sales", "revenue")
    op_profit = _latest_row(pl, "operating profit")
    dep = _latest_row(pl, "depreciation")
    pat = _latest_row(pl, "net profit", "profit after tax")
    eps = _latest_row(pl, "eps")
    ebit = ([o - d if o is not None and d is not None else None
             for o, d in zip(op_profit, dep)] if op_profit and dep else None)

    rows: list[tuple[str, list, str]] = []
    def add(label, values, fmt):
        if values and any(v is not None for v in values):
            rows.append((label, list(values), fmt))

    add("Revenue", revenue, "num")
    add("EBITDA", op_profit, "num")
    add("EBITDA margin %", _ratio_series(op_profit, revenue), "pct")
    add("EBIT", ebit, "num")
    add("PAT", pat, "num")
    add("PAT margin %", _ratio_series(pat, revenue), "pct")
    add("EPS", eps, "num")
    return periods, rows


def focus_charts(periods: list[str],
                 key_rows: list[tuple[str, list, str]]) -> list[tuple[str, bytes]]:
    """Render focus charts as PNG bytes from the key-financials rows.

    Produces a Revenue/PAT trend chart and a margins chart where the data
    exists. Uses the non-interactive Agg backend.

    Args:
        periods: Period labels (x-axis).
        key_rows: Output of :func:`key_financials`.

    Returns:
        List of (chart title, PNG bytes); empty if nothing plottable.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_label = {label: values for label, values, _fmt in key_rows}
    charts: list[tuple[str, bytes]] = []

    def _png(fig) -> bytes:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    def _clean(values):
        return [(v if v is not None else float("nan")) for v in values]

    if "Revenue" in by_label or "PAT" in by_label:
        fig, ax = plt.subplots(figsize=(5.2, 2.6))
        x = range(len(periods))
        if "Revenue" in by_label:
            ax.bar(x, _clean(by_label["Revenue"]), color="#2dd4bf", label="Revenue")
        if "PAT" in by_label:
            ax.plot(x, _clean(by_label["PAT"]), color="#c0392b", marker="o", label="PAT")
        ax.set_xticks(list(x)); ax.set_xticklabels(periods, rotation=45, ha="right", fontsize=7)
        ax.set_title("Revenue & PAT", fontsize=10); ax.legend(fontsize=7)
        ax.spines[["top", "right"]].set_visible(False)
        charts.append(("Revenue & PAT", _png(fig)))

    margin_labels = [lbl for lbl in ("EBITDA margin %", "PAT margin %") if lbl in by_label]
    if margin_labels:
        fig, ax = plt.subplots(figsize=(5.2, 2.6))
        x = range(len(periods))
        for lbl in margin_labels:
            ax.plot(x, [v * 100 if v is not None else float("nan") for v in by_label[lbl]],
                    marker="o", label=lbl.replace(" %", ""))
        ax.set_xticks(list(x)); ax.set_xticklabels(periods, rotation=45, ha="right", fontsize=7)
        ax.set_title("Margins (%)", fontsize=10); ax.legend(fontsize=7)
        ax.spines[["top", "right"]].set_visible(False)
        charts.append(("Margins", _png(fig)))

    return charts


def build_sections(name: str, symbol: str, context: str,
                   client: ChatClient | None = None) -> list[NoteSection]:
    """Generate the thesis sections via the LLM (regex-free JSON parse).

    Args:
        name: Company name.
        symbol: Ticker.
        context: Serialised data block for the model.
        client: Optional injected chat client.

    Returns:
        Parsed NoteSections. Empty list if the LLM is unavailable or the
        response can't be parsed (the note still renders with tables/charts).
    """
    try:
        raw = chat(_SECTIONS_SYSTEM,
                   _SECTIONS_USER.format(name=name, symbol=symbol, context=context),
                   client=client, temperature=0.3, max_tokens=1400)
        payload = json.loads(_FENCE_RE.sub("", raw).strip())
    except (LLMError, json.JSONDecodeError) as exc:
        logger.warning("Section generation unavailable (%s)", exc)
        return []
    sections = []
    for item in payload if isinstance(payload, list) else []:
        heading, body = item.get("heading"), item.get("body")
        if heading and body:
            sections.append(NoteSection(heading=str(heading), body=str(body)))
    return sections


def _serialise_context(fin: CompanyFinancials, metrics: dict[str, Any] | None) -> str:
    """Build the data block the LLM writes from."""
    periods, rows = key_financials(fin)
    lines = ["## Key financials (oldest → newest: " + ", ".join(periods) + ")"]
    for label, values, fmt in rows:
        rendered = ["—" if v is None else (f"{v*100:.1f}%" if fmt == "pct" else f"{v:,.0f}")
                    for v in values]
        lines.append(f"- {label}: {', '.join(rendered)}")
    if metrics:
        lines.append("\n## Forensic / quality metrics")
        for k, v in metrics.items():
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def generate(fin: CompanyFinancials, name: str, symbol: str,
             metrics: dict[str, Any] | None = None,
             peer_ranking: Any = None,
             client: ChatClient | None = None) -> ResearchNote:
    """Assemble the full research-note content for a company.

    Args:
        fin: Parsed company financials.
        name: Company name.
        symbol: Ticker.
        metrics: Optional forensic/quality metrics for the prose + context.
        peer_ranking: Optional ranked peer DataFrame.
        client: Optional injected chat client.

    Returns:
        A populated :class:`ResearchNote`.
    """
    periods, rows = key_financials(fin)
    sections = build_sections(name, symbol, _serialise_context(fin, metrics), client=client)
    note = ResearchNote(symbol=symbol, name=name, sections=sections,
                        periods=periods, key_financials=rows)
    if peer_ranking is not None and not peer_ranking.empty:
        note.peer_columns = ["Symbol", *[str(c) for c in peer_ranking.columns]]
        note.peer_rows = [[idx, *list(r)] for idx, r in
                          zip(peer_ranking.index, peer_ranking.round(3).values.tolist())]
    logger.info("Built research note for %s: %d sections, %d fin rows",
                symbol, len(sections), len(rows))
    return note


def _fmt_cell(value: float | None, fmt: str) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%" if fmt == "pct" else f"{value:,.0f}"


def to_docx(note: ResearchNote) -> bytes:
    """Render a :class:`ResearchNote` to a .docx and return the bytes.

    Args:
        note: The assembled note content.

    Returns:
        The .docx file contents.
    """
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    title = doc.add_heading(f"{note.name} ({note.symbol})", level=0)
    title.runs[0].font.color.rgb = RGBColor(0x10, 0x6B, 0x5E)
    doc.add_paragraph("Research note — auto-generated from uploaded filings & disclosures.").italic = True

    for section in note.sections:
        doc.add_heading(section.heading, level=2)
        doc.add_paragraph(section.body)

    if note.key_financials:
        doc.add_heading("Key financials", level=2)
        table = doc.add_table(rows=1, cols=len(note.periods) + 1)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        hdr[0].text = "Metric"
        for i, period in enumerate(note.periods, start=1):
            hdr[i].text = period
        for label, values, fmt in note.key_financials:
            cells = table.add_row().cells
            cells[0].text = label
            for i, value in enumerate(values, start=1):
                if i < len(cells):
                    cells[i].text = _fmt_cell(value, fmt)

    charts = focus_charts(note.periods, note.key_financials)
    if charts:
        doc.add_heading("Focus charts", level=2)
        for chart_title, png in charts:
            doc.add_paragraph(chart_title).runs[0].bold = True
            doc.add_picture(io.BytesIO(png), width=Inches(5.5))

    if note.peer_rows:
        doc.add_heading("Peer comparison", level=2)
        table = doc.add_table(rows=1, cols=len(note.peer_columns))
        table.style = "Light Grid Accent 1"
        for i, col in enumerate(note.peer_columns):
            table.rows[0].cells[i].text = str(col)
        for row in note.peer_rows:
            cells = table.add_row().cells
            for i, value in enumerate(row):
                if i < len(cells):
                    cells[i].text = str(value)

    doc.add_paragraph()
    disclaimer = doc.add_paragraph(
        "For research and education only. Not investment advice. Figures are "
        "auto-extracted and should be verified against source filings.")
    disclaimer.runs[0].font.size = Pt(8)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
