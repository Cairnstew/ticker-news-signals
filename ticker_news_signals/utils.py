"""Small helpers shared across the ticker-news-signals package.

Centralises the timestamp conversions (RFC 2822 -> aware UTC datetimes, ISO
rendering, hour bucketing) and the market-symbol normalisation used when a
user-visible ``ETH/USD`` becomes the signal-vector key ``ETH_USD``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

# A single UTC hour in seconds (used by the lookback helpers).
HOUR_SECONDS = 3600


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC :class:`datetime`."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return *dt* as an aware UTC datetime (naive input is assumed UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def floor_to_hour(dt: datetime) -> datetime:
    """Snap *dt* to the start of its UTC hour (the signal timestamp)."""
    dt = ensure_utc(dt)
    return dt.replace(minute=0, second=0, microsecond=0)


def isoformat_utc(dt: datetime) -> str:
    """Render an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (JSON-safe string)."""
    dt = ensure_utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_dt(iso: str) -> datetime:
    """Parse a ``YYYY-MM-DDTHH:MM:SSZ`` / ISO 8601 string into an aware UTC datetime."""
    text = iso.rstrip("Z")
    dt = datetime.fromisoformat(text)
    return ensure_utc(dt)


def parse_article_date(text: str | None) -> datetime | None:
    """Best-effort parse of a GNews ``published date`` (RFC 2822 or ISO 8601).

    Returns an aware UTC datetime, or ``None`` when the text cannot be parsed
    (for example the relative SearchApi ``"2 hours ago"`` format — callers
    decide how to treat undated articles).
    """
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return ensure_utc(dt)


def normalize_ticker(ticker: str) -> str:
    """Normalise a market spelling to the signal-vector key.

    ``ETH/USD`` -> ``ETH_USD``; ``eth-usd`` -> ``ETH_USD``; ``ETH:USD`` ->
    ``ETH_USD``.  Anything else is left as-is (uppercased).
    """
    return ticker.strip().upper().replace("/", "_").replace("-", "_").replace(":", "_")


def base_symbol(ticker: str) -> str:
    """Return the base asset of a ticker for use as a search query.

    ``ETH/USD`` -> ``ETH``; ``BTC-USD`` -> ``BTC``; a bare ``ETH`` passes
    through unchanged.
    """
    return ticker.strip().upper().split("/", 1)[0].split("-", 1)[0]
