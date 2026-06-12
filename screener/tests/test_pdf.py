"""Tests for the basic PDF exporter."""

from pathlib import Path

import pytest

from screener.exporters import pdf


@pytest.fixture(autouse=True)
def _redirect_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write exports into a temp dir instead of the configured one."""
    monkeypatch.setitem(pdf._cfg, "output_dir", str(tmp_path))


def test_export_writes_pdf(tmp_path: Path) -> None:
    path = pdf.export([{"title": "Summary", "lines": ["line one", "line two"]}], "t.pdf")
    assert path.exists()
    assert path.read_bytes().startswith(b"%PDF")


def test_multi_section_report(tmp_path: Path) -> None:
    sections = [
        {"title": "P&L", "lines": ["Revenue: 153,670", "PAT: 26,233"]},
        {"title": "Verdict", "lines": ["Healthy"]},
    ]
    path = pdf.export(sections, "multi.pdf")
    assert path.stat().st_size > 0


def test_long_report_paginates_without_error(tmp_path: Path) -> None:
    """More lines than fit on one A4 page must trigger a page break cleanly."""
    sections = [{"title": "Long", "lines": [f"row {i}" for i in range(120)]}]
    path = pdf.export(sections, "long.pdf")
    assert path.read_bytes().startswith(b"%PDF")
