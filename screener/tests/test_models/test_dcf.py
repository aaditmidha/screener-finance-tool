"""Tests for the forward and reverse DCF models.

Validation data: Infosys-scale figures — FCF ≈ ₹23,000 cr, ≈415 cr shares,
net cash ≈ ₹35,000 cr (entered as net_debt = −35,000).
"""

import pytest

from screener.models.dcf import (
    DCFResult,
    ReverseDCFResult,
    calculate,
    implied_growth,
)

# Infosys-scale inputs (₹ crore / crore shares, approximate).
INFY_FCF = 23_000.0
INFY_SHARES = 415.0
INFY_NET_DEBT = -35_000.0   # net cash position


class TestForwardDCF:
    """Forward intrinsic-value calculation."""

    def test_returns_dcf_result(self) -> None:
        result = calculate(INFY_FCF, [0.10], INFY_SHARES)
        assert isinstance(result, DCFResult)

    def test_intrinsic_value_positive(self) -> None:
        result = calculate(INFY_FCF, [0.10], INFY_SHARES)
        assert result.intrinsic_value > 0

    def test_mos_price_below_intrinsic(self) -> None:
        result = calculate(INFY_FCF, [0.10], INFY_SHARES)
        assert result.mos_price < result.intrinsic_value

    def test_net_cash_increases_value(self) -> None:
        """Net cash (negative net debt) must add to equity value."""
        without = calculate(INFY_FCF, [0.10], INFY_SHARES, net_debt=0)
        with_cash = calculate(INFY_FCF, [0.10], INFY_SHARES, net_debt=INFY_NET_DEBT)
        assert with_cash.intrinsic_value > without.intrinsic_value

    def test_higher_growth_higher_value(self) -> None:
        """Intrinsic value must be strictly increasing in growth."""
        low = calculate(INFY_FCF, [0.05], INFY_SHARES)
        high = calculate(INFY_FCF, [0.15], INFY_SHARES)
        assert high.intrinsic_value > low.intrinsic_value

    def test_growth_rates_padded_with_last_value(self) -> None:
        """A short rate list must behave like the rate repeated every year."""
        short = calculate(INFY_FCF, [0.08], INFY_SHARES, projection_years=10)
        full = calculate(INFY_FCF, [0.08] * 10, INFY_SHARES, projection_years=10)
        assert short.intrinsic_value == pytest.approx(full.intrinsic_value)

    def test_discount_rate_must_exceed_terminal_growth(self) -> None:
        with pytest.raises(ValueError):
            calculate(INFY_FCF, [0.10], INFY_SHARES, discount_rate=0.04,
                      terminal_growth_rate=0.05)

    def test_zero_shares_returns_zero(self) -> None:
        result = calculate(INFY_FCF, [0.10], shares_outstanding=0)
        assert result.intrinsic_value == 0.0


class TestReverseDCF:
    """Implied-growth solver."""

    def test_round_trip_recovers_known_growth(self) -> None:
        """Pricing at g=8% then solving must recover g ≈ 8%."""
        price = calculate(
            INFY_FCF, [0.08], INFY_SHARES, net_debt=INFY_NET_DEBT
        ).intrinsic_value
        result = implied_growth(price, INFY_FCF, INFY_SHARES, net_debt=INFY_NET_DEBT)
        assert result.implied_growth_rate == pytest.approx(0.08, abs=1e-3)

    def test_model_price_matches_market_price(self) -> None:
        """The solver must converge to the market price within tolerance."""
        result = implied_growth(1_420.0, INFY_FCF, INFY_SHARES, net_debt=INFY_NET_DEBT)
        assert result.model_price == pytest.approx(1_420.0, rel=0.01)

    def test_infosys_implied_growth_plausible(self) -> None:
        """At ≈ ₹1,420/share the market prices Infosys for positive but
        sub-25% perpetual-decade FCF growth — not hyper-growth."""
        result = implied_growth(1_420.0, INFY_FCF, INFY_SHARES, net_debt=INFY_NET_DEBT)
        assert 0.0 < result.implied_growth_rate < 0.25

    def test_higher_price_implies_higher_growth(self) -> None:
        cheap = implied_growth(1_000.0, INFY_FCF, INFY_SHARES, net_debt=INFY_NET_DEBT)
        dear = implied_growth(1_800.0, INFY_FCF, INFY_SHARES, net_debt=INFY_NET_DEBT)
        assert dear.implied_growth_rate > cheap.implied_growth_rate

    def test_returns_reverse_result(self) -> None:
        result = implied_growth(1_420.0, INFY_FCF, INFY_SHARES, net_debt=INFY_NET_DEBT)
        assert isinstance(result, ReverseDCFResult)
        assert result.iterations >= 1

    def test_zero_price_raises(self) -> None:
        with pytest.raises(ValueError):
            implied_growth(0.0, INFY_FCF, INFY_SHARES)

    def test_negative_fcf_raises(self) -> None:
        """Reverse DCF on negative FCF has no meaningful solution."""
        with pytest.raises(ValueError):
            implied_growth(1_420.0, -5_000.0, INFY_SHARES)

    def test_zero_shares_raises(self) -> None:
        with pytest.raises(ValueError):
            implied_growth(1_420.0, INFY_FCF, 0.0)

    def test_absurdly_high_price_raises(self) -> None:
        """A price unreachable even at the max growth bound must raise."""
        with pytest.raises(ValueError):
            implied_growth(10_000_000.0, INFY_FCF, INFY_SHARES)
