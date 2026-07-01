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

**Why:** A formula box is a code-injection surface, and `eval()` cannot be
made safe by restricting its namespace. Even with `{"__builtins__": {}}`,
Python attribute access lets an expression walk the class hierarchy back to
anything in the interpreter:

```python
().__class__.__bases__[0].__subclasses__()
# tuple → object → every loaded class, including os._wrap_close → os.system
```

No namespace filtering stops that, because the escape route is *attribute
access on a literal*, not a name lookup. AST validation closes it
structurally: we whitelist node types (numeric constants, `Name`, the five
arithmetic operators, unary minus) and reject everything else — `Attribute`,
`Call`, `Subscript`, comparisons, strings — at the syntax-tree level, before
any evaluation happens. The attack surface becomes enumerable and is pinned by
tests (`TestFormulaSecurity` asserts each escape pattern raises
`FormulaError`).

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
to: (a) a missing field neutralises **only its own index** at 1.0 — for a
year-over-year index, 1.0 *is* the statistically neutral "no change"
assumption (e.g. DSRI = 1.0 means receivable days unchanged), not a made-up
number — rather than silently zeroing the score; (b) every approximation and
gap is surfaced in `BeneishSourcing` and shown in the UI as a disclosure
caption. An honest approximate flag beats a hidden "unavailable".

**Upgrade path:** MCA's XBRL filings expose exact receivables and SGA as
structured XML with no bot protection, which would un-neutralise DSRI/SGAI.
Filing quality is uneven below the large-caps, so the planned integration is
scoped to roughly the top 200 companies by market cap, falling back to the
current approximations elsewhere.

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

## 10. Consolidated → standalone fallback + data-quality grading

**Decision:** `CompanyDataService.refresh` tries `/company/{sym}/consolidated/`
first; if that 404s/blocks **or loads with an empty profit-loss table**, it
retries the bare `/company/{sym}/` URL (Screener's default/standalone view).
The view used (`consolidated`/`standalone`), a `data_quality` grade
(`full`/`partial`/`insufficient`), and any `scrape_error` are persisted on the
Company row. A total fetch failure is recorded, not raised, so one bad company
never crashes a batch (e.g. peer comparison).

**Why:** Smaller companies (e.g. Bharat Bijlee) showed blank pages — not a
login wall, but either a consolidated page that's empty or a datacenter-IP
block returning a stripped page. `/company/{sym}/standalone/` does **not**
exist on Screener (it 404s); the bare URL is the real standalone view. Grading
data quality makes the "insufficient data" UI state accurate instead of
guessed. (This is Phase 2 Session A FIX 2; FIX 1 — Screener login — was
deliberately skipped, see §12.)

## 11. Rate-limit retry: jitter + transient-only polite retries

**Decision:** `client._backoff_delay` adds random jitter on top of the
exponential base. On a blocking status, **429/503 (transient)** get a few
polite retries honouring `Retry-After`; **403 (hard block)** fails fast to the
Playwright fallback with no delay. All tunables live under `scraper.retry` in
config.

**Why:** Jitter avoids synchronised-retry thundering herds. 429/503 are
usually transient (worth a short wait), but a 403 is a wall — burning retries
on it only delays the browser fallback that actually gets through. Kept inside
the existing `client.fetch` rather than a separate `retry.py` (the Phase 2
doc's layout) so there's one fetch path, not two. (Phase 2 Session A FIX 3.)

## 12. Screener login deliberately NOT implemented (Phase 2 FIX 1 skipped)

**Decision:** No Screener authentication / credential handling. The blank-data
problem it was meant to solve is fixed by the standalone fallback (§10).

**Why:** Company pages are public (12 years of history scrape fine
anonymously), so login buys little. Against that: automated login ties every
request to the user's identified account (raising *personal-account* ban risk
vs anonymous), and the app deploys to Streamlit Community Cloud from a public
repo on a shared datacenter IP — the worst place to run logged-in automation
or store a password. The marginal benefit didn't justify the account/ToS/
credential risk. Revisit only as a local-only, off-by-default option.

## 13. Peers from the Industry page, not the bare peers API

**Decision:** `discover_peer_symbols` reads the company's **Industry breadcrumb
link** from the `#peers` section and parses that industry's listing page for
peer tickers — rather than calling `/api/company/{id}/peers/`.

**Why:** The bare peers API isn't reliably industry-scoped when called
headlessly — it returned fertilizer companies for CG Power and granite
companies for Bharat Bijlee (both electrical-equipment firms); the site's JS
passes a segment parameter we'd have to reverse-engineer. The Industry page
(`/market/.../IN0702.../`) is the company's own classification and yields true
peers (CG Power → ABB, Siemens, Hitachi Energy, BHEL, Thermax). Separately,
the old discovery scraped **all** `/company/` anchors on the full page (nav,
news, ~53 links) and then **fully scraped + notes-enriched each** — the cause
of the timeouts the user saw. Peers now use a lightweight fetch (`enrich=False`,
no schedule calls) and report progress.

## 14. Operational Data: derived, since Screener has no such section

**Decision:** The Operational Data tab/sheet (`models/operational.py`) is
*derived* from the statements — margins, asset/fixed-asset/inventory turnover,
working-capital days (DSO/DIO/DPO/CCC), and cash conversion (CFO/EBITDA,
CFO/PAT) — not scraped.

**Why:** Screener exposes no generic "operational data" section, but the
reference workbooks' Operational Data sheets are exactly these operating-
efficiency ratios, all computable from P&L/BS/CF + expand-API notes. Metrics
whose inputs are missing are omitted rather than shown as zero, so nothing is
fabricated. Day-metrics use the annual (365-day) convention since the source
periods are fiscal years.

## 15. AR pipeline: local acquisition, cloud read-only (split by the DB)

**Decision:** The annual-report pipeline is split in two. **Local** does the
heavy work — `ar_downloader` (Playwright + IR→NSE→BSE) fetches PDFs,
`pdf_extractor` (pdfplumber) pulls the financial-statement pages, and
`ar_parser` sends them to Groq (regex fallback) for structured figures —
writing rows to `ar_extracted_data`. **Cloud** only ever *reads* those rows to
drive analysis. `pdfplumber`/`playwright` are dev/local-only deps; the hosted
app never imports them.

**Why:** Playwright isn't available on Streamlit Community Cloud and BSE blocks
its datacenter IP, so acquisition can't run there — but it runs fine locally
(installed Chromium, residential IP). The database is the handoff: run the
pipeline locally once per company, then the cloud app serves the extracted data
read-only (commit a small pre-populated `screener.db` for the demo). The PDFs
are a means; the extracted data is the product. Both layers are cache-first —
`ar_downloader` skips downloaded PDFs, `ARPipeline` skips already-extracted
years — so nothing re-downloads or re-extracts.

**Extraction quality:** every unfound field is `null` (never guessed), each
extraction is graded high/medium/low by how much was found, and a regex
fallback (revenue/PAT/CFO + unit detection) keeps the pipeline working at low
confidence when Groq is unavailable.

**Beneish hybrid (done):** `beneish_adapter.from_financials` now takes optional
`ar_current`/`ar_prior` rows and overrides the Screener approximations with
exact AR figures (receivables, total assets, depreciation, PAT, CFO, debt,
revenue) where both years are present — un-neutralising DSRI/TATA — keeping the
Screener base for fields the AR doesn't expose (PPE, current items, SGA). The
overridden fields are reported in `BeneishSourcing.exact_ar` and surfaced in
the UI ("✅ Exact from Annual Report: …"). `latest_ar_pair()` on the service
feeds them in; with no AR data it's a no-op, so the hosted app is unaffected.

## 16. Forensic Red-Flag composite score

**Decision:** `models/forensic_score.py` aggregates four existing signals —
Beneish verdict, earnings quality (CFO/PAT + accruals), promoter-pledge risk,
and debt-to-equity — into one 0–100 health score (higher = healthier) with a
per-component breakdown and a healthy/watch/high-risk verdict. Shown as the
page headline above the Beneish/working-capital row. Weights and bands are in
config; components with no data are excluded and weights renormalised over the
rest.

**Why:** This is the clearest "stand out vs Screener" feature — no free (or
most paid) Indian tool publishes an aggregated forensic score, and it composes
work already built. It runs entirely on Screener-sourced data (no AR/local
dependency), so it works on the hosted app immediately. It degrades honestly:
each component states whether it contributed and why (e.g. "no pledge data"),
so the number is never a black box. Live check: Infosys 100/100 (healthy),
CG Power 66/100 (watch — matching its real accounting-fraud history).

## Phase 2 status

- **Session A (Screener robustness): done** — §10 (FIX 2), §11 (FIX 3); FIX 1
  skipped (§12).
- **Reported bugs: done** — peer comparison (§13), Operational Data tab (§14).
- **Session B (AR downloader): foundation exists** (`ar_downloader.py`, no
  credentials — §12); quarterly-results download not built.
- **Session C (extraction pipeline): done** — `pdf_extractor`, `ar_parser`,
  `ARExtractedData` + repository, `ar_pipeline` (§15), all tested with mocks.
- **Session C consumption: Beneish-AR hybrid done** (§15) and wired into the UI.
- **Forensic Red-Flag score: done** (§16) — headline composite, hosted-ready.
- **AR surfacing done:** 🧾 Annual Reports tab + AR Excel sheets + `ar_insights`
  (discrepancy, risk timeline, guidance scorecard); 🎙 Management tab; tearsheet
  enriched with exact AR data when present.
- **Deferred (low value / unverifiable in CI):** standalone quarterly-results
  PDF downloader, and live concall-transcript fetching. Both are local-only
  network acquisition with limited analytical lift beyond what the AR pipeline
  already provides; the credibility engine is fully built and is fed by AR
  guidance today (a transcript path would be additive, not new capability).

## 17. Local vs hosted split is the deployment contract

The hosted Streamlit app serves everything that runs on Screener-sourced data
(dashboard, forensic score, Beneish, peers, operational, pledge, custom
screener, tearsheet). The AR pipeline (Playwright + Groq) runs **locally only**
and populates the DB; the 🧾/🎙 tabs and AR-upgraded Beneish then read that data
wherever the DB is present. This keeps the cloud deploy light and reliable
while the heavy/blocked-prone acquisition happens on a residential IP. The DB
is the contract between the two halves.

## 18. Accuracy audit (live verification)

**Method:** every analytical model was run on live data for two well-understood
companies and checked against their known real-world profiles, not just unit
fixtures.

**Result — accurate.** Infosys computed to EBITDA/EBIT/PAT margins of
23.7%/20.9%/16.5%, ROCE/ROE ~37%/32%, DSO ~72 days, CFO/PAT 1.15x, Beneish
-2.53 (non-manipulator), forensic 100/100 — all consistent with a top-tier IT
compounder. CG Power computed to ~13% EBITDA margin, normalising ROCE/ROE, DSO
~86 days, CFO/PAT 0.59x, Beneish -2.02 (grey zone), forensic 66/100 (watch) —
consistent with a capex-heavy turnaround. No formula bugs were found; the
Beneish 8-factor coefficients, DCF, earnings-quality CAGR, capital-allocation
WACC, working-capital day-counts, return ratios and composite normalisations
all verified correct.

**Fix from the audit:** the Beneish index-neutralisation logs (fired routinely
on the aggregated Screener page, where some inputs are absent by design) were
downgraded from WARNING to DEBUG — the degradation is expected and already
disclosed in the UI, so it shouldn't spam logs.

## 19. Detailed model + note: shared derivation layer

**Trigger:** the generated Excel model and research note were judged "not
detailed enough" against reference sell-side artifacts (a POCL Nuvama meet note
and full POCL/Paramount workbooks).

**Decision:** introduce `screener/exporters/financial_model.py` as a single
canonical derivation layer — derived income statement, common-size (% of
revenue), YoY growth, mapped balance sheet and cash flow, and a profitability /
returns / leverage / efficiency / per-share ratios block — built once from the
parsed Screener statements (reusing `models/operational.py` for day-counts).
Both `model_workbook.py` and `research_note.py` consume it, so the workbook and
the note can never disagree.

**Excel:** the Output Sheet is now the full sectioned analyst summary (was a
flat metric dashboard), plus an optional Peer Comparison sheet. **Note:** gains
full IS/BS/CF/ratios tables, a four-chart focus grid, 7–9 LLM thesis sections
(richer context now includes common-size/growth/ratios + uploaded-AR guidance
and disclosed risks), and reference-style house formatting (navy headings,
"Source:" captions, INR cr, no em dashes). Qualitative depth (segments,
capacity, shareholding, KMP) is bounded by what's extracted from uploaded ARs;
the fully data-driven financial depth renders regardless. LLM stays on Groq.
