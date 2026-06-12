"""Tests for financial ratio calculations."""

import math
import pytest
from screener.models import ratios


def test_roe_normal() -> None:
    assert ratios.roe(net_income=100, avg_equity=500) == pytest.approx(0.20)


def test_roe_zero_equity() -> None:
    assert ratios.roe(net_income=100, avg_equity=0) == 0.0


def test_roce_normal() -> None:
    assert ratios.roce(ebit=200, capital_employed=1000) == pytest.approx(0.20)


def test_debt_to_equity_zero_equity() -> None:
    assert math.isinf(ratios.debt_to_equity(total_debt=500, shareholders_equity=0))


def test_current_ratio_normal() -> None:
    assert ratios.current_ratio(current_assets=300, current_liabilities=150) == pytest.approx(2.0)


def test_pe_ratio_zero_eps() -> None:
    assert math.isinf(ratios.pe_ratio(market_price=100, eps=0))
