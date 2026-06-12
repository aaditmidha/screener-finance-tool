"""Management credibility tracker: guidance vs delivery scoring.

Serious investors care less about what management promises than whether those
promises come true. This module scores that:

1. :func:`extract_guidance` (optional, LLM-backed via Groq) pulls forward
   guidance statements out of earnings-call transcript text.
2. The caller pairs each guidance item with the actual reported figure.
3. :func:`evaluate` scores delivery on two components:
   * **hit rate** — fraction of guidance delivered within a tolerance band;
   * **bias** — the mean signed deviation (chronic over-promising drags the
     score down; chronic sandbagging also costs, but symmetric).

Thresholds, tolerance, weights and rating cutoffs come from
``thresholds.management_credibility`` in config.yaml.
"""

import json
import logging
import re
from dataclasses import dataclass

from screener.config import CONFIG
from screener.llm import ChatClient, chat

logger = logging.getLogger(__name__)

_cfg = CONFIG["thresholds"]["management_credibility"]

_EXTRACT_SYSTEM_PROMPT = """You extract forward guidance from earnings-call transcripts.
Return ONLY a JSON array, no prose. Each element:
{"fiscal_year": <int, the FY the guidance is for>,
 "metric": "<short name, e.g. revenue_growth, ebitda_margin, capex>",
 "guided_value": <number — use decimals for percentages, e.g. 0.15 for 15%>}
Only include concrete, quantified guidance. If none, return []."""

# Strips markdown code fences an LLM may wrap around JSON.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class GuidanceItem:
    """One piece of management guidance, optionally paired with the outcome.

    Attributes:
        fiscal_year: Fiscal year the guidance targets.
        metric: What was guided (e.g. "revenue_growth", "capex").
        guided: The guided value.
        actual: The delivered value, or None if not yet known/paired.
    """

    fiscal_year: int
    metric: str
    guided: float
    actual: float | None = None


@dataclass
class CredibilityResult:
    """Aggregate guidance-delivery assessment."""

    evaluated: int        # guidance items that had actuals to compare
    hit_rate: float       # fraction delivered within tolerance
    bias: float           # mean signed deviation; > 0 = under-promise/over-deliver
    score: float          # 0–10
    rating: str           # "trustworthy" | "mixed" | "unreliable"


def _clamp01(value: float) -> float:
    """Clamp *value* into [0, 1]."""
    return max(0.0, min(1.0, value))


def evaluate(items: list[GuidanceItem]) -> CredibilityResult:
    """Score management's guidance-vs-delivery record.

    Items without an actual, or with zero guided value (deviation undefined),
    are excluded from scoring.

    Args:
        items: Guidance items, ideally spanning several years.

    Returns:
        A CredibilityResult with hit rate, bias, 0–10 score and rating.

    Raises:
        ValueError: If no item is evaluable (no actuals, or all guided == 0).
    """
    evaluable = [i for i in items if i.actual is not None and i.guided != 0]
    if not evaluable:
        raise ValueError("No evaluable guidance items (need actuals and non-zero guidance)")

    tolerance = _cfg["hit_tolerance"]
    bias_cap = _cfg["bias_cap"]
    weights = _cfg["weights"]

    deviations = [(i.actual - i.guided) / abs(i.guided) for i in evaluable]
    hits = sum(1 for d in deviations if abs(d) <= tolerance)
    hit_rate = hits / len(evaluable)
    bias = sum(deviations) / len(deviations)

    # Bias component: 1.0 at zero bias, linearly down to 0.0 at |bias| ≥ cap.
    bias_component = _clamp01(1 - abs(bias) / bias_cap)
    score = 10 * (weights["hit_rate"] * hit_rate + weights["bias"] * bias_component)

    ratings = _cfg["ratings"]
    if score >= ratings["trustworthy_min"]:
        rating = "trustworthy"
    elif score >= ratings["mixed_min"]:
        rating = "mixed"
    else:
        rating = "unreliable"

    logger.info(
        "Credibility: %d items, hit_rate=%.0f%%, bias=%+.1f%%, score=%.1f (%s)",
        len(evaluable), hit_rate * 100, bias * 100, score, rating,
    )
    return CredibilityResult(
        evaluated=len(evaluable), hit_rate=hit_rate, bias=bias, score=score, rating=rating
    )


def pair_with_actuals(
    guidance: list[GuidanceItem], actuals: dict[tuple[int, str], float]
) -> list[GuidanceItem]:
    """Fill in actual outcomes for guidance items from a lookup table.

    Args:
        guidance: Extracted guidance items (actuals may be None).
        actuals: Mapping of (fiscal_year, metric) → delivered value.

    Returns:
        New items with ``actual`` populated where a match exists.
    """
    paired: list[GuidanceItem] = []
    for item in guidance:
        actual = actuals.get((item.fiscal_year, item.metric), item.actual)
        paired.append(
            GuidanceItem(
                fiscal_year=item.fiscal_year,
                metric=item.metric,
                guided=item.guided,
                actual=actual,
            )
        )
    return paired


def extract_guidance(
    transcript: str, client: ChatClient | None = None
) -> list[GuidanceItem]:
    """Extract quantified forward guidance from transcript text via the LLM.

    Args:
        transcript: Raw earnings-call transcript text.
        client: Optional injected chat client (Groq is built by default,
            following project policy).

    Returns:
        Guidance items with ``actual=None`` (pair them later via
        :func:`pair_with_actuals`).

    Raises:
        ValueError: If the LLM response is not valid JSON of the expected shape.
    """
    if not transcript or not transcript.strip():
        return []

    raw = chat(_EXTRACT_SYSTEM_PROMPT, transcript, client=client)
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Guidance extraction returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("Guidance extraction must return a JSON array")

    items: list[GuidanceItem] = []
    for entry in payload:
        try:
            items.append(
                GuidanceItem(
                    fiscal_year=int(entry["fiscal_year"]),
                    metric=str(entry["metric"]),
                    guided=float(entry["guided_value"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed guidance entry: %r", entry)
    logger.info("Extracted %d guidance item(s) from transcript", len(items))
    return items
