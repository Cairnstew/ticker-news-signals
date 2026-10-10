"""Aggregation orchestrator: sources -> per-hour :class:`SignalVector` rows.

The pipeline is deliberately small at its edges and generous in the middle:

1. Resolve the ticker to a symbol/name/term set.
2. Ask every available provider for its ticker-relevant (and, optionally,
   trending) headlines, concurrently through a thread pool.
3. Collapse cross-source duplicates, remembering which sources carried each
   story.
4. Keep only articles inside the lookback window, bucket by UTC hour.
5. Score each bucket's headlines and aggregate into a
   :class:`~ticker_news_signals.models.SignalVector`.
6. Return the vectors alongside a :class:`DataQuality` block that records
   every error, throttle, skip and missing field.

All network access goes through providers, so tests can substitute fakes.
"""

from __future__ import annotations

import contextlib
import html
import os
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .cache import Cache, CacheLimits, MemoryCache, NullCache, SQLiteCache
from .capabilities import Capability
from .client import NewsClient
from .config import Config
from .http import HttpClient, RateLimiter
from .logging_config import log_event
from .models import (
    DataQuality,
    MissingField,
    NewsArticle,
    Provenance,
    RateLimitInfo,
    SignalResult,
    SignalVector,
    SkippedSource,
    SourceErrorRecord,
)
from .providers import GNewsProvider, NewsProvider, ProviderContext, registry, select
from .reconcile import DedupeResult, dedupe, sentiment_dispersion
from .resolve import ResolvedTicker, resolve_ticker
from .sentiment import score_batch_detail
from .utils import (
    base_symbol,
    ensure_utc,
    floor_to_hour,
    isoformat_utc,
    normalize_ticker,
    now_utc,
)

#: Default age within which a headline marks its hour as novel. Kept as a module
#: constant for back-compat; :attr:`Config.novelty_minutes` is the live value.
NOVELTY_WINDOW = timedelta(minutes=15)


# --------------------------------------------------------------------------- helpers


def _query_period(lookback_hours: int) -> str:
    """Pick a GNews period string that covers *lookback_hours*."""
    if lookback_hours <= 24:
        return f"{lookback_hours}h"
    return f"{(lookback_hours + 23) // 24}d"


def _feed_period(config: Config, lookback_hours: int) -> str:
    """Size the feed window to the lookback unless the user pinned it via env."""
    if os.environ.get("TNS_PERIOD") or os.environ.get("TICKER_NEWS_PERIOD"):
        return config.period
    return _query_period(lookback_hours)


def build_cache(config: Config) -> Cache:
    """Select the cache implementation the config asks for."""
    if config.cache_path is not None:
        return SQLiteCache(
            config.cache_path,
            limits=CacheLimits(
                max_entries=config.cache_max_entries,
                max_bytes=config.cache_max_bytes,
            ),
        )
    if config.cache_enabled:
        return MemoryCache()
    return NullCache()


# --------------------------------------------------------------------------- back-compat helpers


def _dedupe(articles: Iterable[NewsArticle]) -> list[NewsArticle]:
    """Drop duplicates by URL (falling back to normalised title); returns the list.

    Kept as a module-level helper for back-compat with existing callers/tests;
    :func:`ticker_news_signals.reconcile.dedupe` is the richer entry point.
    """
    return dedupe(articles).articles


def collect_articles(
    ticker: str,
    *,
    client: NewsClient | None = None,
    max_results: int = 100,
) -> list[NewsArticle]:
    """Gather and deduplicate candidate headlines for *ticker* (single source).

    A convenience wrapper around the GNews client used by older callers;
    :func:`fetch_result` runs the full multi-source orchestrator.
    """
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


def _headline(article: NewsArticle) -> str:
    """The scored text for an article: title, HTML-entities unescaped."""
    return html.unescape(article.title).strip()


def _is_novel(items: list[NewsArticle], now: datetime, window: timedelta) -> bool:
    """True when *items* contain an article newer than the novelty window."""
    for article in items:
        published = article.published_dt or now
        if now - published <= window:
            return True
    return False


def _dedupe_errors(items: list[SourceErrorRecord]) -> tuple[SourceErrorRecord, ...]:
    seen: set[tuple[str, str, str]] = set()
    out: list[SourceErrorRecord] = []
    for item in items:
        key = (item.source, item.capability, item.message)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


def _dedupe_throttled(items: list[RateLimitInfo]) -> tuple[RateLimitInfo, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[RateLimitInfo] = []
    for item in items:
        key = (item.source, item.capability)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


def _dedupe_missing(items: list[MissingField]) -> tuple[MissingField, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[MissingField] = []
    for item in items:
        key = (item.field, item.reason)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


# --------------------------------------------------------------------------- orchestrator


def _chosen_providers(
    config: Config,
    *,
    providers: tuple[type[NewsProvider], ...] | None,
    client: NewsClient | None,
) -> tuple[type[NewsProvider], ...]:
    """Pick the provider set for a run.

    An explicit ``client`` pins the run to GNews (the documented single-source
    back-compat path). An explicit ``providers`` tuple is honoured as-is.
    Otherwise the config's ``sources`` selection applies (empty => all).
    """
    if client is not None:
        chosen: tuple[type[NewsProvider], ...] = (GNewsProvider,)
    elif providers is not None:
        chosen = tuple(providers)
    else:
        chosen = select(config.sources)
    # ``config.sources`` still filters an explicitly-passed provider tuple, so a
    # caller can keep a tall registry but only exercise a subset in a given run.
    if config.sources:
        filtered = tuple(cls for cls in chosen if cls.name in config.sources)
        chosen = filtered or chosen
    return chosen


def fetch_result(
    ticker: str,
    lookback_hours: int = 1,
    *,
    client: NewsClient | None = None,
    now: datetime | None = None,
    config: Config | None = None,
    providers: tuple[type[NewsProvider], ...] | None = None,
    max_results: int | None = None,
) -> SignalResult:
    """Run the full pipeline and return vectors plus a quality block.

    Parameters
    ----------
    ticker:
        Any market spelling (``ETH/USD``, ``eth-usd``, ``ETH``).
    lookback_hours:
        Hours of history (from *now*) to aggregate.
    client:
        A :class:`NewsClient`, pinning the run to the GNews source (back-compat).
    now:
        Fixed "current" time for tests; defaults to UTC now.
    config:
        Base configuration; defaults to :meth:`Config.from_env`.
    providers:
        Explicit provider classes; overrides the config's source selection.
    max_results:
        Per-source result ceiling; overrides ``config.max_results``.
    """
    config = (config or Config.from_env()).with_overrides(
        lookback_hours=lookback_hours,
        max_results=max_results,
    )
    asset = resolve_ticker(ticker)
    current = ensure_utc(now_utc() if now is None else now)
    cutoff = current - timedelta(hours=lookback_hours)
    novelty = timedelta(minutes=config.novelty_minutes)

    chosen = _chosen_providers(config, providers=providers, client=client)

    cache = build_cache(config)
    http = HttpClient(config=config)
    contexts: dict[str, ProviderContext] = {}
    instances: dict[str, NewsProvider] = {}
    skipped: list[SkippedSource] = []
    setup_errors: list[SourceErrorRecord] = []
    plan: list[tuple[str, Capability]] = []

    for cls in chosen:
        try:
            ok, reason = cls.available(config)
        except Exception as exc:  # a broken provider must not sink the run
            setup_errors.append(_setup_error(cls.name, "available", exc))
            skipped.append(
                SkippedSource(source=cls.name, reason=f"availability check failed: {exc}")
            )
            continue
        if not ok:
            skipped.append(SkippedSource(source=cls.name, reason=reason or "unavailable"))
            continue
        try:
            rpm = cls.rate_limit_rpm(config)
            context = ProviderContext(
                config=config,
                http=http.view(cls.name),
                cache=cache,
                limiter=RateLimiter(rpm, name=cls.name),
            )
            instance = _instantiate(cls, context, client)
        except Exception as exc:
            # An unusable rpm or a throwing ``__init__`` is recorded and the
            # provider is skipped; the remaining sources still run.
            setup_errors.append(_setup_error(cls.name, "setup", exc))
            skipped.append(SkippedSource(source=cls.name, reason=f"initialisation failed: {exc}"))
            continue
        contexts[cls.name] = context
        instances[cls.name] = instance
        plan.extend((cls.name, cap) for cap in _ordered_caps(cls.supports))

    raw_articles: list[NewsArticle] = []
    try:
        if plan:
            with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
                futures = {
                    pool.submit(_run_capability, instances[name], cap, asset, config.max_results): (
                        name,
                        cap,
                    )
                    for name, cap in plan
                }
                for future, (name, cap) in futures.items():
                    try:
                        raw_articles.extend(future.result())
                    except Exception as exc:  # recorded, never fatal
                        contexts[name].note_error(name, cap, exc)
    finally:
        for provider in instances.values():
            # Teardown must never mask an assembled result.
            with contextlib.suppress(Exception):
                provider.close()
        http.close()
        if isinstance(cache, SQLiteCache):
            cache.close()

    result = dedupe(raw_articles)
    vectors = _aggregate(
        asset=asset,
        dedupe_result=result,
        current=current,
        cutoff=cutoff,
        novelty=novelty,
        config=config,
    )

    quality = _build_quality(
        contexts=contexts,
        dedupe_result=result,
        skipped=skipped,
        setup_errors=setup_errors,
        articles_fetched=len(raw_articles),
        current=current,
    )

    log_event(
        "ticker_news_signals.pipeline",
        "built signal vectors",
        level="INFO",
        ticker=asset.symbol,
        lookback_hours=lookback_hours,
        buckets=len(vectors),
        sources=len(quality.sources_used),
        errors=len(quality.errors),
    )

    return SignalResult(
        ticker=asset.symbol,
        vectors=vectors,
        data_quality=quality,
        generated_at=current,
        query=ticker,
        resolved_symbol=asset.base,
        resolved_name=asset.name,
        articles=tuple(result.articles),
    )


def fetch_signals(
    ticker: str,
    lookback_hours: int = 1,
    *,
    client: NewsClient | None = None,
    now: datetime | None = None,
    config: Config | None = None,
    providers: tuple[type[NewsProvider], ...] | None = None,
) -> list[SignalVector]:
    """Compute one :class:`SignalVector` per non-empty UTC hour in the window.

    A thin wrapper over :func:`fetch_result` preserving the original signature
    and return type. Use :func:`fetch_result` when the quality block or the
    underlying articles are needed.
    """
    return fetch_result(
        ticker,
        lookback_hours=lookback_hours,
        client=client,
        now=now,
        config=config,
        providers=providers,
    ).vectors


# --------------------------------------------------------------------------- internals


def _instantiate(
    cls: type[NewsProvider], context: ProviderContext, client: NewsClient | None
) -> NewsProvider:
    """Build a provider instance, threading the back-compat client into GNews."""
    if client is not None and issubclass(cls, GNewsProvider):
        return cls(context, client=client)
    return cls(context)


def _setup_error(source: str, capability: str, exc: BaseException) -> SourceErrorRecord:
    """A tolerated provider-setup failure (availability check or ``__init__``)."""
    return SourceErrorRecord(
        source=source,
        capability=capability,
        exception_type=type(exc).__name__,
        message=str(exc) or repr(exc),
    )


def _ordered_caps(supported: frozenset[Capability]) -> list[Capability]:
    order = (Capability.NEWS, Capability.TRENDING)
    return [cap for cap in order if cap in supported]


def _run_capability(
    provider: NewsProvider, capability: Capability, asset: ResolvedTicker, max_results: int
) -> tuple[NewsArticle, ...]:
    if capability is Capability.NEWS:
        return provider.fetch_news(asset, max_results=max_results)
    if capability is Capability.TRENDING:
        return provider.fetch_trending(max_results=max_results)
    return ()


def _aggregate(
    *,
    asset: ResolvedTicker,
    dedupe_result: DedupeResult,
    current: datetime,
    cutoff: datetime,
    novelty: timedelta,
    config: Config,
) -> list[SignalVector]:
    buckets: dict[datetime, list[NewsArticle]] = defaultdict(list)
    for article in dedupe_result.articles:
        published = article.published_dt or current
        if published > current:
            # Future-dated feed entry (clock skew, scheduled posts): treat as now
            # so it buckets into the current hour rather than a phantom future one.
            published = current
        if published < cutoff:
            continue
        buckets[floor_to_hour(published)].append(article)

    vectors: list[SignalVector] = []
    for hour in sorted(buckets):
        items = buckets[hour]
        detailed = score_batch_detail(
            (_headline(article) for article in items), backend=config.sentiment_backend
        )
        samples = [sample for sample, _components in detailed]
        components = [components for _sample, components in detailed]
        count = len(items)
        sources = sorted(
            {source for article in items for source in dedupe_result.sources_for(article)}
        )
        # Per-headline scorer disagreement: how far the ensemble's constituents
        # diverged on this headline, averaged over the hour (0.0 for a single
        # backend, whose per-headline components are length-1).
        scorer_std = (
            round(
                sum(sentiment_dispersion(list(component)) for component in components)
                / max(1, count),
                4,
            )
            if count
            else 0.0
        )
        # Most polarising headlines (largest |score|) tell the reader what drove
        # the bucket's mean, rather than the first three in feed order.
        ranked = sorted(
            zip(items, samples, strict=True), key=lambda pair: abs(pair[1]), reverse=True
        )
        top = [_headline(article) for article, _score in ranked[:3]]
        vectors.append(
            SignalVector(
                ticker=asset.symbol,
                timestamp=isoformat_utc(hour),
                sentiment_score=round(sum(samples) / count, 4) if count else 0.0,
                article_count=count,
                novelty_flag=_is_novel(items, current, novelty),
                source_count=len(sources),
                sentiment_std=round(sentiment_dispersion(samples), 4),
                scorer_std=scorer_std,
                sources=tuple(sources),
                top_headlines=tuple(top),
            )
        )
    return vectors


def _build_quality(
    *,
    contexts: dict[str, ProviderContext],
    dedupe_result: DedupeResult,
    skipped: list[SkippedSource],
    setup_errors: list[SourceErrorRecord],
    articles_fetched: int,
    current: datetime,
) -> DataQuality:
    provenance: dict[str, Provenance] = {}
    errors: list[SourceErrorRecord] = list(setup_errors)
    throttled: list[RateLimitInfo] = []
    missing: list[MissingField] = []
    for context in contexts.values():
        provenance.update(context.provenance)
        errors.extend(context.errors)
        throttled.extend(context.rate_limited)
        missing.extend(context.missing)

    sources_used = tuple(
        sorted({source for sources in dedupe_result.sources_by_key.values() for source in sources})
    )

    if not dedupe_result.articles:
        missing.append(
            MissingField(
                field="news",
                reason=(
                    "no provider returned ticker-relevant headlines in the window "
                    "(see data_quality.errors / skipped)"
                ),
            )
        )

    return DataQuality(
        provenance=provenance,
        errors=_dedupe_errors(errors),
        rate_limited=_dedupe_throttled(throttled),
        skipped=tuple(skipped),
        missing=_dedupe_missing(missing),
        sources_used=sources_used,
        duplicates_removed=dedupe_result.duplicates_removed,
        articles_fetched=articles_fetched,
    )


__all__ = [
    "NOVELTY_WINDOW",
    "build_cache",
    "collect_articles",
    "fetch_result",
    "fetch_signals",
    "registry",
]
