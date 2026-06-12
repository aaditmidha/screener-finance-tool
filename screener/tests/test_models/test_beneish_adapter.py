"""Tests for the Screener → Beneish input adapter.

Three data shapes are exercised:
1. The standard logged-out Screener page (verified live: aggregated P&L/BS,
   no receivables) — must still produce a real M-Score with disclosures.
2. A rich page with granular rows and ballooning receivables, modelled on the
   classic pre-fraud signature (receivables compounding ~2× while sales grow
   ~1.2×, profits unbacked by cash — the pattern at Satyam before 2009).
3. Healthy Infosys-style figures — must stay non-manipulator.
"""

import pytest

from screener.models.beneish_adapter import BeneishSourcing, from_financials
from screener.scraper.parser import parse_company_financials


def _page(pl_rows: dict, bs_rows: dict, cf_rows: dict | None = None,
          periods: tuple[str, str] = ("Mar 2023", "Mar 2024")) -> str:
    """Build a Screener-shaped page from {label: (prior, current)} mappings."""
    def table(rows: dict) -> str:
        body = "".join(
            f"<tr><td>{label}</td><td>{prior}</td><td>{current}</td></tr>"
            for label, (prior, current) in rows.items()
        )
        return (f"<table><thead><tr><th></th><th>{periods[0]}</th>"
                f"<th>{periods[1]}</th></tr></thead><tbody>{body}</tbody></table>")

    cf_html = f'<section id="cash-flow">{table(cf_rows)}</section>' if cf_rows else ""
    return f"""<html><body><h1>Test Co</h1>
        <section id="profit-loss">{table(pl_rows)}</section>
        <section id="balance-sheet">{table(bs_rows)}</section>
        {cf_html}</body></html>"""


# Shape 1: exactly the rows the live logged-out Screener page exposes.
_STANDARD = _page(
    pl_rows={
        "Sales": (146767, 153670), "Expenses": (112767, 116976),
        "Operating Profit": (34000, 36694), "Depreciation": (3500, 4678),
        "Net Profit": (24095, 26233),
    },
    bs_rows={
        "Equity Capital": (2098, 2098), "Reserves": (73000, 85912),
        "Borrowings": (7000, 8359), "Other Liabilities": (43718, 41445),
        "Fixed Assets": (13346, 12370), "CWIP": (300, 250),
        "Investments": (12000, 13000), "Other Assets": (100170, 112194),
        "Total Assets": (125816, 137814),
    },
    cf_rows={"Cash from Operating Activity": (22467, 25210)},
)

# Shape 2: granular rows, classic manipulation signature — receivables
# compounding far faster than sales, profit not backed by operating cash.
_DETERIORATING = _page(
    pl_rows={
        "Sales": (5000, 6000), "Raw Material Cost": (3000, 3900),
        "Other Expenses": (600, 950), "Depreciation": (180, 120),
        "Net Profit": (700, 900),
    },
    bs_rows={
        "Trade Receivables": (900, 2100),       # 2.3× vs sales 1.2×
        "Other Assets": (2400, 4100),
        "Fixed Assets": (1500, 1450), "CWIP": (100, 600),
        "Investments": (50, 40),
        "Borrowings": (800, 1700), "Other Liabilities": (1100, 1900),
        "Total Assets": (5500, 8600),
    },
    cf_rows={"Cash from Operating Activity": (650, 50)},   # profit ≫ cash
)


class TestStandardScreenerShape:
    """The aggregated public page must yield a usable, disclosed score."""

    @pytest.fixture(scope="class")
    def sourcing(self) -> BeneishSourcing:
        return from_financials(parse_company_financials(_STANDARD))

    def test_produces_a_score(self, sourcing: BeneishSourcing) -> None:
        assert sourcing is not None
        assert isinstance(sourcing.result.m_score, float)

    def test_healthy_company_is_not_flagged(self, sourcing: BeneishSourcing) -> None:
        assert sourcing.result.verdict in ("non_manipulator", "grey_zone")

    def test_missing_receivables_neutralises_dsri(self, sourcing: BeneishSourcing) -> None:
        assert sourcing.result.indices.dsri == 1.0
        assert any("receivable" in m.lower() for m in sourcing.missing)

    def test_cogs_approximation_disclosed(self, sourcing: BeneishSourcing) -> None:
        assert any("expenses" in a.lower() for a in sourcing.approximated)

    def test_periods_reported(self, sourcing: BeneishSourcing) -> None:
        assert sourcing.periods == ("Mar 2023", "Mar 2024")


class TestDeterioratingReceivables:
    """A pre-fraud-style profile must be flagged, not waved through."""

    @pytest.fixture(scope="class")
    def sourcing(self) -> BeneishSourcing:
        return from_financials(parse_company_financials(_DETERIORATING))

    def test_dsri_detects_ballooning_receivables(self, sourcing: BeneishSourcing) -> None:
        # (2100/6000) / (900/5000) ≈ 1.94
        assert sourcing.result.indices.dsri > 1.5

    def test_high_accruals_detected(self, sourcing: BeneishSourcing) -> None:
        # (900 − 50) / 8600 ≈ 0.099
        assert sourcing.result.indices.tata > 0.05

    def test_flagged_as_manipulator(self, sourcing: BeneishSourcing) -> None:
        assert sourcing.result.verdict == "manipulator"

    def test_granular_rows_reduce_approximations(self, sourcing: BeneishSourcing) -> None:
        """With real receivables/materials rows, those disclosures vanish."""
        assert not any("receivable" in m.lower() for m in sourcing.missing)
        assert not any("Total Expenses" in a for a in sourcing.approximated)


class TestInsufficientData:
    def test_single_period_returns_none(self) -> None:
        single = """<html><body><h1>X</h1>
            <section id="profit-loss"><table>
              <thead><tr><th></th><th>Mar 2024</th></tr></thead>
              <tbody><tr><td>Sales</td><td>100</td></tr></tbody>
            </table></section>
            <section id="balance-sheet"><table>
              <thead><tr><th></th><th>Mar 2024</th></tr></thead>
              <tbody><tr><td>Total Assets</td><td>500</td></tr></tbody>
            </table></section></body></html>"""
        assert from_financials(parse_company_financials(single)) is None

    def test_no_statements_returns_none(self) -> None:
        assert from_financials(parse_company_financials("<html><body></body></html>")) is None

    def test_missing_cfo_neutralises_tata(self) -> None:
        page = _page(
            pl_rows={"Sales": (5000, 6000), "Expenses": (4000, 4800),
                     "Depreciation": (100, 110), "Net Profit": (700, 800)},
            bs_rows={"Total Assets": (5500, 6500), "Fixed Assets": (1500, 1450),
                     "Other Assets": (3000, 3800), "Borrowings": (800, 900),
                     "Other Liabilities": (1100, 1200), "Investments": (0, 0)},
            cf_rows=None,
        )
        sourcing = from_financials(parse_company_financials(page))
        assert sourcing.result.indices.tata == 0.0
        assert any("CFO" in m for m in sourcing.missing)
