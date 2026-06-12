"""Tests for the custom formula screener (safe evaluation + ranking)."""

from dataclasses import dataclass
from datetime import date

import pytest

from screener.models.custom_screener import (
    FormulaError,
    UnknownVariableError,
    company_variables,
    evaluate_formula,
    screen,
)


@dataclass
class Rec:
    fiscal_year_end: date
    revenue: float | None = None
    ebit: float | None = None
    net_income: float | None = None
    total_assets: float | None = None
    total_debt: float | None = None
    shareholders_equity: float | None = None
    eps: float | None = None


def _history(revs_pats: list[tuple[int, float, float]], **latest: float) -> list[Rec]:
    recs = [Rec(fiscal_year_end=date(y, 3, 31), revenue=r, net_income=p)
            for y, r, p in revs_pats]
    for key, value in latest.items():
        setattr(recs[-1], key, value)
    return recs


TCS = _history(
    [(2021, 164_177, 32_430), (2022, 191_754, 38_327),
     (2023, 225_458, 42_147), (2024, 240_893, 45_908)],
    ebit=59_719, shareholders_equity=90_127, total_debt=0,
)
WIPRO = _history(
    [(2021, 61_943, 10_855), (2022, 79_093, 12_230),
     (2023, 90_488, 11_350), (2024, 89_760, 11_045)],
    ebit=12_000, shareholders_equity=76_000, total_debt=15_000,
)


class TestEvaluateFormula:
    def test_arithmetic(self) -> None:
        assert evaluate_formula("(a + b) * 2", {"a": 1, "b": 2}) == 6.0

    def test_division_and_power(self) -> None:
        assert evaluate_formula("a / b ** 2", {"a": 8, "b": 2}) == 2.0

    def test_unary_minus(self) -> None:
        assert evaluate_formula("-a + 5", {"a": 3}) == 2.0

    def test_unknown_variable_lists_available(self) -> None:
        with pytest.raises(UnknownVariableError) as exc_info:
            evaluate_formula("pat / revenue", {"revenue": 100.0})
        assert "pat" in str(exc_info.value)
        assert "revenue" in str(exc_info.value)

    def test_division_by_zero_propagates(self) -> None:
        with pytest.raises(ZeroDivisionError):
            evaluate_formula("a / b", {"a": 1, "b": 0})

    def test_syntax_error_rejected(self) -> None:
        with pytest.raises(FormulaError):
            evaluate_formula("a +* b", {"a": 1, "b": 2})

    def test_over_length_rejected(self) -> None:
        with pytest.raises(FormulaError):
            evaluate_formula("1 + " * 100 + "1", {})


class TestFormulaSecurity:
    """Anything beyond arithmetic must be rejected — never executed."""

    @pytest.mark.parametrize("evil", [
        "__import__('os').system('dir')",       # call + attribute
        "().__class__",                          # attribute access
        "[1, 2][0]",                             # subscript
        "(lambda: 1)()",                         # lambda
        "a if a else 0",                         # conditional
        "'pwn' + 'ed'",                          # string constants
        "{1: 2}",                                # dict literal
        "a == 1",                                # comparison
    ])
    def test_rejected(self, evil: str) -> None:
        with pytest.raises(FormulaError):
            evaluate_formula(evil, {"a": 1.0})


class TestCompanyVariables:
    def test_derives_core_and_ratio_variables(self) -> None:
        v = company_variables(TCS)
        assert v["revenue"] == pytest.approx(240_893)
        assert v["pat"] == pytest.approx(45_908)
        assert v["roe"] == pytest.approx(45_908 / 90_127)
        assert v["roce"] == pytest.approx(59_719 / 90_127)
        assert v["pat_margin"] == pytest.approx(45_908 / 240_893)
        # (240893/164177)^(1/3) − 1
        assert v["revenue_growth_3yr"] == pytest.approx(0.1363, abs=1e-3)

    def test_empty_records(self) -> None:
        assert company_variables([]) == {}

    def test_missing_fields_omitted(self) -> None:
        v = company_variables([Rec(fiscal_year_end=date(2024, 3, 31), revenue=100.0)])
        assert "revenue" in v
        assert "roe" not in v
        assert "revenue_growth_3yr" not in v


class TestScreen:
    def test_ranks_descending(self) -> None:
        df = screen({"TCS": TCS, "WIPRO": WIPRO}, "roce")
        assert df.index[0] == "TCS"             # higher ROCE ranks first
        assert df.loc["TCS", "rank"] == 1
        assert df.loc["WIPRO", "rank"] == 2

    def test_example_formula_from_spec(self) -> None:
        """The README example formula must run end-to-end."""
        df = screen({"TCS": TCS, "WIPRO": WIPRO}, "(pat / revenue) * revenue_growth_3yr")
        assert len(df) == 2
        assert df.loc["TCS", "value"] > df.loc["WIPRO", "value"]

    def test_company_missing_variable_skipped(self) -> None:
        bare = [Rec(fiscal_year_end=date(2024, 3, 31), revenue=100.0)]
        df = screen({"TCS": TCS, "BARE": bare}, "roce")
        assert list(df.index) == ["TCS"]        # BARE skipped, not fatal

    def test_bad_formula_fails_fast(self) -> None:
        with pytest.raises(FormulaError):
            screen({"TCS": TCS}, "__import__('os')")

    def test_no_evaluable_company_raises(self) -> None:
        bare = [Rec(fiscal_year_end=date(2024, 3, 31), revenue=100.0)]
        with pytest.raises(ValueError):
            screen({"BARE": bare}, "roce")
