"""Historical pipeline: reconstruct per-ticker sentiment vectors for past windows.

This is the history analogue of :mod:`ticker_news_signals.pipeline`, one axis
over. It answers "what were people saying about ``ETH`` during 2026-03-01..07"
instead of "what are they saying right now", and reuses the same building
blocks — the provider context, HTTP layer (rate limiting / retry / breaker),
cross-source reconciliation and sentiment scoring — so the two paths stay
directly comparable.

Sources declare :class:`~ticker_news_signals.capabilities.Capability.HISTORY`
(only the Wayback provider today) and answer the same window. Two guarantees
borrowed from the live pipeline:

* **Partial-failure tolerance** — a source that explodes is recorded on the
  result's ``data_quality`` block, never an exception.
* **Coverage honesty** — each source reports exactly how much of the requested
  window its archived data actually spans
  (:class:`~ticker_news_signals.models.HistoryCoverage`). A sparse archive
  explains itself rather than pretending to a full graph.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from .cache import SQLiteCache
from .capabilities import Capability
from .config import Config
from .http import HttpClient, RateLimiter
from .logging_config import log_event
from .models import (
    DataQuality,
    HistoryCoverage,
    HistoryRequest,
    HistoryResult,
    HistorySeries,
    MissingField,
    NewsArticle,
    Provenance,
    SignalVector,
    SkippedSource,
    SourceErrorRecord,
)
from .pipeline import build_cache
from .providers import NewsProvider, ProviderContext
from .providers.wayback import WaybackProvider
from .reconcile import dedupe, sentiment_dispersion
from .resolve import ResolvedTicker, resolve_ticker
from .sentiment import score_batch
from .utils import ensure_utc, floor_to_hour, iso_to_dt, isoformat_utc, now_utc

#: Default history source set. ``wayback`` reconstructs headlines from the
#: Internet Archive's captured RSS feeds; it is the only keyless archived
#: headline source shipping today.
HISTORY_PROVIDERS: tuple[type[NewsProvider], ...] = (WaybackProvider,)


def _as_dt(value) -> datetime | None:
    """Coerce a datetime or ISO-8601 string (or epoch) to aware UTC, or ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return iso_to_dt(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    raise ValueError(f"cannot interpret history bound {value!r}")


def _dedupe_errors(items: list[SourceErrorRecord]) -> list[SourceErrorRecord]:
    seen: set[tuple[str, str, str]] = set()
    out: list[SourceErrorRecord] = []
    for item in items:
        key = (item.source, item.capability, item.message)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _dedupe_throttled(items: list) -> list:
    seen: set[tuple[str, str]] = set()
    out = []
    for item in items:
        key = (item.source, item.capability)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _dedupe_missing(items: list[MissingField]) -> list[MissingField]:
    seen: set[tuple[str, str]] = set()
    out: list[MissingField] = []
    for item in items:
        key = (item.field, item.reason)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _bucket_start(moment: datetime, granularity: str) -> datetime:
    """Floor *moment* onto its history bucket (hour or day), UTC."""
    moment = moment.astimezone(UTC)
    if granularity == "day":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "hour":
        return floor_to_hour(moment)
    raise ValueError(f"unknown granularity {granularity!r}; use 'hour' or 'day'")


def _bucket_times(start: datetime, end: datetime, granularity: str) -> list[datetime]:
    """The canonical ordered bucket starts inside [start, end]."""
    out: list[datetime] = []
    moment = _bucket_start(start, granularity)
    stop = _bucket_start(end, granularity)
    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    while moment <= stop:
        if moment >= start and moment <= end:
            out.append(moment)
        moment += step
    return out


class HistoryRunner:
    """Orchestrates a historical fetch across sources."""

    def __init__(self, config: Config, *, providers: tuple[type[NewsProvider], ...] | None = None):
        self._config = config
        self._classes = tuple(providers) if providers is not None else HISTORY_PROVIDERS

    # -- public --------------------------------------------------------------

    def fetch(self, ticker: str, *, granularity: str, start, end, now) -> HistoryResult:
        """Run the historical pipeline for *ticker* over [start, end]."""
        config = self._config
        asset = resolve_ticker(ticker)
        request = HistoryRequest(
            ticker=asset.symbol,
            granularity=granularity,
            start=_as_dt(start),
            end=_as_dt(end),
        )
        now_val = _as_dt(now) if now is not None else now_utc()
        assert now_val is not None  # _as_dt(None) unreachable above
        started = ensure_utc(now_val)

        cache = build_cache(config)
        http = HttpClient(config=config)
        contexts: dict[str, ProviderContext] = {}
        instances: dict[str, NewsProvider] = {}
        skipped: list[SkippedSource] = []
        setup_errors: list[SourceErrorRecord] = []

        for cls in self._classes:
            try:
                ok, reason = cls.available(config)
            except Exception as exc:
                setup_errors.append(
                    SourceErrorRecord(cls.name, "available", type(exc).__name__, str(exc))
                )
                skipped.append(SkippedSource(source=cls.name, reason=f"availability failed: {exc}"))
                continue
            if not ok:
                skipped.append(SkippedSource(source=cls.name, reason=reason or "unavailable"))
                continue
            try:
                context = ProviderContext(
                    config=config,
                    http=http.view(cls.name),
                    cache=cache,
                    limiter=RateLimiter(cls.rate_limit_rpm(config), name=cls.name),
                )
                instance = cls(context)
            except Exception as exc:
                setup_errors.append(
                    SourceErrorRecord(cls.name, "setup", type(exc).__name__, str(exc))
                )
                skipped.append(SkippedSource(source=cls.name, reason=f"init failed: {exc}"))
                continue
            contexts[cls.name] = context
            instances[cls.name] = instance

        raw_articles: list[NewsArticle] = []
        try:
            if instances:
                with ThreadPoolExecutor(max_workers=max(1, len(instances))) as pool:
                    futures = {
                        pool.submit(
                            instance.fetch_history,
                            asset,
                            start=request.start,
                            end=request.end,
                            granularity=request.granularity,
                        ): name
                        for name, instance in instances.items()
                    }
                    for future, name in futures.items():
                        try:
                            raw_articles.extend(future.result())
                        except Exception as exc:  # recorded, never fatal
                            contexts[name].note_error(name, Capability.HISTORY, exc)
        finally:
            for provider in instances.values():
                with contextlib.suppress(Exception):
                    provider.close()
            http.close()
            if isinstance(cache, SQLiteCache):
                cache.close()

        dedup = dedupe(raw_articles)
        vectors = self._aggregate(asset, dedup.articles, request, started)
        series = self._build_series(asset, contexts, dedup.articles, request)

        errors: list[SourceErrorRecord] = list(setup_errors)
        throttled: list = []
        missing: list[MissingField] = []
        provenance: dict[str, Provenance] = {}
        for context in contexts.values():
            errors.extend(context.errors)
            throttled.extend(context.rate_limited)
            missing.extend(context.missing)
            provenance.update(context.provenance)

        if not vectors:
            missing.append(
                MissingField(
                    field="history",
                    reason=(
                        "no archived headlines in the requested window "
                        "(see data_quality.errors / skipped)"
                    ),
                )
            )

        quality = DataQuality(
            provenance=provenance,
            errors=tuple(_dedupe_errors(errors)),
            rate_limited=tuple(_dedupe_throttled(throttled)),
            skipped=tuple(skipped),
            missing=tuple(_dedupe_missing(missing)),
            sources_used=tuple(sorted({a.source for a in raw_articles})),
            duplicates_removed=dedup.duplicates_removed,
            articles_fetched=len(raw_articles),
        )

        log_event(
            "ticker_news_signals.history",
            "built historical vectors",
            level="INFO",
            ticker=asset.symbol,
            granularity=request.granularity,
            buckets=len(vectors),
            sources=len(quality.sources_used),
        )

        return HistoryResult(
            ticker=asset.symbol,
            query=ticker,
            granularity=request.granularity,
            start=request.start,
            end=request.end,
            vectors=tuple(vectors),
            series=tuple(series),
            data_quality=quality,
            generated_at=started,
            resolved_symbol=asset.base,
            resolved_name=asset.name,
        )

    # -- internals -----------------------------------------------------------

    def _aggregate(self, asset: ResolvedTicker, articles, request, started) -> list[SignalVector]:
        """Bucket headlines into (hour|day) vectors, scored like the live path."""
        buckets: dict[datetime, list[NewsArticle]] = defaultdict(list)
        for article in articles:
            published = article.published_dt or started
            if published > started:
                published = started
            if request.start is not None and published < request.start:
                continue
            if request.end is not None and published > request.end:
                continue
            buckets[_bucket_start(published, request.granularity)].append(article)

        vectors: list[SignalVector] = []
        for bucket_start in _bucket_times(
            request.start or started, request.end or started, request.granularity
        ):
            items = buckets.get(bucket_start) or []
            if not items:
                continue
            scores = score_batch(
                (_headline(a) for a in items), backend=self._config.sentiment_backend
            )
            count = len(items)
            sources = sorted({a.source for a in items})
            ranked = sorted(
                zip(items, scores, strict=True), key=lambda pair: abs(pair[1]), reverse=True
            )
            vectors.append(
                SignalVector(
                    ticker=asset.symbol,
                    timestamp=isoformat_utc(bucket_start),
                    sentiment_score=round(sum(scores) / count, 4),
                    article_count=count,
                    novelty_flag=False,  # historical buckets are not "new"
                    source_count=len(sources),
                    sentiment_std=round(sentiment_dispersion(scores), 4),
                    sources=tuple(sources),
                    top_headlines=tuple(_headline(a) for a, _s in ranked[:3]),
                )
            )
        return vectors

    def _build_series(self, asset, contexts, articles, request) -> list[HistorySeries]:
        """Per-source coverage + articles, plus a window-wide coverage block.

        Group articles by their ``source`` stamp (e.g. ``wayback:coindesk``),
        not by provider class name — one Wayback provider fans out over many
        feeds, and each contributes its own series.
        """
        by_source: dict[str, list[NewsArticle]] = defaultdict(list)
        for a in articles:
            by_source[a.source].append(a)

        series: list[HistorySeries] = []
        for source_key, source_articles in sorted(by_source.items()):
            ordered = tuple(
                sorted(
                    source_articles,
                    key=lambda a: a.published_dt or datetime.min.replace(tzinfo=UTC),
                )
            )
            published = [a.published_dt or request.start for a in ordered]
            if source_key.startswith("wayback:"):
                provider_name = self._provider_for(source_key)
                provenance = contexts.get(provider_name, {}).provenance if provider_name else {}
            else:
                provenance = {}
            coverage = build_history_coverage(
                source=source_key,
                published=published,
                start=request.start,
                end=request.end,
                granularity=request.granularity,
            )
            series.append(
                HistorySeries(
                    source=source_key,
                    ticker=asset.symbol,
                    granularity=request.granularity,
                    articles=ordered,
                    coverage=coverage,
                    provenance=provenance.get(source_key),
                )
            )
        return series

    def _provider_for(self, source_key: str) -> str | None:
        """Map a source stamp (``wayback:coindesk``) to its provider class name."""
        prefix, _, _rest = source_key.partition(":")
        for cls in self._classes:
            if cls.name == prefix:
                return cls.name
        return None


def build_history_coverage(
    *,
    source: str,
    published: list[datetime],
    start: datetime | None,
    end: datetime | None,
    granularity: str,
) -> HistoryCoverage:
    """Summarise how much of [start, end] a source's archived data covers.

    Mirrors tickerkit's coverage contract: buckets present vs expected,
    interior gaps, and a ``complete`` flag that only turns on when the window
    is bounded, non-empty, and every expected bucket has at least one article.
    """
    from itertools import pairwise

    start = ensure_utc(start) if start is not None else None
    end = ensure_utc(end) if end is not None else None
    # Buckets strictly inside the requested window. Articles captured for a
    # moment *outside* the window (a feed snapshot is a trailing window of its
    # own) are facts about the archive, not coverage of the request.
    ordered = sorted(
        b
        for b in {_bucket_start(p, granularity) for p in published}
        if (start is None or b >= start) and (end is None or b <= end)
    )
    expected = (
        len(_bucket_times(start, end, granularity)) if (start and end) else (len(ordered) or 0)
    )
    present = len(ordered)

    gaps: list[dict[str, str]] = []
    if start is not None and end is not None and len(ordered) >= 2:
        step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
        for prev, cur in pairwise(ordered):
            expected_between = int((cur - prev) // step - 1)
            if expected_between > 0:
                gaps.append(
                    {
                        "start": isoformat_utc(prev),
                        "end": isoformat_utc(cur),
                        "missing": str(expected_between),
                    }
                )

    missing = max(0, expected - present)
    complete = bool(
        start is not None
        and end is not None
        and ordered
        and ordered[0] == _bucket_start(start, granularity)
        and ordered[-1] == _bucket_start(end, granularity)
        and not gaps
    )
    note = None
    if not complete and (start is not None and end is not None):
        note = (
            f"archive covered {present}/{expected} buckets; "
            "complete reconstruction is not possible from captured feeds"
        )

    return HistoryCoverage(
        source=source,
        requested_start=start,
        requested_end=end,
        first=ordered[0] if ordered else None,
        last=ordered[-1] if ordered else None,
        buckets=present,
        expected=expected,
        missing=missing,
        truncated=False,
        complete=complete,
        note=note,
        gaps=tuple(gaps),
    )


def fetch_history(
    ticker: str,
    *,
    granularity: str = "day",
    start=None,
    end=None,
    now=None,
    config: Config | None = None,
    providers: tuple[type[NewsProvider], ...] | None = None,
) -> HistoryResult:
    """Convenience wrapper: build a runner from *config* and fetch history."""
    config = config or Config.from_env()
    return HistoryRunner(config, providers=providers).fetch(
        ticker, granularity=granularity, start=start, end=end, now=now
    )


def _headline(article: NewsArticle) -> str:
    import html

    return html.unescape(article.title).strip()


__all__ = [
    "HISTORY_PROVIDERS",
    "HistoryRunner",
    "build_history_coverage",
    "fetch_history",
]
