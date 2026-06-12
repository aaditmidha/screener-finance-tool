"""Custom formula screener: user-defined metrics over downloaded companies.

Lets a user write an arithmetic formula over named financial variables —
e.g. ``(pat / revenue) * revenue_growth_3yr`` — and rank every company in the
local database by it. This turns the tool from a downloader into a research
platform.

Safety: formulas are parsed with :mod:`ast` and only arithmetic is allowed —
numbers, named variables, ``+ - * / **`` and unary minus. Function calls,
attribute access, subscripts, lambdas and everything else are rejected, so a
formula can never execute code.
"""

import ast
import logging
from collections.abc import Sequence
from typing import Any

import pandas as pd

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_MAX_LEN = CONFIG["custom_screener"]["max_formula_length"]

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARY = (ast.USub, ast.UAdd)


class FormulaError(ValueError):
    """Raised when a formula is syntactically invalid or uses disallowed syntax."""


class UnknownVariableError(FormulaError):
    """Raised when a formula references a variable that is not available."""

    def __init__(self, name: str, available: Sequence[str]) -> None:
        """Store the missing name and the list of valid variables.

        Args:
            name: The unknown variable referenced by the formula.
            available: Names that are valid in this context.
        """
        self.name = name
        self.available = sorted(available)
        super().__init__(f"Unknown variable {name!r}. Available: {', '.join(self.available)}")


def _validate_node(node: ast.AST) -> None:
    """Recursively reject any AST node outside the arithmetic whitelist.

    Args:
        node: Root of the parsed expression.

    Raises:
        FormulaError: On any disallowed construct.
    """
    if isinstance(node, ast.Expression):
        _validate_node(node.body)
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise FormulaError(f"Operator {type(node.op).__name__} is not allowed")
        _validate_node(node.left)
        _validate_node(node.right)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY):
            raise FormulaError(f"Unary {type(node.op).__name__} is not allowed")
        _validate_node(node.operand)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise FormulaError(f"Only numeric constants allowed, got {node.value!r}")
    elif isinstance(node, ast.Name):
        pass  # resolved against the variables dict at evaluation time
    else:
        raise FormulaError(f"Syntax {type(node).__name__} is not allowed in formulas")


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    """Evaluate a validated AST node against *variables*.

    Args:
        node: A node already passed through :func:`_validate_node`.
        variables: Variable name → value mapping.

    Returns:
        The numeric result.

    Raises:
        UnknownVariableError: If a name is not in *variables*.
        ZeroDivisionError: Propagated for division by zero.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise UnknownVariableError(node.id, list(variables))
        return float(variables[node.id])
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, variables)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left ** right  # Pow — the only remaining allowed operator
    raise FormulaError(f"Unexpected node {type(node).__name__}")  # defensive


def evaluate_formula(formula: str, variables: dict[str, float]) -> float:
    """Safely evaluate an arithmetic *formula* against named *variables*.

    Args:
        formula: Expression such as ``(pat / revenue) * revenue_growth_3yr``.
        variables: Variable name → value mapping.

    Returns:
        The numeric result.

    Raises:
        FormulaError: For syntax errors, disallowed constructs, or over-length
            formulas.
        UnknownVariableError: For unknown variable names.
        ZeroDivisionError: If the formula divides by zero.
    """
    if len(formula) > _MAX_LEN:
        raise FormulaError(f"Formula exceeds the {_MAX_LEN}-character limit")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc.msg}") from exc
    _validate_node(tree)
    return _eval_node(tree, variables)


def _growth(values: list[float | None], years: int) -> float | None:
    """CAGR over the last *years* periods of a value series, or None."""
    series = [v for v in values if v is not None]
    if len(series) < 2:
        return None
    window = series[-(years + 1):]
    first, last = window[0], window[-1]
    periods = len(window) - 1
    if first <= 0 or last <= 0 or periods < 1:
        return None
    return (last / first) ** (1 / periods) - 1


def company_variables(records: Sequence[Any]) -> dict[str, float]:
    """Derive the standard screener variables from a company's annual rows.

    Args:
        records: AnnualData-like rows ordered oldest → newest, each exposing
            revenue, ebit, net_income, total_assets, total_debt,
            shareholders_equity and eps attributes.

    Returns:
        Variable name → value. Only computable variables are present; e.g.
        ``revenue_growth_3yr`` is omitted with < 2 years of revenue.
    """
    if not records:
        return {}
    latest = records[-1]

    out: dict[str, float] = {}
    for name, attr in (
        ("revenue", "revenue"), ("ebit", "ebit"), ("pat", "net_income"),
        ("total_assets", "total_assets"), ("debt", "total_debt"),
        ("equity", "shareholders_equity"), ("eps", "eps"),
    ):
        value = getattr(latest, attr, None)
        if value is not None:
            out[name] = float(value)

    equity = out.get("equity")
    debt = out.get("debt")
    if equity and equity > 0:
        if "pat" in out:
            out["roe"] = out["pat"] / equity
        if "ebit" in out and debt is not None:
            out["roce"] = out["ebit"] / (equity + debt)
        if debt is not None:
            out["debt_to_equity"] = debt / equity
    if out.get("revenue"):
        if "pat" in out:
            out["pat_margin"] = out["pat"] / out["revenue"]
        if "ebit" in out:
            out["ebit_margin"] = out["ebit"] / out["revenue"]

    rev_growth = _growth([getattr(r, "revenue", None) for r in records], 3)
    if rev_growth is not None:
        out["revenue_growth_3yr"] = rev_growth
    pat_growth = _growth([getattr(r, "net_income", None) for r in records], 3)
    if pat_growth is not None:
        out["pat_growth_3yr"] = pat_growth
    return out


def screen(
    companies: dict[str, Sequence[Any]], formula: str
) -> pd.DataFrame:
    """Compute *formula* for every company and rank descending.

    Companies whose data cannot support the formula (missing variables,
    division by zero) are skipped with a warning rather than failing the run.

    Args:
        companies: Symbol → annual rows (oldest → newest).
        formula: The user's arithmetic formula.

    Returns:
        DataFrame indexed by symbol with ``value`` and ``rank`` columns,
        sorted best-first.

    Raises:
        FormulaError: If the formula itself is invalid (checked up front).
        ValueError: If no company could be evaluated.
    """
    # Validate syntax once before touching data, so a bad formula fails fast.
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc.msg}") from exc
    _validate_node(tree)

    rows: dict[str, float] = {}
    for symbol, records in companies.items():
        variables = company_variables(records)
        try:
            rows[symbol] = evaluate_formula(formula, variables)
        except (UnknownVariableError, ZeroDivisionError) as exc:
            logger.warning("Skipping %s: %s", symbol, exc)

    if not rows:
        raise ValueError("No company had the data required by this formula")

    df = pd.DataFrame({"value": pd.Series(rows)})
    df["rank"] = df["value"].rank(ascending=False, method="min").astype(int)
    df = df.sort_values("rank")
    df.index.name = "symbol"
    logger.info("Screened %d companies on %r", len(df), formula)
    return df
