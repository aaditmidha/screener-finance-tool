# Architecture & Design Decisions

A running log of non-obvious choices and the reasoning behind them.

## 1. Groq over the Anthropic/Claude API

**Decision:** All LLM calls (tearsheet, guidance extraction) go through Groq's
free tier (`llama-3.3-70b-versatile`). `screener/llm.py` and
`tearsheet.resolve_provider()` *force* Groq even if config or a caller asks
for something else.

**Why:** The project must stay free to run end-to-end. Tearsheet generation is
a summarisation task over structured data we supply — it needs faithfulness,
not frontier reasoning, so a free fast model is the right trade. The forcing
behaviour exists because "use the Claude API" slips into instructions easily;
the override-and-log pattern makes the policy survive accidents.

## 2. SQLite over Postgres

**Decision:** Single-file SQLite via SQLAlchemy ORM (`data/screener.db`).

**Why:** Single-user research tool, write volume is a handful of rows per
company per week (the 7-day cache gates scraping), and zero-config matters for
Streamlit Cloud deployment where the DB is ephemeral anyway. SQLAlchemy keeps
the swap to Postgres a one-line URL change if it ever becomes multi-user.

## 3. AST whitelist over `eval()` for the custom screener

**Decision:** User formulas are parsed with `ast.parse(mode="eval")` and
validated against a whitelist: numbers, variable names, `+ - * / **`, unary
minus. Calls, attributes, subscripts, comparisons, strings and everything else
raise `FormulaError` before evaluation. Evaluation walks the validated tree —
no `eval()`, no `exec()`, ever.

**Why:** A formula box is a code-injection surface. `eval()` with a filtered
namespace is still escapable via dunder chains (`().__class__...`). The AST
whitelist makes the attack surface enumerable: anything not explicitly allowed
is rejected, which is testable (see `TestFormulaSecurity`) and reviewable.

## 4. Screener.in scraping over the NSE/BSE APIs

**Decision:** Primary data source is the Screener.in company page (HTML),
with exchange portals used only for annual-report PDFs.

**Why:** Screener aggregates and normalises statements across years and
restatements — replicating that from raw exchange filings is a project in
itself. Its pages are stable, lightly protected, and one fetch yields P&L, BS,
CF, ratios and quarterly data. Exchange portals (esp. BSE) are aggressively
bot-protected and serve PDFs, not structured data. MCA XBRL is a future
upgrade path for exact granular fields.

## 5. Cache-first scraping with a 7-day window

**Decision:** Every scrape is gated by `needs_refresh()`; data younger than
`cache.max_age_days` (7) is never re-fetched for persistence.

**Why:** Annual/quarterly figures change at most quarterly; anything fresher
is wasted load on Screener and added block-risk for us. Politeness is also
self-interest with scraped sources.

## 6. Playwright as fallback, not default

**Decision:** Plain `requests` with retry/backoff is the primary fetch path;
a stealth headless browser is used only on hard blocks (403/429/503), and the
AR downloader's exchange chain runs IR-page → NSE → BSE with 15s+ randomised
delays and rotating user agents on BSE.

**Why:** A browser per request is ~100× the cost of an HTTP GET and most
Screener fetches succeed without it. Reserving stealth for where it's needed
keeps the tool fast, light enough for Streamlit Cloud (Playwright isn't even
in the deploy requirements), and minimises fingerprint exposure.

## 7. Beneish M-Score on approximated inputs

### 7.1 Field mapping (Screener public page → Beneish)

| Beneish input | Screener source | Status |
|---|---|---|
| revenue | Sales | exact |
| cogs | Raw Material Cost when present, else **Expenses** | approximated — GM collapses to OPM, so GMI tracks the operating-margin index |
| receivables | Trade Receivables / Debtors | **often absent** on the public page → DSRI neutralised (1.0) |
| current_assets | Current Assets, else **Other Assets** | approximated |
| ppe | Fixed Assets + CWIP | exact |
| securities | Investments | exact |
| total_assets | Total Assets | exact |
| depreciation | Depreciation (P&L) | exact |
| sga | Other Expenses | often absent → SGAI neutralised |
| current liabilities | Current Liabilities, else **Other Liabilities** | approximated |
| long-term debt | Borrowings (mixes ST + LT) | approximated |
| net_income | Net Profit | exact |
| cfo | Cash from Operating Activity | exact |

### 7.2 Why approximations are acceptable here

Beneish is a probabilistic red flag (the published model misclassifies a
material share of firms even on exact COMPUSTAT inputs), not a precise
measurement. Directional inputs preserve the signal: ballooning receivables,
accruals diverging from cash, leverage building. The rules we hold ourselves
to: (a) a missing field neutralises **only its own index** at 1.0 rather than
silently zeroing the score; (b) every approximation and gap is surfaced in
`BeneishSourcing` and shown in the UI as a disclosure caption. An honest
approximate flag beats a hidden "unavailable".

## 8. Concall transcript source for the credibility tracker

**Decision (priority order, scraping not yet wired):**
1. **Screener.in "Concalls" section** — many companies have transcript PDFs
   linked right on the page we already fetch; zero extra infrastructure.
2. **Company IR pages** — most large-caps publish transcripts; reuse the AR
   downloader's IR resolver.
3. **BSE filings** — transcripts filed under board-meeting outcomes; last
   resort behind the same 15s+ delay regime.

**Why this order:** It mirrors the AR downloader's friction gradient (least
protected first). The extraction/scoring pipeline
(`management_credibility.extract_guidance` → `pair_with_actuals` →
`evaluate`) is already built and tested against injected transcripts, so the
remaining work is only the fetch layer.

## 9. Cash as the balance-sheet plug (future model generator)

**Decision:** The Excel financial-model generator (roadmap) will follow the
user's hand-built template convention: forecast cash is the BS plug, financing
flows back-solve from the cash movement, and a balance-check row guards every
year.

**Why:** Matches the eight reference workbooks this tool is meant to
reproduce; deviation from the analyst's own working style would make outputs
unusable for their actual workflow.
