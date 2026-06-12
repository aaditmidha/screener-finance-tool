"""Tests for the earnings quality model.

Validation data: Infosys consolidated FY2022–FY2024 (₹ crore, approximate
published figures). A cash-rich IT services firm converts essentially all
profit to cash, so it must come out clean.
"""

import math

import pytest

from screener.models.earnings_quality import (
    FLAG_HIGH_ACCRUALS,
    FLAG_LOW_CFO_PAT,
    FLAG_PAT_CFO_DIVERGENCE,
    EarningsQualityResult,
    YearFinancials,
    accrual_ratio,
    analyze,
    cfo_pat_ratio,
)

# Infosys consolidated, ₹ crore (approximate published figures).
INFY_YEARS = [
    YearFinancials(year=2022, pat=22_110, cfo=23_224, total_assets=117_885),
    YearFinancials(year=2023, pat=24_095, cfo=22_467, total_assets=125_816),
    YearFinancials(year=2024, pat=26_233, cfo=25_210, total_assets=137_814),
]


class TestCfoPatRatio:
    def test_normal(self) -> None:
        # FY24: 25210 / 26233
        assert cfo_pat_ratio(25_210, 26_233) == pytest.approx(0.9610, abs=1e-3)

    def test_zero_pat_positive_cfo_is_inf(self) -> None:
        assert math.isinf(cfo_pat_ratio(100.0, 0.0))

    def test_zero_pat_zero_cfo_is_zero(self) -> None:
        assert cfo_pat_ratio(0.0, 0.0) == 0.0

    def test_negative_pat_gives_negative_ratio(self) -> None:
        """A loss with positive cash flow yields a negative (flaggable) ratio."""
        assert cfo_pat_ratio(100.0, -50.0) == pytest.approx(-2.0)


class TestAccrualRatio:
    def test_infosys_fy24(self) -> None:
        # (26233 − 25210) / 137814
        assert accrual_ratio(26_233, 25_210, 137_814) == pytest.approx(0.00742, abs=1e-4)

    def test_zero_assets_returns_zero(self) -> None:
        assert accrual_ratio(100.0, 80.0, 0.0) == 0.0

    def test_cash_exceeding_profit_is_negative(self) -> None:
        """CFO above PAT (conservative accounting) gives a negative ratio."""
        assert accrual_ratio(80.0, 100.0, 1_000.0) == pytest.approx(-0.02)


class TestAnalyze:
    def test_infosys_is_healthy(self) -> None:
        """Infosys converts profit to cash — no flags, healthy verdict."""
        result = analyze(INFY_YEARS)
        assert result.flags == []
        assert result.verdict == "healthy"

    def test_infosys_avg_cfo_pat(self) -> None:
        # mean of 1.0504, 0.9324, 0.9610
        result = analyze(INFY_YEARS)
        assert result.avg_cfo_pat == pytest.approx(0.981, abs=1e-3)

    def test_infosys_growth_rates(self) -> None:
        # PAT CAGR ≈ 8.9%, CFO CAGR ≈ 4.2% over FY22→FY24
        result = analyze(INFY_YEARS)
        assert result.pat_cagr == pytest.approx(0.0893, abs=1e-3)
        assert result.cfo_cagr == pytest.approx(0.0419, abs=1e-3)

    def test_low_cash_conversion_flagged(self) -> None:
        """Chronic CFO well below PAT must raise the low_cfo_pat flag."""
        weak = [
            YearFinancials(year=2022, pat=1_000, cfo=500, total_assets=10_000),
            YearFinancials(year=2023, pat=1_100, cfo=560, total_assets=11_000),
            YearFinancials(year=2024, pat=1_200, cfo=600, total_assets=12_000),
        ]
        result = analyze(weak)
        assert FLAG_LOW_CFO_PAT in result.flags

    def test_high_accruals_flagged(self) -> None:
        """Latest-year accruals above 10% of assets must raise the flag."""
        accrual_heavy = [
            YearFinancials(year=2023, pat=1_000, cfo=900, total_assets=5_000),
            YearFinancials(year=2024, pat=1_500, cfo=600, total_assets=5_000),
        ]
        result = analyze(accrual_heavy)
        assert FLAG_HIGH_ACCRUALS in result.flags

    def test_divergence_flagged(self) -> None:
        """Profit compounding far faster than cash must raise the divergence flag."""
        diverging = [
            YearFinancials(year=2021, pat=1_000, cfo=950, total_assets=10_000),
            YearFinancials(year=2022, pat=1_400, cfo=970, total_assets=11_000),
            YearFinancials(year=2023, pat=1_960, cfo=990, total_assets=12_000),
            YearFinancials(year=2024, pat=2_744, cfo=1_010, total_assets=13_000),
        ]
        result = analyze(diverging)
        assert FLAG_PAT_CFO_DIVERGENCE in result.flags

    def test_multiple_flags_is_red_flag(self) -> None:
        """Two or more flags must escalate the verdict to red_flag."""
        ugly = [
            YearFinancials(year=2022, pat=1_000, cfo=400, total_assets=5_000),
            YearFinancials(year=2023, pat=1_600, cfo=420, total_assets=5_500),
            YearFinancials(year=2024, pat=2_560, cfo=440, total_assets=6_000),
        ]
        result = analyze(ugly)
        assert len(result.flags) >= 2
        assert result.verdict == "red_flag"

    def test_single_year_raises(self) -> None:
        with pytest.raises(ValueError):
            analyze(INFY_YEARS[:1])

    def test_loss_years_excluded_from_average(self) -> None:
        """Loss-making years must not poison the CFO/PAT average."""
        with_loss = [
            YearFinancials(year=2022, pat=-500, cfo=100, total_assets=5_000),
            YearFinancials(year=2023, pat=1_000, cfo=950, total_assets=5_500),
            YearFinancials(year=2024, pat=1_100, cfo=1_050, total_assets=6_000),
        ]
        result = analyze(with_loss)
        # average over the two profitable years only: (0.95 + 0.9545…) / 2
        assert result.avg_cfo_pat == pytest.approx(0.9523, abs=1e-3)

    def test_returns_result_type(self) -> None:
        assert isinstance(analyze(INFY_YEARS), EarningsQualityResult)
