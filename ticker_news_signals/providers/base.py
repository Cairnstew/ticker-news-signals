"""The provider contract.

A provider declares which :class:`~ticker_news_signals.capabilities.Capability`
values it implements and implements only those methods. The orchestrator
introspects ``supports`` and never calls a method a provider did not claim, so
adding a provider requires no orchestrator change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from ..cache import Cache
from ..capabilities import Capability
from ..config import Config
from ..errors import RateLimitExceeded
from ..http import HttpClient, RateLimiter
from ..models import (
    MissingField,
    NewsArticle,
    Provenance,
    RateLimitInfo,
    SourceErrorRecord,
)
from ..resolve import ResolvedTicker


@dataclass(slots=True)
class ProviderContext:
    """Everything a provider needs, injected by the orchestrator."""

    config: Config
    http: HttpClient
    cache: Cache
    limiter: RateLimiter | None = None

    provenance: dict[str, Provenance] = field(default_factory=dict)
    errors: list[SourceErrorRecord] = field(default_factory=list)
    rate_limited: list[RateLimitInfo] = field(default_factory=list)
    missing: list[MissingField] = field(default_factory=list)

    def record(
        self,
        key: str,
        *,
        source: str,
        url: str | None = None,
        cached: bool = False,
        article_count: int = 0,
        fetched_at: datetime | None = None,
        notes: tuple[str, ...] = (),
    ) -> Provenance:
        """Record provenance for a field group and return it."""
        prov = Provenance(
            source=source,
            fetched_at=fetched_at or datetime.now(UTC),
            url=url,
            cached=cached,
            article_count=article_count,
            notes=notes,
        )
        self.provenance[key] = prov
        return prov

    def note_missing(self, field_name: str, reason: str) -> None:
        """Explain an empty result so it is never mistaken for a real zero."""
        self.missing.append(MissingField(field=field_name, reason=reason))

    def note_error(
        self, source: str, capability: Capability, exc: BaseException
    ) -> SourceErrorRecord:
        """Record a tolerated failure and return it."""
        message = str(exc) or repr(exc)
        record = SourceErrorRecord(
            source=source,
            capability=str(capability),
            exception_type=type(exc).__name__,
            message=message,
        )
        self.errors.append(record)
        if isinstance(exc, RateLimitExceeded):
            self.rate_limited.append(
                RateLimitInfo(
                    source=exc.source or source,
                    capability=str(capability),
                    message=message,
                    retry_after=exc.retry_after,
                    limit=exc.limit,
                    remaining=exc.remaining,
                    remedy=exc.hint,
                    suppressed=exc.suppressed,
                )
            )
        return record

    def throttle(self) -> None:
        """Wait for this provider's rate-limit slot, if one is configured."""
        if self.limiter is not None:
            self.limiter.acquire()

    def cached(self, key: str, ttl: float) -> Any | None:
        """Return a cached value, or ``None``."""
        return self.cache.get(key)

    def remember(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store a value in the TTL cache."""
        self.cache.set(key, value, self.config.cache_ttl if ttl is None else ttl)


class NewsProvider:
    """Base class for all news providers.

    Deliberately *not* an ``abc.ABC``: every capability hook has a working
    no-op default, so a provider only overrides what it actually supports.
    Capability enforcement lives in ``supports`` and in the orchestrator.
    """

    #: Stable identifier used in provenance, error and skip records.
    name: ClassVar[str] = "provider"

    #: Capabilities this provider implements. Subclasses must narrow this.
    supports: ClassVar[frozenset[Capability]] = frozenset({Capability.NEWS})

    def __init__(self, context: ProviderContext) -> None:
        self.ctx = context

    @classmethod
    def available(cls, config: Config) -> tuple[bool, str | None]:
        """Whether this provider can run, and the reason if it cannot.

        Default: always available. Override to gate on credentials.
        """
        return True, None

    @classmethod
    def rate_limit_rpm(cls, config: Config) -> int:
        """The provider's own documented requests-per-minute ceiling."""
        return config.rate_limit_rpm

    # -- capability hooks; default no-ops so partial implementations stay short --

    def fetch_news(self, asset: ResolvedTicker, *, max_results: int) -> tuple[NewsArticle, ...]:
        """Ticker-relevant headlines for ``asset``."""
        return ()

    def fetch_trending(self, *, max_results: int) -> tuple[NewsArticle, ...]:
        """Broad, ticker-agnostic headlines for the domain."""
        return ()

    def fetch_history(
        self, asset: ResolvedTicker, *, start, end, granularity: str = "day"
    ) -> tuple[NewsArticle, ...]:
        """Ticker headlines *as of* a past window, reconstructed from an archive.

        ``start``/``end`` are aware UTC datetimes bounding the window (inclusive).
        Providers that only serve live content do not declare ``Capability.HISTORY``
        and so are never called here.
        """
        return ()

    def close(self) -> None:
        """Release any provider-owned resources (sessions, clients)."""


__all__ = ["NewsProvider", "ProviderContext"]
