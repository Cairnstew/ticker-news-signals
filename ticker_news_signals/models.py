"""Typed models for the ticker-news-signals pipeline.

Two dataclasses carry data through the pipeline:

- :class:`NewsArticle` — one headline as returned by GNews, normalised to
  plain strings plus a parsed timestamp.  ``from_gnews`` accepts the raw RSS
  entry dict (``title``, ``description``, ``published date``, ``url``,
  ``publisher``), so GNews stays behind :class:`~ticker_news_signals.client.NewsClient`.
- :class:`SignalVector` — one per-ticker, per-hour aggregate: the mean VADER
  sentiment score across that hour's headlines, the article count, and a
  novelty flag.

Both are plain dataclasses (constructible directly in tests) with ``to_dict()``
output that always round-trips through ``json.dumps``: no datetimes, no
Decimals, no surprise keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .utils import parse_article_date


@dataclass(slots=True)
class NewsArticle:
    """One news headline."""

    title: str
    description: str = ""
    published_date: str = ""  # raw string as GNews reported it
    url: str = ""
    publisher: str = ""

    @classmethod
    def from_gnews(cls, entry: dict[str, Any]) -> "NewsArticle":
        """Build from a raw GNews RSS/SearchApi entry dict."""
        published = entry.get("published date") or entry.get("iso_date") or ""
        publisher = entry.get("publisher") or ""
        if isinstance(publisher, dict):
            publisher = publisher.get("title") or publisher.get("href") or ""
        return cls(
            title=str(entry.get("title") or ""),
            description=str(entry.get("description") or ""),
            published_date=str(published),
            url=str(entry.get("url") or ""),
            publisher=str(publisher),
        )

    @property
    def published_dt(self) -> datetime | None:
        """The parsed publish time as an aware UTC datetime, or ``None``."""
        return parse_article_date(self.published_date)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (always string/int/bool/None leaves)."""
        return {
            "title": self.title,
            "description": self.description,
            "published_date": self.published_date,
            "url": self.url,
            "publisher": self.publisher,
        }

    def __str__(self) -> str:
        stamp = self.published_date or "?"
        return f"[{stamp}] {self.publisher}: {self.title}"


@dataclass(slots=True)
class SignalVector:
    """One per-ticker, per-hour sentiment signal."""

    ticker: str
    timestamp: str
    sentiment_score: float = 0.0
    article_count: int = 0
    novelty_flag: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalVector":
        """Build from a previously-exported JSON record (e.g. a JSONL line)."""
        return cls(
            ticker=str(data.get("ticker") or ""),
            timestamp=str(data.get("timestamp") or ""),
            sentiment_score=float(data.get("sentiment_score") or 0.0),
            article_count=int(data.get("article_count") or 0),
            novelty_flag=bool(data.get("novelty_flag") or False),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict matching the documented signal-vector shape."""
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp,
            "sentiment_score": self.sentiment_score,
            "article_count": self.article_count,
            "novelty_flag": self.novelty_flag,
        }

    def __str__(self) -> str:
        novelty = " new" if self.novelty_flag else ""
        return (
            f"{self.ticker} {self.timestamp} score={self.sentiment_score:+.3f}"
            f" n={self.article_count}{novelty}"
        )