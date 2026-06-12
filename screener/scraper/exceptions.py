"""Custom exception hierarchy for the scraper package.

All scraper failures raise a subclass of :class:`ScraperError` so callers can
catch the whole family with one ``except ScraperError`` rather than guessing at
``requests``/``playwright`` internals or using a bare ``except``.
"""


class ScraperError(Exception):
    """Base class for every scraper-related failure."""


class FetchError(ScraperError):
    """Raised when a page could not be retrieved after all retries/fallbacks."""

    def __init__(self, url: str, message: str) -> None:
        """Store the offending URL and a human-readable reason.

        Args:
            url: The URL that failed to fetch.
            message: Description of why the fetch failed.
        """
        self.url = url
        self.message = message
        super().__init__(f"Failed to fetch {url}: {message}")


class BlockedError(FetchError):
    """Raised when the server actively blocks the request (e.g. 403/429/503).

    Distinct from a generic :class:`FetchError` so callers can decide whether to
    trigger the Playwright fallback.
    """

    def __init__(self, url: str, status_code: int) -> None:
        """Store the URL and the blocking HTTP status code.

        Args:
            url: The URL that was blocked.
            status_code: The HTTP status code returned by the server.
        """
        self.status_code = status_code
        super().__init__(url, f"blocked with HTTP {status_code}")


class DownloadError(ScraperError):
    """Raised when a file (e.g. an annual report PDF) could not be downloaded.

    Carries the symbol/year being sought and the list of sources that were
    tried, so callers and logs can see the full fallback chain that failed.
    """

    def __init__(self, symbol: str, year: int, sources_tried: list[str]) -> None:
        """Store the target identifiers and the exhausted source list.

        Args:
            symbol: Company ticker the report was sought for.
            year: Fiscal year of the report.
            sources_tried: Names of the sources attempted, in order.
        """
        self.symbol = symbol
        self.year = year
        self.sources_tried = sources_tried
        tried = ", ".join(sources_tried) if sources_tried else "none"
        super().__init__(
            f"Could not download {symbol} FY{year} annual report (tried: {tried})"
        )


class ParseError(ScraperError):
    """Raised when fetched HTML cannot be parsed into the expected structure."""

    def __init__(self, url: str, field: str) -> None:
        """Store the URL and the field that could not be parsed.

        Args:
            url: The URL whose content failed to parse.
            field: Name of the expected field that was missing/malformed.
        """
        self.url = url
        self.field = field
        super().__init__(f"Failed to parse {field!r} from {url}")
