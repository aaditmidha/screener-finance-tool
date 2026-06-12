"""Tests for the Screener schedules (row-expand) enrichment."""

import json

import pytest

from screener.scraper import schedules
from screener.scraper.parser import parse_company_financials

_PAGE = """
<html><body><h1>CG Power</h1>
  <a href="/api/company/739/chart/"></a>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr></thead>
    <tbody>
      <tr><td>Sales</td><td>9,000</td><td>11,000</td></tr>
      <tr><td>Expenses +</td><td>7,800</td><td>9,500</td></tr>
    </tbody>
  </table></section>
  <section id="balance-sheet"><table>
    <thead><tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr></thead>
    <tbody>
      <tr><td>Other Assets +</td><td>3,100</td><td>9,500</td></tr>
      <tr><td>Total Assets</td><td>7,000</td><td>14,000</td></tr>
    </tbody>
  </table></section>
</body></html>
"""

# Shaped like the real API response, including metadata keys to strip.
_SCHEDULE_RESPONSES = {
    "Expenses": {
        "Material Cost %": {"Mar 2025": "65%", "Mar 2026": "69%",
                            "isExpandable": "Company.showSchedule(...)"},
        "Employee Cost %": {"Mar 2025": "6%", "Mar 2026": "8%"},
    },
    "Other Assets": {
        "Trade receivables": {"Mar 2025": "2,012", "Mar 2026": "2,924",
                              "setAttributes": {"class": "strong"}},
        "Inventories": {"Mar 2025": "1,137", "Mar 2026": "1,584"},
    },
}


def _fake_fetch(url: str) -> str:
    for parent, payload in _SCHEDULE_RESPONSES.items():
        if parent.replace(" ", "%20") in url:
            return json.dumps(payload)
    return json.dumps({})


class TestExtractCompanyId:
    def test_finds_id(self) -> None:
        assert schedules.extract_company_id(_PAGE) == "739"

    def test_none_when_absent(self) -> None:
        assert schedules.extract_company_id("<html></html>") is None
        assert schedules.extract_company_id("") is None


class TestFetchSchedule:
    def test_strips_metadata_and_cleans_numbers(self) -> None:
        children = schedules.fetch_schedule("739", "Other Assets", "balance-sheet",
                                            fetch_json=_fake_fetch)
        assert children["Trade receivables"] == {"Mar 2025": 2012.0, "Mar 2026": 2924.0}
        assert "setAttributes" not in children["Trade receivables"]

    def test_percent_values_parsed(self) -> None:
        children = schedules.fetch_schedule("739", "Expenses", "profit-loss",
                                            fetch_json=_fake_fetch)
        assert children["Material Cost %"]["Mar 2026"] == 69.0

    def test_failure_returns_empty(self) -> None:
        def _boom(url: str) -> str:
            raise RuntimeError("offline")
        assert schedules.fetch_schedule("739", "X", "profit-loss", fetch_json=_boom) == {}

    def test_non_json_returns_empty(self) -> None:
        assert schedules.fetch_schedule("739", "X", "profit-loss",
                                        fetch_json=lambda u: "<html>") == {}


class TestEnrich:
    def test_attaches_notes_tables(self) -> None:
        fin = parse_company_financials(_PAGE)
        schedules.enrich(fin, _PAGE, fetch_json=_fake_fetch)

        assert fin.notes_pl is not None
        assert fin.notes_bs is not None
        # Labels carry the parent prefix; substring lookup still works.
        assert fin.notes_bs.row("receivable") == [2012.0, 2924.0]
        assert fin.notes_pl.row("material cost") == [65.0, 69.0]

    def test_periods_aligned_with_statement(self) -> None:
        fin = parse_company_financials(_PAGE)
        schedules.enrich(fin, _PAGE, fetch_json=_fake_fetch)
        assert fin.notes_bs.periods == fin.balance_sheet.periods

    def test_no_company_id_is_noop(self) -> None:
        page = _PAGE.replace('/api/company/739/chart/', '/x/')
        fin = parse_company_financials(page)
        schedules.enrich(fin, page, fetch_json=_fake_fetch)
        assert fin.notes_pl is None and fin.notes_bs is None

    def test_fetch_failures_leave_fin_usable(self) -> None:
        def _boom(url: str) -> str:
            raise RuntimeError("offline")
        fin = parse_company_financials(_PAGE)
        schedules.enrich(fin, _PAGE, fetch_json=_boom)
        assert fin.notes_pl is None
        assert fin.profit_loss is not None   # statements untouched
