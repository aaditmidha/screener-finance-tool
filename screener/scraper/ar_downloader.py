"""Annual-report PDF downloader with a stealth Playwright backend.

Acquires a company's annual-report PDFs and caches them locally under
``<storage_dir>/<symbol>/<year>/<filename>``. Sources are tried in a fixed
priority order, each behind randomised human-like delays:

1. **Company IR page** — parse the investor-relations page directly for report
   links. Most reliable and least likely to rate-limit.
2. **NSE filing portal** — the exchange's annual-reports API, used when the IR
   page yields nothing.
3. **BSE** — last resort; the most aggressive blocker, so it runs with the
   longest randomised delays (15s+).

Design notes for testability and stealth:
- All Playwright interaction is isolated in small overridable methods
  (:meth:`_render_page`, :meth:`_fetch_bytes`); the orchestration, caching and
  delay logic are pure and unit-tested without launching a browser.
- A lightweight stealth init-script is injected into every browser context to
  blunt the most common headless-detection checks, avoiding a heavyweight
  third-party dependency.
- Every attempt, success and failure is logged. The cache is consulted before
  any network access so the same filing is never downloaded twice.

All URLs, delays, storage paths and keywords come from the ``annual_reports``
section of config.yaml — nothing here is hardcoded.
"""

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from screener.config import CONFIG
from screener.scraper.exceptions import DownloadError

logger = logging.getLogger(__name__)

# Injected into every context to suppress common headless fingerprints.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""

# Recognises a fiscal year in link text, a URL or a filename:
# "FY2024", "FY 2023-24", "2022-23", or a bare "2024".
_YEAR_PATTERN = re.compile(r"(?:FY\s*)?(20\d{2})(?:\s*[-/]\s*\d{2,4})?", re.IGNORECASE)


@dataclass
class DownloadResult:
    """Outcome of a single annual-report acquisition."""

    symbol: str
    year: int
    source: str        # "ir_page" | "nse" | "bse" | "cache"
    path: Path
    from_cache: bool


def extract_year(text: str) -> int | None:
    """Pull a four-digit fiscal year from arbitrary link text/URL/filename.

    Args:
        text: A link label, URL, or filename that may embed a year.

    Returns:
        The four-digit year as an int, or None if no plausible year is found.
    """
    if not text:
        return None
    match = _YEAR_PATTERN.search(text)
    if not match:
        return None
    return int(match.group(1))


class AnnualReportDownloader:
    """Downloads and caches annual-report PDFs across prioritised sources."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialise the downloader from the ``annual_reports`` config block.

        Args:
            config: Full config dict. Defaults to the global CONFIG; injectable
                for testing.
        """
        cfg = (config or CONFIG)["annual_reports"]
        self._cfg = cfg
        self._storage_root = Path(cfg["storage_dir"])
        self._sources = cfg["sources"]
        self._ar_keywords = [kw.lower() for kw in cfg["ar_link_keywords"]]

    # ------------------------------------------------------------------ #
    # Cache helpers
    # ------------------------------------------------------------------ #
    def _year_dir(self, symbol: str, year: int) -> Path:
        """Return the local directory that holds *symbol*'s *year* report."""
        return self._storage_root / symbol.upper() / str(year)

    def cache_path(self, symbol: str, year: int, filename: str) -> Path:
        """Return the full local path a given filing would be cached at.

        Args:
            symbol: Company ticker.
            year: Fiscal year of the report.
            filename: PDF filename to store under the year directory.

        Returns:
            The composed ``<root>/<symbol>/<year>/<filename>`` path.
        """
        return self._year_dir(symbol, year) / filename

    def find_cached(self, symbol: str, year: int) -> Path | None:
        """Return an already-downloaded PDF for *symbol*/*year*, if present.

        Args:
            symbol: Company ticker.
            year: Fiscal year of the report.

        Returns:
            Path to the first cached PDF found, or None if nothing is cached.
        """
        year_dir = self._year_dir(symbol, year)
        if not year_dir.exists():
            return None
        for pdf in sorted(year_dir.glob("*.pdf")):
            if pdf.stat().st_size > 0:
                logger.debug("Cache hit for %s FY%s at %s", symbol, year, pdf)
                return pdf
        return None

    # ------------------------------------------------------------------ #
    # Delay helper
    # ------------------------------------------------------------------ #
    def _sleep_for_source(self, source: str) -> None:
        """Sleep a randomised human-like interval configured for *source*.

        Args:
            source: Source key ("ir_page", "nse", "bse") whose min/max delay
                bounds are read from config.
        """
        src_cfg = self._sources[source]
        low = src_cfg["min_delay_seconds"]
        high = src_cfg["max_delay_seconds"]
        delay = random.uniform(low, high)
        logger.debug("Sleeping %.1fs before %s request", delay, source)
        time.sleep(delay)

    # ------------------------------------------------------------------ #
    # Playwright-backed primitives (thin, overridable for tests)
    # ------------------------------------------------------------------ #
    def _render_page(self, url: str) -> str:
        """Render *url* in a stealth headless browser and return its HTML.

        Args:
            url: Page URL to load.

        Returns:
            The fully-rendered page HTML.

        Raises:
            DownloadError: If Playwright is unavailable or navigation fails.
        """
        try:
            from playwright.sync_api import sync_playwright
            from playwright.sync_api import Error as PlaywrightError
        except ImportError as exc:
            raise DownloadError("?", 0, ["playwright-import"]) from exc

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    context = browser.new_context(user_agent=self._cfg["user_agent"])
                    context.add_init_script(_STEALTH_INIT_SCRIPT)
                    page = context.new_page()
                    page.goto(url, timeout=self._cfg["download_timeout_ms"],
                              wait_until="domcontentloaded")
                    return page.content()
                finally:
                    browser.close()
        except PlaywrightError as exc:
            logger.warning("Render failed for %s: %s", url, exc)
            raise DownloadError("?", 0, ["render"]) from exc

    def _fetch_bytes(self, url: str) -> bytes:
        """Fetch raw bytes for *url* using a stealth browser request context.

        Using the browser's request context carries the same fingerprint and
        cookies as a real navigation, which gets past most PDF gateways.

        Args:
            url: Direct URL to the PDF.

        Returns:
            The response body as bytes.

        Raises:
            DownloadError: If Playwright is unavailable or the fetch fails.
        """
        try:
            from playwright.sync_api import sync_playwright
            from playwright.sync_api import Error as PlaywrightError
        except ImportError as exc:
            raise DownloadError("?", 0, ["playwright-import"]) from exc

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    context = browser.new_context(user_agent=self._cfg["user_agent"])
                    context.add_init_script(_STEALTH_INIT_SCRIPT)
                    response = context.request.get(url, timeout=self._cfg["download_timeout_ms"])
                    if not response.ok:
                        raise DownloadError("?", 0, [f"http-{response.status}"])
                    return response.body()
                finally:
                    browser.close()
        except PlaywrightError as exc:
            logger.warning("Byte fetch failed for %s: %s", url, exc)
            raise DownloadError("?", 0, ["fetch"]) from exc

    def _save_pdf(self, data: bytes, dest: Path) -> None:
        """Write PDF *data* to *dest*, creating parent directories.

        Args:
            data: Raw PDF bytes.
            dest: Destination path.

        Raises:
            DownloadError: If the data is empty or not a PDF.
        """
        if not data or not data.startswith(b"%PDF"):
            raise DownloadError("?", 0, ["invalid-pdf"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.debug("Saved %d bytes to %s", len(data), dest)

    # ------------------------------------------------------------------ #
    # Source resolvers — each maps a symbol to {year: pdf_url}
    # ------------------------------------------------------------------ #
    def resolve_ir_page(self, symbol: str, ir_url: str) -> dict[int, str]:
        """Find annual-report PDF links on a company's IR page.

        Args:
            symbol: Company ticker (for logging).
            ir_url: Fully-qualified investor-relations page URL.

        Returns:
            Mapping of fiscal year → PDF URL for every report link found.
        """
        logger.info("Resolving IR page for %s: %s", symbol, ir_url)
        html = self._render_page(ir_url)
        links = self._extract_pdf_links(html, base_url=ir_url)
        found: dict[int, str] = {}
        for text, href in links:
            label = f"{text} {href}".lower()
            if not any(kw in label for kw in self._ar_keywords):
                continue
            year = extract_year(text) or extract_year(href)
            if year is not None and year not in found:
                found[year] = href
        logger.info("IR page yielded %d report link(s) for %s", len(found), symbol)
        return found

    def resolve_nse(self, symbol: str) -> dict[int, str]:
        """Resolve annual-report URLs from the NSE annual-reports API.

        Args:
            symbol: Company ticker.

        Returns:
            Mapping of fiscal year → PDF URL.
        """
        import json

        api = self._sources["nse"]["annual_reports_api"].format(symbol=symbol.upper())
        logger.info("Resolving NSE filings for %s: %s", symbol, api)
        raw = self._render_page(api)
        found: dict[int, str] = {}
        try:
            # The API endpoint returns JSON; when fetched via a rendered page it
            # is wrapped in <pre> or returned verbatim — strip to the braces.
            start, end = raw.find("{"), raw.rfind("}")
            payload = json.loads(raw[start:end + 1]) if start != -1 else {}
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("NSE response for %s was not valid JSON: %s", symbol, exc)
            return found

        for entry in payload.get("data", []):
            url = entry.get("fileName") or entry.get("file") or ""
            year = extract_year(entry.get("toYr", "")) or extract_year(url)
            if url and year is not None and year not in found:
                found[year] = url
        logger.info("NSE yielded %d report link(s) for %s", len(found), symbol)
        return found

    def resolve_bse(self, symbol: str) -> dict[int, str]:
        """Resolve annual-report URLs from BSE (last-resort fallback).

        Args:
            symbol: Company ticker.

        Returns:
            Mapping of fiscal year → PDF URL.
        """
        base = self._sources["bse"]["base_url"]
        search_url = f"{base}/corporates/annual-report.aspx?scrip={symbol.upper()}"
        logger.info("Resolving BSE filings for %s: %s", symbol, search_url)
        html = self._render_page(search_url)
        links = self._extract_pdf_links(html, base_url=base)
        found: dict[int, str] = {}
        for text, href in links:
            year = extract_year(text) or extract_year(href)
            if year is not None and year not in found:
                found[year] = href
        logger.info("BSE yielded %d report link(s) for %s", len(found), symbol)
        return found

    @staticmethod
    def _extract_pdf_links(html: str, base_url: str) -> list[tuple[str, str]]:
        """Return (link_text, absolute_url) pairs for every PDF anchor in *html*.

        Args:
            html: Page HTML.
            base_url: Page URL, used to resolve relative hrefs.

        Returns:
            List of (text, absolute URL) tuples for hrefs ending in ``.pdf``.
        """
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        out: list[tuple[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if ".pdf" not in href.lower():
                continue
            out.append((anchor.get_text(strip=True), urljoin(base_url, href)))
        return out

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def _ordered_resolvers(
        self, symbol: str, ir_url: str | None
    ) -> list[tuple[str, Callable[[], dict[int, str]]]]:
        """Build the enabled (source_name, resolver) chain in priority order.

        Args:
            symbol: Company ticker.
            ir_url: IR page URL, or None to skip the IR-page source.

        Returns:
            List of (source key, zero-arg resolver) tuples, IR → NSE → BSE,
            filtered to sources enabled in config (and IR only if a URL exists).
        """
        chain: list[tuple[str, Callable[[], dict[int, str]]]] = []
        if self._sources["ir_page"]["enabled"] and ir_url:
            chain.append(("ir_page", lambda: self.resolve_ir_page(symbol, ir_url)))
        if self._sources["nse"]["enabled"]:
            chain.append(("nse", lambda: self.resolve_nse(symbol)))
        if self._sources["bse"]["enabled"]:
            chain.append(("bse", lambda: self.resolve_bse(symbol)))
        return chain

    def download_report(
        self, symbol: str, year: int, ir_url: str | None = None
    ) -> DownloadResult:
        """Acquire one annual report, checking the cache before any network use.

        Tries each enabled source in priority order (IR → NSE → BSE) behind
        randomised delays, downloads the first matching PDF, caches it, and
        returns the result.

        Args:
            symbol: Company ticker.
            year: Fiscal year of the report to fetch.
            ir_url: Optional investor-relations page URL to try first.

        Returns:
            A DownloadResult describing where the PDF came from and its path.

        Raises:
            DownloadError: If no source yields the report.
        """
        cached = self.find_cached(symbol, year)
        if cached is not None:
            logger.info("Using cached %s FY%s report: %s", symbol, year, cached)
            return DownloadResult(symbol, year, "cache", cached, from_cache=True)

        tried: list[str] = []
        for source, resolve in self._ordered_resolvers(symbol, ir_url):
            tried.append(source)
            self._sleep_for_source(source)
            logger.info("Attempting %s for %s FY%s", source, symbol, year)
            try:
                links = resolve()
            except DownloadError as exc:
                logger.warning("%s resolver failed for %s FY%s: %s", source, symbol, year, exc)
                continue

            url = links.get(year)
            if not url:
                logger.info("%s had no FY%s report for %s", source, year, symbol)
                continue

            try:
                data = self._fetch_bytes(url)
                filename = Path(url.split("?")[0]).name or f"{symbol}_FY{year}.pdf"
                dest = self.cache_path(symbol, year, filename)
                self._save_pdf(data, dest)
            except DownloadError as exc:
                logger.warning("Download from %s failed for %s FY%s: %s", source, symbol, year, exc)
                continue

            logger.info("Downloaded %s FY%s from %s → %s", symbol, year, source, dest)
            return DownloadResult(symbol, year, source, dest, from_cache=False)

        logger.error("All sources exhausted for %s FY%s (tried: %s)", symbol, year, tried)
        raise DownloadError(symbol, year, tried)
