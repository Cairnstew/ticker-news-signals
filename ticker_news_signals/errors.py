"""Exception hierarchy for the ticker-news-signals package.

The names present before the provider architecture (``NewsClientError``,
``NewsFetchError``, …) are kept verbatim so existing callers and tests keep
working. The newer types (:class:`SourceError`, :class:`RateLimitExceeded`)
are what the orchestrator converts provider failures into — nothing is ever
silently swallowed.
"""

from __future__ import annotations


class TickerNewsError(Exception):
    """Base class for all errors raised by this package."""


class ConfigurationError(TickerNewsError):
    """Raised when required configuration values are missing or invalid."""


class NewsClientError(TickerNewsError):
    """A news-fetch operation failed at the client layer."""


class NewsFetchError(NewsClientError):
    """The GNews backend raised while fetching or parsing articles."""


class PipelineError(TickerNewsError):
    """Signal aggregation could not complete."""


class SourceError(TickerNewsError):
    """A news provider could not satisfy a capability.

    Unlike the older :class:`NewsFetchError`, this never escapes the
    orchestrator: it is caught and recorded as a
    :class:`~ticker_news_signals.models.SourceErrorRecord` on the result's
    ``data_quality`` block, so one dead feed never sinks a snapshot.
    """

    def __init__(self, source: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.detail = detail


class UnsupportedSource(TickerNewsError):
    """The provider does not implement the requested capability."""


class UnknownSentimentBackend(TickerNewsError):
    """A sentiment backend name was requested that is not registered."""


class RateLimitExceeded(TickerNewsError):
    """A source rate limit was hit and could not be satisfied within retries.

    Carries structured detail rather than a string to be parsed: which source
    throttled, how long to wait, the advertised budget, and a concrete remedy.
    Calls short-circuited by the circuit breaker (never touching the network)
    set ``suppressed`` so a caller can tell "we were blocked" from "we were
    told no".
    """

    def __init__(
        self,
        source: str,
        message: str,
        *,
        retry_after: float | None = None,
        limit: int | None = None,
        remaining: int | None = None,
        reset_at: float | None = None,
        hint: str | None = None,
        suppressed: bool = False,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining
        self.reset_at = reset_at
        self.hint = hint
        self.suppressed = suppressed

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base]
        if self.limit is not None:
            parts.append(f"limit {self.limit}/window")
        if self.retry_after is not None:
            parts.append(f"retry after {self.retry_after:.0f}s")
        return " (".join([parts[0], ", ".join(parts[1:]) + ")"]) if len(parts) > 1 else base


__all__ = [
    "ConfigurationError",
    "NewsClientError",
    "NewsFetchError",
    "PipelineError",
    "RateLimitExceeded",
    "SourceError",
    "TickerNewsError",
    "UnknownSentimentBackend",
    "UnsupportedSource",
]
