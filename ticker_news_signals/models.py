"""Typed models for the ticker-news-signals pipeline.

Data-carrying types:

- :class:`NewsArticle` — one headline as returned by any provider, normalised
  to plain strings plus a parsed timestamp and the ``source`` it came from.
- :class:`SignalVector` — one per-ticker, per-hour aggregate: the mean
  sentiment across that hour's headlines, the article count, and a novelty
  flag. :meth:`SignalVector.to_dict` returns the **canonical five-field
  schema** (the consumer contract documented in ``INTEGRATION.md``) and
  :meth:`SignalVector.to_record` returns the richer form used by reports.
- quality records (:class:`Provenance`, :class:`SourceErrorRecord`,
  :class:`RateLimitInfo`, :class:`SkippedSource`, :class:`MissingField`) and
  the :class:`DataQuality` block that bundles them, so a missing value is
  always explained and never mistaken for a real zero.
- :class:`SignalResult` — everything a fetch produced: the vectors, the
  quality block, and the resolved asset.

Every ``to_dict()`` here returns JSON-ready primitives (str/int/float/bool/
list/dict/None): no datetimes, no Decimals, no surprise keys.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .utils import parse_article_date


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite(value: Any) -> float | None:
    """Coerce to a finite float or ``None``, rejecting NaN/inf so JSON stays clean."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_dt(value: Any) -> datetime | None:
    """Inverse of :func:`_iso`: an ISO string (or datetime) back to aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    # Python's fromisoformat accepts ISO 8601; passthrough naive stamps as UTC.
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- article


@dataclass(slots=True)
class NewsArticle:
    """One news headline, normalised from any provider's raw entry."""

    title: str
    description: str = ""
    published_date: str = ""  # raw string as the provider reported it
    url: str = ""
    publisher: str = ""
    source: str = ""  # provider/feed name, e.g. "rss:coindesk", "gnews"

    @classmethod
    def from_gnews(cls, entry: dict[str, Any], *, source: str = "gnews") -> NewsArticle:
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
            source=source,
        )

    @property
    def published_dt(self) -> datetime | None:
        """The parsed publish time as an aware UTC datetime, or ``None``."""
        return parse_article_date(self.published_date)

    @property
    def dedupe_key(self) -> str:
        """Cross-source identity: canonical URL, falling back to a normalised title."""
        url = self.url.strip().lower()
        if url:
            # Strip the query/fragment so the same story shared with tracking
            # parameters across feeds collapses to one article.
            url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
            return url
        title = " ".join(self.title.lower().split())
        return title

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (always string leaves)."""
        return {
            "title": self.title,
            "description": self.description,
            "published_date": self.published_date,
            "url": self.url,
            "publisher": self.publisher,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewsArticle:
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            published_date=str(data.get("published_date") or ""),
            url=str(data.get("url") or ""),
            publisher=str(data.get("publisher") or ""),
            source=str(data.get("source") or ""),
        )

    def __str__(self) -> str:
        stamp = self.published_date or "?"
        origin = f"{self.source} " if self.source else ""
        return f"[{stamp}] {origin}{self.publisher}: {self.title}"


# --------------------------------------------------------------------------- signals


@dataclass(slots=True)
class SignalVector:
    """One per-ticker, per-hour sentiment signal.

    The first five fields are the canonical, documented output schema and are
    the only ones :meth:`to_dict` emits. The remaining fields are the richer
    detail surfaced by :meth:`to_record` (reports, ``--json``, ``--extended``).
    """

    ticker: str
    timestamp: str
    sentiment_score: float = 0.0
    article_count: int = 0
    novelty_flag: bool = False

    # -- extended (not part of the canonical JSONL schema) --
    source_count: int = 0
    sentiment_std: float = 0.0
    #: Mean per-headline disagreement between the ensemble's constituent
    #: scorers (0.0 for single-backend runs). Raised when VADER and the crypto
    #: lexicon read the same headline differently — a contested hour.
    scorer_std: float = 0.0
    sources: tuple[str, ...] = ()
    top_headlines: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignalVector:
        """Build from a previously-exported JSON record (e.g. a JSONL line)."""
        sources = data.get("sources") or ()
        if isinstance(sources, str):
            sources = (sources,)
        headlines = data.get("top_headlines") or ()
        if isinstance(headlines, str):
            headlines = (headlines,)
        return cls(
            ticker=str(data.get("ticker") or ""),
            timestamp=str(data.get("timestamp") or ""),
            sentiment_score=float(data.get("sentiment_score") or 0.0),
            article_count=int(data.get("article_count") or 0),
            novelty_flag=bool(data.get("novelty_flag") or False),
            source_count=int(data.get("source_count") or 0),
            sentiment_std=float(data.get("sentiment_std") or 0.0),
            scorer_std=float(data.get("scorer_std") or 0.0),
            sources=tuple(str(s) for s in sources),
            top_headlines=tuple(str(h) for h in headlines),
        )

    def to_dict(self) -> dict[str, Any]:
        """The canonical five-field record (the consumer contract)."""
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp,
            "sentiment_score": self.sentiment_score,
            "article_count": self.article_count,
            "novelty_flag": self.novelty_flag,
        }

    def to_record(self) -> dict[str, Any]:
        """The canonical fields plus the extended ones (for reports / ``--json``)."""
        record = self.to_dict()
        record.update(
            {
                "source_count": self.source_count,
                "sentiment_std": self.sentiment_std,
                "scorer_std": self.scorer_std,
                "sources": list(self.sources),
                "top_headlines": list(self.top_headlines),
            }
        )
        return record

    def to_json(self, *, indent: int | None = None, sort_keys: bool = True) -> str:
        """The extended record as JSON."""
        return json.dumps(self.to_record(), indent=indent, sort_keys=sort_keys)

    @classmethod
    def from_json(cls, text: str) -> SignalVector:
        """Parse the extended-record JSON back (or a canonical record)."""
        return cls.from_dict(json.loads(text))

    def __str__(self) -> str:
        novelty = " new" if self.novelty_flag else ""
        spread = f" ±{self.sentiment_std:.3f}" if self.sentiment_std else ""
        return (
            f"{self.ticker} {self.timestamp} score={self.sentiment_score:+.3f}"
            f"{spread} n={self.article_count} src={self.source_count}{novelty}"
        )


# --------------------------------------------------------------------------- quality


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a provenance group came from and how old it is."""

    source: str
    fetched_at: datetime
    url: str | None = None
    cached: bool = False
    article_count: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched_at": _iso(self.fetched_at),
            "url": self.url,
            "cached": self.cached,
            "article_count": self.article_count,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Provenance:
        """Rebuild from :meth:`to_dict` output (the inverse of it)."""
        return cls(
            source=str(data.get("source") or ""),
            fetched_at=_parse_dt(data.get("fetched_at")) or datetime.now(UTC),
            url=data.get("url"),
            cached=bool(data.get("cached") or False),
            article_count=int(_finite(data.get("article_count")) or 0),
            notes=tuple(str(n) for n in data.get("notes") or ()),
        )


@dataclass(frozen=True, slots=True)
class SourceErrorRecord:
    """A tolerated provider failure. Named distinctly from the exception type."""

    source: str
    capability: str
    exception_type: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "capability": self.capability,
            "exception_type": self.exception_type,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceErrorRecord:
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            source=str(data.get("source") or ""),
            capability=str(data.get("capability") or ""),
            exception_type=str(data.get("exception_type") or ""),
            message=str(data.get("message") or ""),
        )


@dataclass(frozen=True, slots=True)
class RateLimitInfo:
    """A structured throttle record, surfaced without parsing exception strings."""

    source: str
    capability: str
    message: str
    retry_after: float | None = None
    limit: int | None = None
    remaining: int | None = None
    remedy: str | None = None
    suppressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "capability": self.capability,
            "message": self.message,
            "retry_after": self.retry_after,
            "limit": self.limit,
            "remaining": self.remaining,
            "remedy": self.remedy,
            "suppressed": self.suppressed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RateLimitInfo:
        """Rebuild from :meth:`to_dict` output."""
        limit = _finite(data.get("limit"))
        remaining = _finite(data.get("remaining"))
        return cls(
            source=str(data.get("source") or ""),
            capability=str(data.get("capability") or ""),
            message=str(data.get("message") or ""),
            retry_after=_finite(data.get("retry_after")),
            limit=int(limit) if limit is not None else None,
            remaining=int(remaining) if remaining is not None else None,
            remedy=data.get("remedy"),
            suppressed=bool(data.get("suppressed") or False),
        )


@dataclass(frozen=True, slots=True)
class SkippedSource:
    """A provider that did not run, and why (missing key, disabled, …)."""

    source: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkippedSource:
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            source=str(data.get("source") or ""),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class MissingField:
    """An explanation for a ``None``/empty result, so it is never a silent zero."""

    field: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissingField:
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            field=str(data.get("field") or ""),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class DataQuality:
    """The accountability block attached to every result."""

    provenance: dict[str, Provenance] = field(default_factory=dict)
    errors: tuple[SourceErrorRecord, ...] = ()
    rate_limited: tuple[RateLimitInfo, ...] = ()
    skipped: tuple[SkippedSource, ...] = ()
    missing: tuple[MissingField, ...] = ()
    sources_used: tuple[str, ...] = ()
    duplicates_removed: int = 0
    articles_fetched: int = 0

    @property
    def ok(self) -> bool:
        """True when no source errored or was throttled."""
        return not self.errors and not self.rate_limited

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": {k: v.to_dict() for k, v in self.provenance.items()},
            "errors": [e.to_dict() for e in self.errors],
            "rate_limited": [r.to_dict() for r in self.rate_limited],
            "skipped": [s.to_dict() for s in self.skipped],
            "missing": [m.to_dict() for m in self.missing],
            "sources_used": list(self.sources_used),
            "duplicates_removed": self.duplicates_removed,
            "articles_fetched": self.articles_fetched,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DataQuality:
        """Rebuild from :meth:`to_dict` output (the inverse of it)."""
        return cls(
            provenance={
                str(k): Provenance.from_dict(v) for k, v in (data.get("provenance") or {}).items()
            },
            errors=tuple(SourceErrorRecord.from_dict(e) for e in data.get("errors") or ()),
            rate_limited=tuple(RateLimitInfo.from_dict(r) for r in data.get("rate_limited") or ()),
            skipped=tuple(SkippedSource.from_dict(s) for s in data.get("skipped") or ()),
            missing=tuple(MissingField.from_dict(m) for m in data.get("missing") or ()),
            sources_used=tuple(str(s) for s in data.get("sources_used") or ()),
            duplicates_removed=int(_finite(data.get("duplicates_removed")) or 0),
            articles_fetched=int(_finite(data.get("articles_fetched")) or 0),
        )


# --------------------------------------------------------------------------- result


@dataclass(slots=True)
class SignalResult:
    """Everything one fetch produced: vectors, quality, and the resolved asset."""

    ticker: str
    vectors: list[SignalVector]
    data_quality: DataQuality
    generated_at: datetime
    query: str = ""
    resolved_symbol: str = ""
    resolved_name: str | None = None
    articles: tuple[NewsArticle, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready envelope body (used by ``--json`` and the export layer).

        Round-trips through :meth:`from_dict`: the unique articles are included
        so a saved fetch can be replayed in full.
        """
        return {
            "ticker": self.ticker,
            "query": self.query,
            "resolved": {
                "symbol": self.resolved_symbol,
                "name": self.resolved_name,
            },
            "generated_at": _iso(self.generated_at),
            "signals": [v.to_record() for v in self.vectors],
            "articles": [a.to_dict() for a in self.articles],
            "data_quality": self.data_quality.to_dict(),
        }

    def to_json(self, *, indent: int | None = 2, sort_keys: bool = False) -> str:
        """JSON-serialise the whole envelope."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=sort_keys)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignalResult:
        """Rebuild a result from :meth:`to_dict` output (its inverse).

        Round-trips vectors, the unique articles, the quality block and the
        resolved asset. Unknown keys are ignored, missing keys fall back to
        the dataclass defaults.
        """
        resolved = data.get("resolved") or {}
        return cls(
            ticker=str(data.get("ticker") or ""),
            vectors=[SignalVector.from_dict(v) for v in data.get("signals") or ()],
            data_quality=DataQuality.from_dict(data.get("data_quality") or {}),
            generated_at=_parse_dt(data.get("generated_at")) or datetime.now(UTC),
            query=str(data.get("query") or ""),
            resolved_symbol=str(resolved.get("symbol") or "") if isinstance(resolved, dict) else "",
            resolved_name=resolved.get("name") if isinstance(resolved, dict) else None,
            articles=tuple(NewsArticle.from_dict(a) for a in data.get("articles") or ()),
        )

    @classmethod
    def from_json(cls, text: str) -> SignalResult:
        """Parse JSON produced by :meth:`to_json` back into a result object."""
        return cls.from_dict(json.loads(text))


# --------------------------------------------------------------------------- history


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    """What a historical query asked for, echoed so coverage can be judged.

    ``start``/``end`` are inclusive/aware UTC bounds; ``granularity`` is ``hour``
    or ``day``. A history result never claims more than this window.
    """

    ticker: str
    granularity: str = "day"
    start: datetime | None = None
    end: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "granularity": self.granularity,
            "start": _iso(self.start),
            "end": _iso(self.end),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryRequest:
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            ticker=str(data.get("ticker") or ""),
            granularity=str(data.get("granularity") or "day"),
            start=_parse_dt(data.get("start")),
            end=_parse_dt(data.get("end")),
        )


@dataclass(frozen=True, slots=True)
class HistoryCoverage:
    """How much of the requested window an archive actually answered.

    Mirrors tickerkit's coverage contract for deep history: a series states the
    window it covers, the intervals it is missing and whether a crawl gap cut
    the walk short. ``complete`` is only true when every requested bucket is
    present; a gap is recorded, never silently skipped.
    """

    source: str
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    first: datetime | None = None
    last: datetime | None = None
    buckets: int = 0
    expected: int = 0
    missing: int = 0
    truncated: bool = False
    complete: bool = False
    note: str | None = None
    gaps: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "requested_start": _iso(self.requested_start),
            "requested_end": _iso(self.requested_end),
            "first": _iso(self.first),
            "last": _iso(self.last),
            "buckets": self.buckets,
            "expected": self.expected,
            "missing": self.missing,
            "truncated": self.truncated,
            "complete": self.complete,
            "gaps": list(self.gaps),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryCoverage:
        """Rebuild from :meth:`to_dict` output (the inverse of it)."""
        return cls(
            source=str(data.get("source") or ""),
            requested_start=_parse_dt(data.get("requested_start")),
            requested_end=_parse_dt(data.get("requested_end")),
            first=_parse_dt(data.get("first")),
            last=_parse_dt(data.get("last")),
            buckets=int(_finite(data.get("buckets")) or 0),
            expected=int(_finite(data.get("expected")) or 0),
            missing=int(_finite(data.get("missing")) or 0),
            truncated=bool(data.get("truncated") or False),
            complete=bool(data.get("complete") or False),
            gaps=tuple(dict(g) for g in data.get("gaps") or ()),
            note=data.get("note"),
        )


@dataclass(slots=True)
class HistorySeries:
    """One archive source's answer for one ``(ticker, granularity)`` window.

    ``articles`` are the reconstructed headlines the source contributed;
    ``coverage`` says how much of the requested window those articles actually
    span — the trust boundary a consumer should read before using the series.
    """

    source: str
    ticker: str
    granularity: str
    articles: tuple[NewsArticle, ...] = ()
    coverage: HistoryCoverage | None = None
    provenance: Provenance | None = None

    @property
    def has_articles(self) -> bool:
        return bool(self.articles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ticker": self.ticker,
            "granularity": self.granularity,
            "articles": [a.to_dict() for a in self.articles],
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistorySeries:
        """Rebuild from :meth:`to_dict` output (the inverse of it)."""
        coverage = data.get("coverage")
        provenance = data.get("provenance")
        return cls(
            source=str(data.get("source") or ""),
            ticker=str(data.get("ticker") or ""),
            granularity=str(data.get("granularity") or "day"),
            articles=tuple(NewsArticle.from_dict(a) for a in data.get("articles") or ()),
            coverage=HistoryCoverage.from_dict(coverage) if coverage else None,
            provenance=Provenance.from_dict(provenance) if provenance else None,
        )


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """The history analogue of :class:`SignalResult`.

    Each source answers the same window; ``series`` carries per-source answers
    and coverage; ``vectors`` are the deduplicated, per-bucket aggregates built
    from every source that answered — the shape a consumer joins on.
    """

    ticker: str
    query: str
    granularity: str
    start: datetime | None
    end: datetime | None
    vectors: tuple[SignalVector, ...] = ()
    series: tuple[HistorySeries, ...] = ()
    data_quality: DataQuality = field(default_factory=DataQuality)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_symbol: str = ""
    resolved_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "ticker": self.ticker,
            "resolved": {
                "symbol": self.resolved_symbol,
                "name": self.resolved_name,
            },
            "granularity": self.granularity,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "generated_at": _iso(self.generated_at),
            "vectors": [v.to_record() for v in self.vectors],
            "series": [s.to_dict() for s in self.series],
            "data_quality": self.data_quality.to_dict(),
        }

    def to_json(self, *, indent: int | None = 2, sort_keys: bool = False) -> str:
        """JSON-serialise the whole result."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=sort_keys)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryResult:
        """Rebuild a result from :meth:`to_dict` output (its inverse).

        Round-trips every field — vectors, per-source series (with their full
        articles, coverage and provenance), the quality block, datetimes and
        the resolved asset. Unknown keys are ignored, missing keys fall back
        to the dataclass defaults.
        """
        resolved = data.get("resolved") or {}
        return cls(
            ticker=str(data.get("ticker") or ""),
            query=str(data.get("query") or ""),
            granularity=str(data.get("granularity") or "day"),
            start=_parse_dt(data.get("start")),
            end=_parse_dt(data.get("end")),
            vectors=tuple(SignalVector.from_dict(v) for v in data.get("vectors") or ()),
            series=tuple(HistorySeries.from_dict(s) for s in data.get("series") or ()),
            data_quality=DataQuality.from_dict(data.get("data_quality") or {}),
            generated_at=_parse_dt(data.get("generated_at")) or datetime.now(UTC),
            resolved_symbol=str(resolved.get("symbol") or "") if isinstance(resolved, dict) else "",
            resolved_name=resolved.get("name") if isinstance(resolved, dict) else None,
        )

    @classmethod
    def from_json(cls, text: str) -> HistoryResult:
        """Parse JSON produced by :meth:`to_json` back into a result object."""
        return cls.from_dict(json.loads(text))


__all__ = [
    "DataQuality",
    "HistoryCoverage",
    "HistoryRequest",
    "HistoryResult",
    "HistorySeries",
    "MissingField",
    "NewsArticle",
    "Provenance",
    "RateLimitInfo",
    "SignalResult",
    "SignalVector",
    "SkippedSource",
    "SourceErrorRecord",
]
