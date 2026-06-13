"""Tests for the research-note generator (sections, charts, key financials, docx)."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from screener.exporters import research_note
from screener.scraper.parser import parse_company_financials

_PAGE = """
<html><body><h1>Test Co</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>9,000</td><td>10,000</td><td>11,000</td></tr>
      <tr><td>Operating Profit</td><td>1,800</td><td>2,000</td><td>2,310</td></tr>
      <tr><td>Depreciation</td><td>200</td><td>210</td><td>220</td></tr>
      <tr><td>Net Profit</td><td>1,200</td><td>1,350</td><td>1,500</td></tr>
      <tr><td>EPS in Rs</td><td>12</td><td>13.5</td><td>15</td></tr>
    </tbody>
  </table></section>
</body></html>
"""

_SECTIONS_JSON = json.dumps([
    {"heading": "Capacity expansion underway", "body": "The company is adding capacity."},
    {"heading": "Valuation & View", "body": "Reasonably valued for the growth."},
])


class _FakeClient:
    def __init__(self, content: str) -> None:
        msg = SimpleNamespace(content=content)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
                choices=[SimpleNamespace(message=msg)])))


@pytest.fixture()
def fin():
    return parse_company_financials(_PAGE)


class TestKeyFinancials:
    def test_rows_and_periods(self, fin) -> None:
        periods, rows = research_note.key_financials(fin)
        assert periods == ["Mar 2023", "Mar 2024", "Mar 2025"]
        labels = {r[0] for r in rows}
        assert {"Revenue", "EBITDA", "EBITDA margin %", "PAT", "PAT margin %"} <= labels

    def test_margin_value(self, fin) -> None:
        _periods, rows = research_note.key_financials(fin)
        ebitda_margin = next(r for r in rows if r[0] == "EBITDA margin %")
        # FY25: 2310/11000 = 0.21
        assert ebitda_margin[1][-1] == pytest.approx(0.21)
        assert ebitda_margin[2] == "pct"

    def test_empty_without_pl(self) -> None:
        fin = parse_company_financials("<html><body></body></html>")
        assert research_note.key_financials(fin) == ([], [])


class TestFocusCharts:
    def test_returns_png_bytes(self, fin) -> None:
        periods, rows = research_note.key_financials(fin)
        charts = research_note.focus_charts(periods, rows)
        assert len(charts) == 2                       # revenue/PAT + margins
        for _title, png in charts:
            assert png[:8] == b"\x89PNG\r\n\x1a\n"    # PNG magic

    def test_empty_when_nothing_plottable(self) -> None:
        assert research_note.focus_charts([], []) == []


class TestBuildSections:
    def test_parses_llm_json(self) -> None:
        sections = research_note.build_sections("Test Co", "TEST", "context",
                                                client=_FakeClient(_SECTIONS_JSON))
        assert [s.heading for s in sections] == ["Capacity expansion underway", "Valuation & View"]

    def test_strips_fences(self) -> None:
        sections = research_note.build_sections("T", "T", "c",
                                                client=_FakeClient("```json\n" + _SECTIONS_JSON + "\n```"))
        assert len(sections) == 2

    def test_bad_json_returns_empty(self) -> None:
        assert research_note.build_sections("T", "T", "c", client=_FakeClient("nope")) == []

    def test_no_key_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from screener import llm
        monkeypatch.delenv(llm._cfg["api_key_env"], raising=False)
        assert research_note.build_sections("T", "T", "c") == []


class TestGenerateAndDocx:
    def test_generate_assembles_note(self, fin) -> None:
        note = research_note.generate(fin, "Test Co", "TEST",
                                      metrics={"Forensic score": "100/100 (healthy)"},
                                      client=_FakeClient(_SECTIONS_JSON))
        assert note.symbol == "TEST"
        assert len(note.sections) == 2
        assert note.key_financials

    def test_to_docx_is_valid_and_has_title(self, fin) -> None:
        note = research_note.generate(fin, "Test Co", "TEST", client=_FakeClient(_SECTIONS_JSON))
        data = research_note.to_docx(note)
        assert data[:2] == b"PK"                       # docx is a zip
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8", "ignore")
        assert "Test Co" in xml
        assert "Key financials" in xml
        assert "Capacity expansion underway" in xml

    def test_to_docx_without_sections_still_renders(self, fin) -> None:
        note = research_note.generate(fin, "Test Co", "TEST", client=_FakeClient("bad"))
        data = research_note.to_docx(note)
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8", "ignore")
        assert "Key financials" in xml                 # tables/charts present without LLM
