"""Ticker news signals.

Pull crypto news headlines via GNews (keyless Google News RSS), score each
headline's sentiment with VADER, and aggregate into per-ticker, per-hour
signal vectors written as NDJSON.

    from ticker_news_signals.pipeline import fetch_signals

    vectors = fetch_signals("ETH/USD", lookback_hours=3)
    for vector in vectors:
        print(vector.to_dict())
"""

from __future__ import annotations

import logging

from .errors import (
    ConfigurationError,
    NewsClientError,
    NewsFetchError,
    PipelineError,
    TickerNewsError,
)
from .models import NewsArticle, SignalVector

__version__ = "0.1.0"

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "NewsArticle",
    "SignalVector",
    "TickerNewsError",
    "ConfigurationError",
    "NewsClientError",
    "NewsFetchError",
    "PipelineError",
    "__version__",
]