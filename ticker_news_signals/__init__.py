"""ticker-news-signals — a standalone crypto news-sentiment signal pipeline.

Pull crypto headlines from many keyless sources, de-duplicate them across
feeds, score sentiment locally, and aggregate into per-ticker, per-hour signal
vectors written as NDJSON — ready to feed a trading strategy or a dashboard.

    from ticker_news_signals.pipeline import fetch_result

    result = fetch_result("ETH/USD", lookback_hours=3)
    for vector in result.vectors:
        print(vector.to_dict())
    print(result.data_quality.sources_used)

Two entry points share one orchestrator:

* :func:`~ticker_news_signals.pipeline.fetch_signals` returns just the
  canonical vectors (the documented consumer contract).
* :func:`~ticker_news_signals.pipeline.fetch_result` returns a
  :class:`~ticker_news_signals.models.SignalResult` with the vectors, the
  contributing articles, and a ``data_quality`` block (provenance, errors,
  throttles, skips, missing fields, duplicate counts).

Nothing here requires an API key: GNews and the RSS provider are keyless, and
sentiment scoring is local. An optional provider (CryptoPanic) lights up when
its token is present.
"""

from __future__ import annotations

import logging

from .cache import Cache, CacheLimits, MemoryCache, NullCache, SQLiteCache
from .capabilities import Capability
from .config import Config, FeedSpec
from .errors import (
    ConfigurationError,
    NewsClientError,
    NewsFetchError,
    PipelineError,
    RateLimitExceeded,
    SourceError,
    TickerNewsError,
    UnknownSentimentBackend,
    UnsupportedSource,
)
from .history import HISTORY_PROVIDERS, HistoryRunner, fetch_history
from .models import (
    DataQuality,
    HistoryCoverage,
    HistoryRequest,
    HistoryResult,
    HistorySeries,
    MissingField,
    NewsArticle,
    Provenance,
    RateLimitInfo,
    SignalResult,
    SignalVector,
    SkippedSource,
    SourceErrorRecord,
)
from .providers import NewsProvider, ProviderContext, register, registry
from .resolve import ResolvedTicker, resolve_ticker
from .sentiment import (
    available_backends,
    register_backend,
    score_batch,
    score_headline,
)
from .store import QueryCache

__version__ = "0.2.0"

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "HISTORY_PROVIDERS",
    "Cache",
    "CacheLimits",
    "Capability",
    "Config",
    "ConfigurationError",
    "DataQuality",
    "FeedSpec",
    "HistoryCoverage",
    "HistoryRequest",
    "HistoryResult",
    "HistoryRunner",
    "HistorySeries",
    "MemoryCache",
    "MissingField",
    "NewsArticle",
    "NewsClientError",
    "NewsFetchError",
    "NewsProvider",
    "NullCache",
    "PipelineError",
    "Provenance",
    "ProviderContext",
    "QueryCache",
    "RateLimitExceeded",
    "RateLimitInfo",
    "ResolvedTicker",
    "SQLiteCache",
    "SignalResult",
    "SignalVector",
    "SkippedSource",
    "SourceError",
    "SourceErrorRecord",
    "TickerNewsError",
    "UnknownSentimentBackend",
    "UnsupportedSource",
    "__version__",
    "available_backends",
    "fetch_history",
    "register",
    "register_backend",
    "registry",
    "resolve_ticker",
    "score_batch",
    "score_headline",
]
