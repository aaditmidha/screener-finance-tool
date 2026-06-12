"""Tests for the capital allocation model.

Validation data: Infosys-style ROIC/WACC profile — ROIC near 30% against a
WACC near 11–12% is a textbook excellent allocator and must score 10/10.
"""

import pytest

from screener.models.capital_allocation import (
    CapitalAllocationResult,
    YearSpread,
    build_trend,
    nopat,
    roic,
    score_trend,
    wacc,
)

# Infosys-style multi-year profile (approximate): wide, widening spread.
INFY_TREND = build_trend([
    (2020, 0.255, 0.110),
    (2021, 0.272, 0.112),
    (2022, 0.301, 0.115),
    (2023, 0.320, 0.118),
    (2024, 0.316, 0.120),
])


class TestComponents:
    def test_nopat(self) -> None:
        # EBIT 100 at 25% tax
        assert nopat(100.0, 0.25) == pytest.approx(75.0)

    def test_roic_normal(self) -> None:
        assert roic(75.0, 500.0) == pytest.approx(0.15)

    def test_roic_zero_capital_returns_zero(self) -> None:
        assert roic(75.0, 0.0) == 0.0

    def test_roic_negative_capital_returns_zero(self) -> None:
        assert roic(75.0, -100.0) == 0.0

    def test_wacc_hand_computed(self) -> None:
        # 0.88×0.13 + 0.12×0.08×(1−0.25) = 0.1144 + 0.0072
        result = wacc(equity_value=880, debt_value=120,
                      cost_of_equity=0.13, cost_of_debt=0.08, tax_rate=0.25)
        assert result == pytest.approx(0.1216)

    def test_wacc_zero_capital_returns_zero(self) -> None:
        assert wacc(0, 0, 0.13, 0.08, 0.25) == 0.0

    def test_wacc_all_equity(self) -> None:
        """With no debt, WACC equals the cost of equity."""
        assert wacc(1_000, 0, 0.13, 0.08, 0.25) == pytest.approx(0.13)

    def test_year_spread_property(self) -> None:
        y = YearSpread(year=2024, roic=0.316, wacc=0.120)
        assert y.spread == pytest.approx(0.196)

    def test_build_trend_preserves_order(self) -> None:
        assert [y.year for y in INFY_TREND] == [2020, 2021, 2022, 2023, 2024]


class TestScoreTrend:
    def test_infosys_avg_spread(self) -> None:
        # spreads: 0.145, 0.160, 0.186, 0.202, 0.196 → mean 0.1778
        result = score_trend(INFY_TREND)
        assert result.avg_spread == pytest.approx(0.1778, abs=1e-4)

    def test_infosys_scores_perfect(self) -> None:
        """Wide positive spread, every year positive, widening → 10/10."""
        result = score_trend(INFY_TREND)
        assert result.score == pytest.approx(10.0)
        assert result.rating == "excellent"

    def test_value_destroyer_scores_zero(self) -> None:
        """ROIC below WACC every year, worsening → 0/10, poor."""
        destroyer = build_trend([
            (2022, 0.06, 0.11),
            (2023, 0.05, 0.11),
            (2024, 0.04, 0.12),
        ])
        result = score_trend(destroyer)
        assert result.score == pytest.approx(0.0)
        assert result.rating == "poor"

    def test_mixed_record_scores_in_between(self) -> None:
        """Half-positive, flat spread should land between the extremes."""
        mixed = build_trend([
            (2022, 0.12, 0.11),
            (2023, 0.10, 0.11),
            (2024, 0.12, 0.11),
        ])
        result = score_trend(mixed)
        assert 0.0 < result.score < 10.0

    def test_single_year_gets_neutral_trend_points(self) -> None:
        """One year has no trend — that component scores half marks."""
        single = build_trend([(2024, 0.30, 0.11)])
        result = score_trend(single)
        # full spread (4) + full consistency (3) + half trend (1.5)
        assert result.score == pytest.approx(8.5)

    def test_empty_trend_raises(self) -> None:
        with pytest.raises(ValueError):
            score_trend([])

    def test_returns_result_type(self) -> None:
        result = score_trend(INFY_TREND)
        assert isinstance(result, CapitalAllocationResult)
        assert result.avg_roic == pytest.approx(0.2928, abs=1e-4)
        assert result.avg_wacc == pytest.approx(0.115, abs=1e-4)
