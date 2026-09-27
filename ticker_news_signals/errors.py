"""Exception hierarchy for the ticker-news-signals package."""

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