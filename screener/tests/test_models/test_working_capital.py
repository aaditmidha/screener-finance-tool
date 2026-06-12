"""Tests for the working capital / cash conversion cycle model.

Validation data: a manufacturing-style quarterly profile (₹ crore,
representative of an Indian paints/FMCG maker — high inventory days, moderate
receivables, supplier credit partially offsetting). Expected values are
hand-computed with a 91-day quarter.
"""

import pytest

from screener.models.working_capital import (
    QuarterFinancials,
    ccc,
    dio,
    dpo,
    dso,
    heatmap_data,
)

# Manufacturing-style quarterly profile (₹ crore, representative values).
QUARTERS = [
    QuarterFinancials("FY24 Q1", revenue=8_400, cogs=4_800,
                      receivables=3_200, inventory=5_600, payables=3_400),
    QuarterFinancials("FY24 Q2", revenue=8_900, cogs=5_100,
                      receivables=3_350, inventory=5_900, payables=3_600),
    QuarterFinancials("FY24 Q3", revenue=9_600, cogs=5_500,
                      receivables=3_500, inventory=6_100, payables=3_900),
    QuarterFinancials("FY24 Q4", revenue=9_100, cogs=5_200,
                      receivables=3_400, inventory=5_800, payables=3_700),
]


class TestDayMetrics:
    def test_dso_hand_computed(self) -> None:
        # 3200 / 8400 × 91
        assert dso(3_200, 8_400) == pytest.approx(34.667, abs=1e-2)

    def test_dio_hand_computed(self) -> None:
        # 5600 / 4800 × 91
        assert dio(5_600, 4_800) == pytest.approx(106.167, abs=1e-2)

    def test_dpo_hand_computed(self) -> None:
        # 3400 / 4800 × 91
        assert dpo(3_400, 4_800) == pytest.approx(64.458, abs=1e-2)

    def test_explicit_day_count_overrides_config(self) -> None:
        """Passing days=365 must switch to an annual convention."""
        assert dso(3_200, 33_600, days=365) == pytest.approx(34.762, abs=1e-2)

    def test_dso_zero_revenue_returns_zero(self) -> None:
        assert dso(3_200, 0) == 0.0

    def test_dio_zero_cogs_returns_zero(self) -> None:
        assert dio(5_600, 0) == 0.0

    def test_dpo_zero_cogs_returns_zero(self) -> None:
        assert dpo(3_400, 0) == 0.0


class TestCCC:
    def test_hand_computed(self) -> None:
        # 34.667 + 106.167 − 64.458
        assert ccc(34.667, 106.167, 64.458) == pytest.approx(76.376, abs=1e-2)

    def test_negative_ccc_possible(self) -> None:
        """Supplier-financed models (e.g. retail) can run a negative cycle."""
        assert ccc(5.0, 20.0, 60.0) == pytest.approx(-35.0)


class TestHeatmapData:
    def test_structure(self) -> None:
        """Output must contain parallel series keyed for the heatmap."""
        data = heatmap_data(QUARTERS)
        assert set(data.keys()) == {"quarters", "dso", "dio", "dpo", "ccc"}
        for key in ("dso", "dio", "dpo", "ccc"):
            assert len(data[key]) == len(QUARTERS)

    def test_quarter_labels_preserved_in_order(self) -> None:
        data = heatmap_data(QUARTERS)
        assert data["quarters"] == ["FY24 Q1", "FY24 Q2", "FY24 Q3", "FY24 Q4"]

    def test_first_quarter_values(self) -> None:
        """Q1 series values must match the hand-computed day metrics."""
        data = heatmap_data(QUARTERS)
        assert data["dso"][0] == pytest.approx(34.667, abs=1e-2)
        assert data["dio"][0] == pytest.approx(106.167, abs=1e-2)
        assert data["dpo"][0] == pytest.approx(64.458, abs=1e-2)
        assert data["ccc"][0] == pytest.approx(76.375, abs=1e-2)

    def test_ccc_consistent_with_components(self) -> None:
        """Every quarter must satisfy CCC = DSO + DIO − DPO."""
        data = heatmap_data(QUARTERS)
        for i in range(len(QUARTERS)):
            expected = data["dso"][i] + data["dio"][i] - data["dpo"][i]
            assert data["ccc"][i] == pytest.approx(expected)

    def test_empty_quarters_raises(self) -> None:
        with pytest.raises(ValueError):
            heatmap_data([])

    def test_zero_revenue_quarter_degrades_gracefully(self) -> None:
        """A shutdown quarter (zero flows) must yield zeros, not raise."""
        shut = [QuarterFinancials("FY25 Q1", revenue=0, cogs=0,
                                  receivables=500, inventory=900, payables=400)]
        data = heatmap_data(shut)
        assert data["dso"][0] == 0.0
        assert data["dio"][0] == 0.0
        assert data["dpo"][0] == 0.0
        assert data["ccc"][0] == 0.0
