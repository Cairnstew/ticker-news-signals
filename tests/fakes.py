"""Shared fakes for offline pipeline tests.

Providers here never touch the network; they hand the orchestrator fixed
headlines so tests about bucketing, dedupe, coverage and quality are fully
deterministic.
"""

from __future__ import annotations

from ticker_news_signals.capabilities import Capability
from ticker_news_signals.models import NewsArticle
from ticker_news_signals.providers.base import NewsProvider


def article(
    title: str,
    published: str,
    url: str,
    source: str = "fake",
    publisher: str = "Fake News",
) -> NewsArticle:
    return NewsArticle(
        title=title,
        published_date=published,
        url=url,
        publisher=publisher,
        source=source,
    )


class StaticProvider(NewsProvider):
    """A provider returning a fixed list (plus optional per-call failure)."""

    name = "static"
    supports = frozenset({Capability.NEWS})
    articles: tuple[NewsArticle, ...] = ()

    def fetch_news(self, asset, *, max_results):
        out = self.articles[:max_results]
        self.ctx.record(f"{self.name}:news", source=self.name, article_count=len(out))
        return out


class TrendProvider(NewsProvider):
    """A provider whose only capability is trending (no search endpoint)."""

    name = "trend"
    supports = frozenset({Capability.TRENDING})
    articles: tuple[NewsArticle, ...] = ()

    def fetch_trending(self, *, max_results):
        out = self.articles[:max_results]
        self.ctx.record(f"{self.name}:trending", source=self.name, article_count=len(out))
        return out


class BrokenProvider(NewsProvider):
    """A provider that always raises while fetching."""

    name = "broken"
    supports = frozenset({Capability.NEWS})

    def fetch_news(self, asset, *, max_results):
        raise RuntimeError("feed exploded")


class UnavailableProvider(NewsProvider):
    """A provider whose availability check itself raises."""

    name = "unavailable"
    supports = frozenset({Capability.NEWS})

    @classmethod
    def available(cls, config):
        raise RuntimeError("availability blew up")


class ExplodingInitProvider(NewsProvider):
    """A provider that raises while being constructed."""

    name = "exploding"
    supports = frozenset({Capability.NEWS})

    def __init__(self, context):
        raise RuntimeError("init exploded")


class BadRpmProvider(NewsProvider):
    """A provider declaring an invalid (zero) rate limit."""

    name = "badrpm"
    supports = frozenset({Capability.NEWS})

    @classmethod
    def rate_limit_rpm(cls, config):
        return 0


def provider_with(name: str, articles: tuple[NewsArticle, ...]) -> type[NewsProvider]:
    """A concrete :class:`StaticProvider` subclass carrying ``articles``."""
    return type(name, (StaticProvider,), {"name": name, "articles": articles})
