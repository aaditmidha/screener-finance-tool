"""Smoke tests for the Streamlit app module.

The app's Streamlit calls live inside main(); importing must not execute them,
and the non-Streamlit helpers should work standalone.
"""

from screener.scraper.parser import parse_company_financials
from screener.ui import app

_PAGE = """
<html><body><h1>Infosys</h1>
  <section id="profit-loss"><table>
    <thead><tr><th></th><th>Mar 2024</th></tr></thead>
    <tbody><tr><td>Sales</td><td>153,670</td></tr></tbody>
  </table></section>
</body></html>
"""


def test_module_imports_without_running_streamlit() -> None:
    """Importing the app must not require a Streamlit runtime."""
    assert hasattr(app, "main")
    assert callable(app.main)


def test_financials_to_excel_bytes_produces_xlsx() -> None:
    """The Excel-export helper must yield a valid .xlsx byte string."""
    fin = parse_company_financials(_PAGE)
    data = app._financials_to_excel_bytes(fin, ar_rows=[], annual_rows=[])
    # .xlsx files are zip archives — they start with the PK signature.
    assert data[:2] == b"PK"
