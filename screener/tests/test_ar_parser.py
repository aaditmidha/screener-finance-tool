"""Tests for AR structured extraction (LLM client faked; regex fallback)."""

import json
from types import SimpleNamespace

import pytest

from screener.scraper import ar_parser

_GOOD_JSON = {
    "unit": "Cr",
    "confidence": "high",
    "pages_used": [142, 143],
    "financials": {
        "revenue": 9000, "ebitda": 1500, "pat": 900, "cfo": 1100, "capex": 400,
        "trade_receivables": 1600, "inventory": 1100, "trade_payables": 850,
        "total_assets": 12000, "total_equity": 6000, "total_debt": 1700,
        "cash": 1028, "depreciation": 400, "interest_expense": 120, "tax_expense": 300,
    },
    "guidance": {"revenue_growth_pct": 0.15, "margin_pct": 0.17,
                 "capex_amount": 500, "raw_text": "We target 15% growth."},
    "key_risks": ["input cost inflation", "order execution"],
}


class _FakeClient:
    """Groq-shaped client returning a canned completion."""

    def __init__(self, content: str) -> None:
        message = SimpleNamespace(content=content)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
                choices=[SimpleNamespace(message=message)]))
        )


class TestParseText:
    def test_parses_clean_json(self) -> None:
        client = _FakeClient(json.dumps(_GOOD_JSON))
        result = ar_parser.parse_text("text", "CG Power", 2026, client=client)
        assert result["financials"]["trade_receivables"] == 1600
        assert result["confidence"] == "high"
        assert result["extraction_method"] == "llm"

    def test_strips_markdown_fences(self) -> None:
        client = _FakeClient("```json\n" + json.dumps(_GOOD_JSON) + "\n```")
        result = ar_parser.parse_text("text", "CG Power", 2026, client=client)
        assert result["financials"]["revenue"] == 9000

    def test_malformed_json_falls_back_to_regex(self) -> None:
        client = _FakeClient("not json at all")
        text = "Revenue from operations 9,000 Profit after tax 900"
        result = ar_parser.parse_text(text, "X", 2026, client=client)
        assert result["extraction_method"] == "regex_fallback"
        assert result["confidence"] == "low"
        assert result["financials"]["revenue"] == 9000.0

    def test_missing_financials_key_falls_back(self) -> None:
        client = _FakeClient(json.dumps({"unit": "Cr"}))   # no 'financials'
        result = ar_parser.parse_text("Net profit 500", "X", 2026, client=client)
        assert result["extraction_method"] == "regex_fallback"

    def test_no_api_key_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no client and no key, extraction degrades to regex, not crash."""
        from screener import llm
        monkeypatch.delenv(llm._cfg["api_key_env"], raising=False)
        text = "Total revenue 1,234.5 Net profit 200"
        result = ar_parser.parse_text(text, "X", 2026)
        assert result["extraction_method"] == "regex_fallback"
        assert result["financials"]["revenue"] == pytest.approx(1234.5)


class TestRegexFallback:
    def test_extracts_headline_numbers(self) -> None:
        text = ("Revenue from operations 12,500.00 ... "
                "Net cash generated from operating activities 1,800")
        result = ar_parser._regex_fallback(text)
        assert result["financials"]["revenue"] == 12500.0
        assert result["financials"]["cfo"] == 1800.0
        assert result["financials"]["ebitda"] is None      # not matched → null

    def test_detects_unit(self) -> None:
        result = ar_parser._regex_fallback("(₹ in Lakhs) Net profit 50")
        assert result["unit"] == "Lakhs"


class TestParseFromPdf:
    def test_parse_uses_extractor_then_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from screener.scraper.pdf_extractor import ExtractedPdf
        monkeypatch.setattr(ar_parser.pdf_extractor, "extract_text",
                            lambda p: ExtractedPdf(pages={142: "Balance Sheet ..."}))
        monkeypatch.setattr(ar_parser.pdf_extractor, "find_financial_pages", lambda e: [142])
        client = _FakeClient(json.dumps({**_GOOD_JSON, "pages_used": []}))
        result = ar_parser.parse("/tmp/ar.pdf", "CG Power", 2026, client=client)
        assert result["pages_used"] == [142]              # filled from detected pages
        assert result["financials"]["pat"] == 900
