"""Discounted Cash Flow models: forward intrinsic value and reverse DCF.

Forward DCF (:func:`calculate`) projects free cash flow at given growth rates
and discounts it back to an intrinsic value per share.

Reverse DCF (:func:`implied_growth`) inverts the question: given today's
market price, what constant FCF growth rate is the market pricing in? Because
intrinsic value is strictly increasing in the growth rate, the rate is found
by bisection inside a configurable search window.
"""

import logging
from dataclasses import dataclass

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["dcf"]
_rev_cfg = CONFIG["dcf"]["reverse"]


@dataclass
class DCFResult:
    """Forward DCF output with intrinsic value and margin-of-safety price."""

    intrinsic_value: float
    mos_price: float          # margin-of-safety entry price
    discount_rate: float
    terminal_growth_rate: float
    projection_years: int


@dataclass
class ReverseDCFResult:
    """Reverse DCF output: the growth rate implied by the market price."""

    implied_growth_rate: float
    market_price: float
    model_price: float        # forward DCF value at the implied rate
    iterations: int
    discount_rate: float
    terminal_growth_rate: float
    projection_years: int


def calculate(
    free_cash_flow: float,
    growth_rates: list[float],
    shares_outstanding: float,
    net_debt: float = 0.0,
    discount_rate: float | None = None,
    terminal_growth_rate: float | None = None,
    projection_years: int | None = None,
) -> DCFResult:
    """Compute forward DCF intrinsic value per share.

    Args:
        free_cash_flow: Most recent annual free cash flow (₹ crore).
        growth_rates: Annual FCF growth rates for each projection year. If
            shorter than projection_years, the last rate is repeated.
        shares_outstanding: Shares outstanding in crore.
        net_debt: Net debt (total debt − cash) in ₹ crore. Negative for net
            cash. Defaults to 0.
        discount_rate: WACC / required return. Defaults to config value.
        terminal_growth_rate: Perpetuity growth rate. Defaults to config value.
        projection_years: Number of years to project. Defaults to config value.

    Returns:
        DCFResult with intrinsic and margin-of-safety values per share.

    Raises:
        ValueError: If the discount rate does not exceed the terminal growth
            rate (the perpetuity would be undefined).
    """
    dr = discount_rate if discount_rate is not None else _cfg["default_discount_rate"]
    tgr = terminal_growth_rate if terminal_growth_rate is not None else _cfg["default_terminal_growth_rate"]
    years = projection_years if projection_years is not None else _cfg["default_projection_years"]
    mos = _cfg["margin_of_safety"]

    if dr <= tgr:
        raise ValueError(
            f"Discount rate ({dr}) must exceed terminal growth rate ({tgr})"
        )

    # Pad / trim growth_rates to exactly `years` entries.
    rates = list(growth_rates)
    while len(rates) < years:
        rates.append(rates[-1] if rates else 0.0)
    rates = rates[:years]

    pv_fcf = 0.0
    fcf = free_cash_flow
    for i, g in enumerate(rates, start=1):
        fcf *= 1 + g
        pv_fcf += fcf / (1 + dr) ** i

    terminal_value = (fcf * (1 + tgr)) / (dr - tgr)
    pv_terminal = terminal_value / (1 + dr) ** years

    equity_value = pv_fcf + pv_terminal - net_debt
    intrinsic_per_share = equity_value / shares_outstanding if shares_outstanding else 0.0
    mos_price = intrinsic_per_share * (1 - mos)

    logger.debug(
        "DCF: intrinsic=%.2f mos_price=%.2f dr=%.2f tgr=%.2f",
        intrinsic_per_share, mos_price, dr, tgr,
    )
    return DCFResult(
        intrinsic_value=intrinsic_per_share,
        mos_price=mos_price,
        discount_rate=dr,
        terminal_growth_rate=tgr,
        projection_years=years,
    )


def implied_growth(
    market_price: float,
    free_cash_flow: float,
    shares_outstanding: float,
    net_debt: float = 0.0,
    discount_rate: float | None = None,
    terminal_growth_rate: float | None = None,
    projection_years: int | None = None,
) -> ReverseDCFResult:
    """Solve for the constant FCF growth rate implied by the market price.

    Uses bisection: intrinsic value is strictly increasing in the growth rate,
    so the implied rate is the unique root of ``value(g) − market_price``
    within the configured search window.

    Args:
        market_price: Current market price per share (must be > 0).
        free_cash_flow: Most recent annual free cash flow (must be > 0; a
            reverse DCF on zero/negative FCF has no meaningful solution).
        shares_outstanding: Shares outstanding in crore (must be > 0).
        net_debt: Net debt in ₹ crore. Negative for net cash. Defaults to 0.
        discount_rate: WACC / required return. Defaults to config value.
        terminal_growth_rate: Perpetuity growth rate. Defaults to config value.
        projection_years: Number of years to project. Defaults to config value.

    Returns:
        ReverseDCFResult with the implied growth rate and diagnostics.

    Raises:
        ValueError: If inputs are non-positive, or the market price falls
            outside the values achievable within the configured growth window.
    """
    if market_price <= 0:
        raise ValueError(f"market_price must be positive, got {market_price}")
    if free_cash_flow <= 0:
        raise ValueError(
            f"Reverse DCF requires positive free cash flow, got {free_cash_flow}"
        )
    if shares_outstanding <= 0:
        raise ValueError(f"shares_outstanding must be positive, got {shares_outstanding}")

    dr = discount_rate if discount_rate is not None else _cfg["default_discount_rate"]
    tgr = terminal_growth_rate if terminal_growth_rate is not None else _cfg["default_terminal_growth_rate"]
    years = projection_years if projection_years is not None else _cfg["default_projection_years"]

    lo: float = _rev_cfg["growth_lower_bound"]
    hi: float = _rev_cfg["growth_upper_bound"]
    tolerance: float = _rev_cfg["price_tolerance"]
    max_iterations: int = _rev_cfg["max_iterations"]

    def _value(growth: float) -> float:
        """Forward DCF per-share value at constant growth *growth*."""
        return calculate(
            free_cash_flow=free_cash_flow,
            growth_rates=[growth],
            shares_outstanding=shares_outstanding,
            net_debt=net_debt,
            discount_rate=dr,
            terminal_growth_rate=tgr,
            projection_years=years,
        ).intrinsic_value

    value_lo = _value(lo)
    value_hi = _value(hi)
    if market_price < value_lo:
        raise ValueError(
            f"Market price {market_price:.2f} is below model value {value_lo:.2f} "
            f"even at the minimum growth bound ({lo:+.0%})"
        )
    if market_price > value_hi:
        raise ValueError(
            f"Market price {market_price:.2f} exceeds model value {value_hi:.2f} "
            f"even at the maximum growth bound ({hi:+.0%})"
        )

    mid = (lo + hi) / 2
    model_price = _value(mid)
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        mid = (lo + hi) / 2
        model_price = _value(mid)
        if abs(model_price - market_price) <= market_price * tolerance:
            break
        if model_price < market_price:
            lo = mid
        else:
            hi = mid
    else:
        logger.warning(
            "Reverse DCF did not converge within %d iterations (last error %.4f)",
            max_iterations, abs(model_price - market_price),
        )

    logger.debug(
        "Reverse DCF: price=%.2f implied_growth=%.4f in %d iterations",
        market_price, mid, iterations,
    )
    return ReverseDCFResult(
        implied_growth_rate=mid,
        market_price=market_price,
        model_price=model_price,
        iterations=iterations,
        discount_rate=dr,
        terminal_growth_rate=tgr,
        projection_years=years,
    )
