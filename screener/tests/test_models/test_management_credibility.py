"""Tests for the management credibility tracker."""

from types import SimpleNamespace

import pytest

from screener.models.management_credibility import (
    CredibilityResult,
    GuidanceItem,
    evaluate,
    extract_guidance,
    pair_with_actuals,
)


def _item(year: int, guided: float, actual: float | None) -> GuidanceItem:
    return GuidanceItem(fiscal_year=year, metric="revenue_growth", guided=guided, actual=actual)


class _FakeClient:
    """Returns canned content from chat.completions.create()."""

    def __init__(self, content: str) -> None:
        message = SimpleNamespace(content=content)
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: response)
        )


class TestEvaluate:
    def test_perfect_delivery_is_trustworthy(self) -> None:
        """Guidance hit exactly every year → hit rate 1, no bias, 10/10."""
        items = [_item(2020 + i, 0.15, 0.15) for i in range(8)]
        result = evaluate(items)
        assert result.hit_rate == 1.0
        assert result.bias == pytest.approx(0.0)
        assert result.score == pytest.approx(10.0)
        assert result.rating == "trustworthy"

    def test_chronic_overpromising_is_unreliable(self) -> None:
        """Delivering ~half of guidance every year must score poorly."""
        items = [_item(2020 + i, 0.20, 0.10) for i in range(8)]
        result = evaluate(items)
        assert result.hit_rate == 0.0
        assert result.bias == pytest.approx(-0.5)
        assert result.rating == "unreliable"

    def test_within_tolerance_counts_as_hit(self) -> None:
        """Actual within ±10% of guided counts as delivered."""
        result = evaluate([_item(2024, 0.20, 0.19)])   # 5% miss → hit
        assert result.hit_rate == 1.0

    def test_mixed_record(self) -> None:
        items = [
            _item(2021, 0.15, 0.15),  # hit
            _item(2022, 0.15, 0.14),  # hit (within 10%)
            _item(2023, 0.20, 0.10),  # big miss
            _item(2024, 0.20, 0.12),  # big miss
        ]
        result = evaluate(items)
        assert result.hit_rate == pytest.approx(0.5)
        assert result.rating in ("mixed", "unreliable")

    def test_items_without_actuals_excluded(self) -> None:
        items = [_item(2024, 0.15, 0.15), _item(2025, 0.18, None)]
        assert evaluate(items).evaluated == 1

    def test_no_evaluable_items_raises(self) -> None:
        with pytest.raises(ValueError):
            evaluate([_item(2025, 0.18, None)])

    def test_zero_guided_excluded(self) -> None:
        with pytest.raises(ValueError):
            evaluate([_item(2024, 0.0, 0.1)])

    def test_returns_result_type(self) -> None:
        assert isinstance(evaluate([_item(2024, 0.1, 0.1)]), CredibilityResult)


class TestPairWithActuals:
    def test_pairs_by_year_and_metric(self) -> None:
        guidance = [GuidanceItem(2024, "revenue_growth", 0.15)]
        paired = pair_with_actuals(guidance, {(2024, "revenue_growth"): 0.12})
        assert paired[0].actual == pytest.approx(0.12)

    def test_unmatched_stays_none(self) -> None:
        guidance = [GuidanceItem(2024, "capex", 700.0)]
        paired = pair_with_actuals(guidance, {(2024, "revenue_growth"): 0.12})
        assert paired[0].actual is None


class TestExtractGuidance:
    def test_parses_json_array(self) -> None:
        client = _FakeClient(
            '[{"fiscal_year": 2025, "metric": "revenue_growth", "guided_value": 0.15}]'
        )
        items = extract_guidance("We expect 15% growth in FY25.", client=client)
        assert items == [GuidanceItem(2025, "revenue_growth", 0.15)]

    def test_strips_markdown_fences(self) -> None:
        client = _FakeClient(
            '```json\n[{"fiscal_year": 2025, "metric": "capex", "guided_value": 700}]\n```'
        )
        items = extract_guidance("Capex of 700cr planned.", client=client)
        assert items[0].metric == "capex"

    def test_empty_transcript_returns_empty(self) -> None:
        assert extract_guidance("   ", client=_FakeClient("[]")) == []

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError):
            extract_guidance("text", client=_FakeClient("no json here"))

    def test_malformed_entries_skipped(self) -> None:
        client = _FakeClient(
            '[{"fiscal_year": 2025, "metric": "capex", "guided_value": 700},'
            ' {"metric": "missing_year"}]'
        )
        items = extract_guidance("text", client=client)
        assert len(items) == 1
