"""Wayback Machine history provider — historical headlines from archived feeds.

The live pipeline answers "what are people saying right now?". This provider
answers "what were people saying during a past window?" by reconstructing the
headline set from the Internet Archive's *captured* copies of the configured
RSS feeds.

Mechanism (two archive.org endpoints, both keyless):

1. **CDX search** — ``web.archive.org/cdx/search/cdx`` lists the capture
   timestamps for a URL. We ask for the feed's snapshots inside the requested
   window and pick the nearest one per granularity bucket (hour/day).
2. **Snapshot fetch** — the ``{timestamp}id_`` replay form returns the raw
   archived bytes of the feed at that moment, which the ordinary RSS parser
   reads (it is exactly the same feedparser path the live ``rss`` provider
   uses, so the two sources stay directly comparable).

Coverage is deliberately honest about the archive's uneven crawl rate: a feed
captured ~240 minutes out of a day supports fine-grained reconstruction, one
captured once a month does not. :class:`~ticker_news_signals.models.HistoryCoverage`
reports exactly how many buckets the archive could answer, and ``complete`` is
only true when every requested bucket had a snapshot.

Rate limiting is a real constraint: archive.org throttles hard (a burst gets an
"Internet Archive: Temporarily Offline" page), so this provider uses the shared
safety-net limiter and each CDX/snapshot request passes through the same
retry/backoff/circuit-breaker machinery as every other source.

Docs:
- CDX API: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server
- availability/wb replay: https://archive.org/help/wayback_api.php
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from ..capabilities import Capability
from ..config import Config, FeedSpec
from ..models import NewsArticle
from ..resolve import ResolvedTicker
from .base import NewsProvider

#: CDX search endpoint (keyless). ``limit`` bound per query.
CDX_URL = "https://web.archive.org/cdx/search/cdx"
#: Snapshot replay endpoint. The ``{timestamp}``/``{url}`` are filled in.
WB_BASE = "https://web.archive.org/web/{ts}{modifier}/{url}"

_GRANULARITY_SECONDS = {"hour": 3600, "day": 86400}


def _url_key(feed_url: str) -> str:
    """Normalise a feed URL for CDX 'exact' matching (strip trailing slash query)."""
    url = feed_url.split("#")[0].strip("/")
    return url


def _parse_wb_ts(text: str) -> datetime:
    """Parse a 14-digit Wayback timestamp string (YYYYMMDDHHMMSS) as UTC."""
    return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def _fmt_wb_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M%S")


class WaybackProvider(NewsProvider):
    """Reconstructs historical headlines from archived RSS feed snapshots."""

    name: ClassVar[str] = "wayback"
    supports: ClassVar[frozenset[Capability]] = frozenset({Capability.HISTORY})

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        from .rss import _entry_published, _strip_html  # reuse the RSS normalisers

        self._entry_published = _entry_published
        self._strip_html = _strip_html

    @classmethod
    def available(cls, config: Any) -> tuple[bool, str | None]:
        if not config.rss_feeds:
            return (
                False,
                "no RSS feeds configured (set TNS_RSS_FEEDS) — the archive is keyed by feed",
            )
        return True, None

    @classmethod
    def rate_limit_rpm(cls, config: Config) -> int:
        # archive.org is public infrastructure and throttles aggressively; keep a
        # calm ceiling under the shared net even for a CDX fan-out.
        return min(config.rate_limit_rpm, 15)

    # -- capability hook -----------------------------------------------------

    def fetch_history(
        self, asset: ResolvedTicker, *, start, end, granularity: str = "day"
    ) -> tuple[NewsArticle, ...]:
        """Reconstruct headlines for *asset* from archived snapshots of each feed."""
        from datetime import datetime

        from ..utils import ensure_utc, iso_to_dt

        def _coerce(value: Any):
            if value is None or isinstance(value, datetime):
                return value
            if isinstance(value, str):
                return iso_to_dt(value)
            return value

        raw_start = _coerce(start)
        raw_end = _coerce(end)
        start = ensure_utc(raw_start) if raw_start is not None else None
        end = ensure_utc(raw_end) if raw_end is not None else None
        out: list[NewsArticle] = []
        for feed in self.ctx.config.rss_feeds:
            try:
                out.extend(
                    self._reconstruct_feed(
                        feed, asset=asset, start=start, end=end, granularity=granularity
                    )
                )
            except Exception as exc:  # one feed's archive must not sink the rest
                self.ctx.note_error(feed.provenance_source(), Capability.HISTORY, exc)
                self.ctx.note_missing(f"wayback.{feed.name}", f"archive unavailable: {exc}")
        return tuple(out)

    # -- internals -----------------------------------------------------------

    def _reconstruct_feed(
        self, feed: FeedSpec, *, asset: ResolvedTicker, start, end, granularity: str
    ) -> list[NewsArticle]:
        interval = _GRANULARITY_SECONDS[granularity]
        snapshots = self._cdx_snapshots(feed.url, start=start, end=end)
        if not snapshots:
            self.ctx.record(
                f"wayback:{feed.name}",
                source=feed.provenance_source(),
                url=CDX_URL,
                article_count=0,
                notes=(f"no archive captures in [{start}..{end}]",),
            )
            return []

        buckets = self._bucket_times(start, end, interval)
        # Closest snapshot per bucket: prefer the one >= bucket start (the feed
        # as-it-was near that hour/day), else the latest before it.
        chosen: list[datetime] = []
        for bucket in buckets:
            nearest = self._nearest_snapshot(snapshots, bucket)
            if nearest is not None:
                chosen.append(nearest)
        if not chosen:
            return []

        articles: list[NewsArticle] = []
        for ts in chosen:
            articles.extend(self._read_snapshot(feed, ts, asset=asset))
        return articles

    def _bucket_times(self, start, end, interval: int) -> list[datetime]:
        if start is None or end is None:
            return []
        ordered = []
        moment = start
        while moment <= end:
            ordered.append(moment)
            moment += timedelta(seconds=interval)
        return ordered

    def _cdx_snapshots(self, url: str, *, start, end) -> list[datetime]:
        """Capture timestamps for a feed URL inside [start, end], via CDX."""
        cache_key = f"wb-cdx:{_url_key(url)}:{_fmt_wb_ts(start)}:{_fmt_wb_ts(end)}"
        raw = self.ctx.cached(cache_key, -1.0)  # archives are immutable
        if raw is None:
            self.ctx.throttle()
            payload = self.ctx.http.get_json(
                CDX_URL,
                params={
                    "url": _url_key(url),
                    "from": _fmt_wb_ts(start),
                    "to": _fmt_wb_ts(end),
                    "output": "json",
                    "fl": "timestamp,statuscode",
                    "filter": "statuscode:200",
                    "limit": 5000,
                },
            )
            self.ctx.remember(cache_key, json.dumps(payload), -1.0)
        else:
            payload = json.loads(raw)

        rows = payload if isinstance(payload, list) else []
        # rows[0] is the header ("timestamp","statuscode"); skip it.
        out: list[datetime] = []
        for row in rows[1:]:
            if isinstance(row, list) and row and row[0].isdigit():
                out.append(_parse_wb_ts(row[0]))
        out.sort()
        return out

    def _nearest_snapshot(self, snapshots: list[datetime], bucket: datetime) -> datetime | None:
        """Nearest snapshot to *bucket*: earliest at/after bucket, else latest before."""
        upcoming = [ts for ts in snapshots if ts >= bucket]
        if upcoming:
            return min(upcoming)
        prior = [ts for ts in snapshots if ts < bucket]
        return max(prior) if prior else None

    def _read_snapshot(
        self, feed: FeedSpec, snapshot: datetime, *, asset: ResolvedTicker
    ) -> list[NewsArticle]:
        from ..utils import isoformat_utc

        ts = _fmt_wb_ts(snapshot)
        url = WB_BASE.format(ts=ts, modifier="id_", url=feed.url)
        cache_key = f"wb-snap:{_url_key(feed.url)}:{ts}"
        raw = self.ctx.cached(cache_key, -1.0)
        was_cached = raw is not None
        if raw is None:
            self.ctx.throttle()
            raw = self.ctx.http.get_bytes(url)
            self.ctx.remember(
                cache_key, raw.decode("utf-8", "replace"), -1.0
            )  # archives never change
        elif isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")

        if asset is None:
            return []

        import feedparser

        parsed = feedparser.parse(raw)
        entries = getattr(parsed, "entries", []) or []
        articles: list[NewsArticle] = []
        for entry in entries:
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            description = self._strip_html(
                str(entry.get("summary") or entry.get("description") or "")
            )
            if not asset.matches(f"{title} {description}"):
                continue
            articles.append(
                NewsArticle(
                    title=title,
                    description=description,
                    published_date=self._entry_published(entry),
                    url=str(entry.get("link") or "").strip(),
                    publisher=str(parsed.feed.get("title") or feed.name),
                    source=f"wayback:{feed.name}",
                )
            )

        self.ctx.record(
            f"wayback:{feed.name}:{ts}",
            source=f"wayback:{feed.name}",
            url=url,
            cached=was_cached,
            fetched_at=snapshot,
            article_count=len(articles),
            notes=(f"archived_snapshot={isoformat_utc(snapshot)}",),
        )
        return articles


__all__ = ["CDX_URL", "WB_BASE", "_GRANULARITY_SECONDS", "WaybackProvider"]
