"""Aggregation logic: articles -> per-hour :class:`SignalVector` rows.

The pipeline is deliberately small and single-purpose:

1. Gather candidate headlines for a ticker: a keyword search on the base
   symbol plus a ``DIGITAL CURRENCIES`` section pull, deduplicated by URL /
   title.
2. Keep only articles inside the lookback window.
3. Bucket by UTC hour; for each bucket compute the mean VADER sentiment,
   the article count, and the novelty flag.
4. Return one :class:`SignalVector` per non-empty hour, oldest first.

All network access goes through :class:`~ticker_news_signals.client.NewsClient`
so tests can substitute a fake client or mock the GNews backend.
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from .client import NewsClient
from .logging_config import log_event
from .models import NewsArticle, SignalVector
from .sentiment import score_batch
from .utils import (
    base_symbol,
    ensure_utc,
    floor_to_hour,
    isoformat_utc,
    normalize_ticker,
    now_utc,
)

# An article published within this window of "now" marks its hour as novel
# (fresh information the strategy has not had time to react to).
NOVELTY_WINDOW = timedelta(minutes=15)


def _query_period(lookback_hours: int) -> str:
    """Pick a GNews period string that covers *lookback_hours*."""
    if lookback_hours <= 24:
        return f"{lookback_hours}h"
    return f"{(lookback_hours + 23) // 24}d"


def collect_articles(
    ticker: str,
    *,
    client: NewsClient | None = None,
    max_results: int = 100,
) -> list[NewsArticle]:
    """Gather and deduplicate candidate headlines for *ticker*."""
    client = client or NewsClient()
    base = base_symbol(ticker)
    raw: list[NewsArticle] = []
    raw.extend(client.search(base, max_results=max_results))
    raw.extend(client.search_by_topic("DIGITAL CURRENCIES", max_results=max_results))
    articles = _dedupe(raw)
    log_event(
        "ticker_news_signals.pipeline",
        "collected candidate articles",
        level="INFO",
        ticker=normalize_ticker(ticker),
        fetched=len(raw),
        unique=len(articles),
    )
    return articles


def _dedupe(articles: Iterable[NewsArticle]) -> list[NewsArticle]:
    """Drop exact duplicates by URL, falling back to normalised title."""
    seen: set[str] = set()
    unique: list[NewsArticle] = []
    for article in articles:
        key = (article.url or article.title).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(article)
    return unique


def _headline(article: NewsArticle) -> str:
    """The scored text for an article: title, HTML-entities unescaped."""
    return html.unescape(article.title).strip()


def _is_novel(items: Iterable[NewsArticle], now: datetime) -> bool:
    """True when *items* contain an article newer than the novelty window."""
    for article in items:
        published = article.published_dt or now
        if now - published <= NOVELTY_WINDOW:
            return True
    return False


def fetch_signals(
    ticker: str,
    lookback_hours: int = 1,
    *,
    client: NewsClient | None = None,
    now: datetime | None = None,
) -> list[SignalVector]:
    """Compute one :class:`SignalVector` per non-empty UTC hour in the window.

    Parameters
    ----------
    ticker:
        Any market spelling (``ETH/USD``, ``eth-usd``, ``ETH``); the vector's
        ``ticker`` field uses the normalised underscore form (``ETH_USD``).
    lookback_hours:
        How many hours of history (from *now*) to aggregate.  The client's
        GNews period is sized to match when a client is created here.
    client:
        A :class:`NewsClient` (or a fake in tests).  Created with a
        lookback-sized period when omitted.
    now:
        Fixed "current" time for tests; defaults to UTC now.

    Returns
    -------
    list[SignalVector]
        One row per non-empty hour, oldest first.  An empty window returns
        an empty list.
    """
    ticker_key = normalize_ticker(ticker)
    base = base_symbol(ticker)
    if client is None:
        client = NewsClient(period=_query_period(lookback_hours))
    current = ensure_utc(now_utc() if now is None else now)

    articles = collect_articles(base, client=client)
    cutoff = current - timedelta(hours=lookback_hours)

    buckets: dict[datetime, list[NewsArticle]] = defaultdict(list)
    for article in articles:
        published = article.published_dt
        if published is None:
            # Undated article — treat as now so it is never silently dropped.
            published = current
        if published < cutoff:
            continue
        buckets[floor_to_hour(published)].append(article)

    vectors: list[SignalVector] = []
    for hour in sorted(buckets):
        items = buckets[hour]
        scores = score_batch(_headline(article) for article in items)
        count = len(items)
        vectors.append(
            SignalVector(
                ticker=ticker_key,
                timestamp=isoformat_utc(hour),
                sentiment_score=round(sum(scores) / count, 4),
                article_count=count,
                novelty_flag=_is_novel(items, current),
            )
        )

    log_event(
        "ticker_news_signals.pipeline",
        "built signal vectors",
        level="INFO",
        ticker=ticker_key,
        lookback_hours=lookback_hours,
        buckets=len(vectors),
    )
    return vectors