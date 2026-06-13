"""Tests for cross-year AR insights (discrepancy, risk timeline, guidance)."""

import json
from dataclasses import dataclass
from datetime import date

import pytest

from screener.models import ar_insights


@dataclass
class _AR:
    fiscal_year: int
    revenue: float | None = None
    pat: float | None = None
    total_assets: float | None = None
    total_debt: float | None = None
    total_equity: float | None = None
    guided_revenue_growth: float | None = None
    key_risks: object = None


@dataclass
class _Annual:
    fiscal_year_end: date
    revenue: float | None = None
    net_income: float | None = None
    total_assets: float | None = None
    total_debt: float | None = None
    shareholders_equity: float | None = None


class TestDiscrepancies:
    def test_flags_by_severity(self) -> None:
        ar = [_AR(2025, revenue=10_300, pat=900, total_assets=12_000)]
        annual = [_Annual(date(2025, 3, 31), revenue=10_000, net_income=1_200,
                          total_assets=12_000)]
        cells = ar_insights.discrepancies(ar, annual)
        by_metric = {c.metric: c for c in cells}
        # revenue +3% → moderate? 0.03 < 0.05 → ok
        assert by_metric["Revenue"].severity == "ok"
        assert by_metric["Revenue"].diff_pct == pytest.approx(0.03)
        # PAT: (900-1200)/1200 = -0.25 → large
        assert by_metric["Net profit / PAT"].severity == "large"
        # identical total assets → ok, 0%
        assert by_metric["Total assets"].diff_pct == pytest.approx(0.0)

    def test_moderate_band(self) -> None:
        ar = [_AR(2025, revenue=10_800)]
        annual = [_Annual(date(2025, 3, 31), revenue=10_000)]
        cells = ar_insights.discrepancies(ar, annual)
        assert cells[0].severity == "moderate"     # +8% between 5% and 20%

    def test_skips_when_one_side_missing(self) -> None:
        ar = [_AR(2025, revenue=None)]
        annual = [_Annual(date(2025, 3, 31), revenue=10_000)]
        assert ar_insights.discrepancies(ar, annual) == []

    def test_year_alignment(self) -> None:
        ar = [_AR(2024, revenue=9_000), _AR(2025, revenue=10_000)]
        annual = [_Annual(date(2025, 3, 31), revenue=9_900)]   # only 2025
        cells = ar_insights.discrepancies(ar, annual)
        assert len(cells) == 1 and cells[0].year == 2025

    def test_worst_discrepancies_ranked(self) -> None:
        ar = [_AR(2025, revenue=10_300, pat=600, total_assets=12_000)]
        annual = [_Annual(date(2025, 3, 31), revenue=10_000, net_income=1_200,
                          total_assets=11_900)]
        worst = ar_insights.worst_discrepancies(ar_insights.discrepancies(ar, annual), top=1)
        assert worst[0].metric == "Net profit / PAT"   # -50% is the biggest gap


class TestRiskTimeline:
    def test_aggregates_and_ranks(self) -> None:
        rows = [
            _AR(2023, key_risks=json.dumps(["Input cost inflation", "Forex"])),
            _AR(2024, key_risks=json.dumps(["input cost inflation", "Competition"])),
            _AR(2025, key_risks=["Input Cost Inflation"]),   # list form, varied case
        ]
        timeline = ar_insights.risk_timeline(rows)
        top = timeline[0]
        assert top.risk == "Input cost inflation"      # first-seen spelling
        assert top.frequency == 3
        assert (top.first_year, top.last_year) == (2023, 2025)

    def test_handles_missing_and_bad_risks(self) -> None:
        rows = [_AR(2024, key_risks=None), _AR(2025, key_risks="not json")]
        assert ar_insights.risk_timeline(rows) == []

    def test_sorted_by_frequency_then_year(self) -> None:
        rows = [
            _AR(2023, key_risks=["A", "B"]),
            _AR(2024, key_risks=["B"]),
        ]
        timeline = ar_insights.risk_timeline(rows)
        assert [e.risk for e in timeline] == ["B", "A"]   # B freq 2, A freq 1


class TestGuidanceScorecard:
    def test_scores_guidance_vs_actual(self) -> None:
        # FY2023 guided 10% for FY2024; actual = 11000/10000-1 = 10% → delivered.
        ar = [_AR(2023, guided_revenue_growth=0.10)]
        revenue = {2023: 10_000, 2024: 11_000}
        result = ar_insights.guidance_scorecard(ar, revenue)
        assert result is not None
        assert result.evaluated == 1
        assert result.hit_rate == 1.0

    def test_none_when_no_actuals(self) -> None:
        ar = [_AR(2025, guided_revenue_growth=0.12)]   # FY2026 actual unknown
        assert ar_insights.guidance_scorecard(ar, {2025: 10_000}) is None

    def test_none_when_no_guidance(self) -> None:
        ar = [_AR(2024, guided_revenue_growth=None)]
        assert ar_insights.guidance_scorecard(ar, {2023: 1, 2024: 2}) is None
