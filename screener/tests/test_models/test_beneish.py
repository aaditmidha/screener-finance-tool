"""Tests for the Beneish M-Score model.

Validation data: Infosys consolidated FY2024 vs FY2023 (₹ crore, rounded —
figures approximate the published annual report). A blue-chip IT services
company with conservative accounting should land safely below the
manipulation thresholds.
"""

import pytest

from screener.models.beneish import (
    BeneishIndices,
    BeneishResult,
    BeneishYear,
    analyze,
    calculate,
    compute_indices,
)

# Infosys consolidated, ₹ crore (approximate published figures).
INFY_FY24 = BeneishYear(
    revenue=153_670, cogs=107_413, receivables=30_193, current_assets=90_688,
    ppe=12_370, total_assets=137_814, depreciation=4_678, sga=7_537,
    current_liabilities=33_498, long_term_debt=8_359, net_income=26_233, cfo=25_210,
)
INFY_FY23 = BeneishYear(
    revenue=146_767, cogs=102_353, receivables=25_424, current_assets=80_684,
    ppe=13_346, total_assets=125_816, depreciation=4_225, sga=6_973,
    current_liabilities=33_385, long_term_debt=8_299, net_income=24_095, cfo=22_467,
)


class TestComputeIndices:
    """Each of the eight indices, hand-computed from the Infosys fixture."""

    @pytest.fixture(scope="class")
    def indices(self) -> BeneishIndices:
        return compute_indices(INFY_FY24, INFY_FY23)

    def test_dsri(self, indices: BeneishIndices) -> None:
        # (30193/153670) / (25424/146767)
        assert indices.dsri == pytest.approx(1.1342, abs=1e-3)

    def test_gmi(self, indices: BeneishIndices) -> None:
        # prior margin 0.30262 / current margin 0.30102
        assert indices.gmi == pytest.approx(1.0053, abs=1e-3)

    def test_aqi(self, indices: BeneishIndices) -> None:
        # soft-asset share barely moved year-over-year
        assert indices.aqi == pytest.approx(0.9982, abs=1e-3)

    def test_sgi(self, indices: BeneishIndices) -> None:
        # 153670 / 146767
        assert indices.sgi == pytest.approx(1.0470, abs=1e-3)

    def test_depi(self, indices: BeneishIndices) -> None:
        # dep rate rose 0.2405 → 0.2744, so DEPI < 1
        assert indices.depi == pytest.approx(0.8763, abs=1e-3)

    def test_sgai(self, indices: BeneishIndices) -> None:
        assert indices.sgai == pytest.approx(1.0323, abs=1e-3)

    def test_lvgi(self, indices: BeneishIndices) -> None:
        # leverage fell 0.3313 → 0.3037
        assert indices.lvgi == pytest.approx(0.9167, abs=1e-3)

    def test_tata(self, indices: BeneishIndices) -> None:
        # (26233 − 25210) / 137814
        assert indices.tata == pytest.approx(0.00742, abs=1e-4)


class TestCalculate:
    """M-Score combination and verdict classification."""

    def test_infosys_m_score(self) -> None:
        """Hand-computed M-Score for the Infosys fixture is ≈ −2.27."""
        result = analyze(INFY_FY24, INFY_FY23)
        assert result.m_score == pytest.approx(-2.27, abs=0.01)

    def test_infosys_is_non_manipulator(self) -> None:
        """Infosys must classify below the grey zone."""
        result = analyze(INFY_FY24, INFY_FY23)
        assert result.verdict == "non_manipulator"

    def test_neutral_indices_yield_non_manipulator(self) -> None:
        """All-neutral indices (no YoY change, zero accruals) score ≈ −2.48."""
        neutral = BeneishIndices(
            dsri=1.0, gmi=1.0, aqi=1.0, sgi=1.0, depi=1.0, sgai=1.0, lvgi=1.0, tata=0.0
        )
        result = calculate(neutral)
        assert result.m_score == pytest.approx(-2.48, abs=0.01)
        assert result.verdict == "non_manipulator"

    def test_aggressive_profile_is_manipulator(self) -> None:
        """Ballooning receivables, soft assets and accruals must flag."""
        aggressive = BeneishIndices(
            dsri=2.5, gmi=1.2, aqi=1.5, sgi=1.8, depi=0.7, sgai=0.9, lvgi=1.4, tata=0.18
        )
        result = calculate(aggressive)
        assert result.verdict == "manipulator"

    def test_returns_result_with_indices(self) -> None:
        """The result must carry the indices it was computed from."""
        result = analyze(INFY_FY24, INFY_FY23)
        assert isinstance(result, BeneishResult)
        assert isinstance(result.indices, BeneishIndices)


class TestEdgeCases:
    """Zero denominators must degrade to neutral values, not raise."""

    def test_zero_revenue_neutralises_revenue_indices(self) -> None:
        zero_rev = BeneishYear(
            revenue=0, cogs=0, receivables=100, current_assets=500,
            ppe=200, total_assets=1000, depreciation=50, sga=80,
            current_liabilities=300, long_term_debt=100, net_income=10, cfo=20,
        )
        indices = compute_indices(zero_rev, INFY_FY23)
        assert indices.dsri == 1.0
        assert indices.gmi == 1.0
        assert indices.sgai == 1.0
        # SGI itself is well-defined at zero current revenue: 0 / prior = 0.
        assert indices.sgi == 0.0

    def test_zero_total_assets_neutralises_tata(self) -> None:
        zero_ta = BeneishYear(
            revenue=100, cogs=60, receivables=10, current_assets=50,
            ppe=20, total_assets=0, depreciation=5, sga=8,
            current_liabilities=30, long_term_debt=10, net_income=10, cfo=8,
        )
        indices = compute_indices(zero_ta, INFY_FY23)
        assert indices.tata == 0.0
        assert indices.aqi == 1.0
        assert indices.lvgi == 1.0
