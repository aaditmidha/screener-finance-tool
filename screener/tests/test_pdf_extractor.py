"""Tests for AR PDF text extraction (pdfplumber mocked — no real PDFs)."""

from types import SimpleNamespace

import pytest

from screener.scraper import pdf_extractor
from screener.scraper.pdf_extractor import ExtractedPdf


class _FakePage:
    def __init__(self, text: str, tables: list | None = None) -> None:
        self._text = text
        self._tables = tables or []

    def extract_text(self, **_kw) -> str:
        return self._text

    def extract_tables(self) -> list:
        return self._tables


class _FakePdf:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestExtractText:
    def test_extracts_pages_and_count(self, tmp_path, monkeypatch) -> None:
        pdf_file = tmp_path / "ar.pdf"
        pdf_file.write_bytes(b"%PDF-1.7 stub")
        fake = _FakePdf([_FakePage("cover"), _FakePage("Balance Sheet ...", [[["a", "b"]]]),
                         _FakePage("")])
        monkeypatch.setattr("pdfplumber.open", lambda _p: fake)

        result = pdf_extractor.extract_text(pdf_file)
        assert result.page_count == 3
        assert result.pages == {1: "cover", 2: "Balance Sheet ..."}   # blank page dropped
        assert result.tables == {}     # tables intentionally not extracted (memory/time)
        assert "cover" in result.full_text

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            pdf_extractor.extract_text(tmp_path / "nope.pdf")


class TestFindFinancialPages:
    def test_matches_statement_keywords(self) -> None:
        extracted = ExtractedPdf(pages={
            1: "Chairman's letter",
            2: "Consolidated Statement of Profit and Loss for the year",
            3: "Notes",
            4: "Consolidated Balance Sheet as at 31 March",
        })
        assert pdf_extractor.find_financial_pages(extracted) == [2, 4]

    def test_no_matches_returns_empty(self) -> None:
        assert pdf_extractor.find_financial_pages(ExtractedPdf(pages={1: "hello"})) == []


class TestDetectUnit:
    @pytest.mark.parametrize("text,expected", [
        ("All figures ₹ in crores", "Cr"),
        ("Amounts in Lakhs unless stated", "Lakhs"),
        ("no marker here", "Cr"),                      # default
    ])
    def test_detection(self, text: str, expected: str) -> None:
        assert pdf_extractor.detect_unit(text) == expected


class TestTruncateForLlm:
    def test_includes_target_pages_with_markers(self) -> None:
        extracted = ExtractedPdf(pages={1: "intro", 2: "PL data", 4: "BS data"})
        blob = pdf_extractor.truncate_for_llm(extracted, [2, 4])
        assert "[Page 2]" in blob and "PL data" in blob
        assert "[Page 4]" in blob and "BS data" in blob
        assert "intro" not in blob

    def test_respects_max_chars(self) -> None:
        extracted = ExtractedPdf(pages={1: "x" * 5000})
        blob = pdf_extractor.truncate_for_llm(extracted, [1], max_chars=100)
        assert len(blob) == 100

    def test_empty_targets_falls_back_to_all(self) -> None:
        extracted = ExtractedPdf(pages={1: "only page"})
        assert "only page" in pdf_extractor.truncate_for_llm(extracted, [])
