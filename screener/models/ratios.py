"""Common financial ratio calculations."""

import logging

logger = logging.getLogger(__name__)


def roe(net_income: float, avg_equity: float) -> float:
    """Return Return on Equity (net_income / avg_equity).

    Args:
        net_income: Net profit for the period.
        avg_equity: Average shareholders' equity over the period.

    Returns:
        ROE as a decimal (e.g. 0.18 = 18 %).
    """
    if avg_equity == 0:
        return 0.0
    return net_income / avg_equity


def roce(ebit: float, capital_employed: float) -> float:
    """Return Return on Capital Employed (ebit / capital_employed).

    Args:
        ebit: Earnings before interest and taxes.
        capital_employed: Total assets minus current liabilities.

    Returns:
        ROCE as a decimal.
    """
    if capital_employed == 0:
        return 0.0
    return ebit / capital_employed


def debt_to_equity(total_debt: float, shareholders_equity: float) -> float:
    """Return Debt-to-Equity ratio.

    Args:
        total_debt: Total interest-bearing debt.
        shareholders_equity: Book value of equity.

    Returns:
        D/E ratio.
    """
    if shareholders_equity == 0:
        return float("inf")
    return total_debt / shareholders_equity


def current_ratio(current_assets: float, current_liabilities: float) -> float:
    """Return Current Ratio (current_assets / current_liabilities).

    Args:
        current_assets: Total current assets.
        current_liabilities: Total current liabilities.

    Returns:
        Current ratio.
    """
    if current_liabilities == 0:
        return float("inf")
    return current_assets / current_liabilities


def pe_ratio(market_price: float, eps: float) -> float:
    """Return Price-to-Earnings ratio.

    Args:
        market_price: Current market price per share.
        eps: Earnings per share (trailing twelve months).

    Returns:
        P/E ratio, or inf if EPS is zero.
    """
    if eps == 0:
        return float("inf")
    return market_price / eps
