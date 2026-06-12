# Screener Finance Tool

## What This Is
Python tool that scrapes Screener.in for Indian stock financials,
calculates advanced metrics, and serves a Streamlit dashboard.

## Stack
- Python 3.11, SQLite, SQLAlchemy
- BeautifulSoup + Playwright for scraping
- Streamlit for UI
- pytest for tests

## Project Structure
screener/
  scraper/       ← data collection only
  models/        ← financial calculations (Beneish, DCF, ratios)
  database/      ← SQLite layer
  exporters/     ← Excel, PDF output
  ui/            ← Streamlit app
  tests/         ← pytest

## Key References
- DECISIONS.md — architecture decisions log; §7.1 has the Screener→Beneish
  field mapping and approximation policy. Update it when making non-obvious choices.

## Rules
- Never hardcode URLs, thresholds, or date ranges — use config.yaml
- Every function needs type hints and a docstring
- Every new module needs a corresponding test file
- Use logging, never print()
