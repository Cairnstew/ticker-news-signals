"""Bing News provider — keyless Microsoft News search RSS.

Bing's public news search returns an RSS feed with no API key and no account
(`format=rss` on the news search endpoint). It is *per-ticker queryable*, so
unlike the broad RSS feeds (which are filtered locally after a full feed
download) this provider hands the query to the search engine up-front.

That makes it the "redundant keyless source" tickerkit reaches for: a second,
independent search engine alongside GNews's Google News RSS, so the two
crawlers corroborate or contradict each other rather than one search engine
being able to shape an hour silently.

API docs: there is no formal developer page for the format=rss endpoint; it is
the same search surface as https://www.bing.com/news (the response is a
standard RSS 2.0 document with RFC-822 publish dates).

Bing's endpoint tolerates a bare client and has no published numeric rate
limit, so it relies on the shared safety-net limiter rather than a
source-specific ceiling.
"""

from __future__ import annotations

from typing import ClassVar

from ..capabilities import Capability
from ..config import Config
from ..models import NewsArticle
from ..resolve import ResolvedTicker
from .base import NewsProvider
from .rss import _entry_published, _strip_html

try:  # feedparser is a declared dependency; the guard keeps import failures a skip.
    import feedparser
except Exception:  # pragma: no cover - only on a broken install
    feedparser = None  # type: ignore[assignment]

#: Bing's news-search RSS endpoint. ``format=rss`` is the keyless switch.
SEARCH_URL = "https://www.bing.com/news/search"

#: A generous ceiling on entries considered per query before filtering.
_MAX_ENTRIES = 50


class BingNewsProvider(NewsProvider):
    """Fetches per-ticker headlines from Bing News via its keyless search RSS."""

    name: ClassVar[str] = "bingnews"
    supports: ClassVar[frozenset[Capability]] = frozenset({Capability.NEWS, Capability.TRENDING})

    @classmethod
    def available(cls, config: Config) -> tuple[bool, str | None]:
        if feedparser is None:
            return False, "feedparser is not installed"
        return True, None

    @classmethod
    def rate_limit_rpm(cls, config: Config) -> int:
        # No documented per-account ceiling; keep the shared safety net.
        return config.rate_limit_rpm

    # -- capability hooks ----------------------------------------------------

    def fetch_news(self, asset: ResolvedTicker, *, max_results: int) -> tuple[NewsArticle, ...]:
        return self._search(asset.search_query, asset=asset, max_results=max_results)

    def fetch_trending(self, *, max_results: int) -> tuple[NewsArticle, ...]:
        return self._search("cryptocurrency", asset=None, max_results=max_results)

    # -- internals -----------------------------------------------------------

    def _search(
        self, query: str, *, asset: ResolvedTicker | None, max_results: int
    ) -> tuple[NewsArticle, ...]:
        cache_key = f"bing:{query}"
        raw = self.ctx.cached(cache_key, self.ctx.config.cache_ttl)
        was_cached = raw is not None
        if raw is None:
            self.ctx.throttle()
            raw = self.ctx.http.get_bytes(
                SEARCH_URL,
                params={"q": query, "format": "rss"},
            )
            # Feed bytes are not JSON; stash as latin-1 text so the SQLite
            # cache's canonical-JSON encoding has a stable, lossless form.
            self.ctx.remember(cache_key, raw.decode("utf-8", "replace"), self.ctx.config.cache_ttl)
        elif isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")

        assert feedparser is not None  # guaranteed by available() before dispatch
        parsed = feedparser.parse(raw)
        entries = getattr(parsed, "entries", []) or []
        feed_title = str(parsed.feed.get("title") or "Bing News")
        articles: list[NewsArticle] = []
        for entry in entries[:_MAX_ENTRIES]:
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            description = _strip_html(str(entry.get("summary") or entry.get("description") or ""))
            if asset is not None and not asset.matches(f"{title} {description}"):
                continue
            articles.append(
                NewsArticle(
                    title=title,
                    description=description,
                    published_date=_entry_published(entry),
                    url=str(entry.get("link") or "").strip(),
                    publisher=str(entry.get("source") or feed_title),
                    source=self.name,
                )
            )
            if len(articles) >= max_results:
                break

        self.ctx.record(
            f"{self.name}:{self._capability_label(asset)}",
            source=self.name,
            url=SEARCH_URL,
            cached=was_cached,
            article_count=len(articles),
            notes=(f"q={query}",),
        )
        return tuple(articles)

    @staticmethod
    def _capability_label(asset: ResolvedTicker | None) -> str:
        return "trending" if asset is None else "news"


__all__ = ["SEARCH_URL", "BingNewsProvider"]
