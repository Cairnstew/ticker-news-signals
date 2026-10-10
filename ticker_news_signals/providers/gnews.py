"""GNews provider — keyless Google News RSS.

Thin adapter over :class:`~ticker_news_signals.client.NewsClient`, which owns
the GNews backend, its retry/backoff and its entry normalisation. This provider
only claims the capabilities it can serve and records provenance.

GNews has no published numeric rate limit (it reads the public Google News RSS
endpoint), so it relies on the shared safety-net limiter rather than a
source-specific ceiling.
"""

from __future__ import annotations

from typing import ClassVar

from ..capabilities import Capability
from ..client import NewsClient
from ..models import NewsArticle
from ..resolve import ResolvedTicker
from .base import NewsProvider, ProviderContext

#: The Google News section that carries crypto coverage (note the space — it is
#: the section's display name).
CRYPTO_TOPIC = "DIGITAL CURRENCIES"


class GNewsProvider(NewsProvider):
    """Fetches headlines from Google News via GNews."""

    name: ClassVar[str] = "gnews"
    supports: ClassVar[frozenset[Capability]] = frozenset({Capability.NEWS, Capability.TRENDING})

    def __init__(self, context: ProviderContext, *, client: NewsClient | None = None) -> None:
        super().__init__(context)
        config = context.config
        self._client = client or NewsClient(
            language=config.language,
            country=config.country,
            period=config.period,
        )

    def fetch_news(self, asset: ResolvedTicker, *, max_results: int) -> tuple[NewsArticle, ...]:
        articles = self._client.search(asset.search_query, max_results=max_results)
        self.ctx.record(
            f"{self.name}:news",
            source=self.name,
            article_count=len(articles),
        )
        return tuple(articles)

    def fetch_trending(self, *, max_results: int) -> tuple[NewsArticle, ...]:
        articles = self._client.search_by_topic(CRYPTO_TOPIC, max_results=max_results)
        self.ctx.record(
            f"{self.name}:trending",
            source=f"{self.name}:{CRYPTO_TOPIC.lower()}",
            article_count=len(articles),
        )
        return tuple(articles)


__all__ = ["CRYPTO_TOPIC", "GNewsProvider"]
