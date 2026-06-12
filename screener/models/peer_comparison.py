"""Sector peer comparison: discover, persist, compare and rank peers.

Given a single company symbol this module:

1. **Discovers sector peers** by parsing the peer table on the company's
   Screener.in page.
2. **Pulls each peer's annual data into the database** via an injected
   acquisition callable, persisting through the repository layer.
3. **Builds a side-by-side comparison DataFrame** of headline metrics.
4. **Ranks peers** by ROCE, ROE, revenue growth, and a weighted composite
   score (weights from config.yaml).

The network and database touch-points are injected as callables so the parsing,
metric, and ranking logic can all be unit-tested without a live Screener or DB.
ROCE uses EBIT / (equity + debt) and ROE uses net income / equity — both
derivable from the columns stored on :class:`~screener.database.models.AnnualData`.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import pandas as pd
from bs4 import BeautifulSoup

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["peer_comparison"]

# Screener company-page peer links look like /company/<SYMBOL>/ or
# /company/<SYMBOL>/consolidated/.
_PEER_HREF = re.compile(r"^/company/([A-Za-z0-9&._-]+)/")

# Ranking metrics — all "higher is better".
_RANK_METRICS = ("roce", "roe", "revenue_growth")


class YearRecord(Protocol):
    """Structural type for one fiscal year of inputs (ORM row or fixture)."""

    revenue: float | None
    ebit: float | None
    net_income: float | None
    total_debt: float | None
    shareholders_equity: float | None


@dataclass
class PeerMetrics:
    """Headline comparison metrics for a single company."""

    symbol: str
    name: str
    roce: float
    roe: float
    revenue_growth: float   # CAGR over the configured window


def discover_peers(html: str, base_symbol: str, max_peers: int | None = None) -> list[str]:
    """Extract sector-peer symbols from a Screener company page.

    Args:
        html: Raw HTML of the company's Screener page.
        base_symbol: The company being analysed; excluded from the result.
        max_peers: Cap on peers returned. Defaults to config ``max_peers``.

    Returns:
        Up to *max_peers* unique peer symbols, in page order, excluding the
        base symbol itself.
    """
    cap = max_peers if max_peers is not None else _cfg["max_peers"]
    soup = BeautifulSoup(html, "lxml")
    base = base_symbol.upper()

    peers: list[str] = []
    for anchor in soup.find_all("a", href=True):
        match = _PEER_HREF.match(anchor["href"])
        if not match:
            continue
        symbol = match.group(1).upper()
        if symbol == base or symbol in peers:
            continue
        peers.append(symbol)
        if len(peers) >= cap:
            break

    logger.info("Discovered %d peer(s) for %s", len(peers), base_symbol)
    return peers


def _cagr(records: Sequence[YearRecord], years: int) -> float:
    """Return revenue CAGR over the last *years* periods, 0.0 if undefined.

    Args:
        records: Annual records ordered oldest → newest.
        years: Look-back window in years.

    Returns:
        Compound annual growth rate as a decimal, or 0.0 when it cannot be
        computed (too few records, or a non-positive endpoint).
    """
    revenues = [r.revenue for r in records if r.revenue is not None]
    if len(revenues) < 2:
        return 0.0
    window = revenues[-(years + 1):] if years + 1 <= len(revenues) else revenues
    first, last = window[0], window[-1]
    periods = len(window) - 1
    if first <= 0 or last <= 0 or periods < 1:
        return 0.0
    return (last / first) ** (1 / periods) - 1


def compute_metrics(symbol: str, name: str, records: Sequence[YearRecord]) -> PeerMetrics:
    """Compute headline metrics for one company from its annual records.

    ROCE and ROE use the latest year; revenue growth is the CAGR over the
    configured window. Undefined ratios (zero/negative denominators) degrade
    to 0.0 rather than raising.

    Args:
        symbol: Company ticker.
        name: Company display name.
        records: Annual records ordered oldest → newest (≥ 1).

    Returns:
        A populated :class:`PeerMetrics`.

    Raises:
        ValueError: If *records* is empty.
    """
    if not records:
        raise ValueError(f"No annual records for {symbol}")

    latest = records[-1]
    equity = latest.shareholders_equity or 0.0
    debt = latest.total_debt or 0.0
    ebit = latest.ebit or 0.0
    net_income = latest.net_income or 0.0

    capital_employed = equity + debt
    roce = ebit / capital_employed if capital_employed > 0 else 0.0
    roe = net_income / equity if equity > 0 else 0.0
    revenue_growth = _cagr(records, _cfg["revenue_growth_years"])

    logger.debug(
        "%s metrics: ROCE=%.3f ROE=%.3f rev_growth=%.3f", symbol, roce, roe, revenue_growth
    )
    return PeerMetrics(
        symbol=symbol, name=name, roce=roce, roe=roe, revenue_growth=revenue_growth
    )


def build_comparison(metrics: list[PeerMetrics]) -> pd.DataFrame:
    """Assemble metrics into a side-by-side comparison DataFrame.

    Args:
        metrics: One :class:`PeerMetrics` per company.

    Returns:
        DataFrame indexed by symbol with columns name, roce, roe,
        revenue_growth.

    Raises:
        ValueError: If *metrics* is empty.
    """
    if not metrics:
        raise ValueError("Cannot build a comparison from zero companies")

    df = pd.DataFrame(
        {
            "symbol": [m.symbol for m in metrics],
            "name": [m.name for m in metrics],
            "roce": [m.roce for m in metrics],
            "roe": [m.roe for m in metrics],
            "revenue_growth": [m.revenue_growth for m in metrics],
        }
    ).set_index("symbol")
    return df


def _normalise(series: pd.Series) -> pd.Series:
    """Min-max normalise a Series to [0, 1]; all-equal values map to 0.5.

    Args:
        series: Numeric series to scale.

    Returns:
        Series scaled to [0, 1], where a flat series becomes a constant 0.5.
    """
    lo, hi = series.min(), series.max()
    if hi == lo:
        return pd.Series(0.5, index=series.index)
    return (series - lo) / (hi - lo)


def rank_peers(df: pd.DataFrame, weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Rank companies by each metric and a weighted composite score.

    For every metric in (roce, roe, revenue_growth) higher is better, so rank
    1 is the best. The composite score is the weighted sum of each metric's
    min-max normalised value (higher is better), and ``rank_composite`` ranks
    that.

    Args:
        df: Comparison DataFrame from :func:`build_comparison`.
        weights: Metric → weight mapping (should sum to 1). Defaults to config
            ``composite_weights``.

    Returns:
        A copy of *df* with added columns: ``rank_roce``, ``rank_roe``,
        ``rank_revenue_growth``, ``composite_score`` and ``rank_composite``,
        sorted by ``rank_composite`` ascending.
    """
    w = weights if weights is not None else _cfg["composite_weights"]
    out = df.copy()

    composite = pd.Series(0.0, index=out.index)
    for metric in _RANK_METRICS:
        out[f"rank_{metric}"] = out[metric].rank(ascending=False, method="min").astype(int)
        composite = composite + _normalise(out[metric]) * w[metric]

    out["composite_score"] = composite
    out["rank_composite"] = composite.rank(ascending=False, method="min").astype(int)
    out = out.sort_values("rank_composite")
    logger.info("Ranked %d companies; leader: %s", len(out), out.index[0])
    return out


class PeerComparison:
    """Orchestrates end-to-end peer discovery, persistence, and ranking."""

    def __init__(
        self,
        company_repo: Any,
        annual_repo: Any,
        fetch_page: Callable[[str], str],
        fetch_annual_data: Callable[[str], tuple[str, list[Any]]],
        config: dict[str, Any] | None = None,
    ) -> None:
        """Wire the orchestrator to its repositories and acquisition callables.

        Args:
            company_repo: A CompanyRepository (or compatible) for company rows.
            annual_repo: An AnnualDataRepository (or compatible) for annual rows.
            fetch_page: ``url -> html`` for the base company's Screener page.
            fetch_annual_data: ``symbol -> (company_name, [year_records])`` that
                scrapes a peer's annual data. Each record must expose revenue,
                ebit, net_income, total_debt, shareholders_equity and a
                ``fiscal_year_end`` date plus the persisted metric fields.
            config: Override config; defaults to the global CONFIG section.
        """
        self._company_repo = company_repo
        self._annual_repo = annual_repo
        self._fetch_page = fetch_page
        self._fetch_annual_data = fetch_annual_data
        self._cfg = (config or CONFIG)["peer_comparison"] if config else _cfg

    def _persist(self, symbol: str, name: str, records: list[Any]) -> None:
        """Persist a company and its annual rows through the repositories.

        Args:
            symbol: Company ticker.
            name: Company display name.
            records: Annual records carrying a ``fiscal_year_end`` and metrics.
        """
        company = self._company_repo.upsert(symbol=symbol, name=name)
        for rec in records:
            fields = {
                f: getattr(rec, f, None)
                for f in ("revenue", "ebit", "net_income", "free_cash_flow",
                          "total_assets", "total_debt", "shareholders_equity", "eps")
                if getattr(rec, f, None) is not None
            }
            self._annual_repo.upsert(company.id, rec.fiscal_year_end, **fields)

    def compare(self, base_symbol: str, base_page_url: str) -> pd.DataFrame:
        """Run the full pipeline for *base_symbol* and return a ranked frame.

        Discovers peers, pulls annual data for the base company and every peer
        into the database, computes metrics, and ranks them.

        Args:
            base_symbol: The company to anchor the comparison on.
            base_page_url: URL of the base company's Screener page (for peer
                discovery).

        Returns:
            A ranked comparison DataFrame (see :func:`rank_peers`).
        """
        html = self._fetch_page(base_page_url)
        peers = discover_peers(html, base_symbol, self._cfg["max_peers"])
        symbols = [base_symbol.upper(), *peers]

        metrics: list[PeerMetrics] = []
        for symbol in symbols:
            try:
                name, records = self._fetch_annual_data(symbol)
            except Exception as exc:  # acquisition is best-effort per peer
                logger.warning("Skipping %s — data fetch failed: %s", symbol, exc)
                continue
            if not records:
                logger.warning("Skipping %s — no annual data returned", symbol)
                continue
            self._persist(symbol, name, records)
            metrics.append(compute_metrics(symbol, name, records))

        if not metrics:
            raise ValueError(f"No comparable data gathered for {base_symbol} or its peers")

        return rank_peers(build_comparison(metrics))
