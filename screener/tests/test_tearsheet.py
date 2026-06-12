"""Tests for the LLM-backed investment tearsheet exporter.

A fake chat client stands in for the Groq SDK, so no API key or network call is
needed. PDF export is exercised against a temp dir.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from screener.exporters import tearsheet as ts
from screener.exporters.tearsheet import (
    Tearsheet,
    TearsheetError,
    TearsheetInput,
    build_prompt,
    generate_summary,
    generate_tearsheet,
    render_streamlit,
    resolve_provider,
    to_pdf,
)

_SUMMARY = (
    "Trend Analysis\nRevenue compounded steadily.\n\n"
    "Red Flags\nNone material.\n\n"
    "Peer Comparison\nRanks #1 of 3 on ROCE.\n\n"
    "Overall View\nHigh-quality compounder."
)


@pytest.fixture()
def sample_input() -> TearsheetInput:
    peers = pd.DataFrame(
        {"name": ["TCS", "Infosys"], "roce": [0.66, 0.33], "rank_composite": [1, 2]},
        index=["TCS", "INFY"],
    )
    return TearsheetInput(
        symbol="INFY",
        name="Infosys Ltd",
        financials={"revenue_fy24": 153670, "ebitda_margin": 0.21},
        metrics={"beneish_verdict": "non_manipulator", "earnings_quality": "healthy"},
        peer_ranking=peers,
    )


class _FakeClient:
    """Records the create() call and returns a canned completion."""

    def __init__(self, content: str = _SUMMARY) -> None:
        self.content = content
        self.last_kwargs: dict | None = None
        message = SimpleNamespace(content=content)
        choice = SimpleNamespace(message=message)
        self._response = SimpleNamespace(choices=[choice])
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class TestResolveProvider:
    def test_default_is_groq(self) -> None:
        assert resolve_provider() == "groq"

    def test_forced_to_groq_even_if_config_says_otherwise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(ts._llm_cfg, "provider", "anthropic")
        assert resolve_provider() == "groq"


class TestBuildPrompt:
    def test_system_prompt_lists_four_sections(self, sample_input: TearsheetInput) -> None:
        system, _user = build_prompt(sample_input)
        for heading in ("Trend Analysis", "Red Flags", "Peer Comparison", "Overall View"):
            assert heading in system

    def test_user_prompt_carries_company_and_data(self, sample_input: TearsheetInput) -> None:
        _system, user = build_prompt(sample_input)
        assert "Infosys Ltd" in user and "INFY" in user
        assert "beneish_verdict" in user
        assert "non_manipulator" in user

    def test_user_prompt_includes_peer_table(self, sample_input: TearsheetInput) -> None:
        _system, user = build_prompt(sample_input)
        assert "TCS" in user and "rank_composite" in user

    def test_empty_blocks_omitted(self) -> None:
        bare = TearsheetInput(symbol="X", name="X Co")
        _system, user = build_prompt(bare)
        assert "Financials" not in user        # empty dict block dropped
        assert "Peer Ranking" not in user


class TestGenerateSummary:
    def test_returns_client_content(self, sample_input: TearsheetInput) -> None:
        client = _FakeClient()
        assert generate_summary(sample_input, client=client) == _SUMMARY.strip()

    def test_passes_config_model_and_messages(self, sample_input: TearsheetInput) -> None:
        client = _FakeClient()
        generate_summary(sample_input, client=client)
        kwargs = client.last_kwargs
        assert kwargs["model"] == ts._llm_cfg["model"]
        roles = [m["role"] for m in kwargs["messages"]]
        assert roles == ["system", "user"]

    def test_empty_response_raises(self, sample_input: TearsheetInput) -> None:
        with pytest.raises(TearsheetError):
            generate_summary(sample_input, client=_FakeClient(content="   "))

    def test_missing_api_key_raises(
        self, sample_input: TearsheetInput, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no injected client and no key in env, a clear error is raised."""
        monkeypatch.delenv(ts._llm_cfg["api_key_env"], raising=False)
        with pytest.raises(TearsheetError):
            generate_summary(sample_input)


class TestToPdf:
    def test_writes_valid_pdf(self, sample_input: TearsheetInput, tmp_path) -> None:
        path = to_pdf(_SUMMARY, sample_input, out_dir=tmp_path)
        assert path.exists()
        assert path.read_bytes().startswith(b"%PDF")

    def test_default_filename_uses_symbol(self, sample_input: TearsheetInput, tmp_path) -> None:
        path = to_pdf(_SUMMARY, sample_input, out_dir=tmp_path)
        assert path.name == "INFY_tearsheet.pdf"


class TestRenderStreamlit:
    def test_calls_streamlit_methods(self, sample_input: TearsheetInput) -> None:
        calls: list[tuple[str, object]] = []
        fake_st = SimpleNamespace(
            subheader=lambda t: calls.append(("subheader", t)),
            markdown=lambda t: calls.append(("markdown", t)),
            caption=lambda t: calls.append(("caption", t)),
            dataframe=lambda d: calls.append(("dataframe", d)),
        )
        render_streamlit(sample_input, _SUMMARY, st_module=fake_st)
        kinds = [c[0] for c in calls]
        assert "subheader" in kinds
        assert ("markdown", _SUMMARY) in calls
        assert "dataframe" in kinds          # peer table rendered


class TestGenerateTearsheet:
    def test_end_to_end_with_pdf(self, sample_input: TearsheetInput, tmp_path) -> None:
        result = generate_tearsheet(sample_input, client=_FakeClient(), out_dir=tmp_path)
        assert isinstance(result, Tearsheet)
        assert result.summary == _SUMMARY.strip()
        assert result.pdf_path is not None and result.pdf_path.exists()

    def test_skip_pdf(self, sample_input: TearsheetInput) -> None:
        result = generate_tearsheet(sample_input, client=_FakeClient(), make_pdf=False)
        assert result.pdf_path is None
