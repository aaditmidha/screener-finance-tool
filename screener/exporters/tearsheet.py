"""LLM-generated 1-page investment tearsheet (PDF + Streamlit).

Feeds scraped financials and calculated metrics to an LLM and asks for a
plain-English, one-page investment summary covering four fixed sections:
**Trend Analysis, Red Flags, Peer Comparison, Overall View**. The result can be
exported to PDF and/or rendered as a Streamlit component.

Provider policy: this project uses **Groq** for all LLM calls and never the
Anthropic/Claude API (to keep the tool free to run). :func:`resolve_provider`
enforces this even if config or a caller asks for something else. The Groq API
key is read from the env var named in ``llm.api_key_env`` — never hardcoded.

The LLM client is injectable, so prompt construction, PDF export and the
Streamlit component are all unit-tested without any network call or API key.
"""

import html as _html
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_llm_cfg = CONFIG["llm"]

_SYSTEM_PROMPT = """You are a buy-side equity analyst writing for a portfolio manager.
Using ONLY the data provided, write a concise, plain-English investment tearsheet
that fits on a single page. Use exactly these four section headings, in this order:

1. Trend Analysis — how revenue, margins and returns have moved over time.
2. Red Flags — accounting-quality or balance-sheet concerns; say "None material" if so.
3. Peer Comparison — how the company ranks against the peers provided.
4. Overall View — a balanced concluding judgement. Do not give buy/sell price targets.

Rules:
- Be specific and reference the numbers given; never invent data not provided.
- Keep it under ~450 words total. No preamble, no disclaimers, no markdown tables.
- Write for a smart non-specialist: explain jargon briefly where used."""


class TearsheetError(Exception):
    """Raised when a tearsheet cannot be generated (e.g. missing API key)."""


class ChatClient(Protocol):
    """Structural type for the subset of the Groq client we use."""

    chat: Any  # client.chat.completions.create(...)


@dataclass
class TearsheetInput:
    """Everything the LLM needs to write the summary.

    Attributes:
        symbol: Company ticker.
        name: Company display name.
        financials: Headline scraped figures/trends (free-form mapping).
        metrics: Calculated model outputs (Beneish, earnings quality, etc.).
        peer_ranking: Optional ranked peer-comparison frame.
    """

    symbol: str
    name: str
    financials: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    peer_ranking: pd.DataFrame | None = None
    ar_context: dict[str, Any] = field(default_factory=dict)   # exact Annual-Report data

    @property
    def ar_enhanced(self) -> bool:
        """True if exact Annual-Report context was supplied."""
        return bool(self.ar_context)


@dataclass
class Tearsheet:
    """A generated tearsheet and where it was written."""

    symbol: str
    name: str
    summary: str
    pdf_path: Path | None = None


def resolve_provider() -> str:
    """Return the LLM provider to use — always ``"groq"`` by project policy.

    If config requests anything other than Groq, a warning is logged and Groq
    is used regardless (the project must stay on the free Groq tier).

    Returns:
        The string ``"groq"``.
    """
    configured = str(_llm_cfg.get("provider", "groq")).lower()
    if configured != "groq":
        logger.warning(
            "LLM provider %r requested but project policy forces Groq; ignoring.",
            configured,
        )
    return "groq"


def _build_client() -> ChatClient:
    """Construct a Groq client using the API key from the configured env var.

    Returns:
        An initialised Groq client.

    Raises:
        TearsheetError: If the Groq package is missing or the key is unset.
    """
    resolve_provider()
    key_env = _llm_cfg["api_key_env"]
    api_key = os.environ.get(key_env)
    if not api_key:
        raise TearsheetError(
            f"Environment variable {key_env!r} is not set; cannot call the Groq API."
        )
    try:
        from groq import Groq
    except ImportError as exc:
        raise TearsheetError("The 'groq' package is not installed.") from exc
    return Groq(api_key=api_key)


def _format_block(title: str, payload: Any) -> str:
    """Render one labelled data block for the user prompt.

    Args:
        title: Section heading.
        payload: A dict, DataFrame, or scalar to render beneath the heading.

    Returns:
        A formatted multi-line string (empty if payload is empty/None).
    """
    if payload is None:
        return ""
    lines = [f"## {title}"]
    if isinstance(payload, pd.DataFrame):
        if payload.empty:
            return ""
        lines.append(payload.round(4).to_string())
    elif isinstance(payload, dict):
        if not payload:
            return ""
        for key, value in payload.items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append(str(payload))
    return "\n".join(lines)


def build_prompt(data: TearsheetInput) -> tuple[str, str]:
    """Build the (system, user) prompt pair for the summary request.

    Args:
        data: The tearsheet inputs.

    Returns:
        A tuple of (system prompt, user prompt). The system prompt fixes the
        four required sections; the user prompt carries the serialised data.
    """
    blocks = [
        f"Company: {data.name} ({data.symbol})",
        _format_block("Annual Report data (exact figures)", data.ar_context),
        _format_block("Financials", data.financials),
        _format_block("Calculated Metrics", data.metrics),
        _format_block("Peer Ranking", data.peer_ranking),
    ]
    user_prompt = "\n\n".join(b for b in blocks if b)
    return _SYSTEM_PROMPT, user_prompt


def generate_summary(data: TearsheetInput, client: ChatClient | None = None) -> str:
    """Call the LLM and return the plain-English tearsheet text.

    Args:
        data: The tearsheet inputs.
        client: An optional pre-built chat client (injected for testing). When
            omitted, a Groq client is constructed from the environment.

    Returns:
        The generated summary text.

    Raises:
        TearsheetError: If the client cannot be built or the response is empty.
    """
    client = client or _build_client()
    system_prompt, user_prompt = build_prompt(data)

    response = client.chat.completions.create(
        model=_llm_cfg["model"],
        temperature=_llm_cfg["temperature"],
        max_tokens=_llm_cfg["max_tokens"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    try:
        text = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        raise TearsheetError(f"Malformed LLM response: {exc}") from exc

    if not text or not text.strip():
        raise TearsheetError("LLM returned an empty summary.")
    summary = text.strip()
    logger.info("Generated tearsheet summary for %s (%d chars)", data.symbol, len(summary))
    return summary


def to_pdf(
    summary: str,
    data: TearsheetInput,
    out_dir: Path | None = None,
    filename: str | None = None,
) -> Path:
    """Render *summary* to a one-page PDF and return its path.

    Args:
        summary: The generated tearsheet text.
        data: The inputs (used for the title and default filename).
        out_dir: Output directory. Defaults to config ``tearsheet.output_dir``.
        filename: Output filename. Defaults to ``<symbol>_tearsheet.pdf``.

    Returns:
        Path to the written PDF.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    directory = out_dir or Path(CONFIG["exporters"]["tearsheet"]["output_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename or f"{data.symbol}_tearsheet.pdf")

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=42,
    )
    story: list[Any] = [
        Paragraph(f"{data.name} ({data.symbol}) — Investment Tearsheet", styles["Title"]),
        Spacer(1, 12),
    ]
    for block in summary.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        safe = _html.escape(block).replace("\n", "<br/>")
        story.append(Paragraph(safe, styles["BodyText"]))
        story.append(Spacer(1, 8))

    doc.build(story)
    logger.info("Tearsheet PDF written: %s", path)
    return path


def render_streamlit(data: TearsheetInput, summary: str, st_module: Any = None) -> None:
    """Render the tearsheet as a Streamlit component.

    Args:
        data: The inputs (title, peer table).
        summary: The generated tearsheet text.
        st_module: Streamlit module to render into; imported if omitted
            (injectable for testing).
    """
    st = st_module
    if st is None:
        import streamlit as st  # noqa: PLC0415 — lazy import keeps tests light

    st.subheader(f"{data.name} ({data.symbol}) — Investment Tearsheet")
    st.markdown(summary)
    if data.peer_ranking is not None and not data.peer_ranking.empty:
        st.caption("Peer ranking")
        st.dataframe(data.peer_ranking)


def generate_tearsheet(
    data: TearsheetInput,
    client: ChatClient | None = None,
    make_pdf: bool = True,
    out_dir: Path | None = None,
) -> Tearsheet:
    """Generate the summary and optionally export it to PDF.

    Args:
        data: The tearsheet inputs.
        client: Optional injected chat client.
        make_pdf: Whether to also write a PDF.
        out_dir: Optional PDF output directory override.

    Returns:
        A :class:`Tearsheet` with the summary and (optionally) the PDF path.
    """
    summary = generate_summary(data, client=client)
    pdf_path = to_pdf(summary, data, out_dir=out_dir) if make_pdf else None
    return Tearsheet(symbol=data.symbol, name=data.name, summary=summary, pdf_path=pdf_path)
