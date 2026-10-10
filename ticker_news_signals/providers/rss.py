"""RSS/Atom provider — arbitrary keyless feeds, filtered to a ticker.

This is the provider that turns a single-source tool into a pipeline: any
number of public crypto news feeds can be read at once (see
:data:`ticker_news_signals.config.DEFAULT_RSS_FEEDS`), each contributing
headlines that are then de-duplicated across sources.

Every feed is fetched through the shared, rate-limited, retrying HTTP client,
parsed with :mod:`feedparser`, and filtered to the requested ticker with
whole-word term matching (so ``ETH`` never matches "together"). One dead feed
is recorded on the result's quality block and never sinks the others.

Wide, ticker-agnostic feeds are exposed through the optional ``TRENDING``
capability, which returns the newest entries unfiltered.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from ..capabilities import Capability
from ..config import FeedSpec
from ..http import HttpClient
from ..models import NewsArticle
from ..resolve import ResolvedTicker
from ..utils import isoformat_utc, parse_article_date
from .base import NewsProvider, ProviderContext

try:  # feedparser is a declared dependency; the guard keeps import failures a skip.
    import feedparser
except Exception:  # pragma: no cover - only on a broken install
    feedparser = None  # type: ignore[assignment]

#: A generous ceiling on entries considered per feed before filtering; feeds can
#: carry hundreds of items and we only want the recent tail.
_MAX_ENTRIES_PER_FEED = 200

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", text)).split())


def _entry_published(entry: Any) -> str:
    """Best-effort publish timestamp as a string the pipeline can parse."""
    raw = str(entry.get("published") or entry.get("updated") or "")
    if raw and parse_article_date(raw) is not None:
        return raw
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is not None:
        try:
            dt = datetime.fromtimestamp(time.mktime(parsed), tz=UTC)
            return isoformat_utc(dt)
        except (OverflowError, ValueError, TypeError):
            pass
    return raw


class RssProvider(NewsProvider):
    """Fetches and filters a configured set of RSS/Atom feeds."""

    name: ClassVar[str] = "rss"
    supports: ClassVar[frozenset[Capability]] = frozenset({Capability.NEWS, Capability.TRENDING})

    def __init__(self, context: ProviderContext) -> None:
        super().__init__(context)
        # One HTTP *view* per feed. Views share the transport and rate limiter
        # but each carries a private circuit breaker, so a single feed returning
        # 429 short-circuits only that feed — never the other seventeen. This is
        # the per-source breaker guarantee, applied per feed rather than per
        # provider.
        self._views: dict[str, HttpClient] = {
            feed.name: context.http.view(feed.provenance_source())
            for feed in context.config.rss_feeds
        }

    @classmethod
    def available(cls, config: Any) -> tuple[bool, str | None]:
        if feedparser is None:
            return False, "feedparser is not installed"
        if not config.rss_feeds:
            return False, "no RSS feeds configured (set TNS_RSS_FEEDS)"
        return True, None

    @classmethod
    def rate_limit_rpm(cls, config: Any) -> int:
        # Feeds are cheap to read, but they are still public infrastructure;
        # keep the shared safety-net ceiling rather than hammering them.
        return config.rate_limit_rpm

    # -- capability hooks ----------------------------------------------------

    def fetch_news(self, asset: ResolvedTicker, *, max_results: int) -> tuple[NewsArticle, ...]:
        return self._collect(asset=asset, max_results=max_results)

    def fetch_trending(self, *, max_results: int) -> tuple[NewsArticle, ...]:
        return self._collect(asset=None, max_results=max_results)

    # -- internals -----------------------------------------------------------

    def _collect(
        self, *, asset: ResolvedTicker | None, max_results: int
    ) -> tuple[NewsArticle, ...]:
        capability = Capability.TRENDING if asset is None else Capability.NEWS
        articles: list[NewsArticle] = []
        for feed in self.ctx.config.rss_feeds:
            try:
                articles.extend(self._read_feed(feed, asset=asset, max_results=max_results))
            except Exception as exc:  # one dead feed must not sink the rest
                self.ctx.note_error(feed.provenance_source(), capability, exc)
                self.ctx.note_missing(
                    f"rss.{feed.name}",
                    f"feed unavailable: {exc}",
                )
        return tuple(articles)

    def _read_feed(
        self, feed: FeedSpec, *, asset: ResolvedTicker | None, max_results: int
    ) -> list[NewsArticle]:
        cache_key = f"rss:{feed.name}"
        raw = self.ctx.cached(cache_key, self.ctx.config.cache_ttl)
        was_cached = raw is not None
        if raw is None:
            self.ctx.throttle()
            raw = self._views[feed.name].get_bytes(feed.url)
            # Feed bytes are not JSON; stash as latin-1 text so the SQLite cache's
            # canonical-JSON encoding has a stable, lossless representation.
            self.ctx.remember(cache_key, raw.decode("utf-8", "replace"), self.ctx.config.cache_ttl)
        elif isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")

        assert feedparser is not None  # guaranteed by available() before dispatch
        parsed = feedparser.parse(raw)
        entries = getattr(parsed, "entries", []) or []
        articles: list[NewsArticle] = []
        for entry in entries[:_MAX_ENTRIES_PER_FEED]:
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
                    publisher=str(parsed.feed.get("title") or feed.name),
                    source=feed.provenance_source(),
                )
            )
            if len(articles) >= max_results:
                break

        self.ctx.record(
            f"rss:{feed.name}",
            source=feed.provenance_source(),
            url=feed.url,
            cached=was_cached,
            article_count=len(articles),
        )
        return articles


__all__ = ["RssProvider"]
