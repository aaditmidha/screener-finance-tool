"""Smoke tests — verify the package loads and config is well-formed."""

from screener.config import CONFIG


def test_config_loads() -> None:
    """CONFIG should be a non-empty dict after import."""
    assert isinstance(CONFIG, dict)
    assert len(CONFIG) > 0


def test_config_has_required_sections() -> None:
    """config.yaml must contain all top-level sections the app depends on."""
    required = {"scraper", "database", "thresholds", "dcf", "exporters", "logging"}
    assert required.issubset(CONFIG.keys())


def test_scraper_base_url_is_screener() -> None:
    """Scraper base URL must point to screener.in."""
    base_url: str = CONFIG["scraper"]["base_url"]
    assert "screener.in" in base_url


def test_dcf_discount_rate_is_positive() -> None:
    """Default DCF discount rate must be a positive number."""
    dr = CONFIG["dcf"]["default_discount_rate"]
    assert isinstance(dr, float | int)
    assert dr > 0


def test_beneish_cutoffs_are_ordered() -> None:
    """Grey-zone lower bound must be less than manipulation cutoff."""
    t = CONFIG["thresholds"]["beneish_m_score"]
    assert t["grey_zone_lower"] < t["manipulation_cutoff"]
