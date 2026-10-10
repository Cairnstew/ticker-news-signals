"""Configuration for ticker-news-signals.

Everything is environment-driven with sane defaults, and no secret is ever
hardcoded: optional credentials are simply absent unless the user exports them,
and a provider that needs one is skipped with a recorded reason instead of
failing the run.

Environment variables (all optional)
------------------------------------
``TNS_SOURCES``
    Comma-separated provider names to use. Default: every registered provider
    that is available. e.g. ``TNS_SOURCES=gnews,rss``.
``TNS_SENTIMENT``
    Sentiment backend name. Default ``vader``. See
    :mod:`ticker_news_signals.sentiment` for the registry.
``TNS_LANGUAGE`` / ``TICKER_NEWS_LANGUAGE``
    Language of the Google News feed. Default ``en``.
``TNS_COUNTRY`` / ``TICKER_NEWS_COUNTRY``
    Country/region of the Google News feed. Default ``US``.
``TNS_PERIOD`` / ``TICKER_NEWS_PERIOD``
    Google News lookback window (``h``/``d``). The pipeline sizes this from
    ``lookback_hours`` automatically; override only to widen the feed.
``TNS_MAX_RESULTS``
    Per-source result ceiling. Default ``100``.
``TNS_LOOKBACK_HOURS``
    Default aggregation window in hours. Default ``1``.
``TNS_NOVELTY_MINUTES``
    Age within which a headline marks its hour as novel. Default ``15``.
``TNS_TIMEOUT``
    Per-request timeout in seconds. Default ``15``.
``TNS_RATE_LIMIT``
    Safety-net requests-per-minute cap per source. Default ``60``.
``TNS_MAX_RETRIES`` / ``TNS_RETRY_BASE_DELAY`` / ``TNS_RETRY_MAX_DELAY`` /
``TNS_MAX_RETRY_AFTER``
    Retry policy knobs (jittered exponential backoff).
``TNS_CACHE_ENABLED`` / ``TNS_CACHE_TTL`` / ``TNS_CACHE_PATH`` /
``TNS_CACHE_MAX_ENTRIES`` / ``TNS_CACHE_MAX_BYTES``
    TTL cache controls. Set ``TNS_CACHE_PATH`` to persist to a git-trackable
    SQLite file.
``TNS_RSS_FEEDS``
    Comma-separated ``name=url`` feed list for the RSS provider. Defaults to a
    curated set of keyless crypto feeds.
``TNS_CRYPTOPANIC_TOKEN``
    Optional CryptoPanic auth token; when unset that provider is skipped.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

#: Curated keyless crypto RSS feeds. All are free, public and require no key.
#:
#: The set deliberately spans independent publishers (a story covered by
#: several of them is cross-source reconciled into a single, well-covered
#: article; a single publisher's outage never empties an hour). The Tier-1
#: additions (BeInCrypto … Forkast) were verified live; per-ticker tag feeds
#: are intentionally *not* listed here so no single asset's coverage is
#: overweighted by construction.
DEFAULT_RSS_FEEDS_RAW: tuple[tuple[str, str], ...] = (
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("decrypt", "https://decrypt.co/feed"),
    ("theblock", "https://www.theblock.co/rss.xml"),
    ("cryptoslate", "https://cryptoslate.com/feed/"),
    ("bitcoinmagazine", "https://bitcoinmagazine.com/feed"),
    # Tier-1 additions — independent major publishers, all live-verified.
    ("beincrypto", "https://beincrypto.com/feed/"),
    ("newsbtc", "https://www.newsbtc.com/feed/"),
    ("bitcoinist", "https://bitcoinist.com/feed/"),
    ("utoday", "https://u.today/rss"),
    ("zycrypto", "https://zycrypto.com/feed/"),
    ("cryptonews", "https://cryptonews.com/feed/"),
    ("coingape", "https://coingape.com/feed/"),
    ("newsbitcoin", "https://news.bitcoin.com/feed/"),
    ("coinspeaker", "https://www.coinspeaker.com/feed/"),
    ("cryptobriefing", "https://cryptobriefing.com/feed/"),
    ("thenewscrypto", "https://thenewscrypto.com/feed/"),
    ("forkast", "https://forkast.news/feed/"),
)

_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class FeedSpec:
    """One RSS/Atom feed the :class:`~ticker_news_signals.providers.rss.RssProvider` reads."""

    name: str
    url: str

    def provenance_source(self) -> str:
        """The ``source`` string recorded for articles from this feed."""
        return f"rss:{self.name}"


#: Parsed once at import time; keeps :func:`default_rss_feeds` allocation-free.
DEFAULT_RSS_FEEDS: tuple[FeedSpec, ...] = tuple(
    FeedSpec(name=name, url=url) for name, url in DEFAULT_RSS_FEEDS_RAW
)


def _env_opt(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() or None if value else None


def _env_str(name: str, default: str) -> str:
    return _env_opt(name) or default


def _env_first(names: tuple[str, ...]) -> str | None:
    """First non-empty of ``names`` (lets ``TNS_*`` shadow legacy ``TICKER_NEWS_*``)."""
    for name in names:
        value = _env_opt(name)
        if value is not None:
            return value
    return None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_opt(name)
    if raw is None:
        return default
    return raw.lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    raw = _env_opt(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_opt(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_tuple(name: str) -> tuple[str, ...] | None:
    raw = _env_opt(name)
    if raw is None:
        return None
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or None


def _env_path(name: str) -> Path | None:
    raw = _env_opt(name)
    return Path(raw) if raw else None


def parse_feeds(raw: str) -> tuple[FeedSpec, ...]:
    """Parse a ``name=url`` comma list into :class:`FeedSpec` entries.

    A bare ``url`` is accepted and named from its host. Entries without an
    ``http(s)://`` scheme and other malformed fragments are dropped rather than
    raising — a typo in one feed must not stop the run.
    """
    feeds: list[FeedSpec] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" in item:
            name, url = item.split("=", 1)
            name, url = name.strip(), url.strip()
        else:
            name, url = "", item
        if not url or not url.lower().startswith(("http://", "https://")):
            continue
        if not name:
            host = url.split("//", 1)[-1].split("/", 1)[0]
            name = host.replace("www.", "").split(".", 1)[0] or "feed"
        feeds.append(FeedSpec(name=name, url=url))
    return tuple(feeds)


def default_rss_feeds() -> tuple[FeedSpec, ...]:
    """The feed set for this process: ``TNS_RSS_FEEDS`` when set, else the default."""
    raw = _env_opt("TNS_RSS_FEEDS")
    if raw is None:
        return DEFAULT_RSS_FEEDS
    return parse_feeds(raw)


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable run configuration.

    Build one with :meth:`from_env` for a normal CLI run, or construct directly
    in library/test code. Use :meth:`with_overrides` to derive a per-call copy
    (``None`` fields are ignored, so an unset override never clobbers a default).
    """

    # Sources / sentiment.
    sources: tuple[str, ...] = ()
    """Provider names to use; empty means "every available provider"."""
    sentiment_backend: str = "vader"

    # Query shaping.
    language: str = "en"
    country: str = "US"
    period: str = "1d"
    max_results: int = 100
    lookback_hours: int = 1
    novelty_minutes: int = 15

    # HTTP / reliability.
    timeout: float = 15.0
    rate_limit_rpm: int = 60
    max_retries: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0
    retry_max_after: float = 60.0

    # Cache.
    cache_enabled: bool = True
    cache_ttl: float = 300.0
    cache_path: Path | None = None
    cache_max_entries: int = 2000
    cache_max_bytes: int = 10 * 1024 * 1024

    # RSS provider.
    rss_feeds: tuple[FeedSpec, ...] = DEFAULT_RSS_FEEDS

    # Optional credentials (absent => provider skipped with a reason).
    cryptopanic_token: str | None = None

    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Free-form provider-specific settings; used by tests and embedders."""

    @classmethod
    def from_env(cls, **overrides: Any) -> Config:
        """Build a config from the process environment, then apply overrides."""
        config = cls(
            sources=_env_tuple("TNS_SOURCES") or (),
            sentiment_backend=_env_str("TNS_SENTIMENT", "vader"),
            language=_env_first(("TNS_LANGUAGE", "TICKER_NEWS_LANGUAGE")) or "en",
            country=_env_first(("TNS_COUNTRY", "TICKER_NEWS_COUNTRY")) or "US",
            period=_env_first(("TNS_PERIOD", "TICKER_NEWS_PERIOD")) or "1d",
            max_results=_env_int("TNS_MAX_RESULTS", 100),
            lookback_hours=_env_int("TNS_LOOKBACK_HOURS", 1),
            novelty_minutes=_env_int("TNS_NOVELTY_MINUTES", 15),
            timeout=_env_float("TNS_TIMEOUT", 15.0),
            rate_limit_rpm=_env_int("TNS_RATE_LIMIT", 60),
            max_retries=_env_int("TNS_MAX_RETRIES", 3),
            retry_base_delay=_env_float("TNS_RETRY_BASE_DELAY", 0.5),
            retry_max_delay=_env_float("TNS_RETRY_MAX_DELAY", 8.0),
            retry_max_after=_env_float("TNS_MAX_RETRY_AFTER", 60.0),
            cache_enabled=_env_bool("TNS_CACHE_ENABLED", True),
            cache_ttl=_env_float("TNS_CACHE_TTL", 300.0),
            cache_path=_env_path("TNS_CACHE_PATH"),
            cache_max_entries=_env_int("TNS_CACHE_MAX_ENTRIES", 2000),
            cache_max_bytes=_env_int("TNS_CACHE_MAX_BYTES", 10 * 1024 * 1024),
            rss_feeds=default_rss_feeds(),
            cryptopanic_token=_env_opt("TNS_CRYPTOPANIC_TOKEN"),
        )
        return config.with_overrides(**overrides)

    def with_overrides(self, **kwargs: Any) -> Config:
        """Return a copy with the given non-``None`` fields replaced."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        if not clean:
            return self
        return replace(self, **clean)


__all__ = [
    "DEFAULT_RSS_FEEDS",
    "Config",
    "FeedSpec",
    "default_rss_feeds",
    "parse_feeds",
]
