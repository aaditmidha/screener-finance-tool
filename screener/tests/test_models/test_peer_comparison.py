"""Tests for the peer comparison model.

Validation data: Indian IT-services peers (TCS, Infosys, Wipro), FY21–FY24
revenue and FY24 profitability (₹ crore, approximate published figures). The
sector ordering is well known — TCS is the most capital-efficient, Wipro the
least — which lets us assert the ranking deterministically.
"""

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from screener.models import peer_comparison as pc
from screener.models.peer_comparison import (
    PeerComparison,
    PeerMetrics,
    build_comparison,
    compute_metrics,
    discover_peers,
    rank_peers,
)


@dataclass
class Rec:
    """Minimal annual record standing in for an AnnualData ORM row."""

    fiscal_year_end: date
    revenue: float
    ebit: float | None = None
    net_income: float | None = None
    total_debt: float | None = None
    shareholders_equity: float | None = None
    free_cash_flow: float | None = None
    total_assets: float | None = None
    eps: float | None = None


def _records(revenues: list[tuple[int, float]], **latest: float) -> list[Rec]:
    """Build a record list from (year, revenue) pairs; latest year gets *latest*."""
    recs = [Rec(fiscal_year_end=date(y, 3, 31), revenue=r) for y, r in revenues]
    for k, v in latest.items():
        setattr(recs[-1], k, v)
    return recs


# FY21–FY24 revenue + FY24 EBIT / PAT / equity / debt (₹ crore, approximate).
TCS = _records(
    [(2021, 164_177), (2022, 191_754), (2023, 225_458), (2024, 240_893)],
    ebit=59_719, net_income=45_908, shareholders_equity=90_127, total_debt=0,
)
INFY = _records(
    [(2021, 100_472), (2022, 121_641), (2023, 146_767), (2024, 153_670)],
    ebit=32_016, net_income=26_233, shareholders_equity=88_010, total_debt=8_359,
)
WIPRO = _records(
    [(2021, 61_943), (2022, 79_093), (2023, 90_488), (2024, 89_760)],
    ebit=12_000, net_income=11_045, shareholders_equity=76_000, total_debt=15_000,
)


class TestDiscoverPeers:
    _HTML = """
    <table>
      <a href="/company/TCS/">TCS</a>
      <a href="/company/INFY/consolidated/">Infosys</a>
      <a href="/company/WIPRO/">Wipro</a>
      <a href="/company/INFY/">Infosys dup</a>
      <a href="/login/">Login</a>
    </table>
    """

    def test_extracts_peer_symbols(self) -> None:
        peers = discover_peers(self._HTML, base_symbol="INFY")
        assert peers == ["TCS", "WIPRO"]   # base excluded, dup collapsed

    def test_excludes_base_case_insensitive(self) -> None:
        peers = discover_peers(self._HTML, base_symbol="infy")
        assert "INFY" not in peers

    def test_respects_max_peers(self) -> None:
        peers = discover_peers(self._HTML, base_symbol="INFY", max_peers=1)
        assert peers == ["TCS"]

    def test_ignores_non_company_links(self) -> None:
        peers = discover_peers(self._HTML, base_symbol="INFY")
        assert "LOGIN" not in peers


class TestComputeMetrics:
    def test_tcs_roce(self) -> None:
        # 59719 / (90127 + 0)
        m = compute_metrics("TCS", "TCS Ltd", TCS)
        assert m.roce == pytest.approx(0.6626, abs=1e-3)

    def test_tcs_roe(self) -> None:
        # 45908 / 90127
        m = compute_metrics("TCS", "TCS Ltd", TCS)
        assert m.roe == pytest.approx(0.5094, abs=1e-3)

    def test_infy_roce_includes_debt_in_capital(self) -> None:
        # 32016 / (88010 + 8359)
        m = compute_metrics("INFY", "Infosys", INFY)
        assert m.roce == pytest.approx(0.3322, abs=1e-3)

    def test_revenue_cagr_three_year(self) -> None:
        # (240893 / 164177) ** (1/3) - 1
        m = compute_metrics("TCS", "TCS Ltd", TCS)
        assert m.revenue_growth == pytest.approx(0.1363, abs=1e-3)

    def test_zero_equity_gives_zero_ratios(self) -> None:
        recs = _records([(2023, 100), (2024, 120)], ebit=10, net_income=8,
                        shareholders_equity=0, total_debt=0)
        m = compute_metrics("X", "X", recs)
        assert m.roce == 0.0
        assert m.roe == 0.0

    def test_single_record_zero_growth(self) -> None:
        recs = _records([(2024, 120)], ebit=10, net_income=8,
                        shareholders_equity=100, total_debt=0)
        assert compute_metrics("X", "X", recs).revenue_growth == 0.0

    def test_empty_records_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_metrics("X", "X", [])


class TestBuildComparison:
    def test_frame_shape_and_index(self) -> None:
        metrics = [compute_metrics(s, s, r)
                   for s, r in [("TCS", TCS), ("INFY", INFY), ("WIPRO", WIPRO)]]
        df = build_comparison(metrics)
        assert list(df.index) == ["TCS", "INFY", "WIPRO"]
        assert set(df.columns) == {"name", "roce", "roe", "revenue_growth"}

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            build_comparison([])


class TestRankPeers:
    @pytest.fixture()
    def ranked(self) -> pd.DataFrame:
        metrics = [compute_metrics(s, s, r)
                   for s, r in [("TCS", TCS), ("INFY", INFY), ("WIPRO", WIPRO)]]
        return rank_peers(build_comparison(metrics))

    def test_roce_ranking(self, ranked: pd.DataFrame) -> None:
        assert ranked.loc["TCS", "rank_roce"] == 1
        assert ranked.loc["WIPRO", "rank_roce"] == 3

    def test_revenue_growth_ranking(self, ranked: pd.DataFrame) -> None:
        # Infosys has the highest 3-year revenue CAGR of the three.
        assert ranked.loc["INFY", "rank_revenue_growth"] == 1

    def test_composite_leader_is_tcs(self, ranked: pd.DataFrame) -> None:
        assert ranked.index[0] == "TCS"
        assert ranked.loc["TCS", "rank_composite"] == 1

    def test_composite_laggard_is_wipro(self, ranked: pd.DataFrame) -> None:
        assert ranked.loc["WIPRO", "rank_composite"] == 3

    def test_sorted_by_composite_rank(self, ranked: pd.DataFrame) -> None:
        assert list(ranked["rank_composite"]) == sorted(ranked["rank_composite"])

    def test_all_equal_metric_normalises_to_half(self) -> None:
        """A flat metric must not break ranking (min-max guards div-by-zero)."""
        flat = [
            PeerMetrics("A", "A", roce=0.2, roe=0.2, revenue_growth=0.1),
            PeerMetrics("B", "B", roce=0.2, roe=0.2, revenue_growth=0.1),
        ]
        ranked = rank_peers(build_comparison(flat))
        assert ranked["composite_score"].nunique() == 1
        assert (ranked["rank_composite"] == 1).all()   # tie → both rank 1 (method=min)


class TestOrchestration:
    class _FakeCompanyRepo:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self._id = 0

        def upsert(self, symbol: str, name: str, **kw: object) -> SimpleNamespace:
            self.calls.append(symbol)
            self._id += 1
            return SimpleNamespace(id=self._id, symbol=symbol, name=name)

    class _FakeAnnualRepo:
        def __init__(self) -> None:
            self.rows: list[tuple] = []

        def upsert(self, company_id: int, fiscal_year_end: date, **fields: object) -> None:
            self.rows.append((company_id, fiscal_year_end, fields))

    # The injected discovery callable returns peer symbols directly (the
    # service resolves these from Screener's peers API).
    _PEERS = ["TCS", "WIPRO", "BADCO"]

    def _data_for(self, symbol: str) -> tuple[str, list[Rec]]:
        table = {"INFY": INFY, "TCS": TCS, "WIPRO": WIPRO}
        if symbol == "BADCO":
            raise RuntimeError("scrape failed")
        return symbol, table[symbol]

    def test_compare_end_to_end(self) -> None:
        company_repo = self._FakeCompanyRepo()
        annual_repo = self._FakeAnnualRepo()
        comparer = PeerComparison(
            company_repo=company_repo,
            annual_repo=annual_repo,
            discover_peers=lambda base: list(self._PEERS),
            fetch_annual_data=self._data_for,
        )

        result = comparer.compare("INFY")

        # Base + the two good peers ranked; BADCO skipped on fetch error.
        assert set(result.index) == {"INFY", "TCS", "WIPRO"}
        assert "BADCO" not in result.index
        assert result.index[0] == "TCS"               # composite leader
        # Persistence happened for every successfully fetched company.
        assert set(company_repo.calls) == {"INFY", "TCS", "WIPRO"}
        assert len(annual_repo.rows) == 12            # 3 companies × 4 years

    def test_progress_callback_invoked_per_company(self) -> None:
        seen: list[tuple[str, int, int]] = []
        comparer = PeerComparison(
            company_repo=self._FakeCompanyRepo(),
            annual_repo=self._FakeAnnualRepo(),
            discover_peers=lambda base: ["TCS", "WIPRO"],
            fetch_annual_data=self._data_for,
        )
        comparer.compare("INFY", on_progress=lambda s, i, t: seen.append((s, i, t)))
        assert seen[0] == ("INFY", 1, 3)
        assert [s for s, _i, _t in seen] == ["INFY", "TCS", "WIPRO"]

    def test_compare_raises_when_nothing_gathered(self) -> None:
        comparer = PeerComparison(
            company_repo=self._FakeCompanyRepo(),
            annual_repo=self._FakeAnnualRepo(),
            discover_peers=lambda base: [],              # no peers
            fetch_annual_data=lambda s: (s, []),         # no data
        )
        with pytest.raises(ValueError):
            comparer.compare("INFY")
