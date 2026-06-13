"""Structured extraction of financials from annual-report text.

Sends the focused financial-statement text (from :mod:`pdf_extractor`) to the
LLM (Groq, via :mod:`screener.llm` — never the Anthropic API) and parses a
strict JSON payload of exact figures, management guidance, and key risks. If
the LLM is unavailable or returns unusable output, a regex fallback extracts a
few headline numbers at low confidence, so the pipeline degrades rather than
fails.

Every field the model can't find is ``null`` — we never guess — and each
extraction is graded high/medium/low confidence by how much was found.
"""

import json
import logging
import re
from typing import Any

from screener.config import CONFIG
from screener.llm import ChatClient, LLMError, chat
from screener.scraper import pdf_extractor

logger = logging.getLogger(__name__)

_cfg = CONFIG["annual_report_extraction"]

# Financial fields the schema carries (also the ARExtractedData columns).
FINANCIAL_FIELDS = (
    "revenue", "ebitda", "pat", "cfo", "capex", "trade_receivables",
    "inventory", "trade_payables", "total_assets", "total_equity",
    "total_debt", "cash", "depreciation", "interest_expense", "tax_expense",
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Extraction-dict guidance keys → ARExtractedData columns.
_GUIDANCE_MAP = {
    "revenue_growth_pct": "guided_revenue_growth",
    "margin_pct": "guided_margin",
    "capex_amount": "guided_capex",
    "raw_text": "guidance_raw_text",
}


def flatten_extraction(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten an extraction result into ARExtractedData column kwargs.

    Args:
        data: Extraction dict (nested financials/guidance) from :func:`parse`.

    Returns:
        Flat mapping of column name → value, ready for the repository upsert;
        ``key_risks`` and ``pages_used`` are JSON-encoded.
    """
    flat: dict[str, Any] = {}
    financials = data.get("financials", {}) or {}
    for field in FINANCIAL_FIELDS:
        if financials.get(field) is not None:
            flat[field] = financials[field]

    guidance = data.get("guidance", {}) or {}
    for src_key, col in _GUIDANCE_MAP.items():
        if guidance.get(src_key) is not None:
            flat[col] = guidance[src_key]

    if data.get("key_risks"):
        flat["key_risks"] = json.dumps(data["key_risks"])
    if data.get("confidence"):
        flat["extraction_confidence"] = data["confidence"]
    if data.get("pages_used"):
        flat["pages_used"] = json.dumps(data["pages_used"])
    if data.get("unit"):
        flat["unit"] = data["unit"]
    return flat

SYSTEM_PROMPT = """You are a financial data extraction specialist for Indian \
company annual reports. Return ONLY valid JSON — no explanation, no markdown, \
no preamble. Report all amounts in the source unit (Crores or Lakhs). Use null \
for any field not found. Never guess or estimate."""

EXTRACTION_PROMPT = """Extract the following from this Annual Report text for \
{company} FY{year} ({view_type} financial statements).

Return EXACTLY this JSON structure:
{{
  "unit": "Cr" or "Lakhs",
  "confidence": "high" or "medium" or "low",
  "pages_used": [page numbers referenced],
  "financials": {{
    "revenue": number or null, "ebitda": number or null, "pat": number or null,
    "cfo": number or null, "capex": number or null,
    "trade_receivables": number or null, "inventory": number or null,
    "trade_payables": number or null, "total_assets": number or null,
    "total_equity": number or null, "total_debt": number or null,
    "cash": number or null, "depreciation": number or null,
    "interest_expense": number or null, "tax_expense": number or null
  }},
  "guidance": {{
    "revenue_growth_pct": number or null, "margin_pct": number or null,
    "capex_amount": number or null, "raw_text": "exact quote or null"
  }},
  "key_risks": ["risk 1", "risk 2"] or []
}}

Annual Report text:
{text}
"""

# Regex-fallback patterns (low confidence).
_FALLBACK_PATTERNS = {
    "revenue": [r"(?:revenue from operations|total revenue|net sales|total income)[^\d\-]{0,40}([\d,]+\.?\d*)"],
    "pat": [r"(?:profit after tax|net profit|profit for the year)[^\d\-]{0,40}([\d,]+\.?\d*)"],
    "cfo": [r"(?:net cash (?:from|generated).{0,30}operating|cash flow from operating)[^\d\-]{0,40}([\d,]+\.?\d*)"],
}


class ARParseError(ValueError):
    """Raised when an LLM extraction result is missing required structure."""


def _parse_json(raw: str) -> dict[str, Any]:
    """Strip markdown fences and parse the LLM response as JSON.

    Args:
        raw: Raw LLM text.

    Returns:
        Parsed dict.

    Raises:
        json.JSONDecodeError: If the cleaned text is not valid JSON.
    """
    cleaned = _FENCE_RE.sub("", raw).strip()
    return json.loads(cleaned)


def _validate_result(result: dict[str, Any]) -> None:
    """Ensure an extraction result carries the required top-level structure.

    Args:
        result: Parsed extraction dict.

    Raises:
        ARParseError: If ``unit`` or a dict ``financials`` block is missing.
    """
    if not isinstance(result, dict) or "financials" not in result:
        raise ARParseError("Extraction result missing 'financials'")
    if not isinstance(result["financials"], dict):
        raise ARParseError("'financials' must be an object")
    if "unit" not in result:
        raise ARParseError("Extraction result missing 'unit'")


def _clean_number(text: str) -> float | None:
    """Parse a regex-captured Indian-format number, or None."""
    cleaned = text.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _regex_fallback(text: str) -> dict[str, Any]:
    """Extract a few headline figures by pattern matching (low confidence).

    Args:
        text: Financial-statement text.

    Returns:
        An extraction dict in the standard schema; unmatched fields are null
        and confidence is "low".
    """
    financials: dict[str, float | None] = dict.fromkeys(FINANCIAL_FIELDS, None)
    low = text.lower()
    for field, patterns in _FALLBACK_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, low)
            if match:
                financials[field] = _clean_number(match.group(1))
                break
    found = sum(1 for v in financials.values() if v is not None)
    logger.info("Regex fallback recovered %d field(s)", found)
    return {
        "unit": pdf_extractor.detect_unit(text),
        "confidence": "low",
        "pages_used": [],
        "financials": financials,
        "guidance": {"revenue_growth_pct": None, "margin_pct": None,
                     "capex_amount": None, "raw_text": None},
        "key_risks": [],
        "extraction_method": "regex_fallback",
    }


def parse_text(
    text: str,
    company_name: str,
    fiscal_year: int,
    view_type: str = "consolidated",
    client: ChatClient | None = None,
) -> dict[str, Any]:
    """Extract structured data from AR *text* via the LLM, with regex fallback.

    Args:
        text: Focused financial-statement text.
        company_name: Company display name (for the prompt).
        fiscal_year: Fiscal year being extracted.
        view_type: "consolidated" or "standalone".
        client: Optional injected chat client (Groq built by default).

    Returns:
        An extraction dict in the standard schema.
    """
    prompt = EXTRACTION_PROMPT.format(
        company=company_name, year=fiscal_year, view_type=view_type, text=text
    )
    try:
        raw = chat(SYSTEM_PROMPT, prompt, client=client,
                   temperature=0.0, max_tokens=_cfg["llm_max_tokens"])
        result = _parse_json(raw)
        _validate_result(result)
        result.setdefault("extraction_method", "llm")
        return result
    except (LLMError, ARParseError, json.JSONDecodeError) as exc:
        logger.warning("LLM extraction failed (%s); using regex fallback", exc)
        return _regex_fallback(text)


def parse(
    pdf_path: str,
    company_name: str,
    fiscal_year: int,
    view_type: str = "consolidated",
    client: ChatClient | None = None,
) -> dict[str, Any]:
    """Extract structured data from an AR PDF file end-to-end.

    Args:
        pdf_path: Path to the AR PDF.
        company_name: Company display name.
        fiscal_year: Fiscal year being extracted.
        view_type: "consolidated" or "standalone".
        client: Optional injected chat client.

    Returns:
        An extraction dict in the standard schema, with ``pages_used`` filled
        from the detected financial pages when the model didn't supply them.
    """
    extracted = pdf_extractor.extract_text(pdf_path)
    pages = pdf_extractor.find_financial_pages(extracted)
    text = pdf_extractor.truncate_for_llm(extracted, pages)
    result = parse_text(text, company_name, fiscal_year, view_type, client=client)
    if not result.get("pages_used"):
        result["pages_used"] = pages
    return result
