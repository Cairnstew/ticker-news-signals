"""Per-source rate limiting, retry with jittered backoff, and an httpx wrapper.

Retry policy is deliberately narrow: only ``408``/``425``/``429``/``5xx`` and
transport timeouts are retried. A ``400/401/403/404`` is a caller or
credential problem — retrying it only burns quota and, for a shared news feed,
risks getting the whole IP range throttled.

The transport is synchronous (an ``httpx.Client``) so the package stays
importable and usable without an event loop; the orchestrator fans out over
sources with a thread pool instead.
"""

from __future__ import annotations

import asyncio  # noqa: F401  (kept for API parity / future async transport)
import random
import threading
import time
from typing import Any

import httpx

from .config import Config
from .errors import RateLimitExceeded

#: HTTP statuses worth another attempt.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Cap on how long a source may park us via ``Retry-After``. A hostile or
#: buggy value must not turn one throttled call into an hours-long stall.
MAX_RETRY_AFTER = 60.0

#: Reminder appended to a throttling error when the source suggests nothing better.
GENERIC_HINT = (
    "lower TNS_RATE_LIMIT, stagger requests, set the provider's API token "
    "for a higher quota, or rely on another source"
)


def _header_int(headers: httpx.Headers, *names: str) -> int | None:
    for name in names:
        raw = headers.get(name)
        if raw:
            try:
                return int(float(raw))
            except ValueError:
                continue
    return None


def parse_retry_after(
    headers: httpx.Headers, *, now: float | None = None, ceiling: float = MAX_RETRY_AFTER
) -> float | None:
    """Seconds to wait, from ``Retry-After`` (delta or HTTP-date) or reset hints.

    Providers spell this half a dozen ways, so all the common spellings are
    read: a bare delta-seconds value, an HTTP-date, and an epoch or
    seconds-from-now ``x-ratelimit-reset``.
    """
    now = time.time() if now is None else now
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(0.0, min(ceiling, float(raw)))
        except ValueError:
            try:  # HTTP-date form
                from email.utils import parsedate_to_datetime

                delta = parsedate_to_datetime(raw).timestamp() - now
                return max(0.0, min(ceiling, delta))
            except (TypeError, ValueError, IndexError):
                pass
    for name in ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        # Values below ~1e9 cannot be epoch seconds; treat them as relative.
        delta = value - now if value > 1_000_000_000 else value
        return max(0.0, min(ceiling, delta))
    return None


def _limit_hint(source: str) -> str:
    """A remediation hint, source-specific where we know the answer.

    Sources may be namespaced (``rss:coindesk``), so the hint is keyed on the
    provider root before the first colon.
    """
    known = {
        "gnews": "Google News RSS is shared per-IP; lower TNS_RATE_LIMIT",
        "cryptopanic": "CryptoPanic free tier is 100 req/day; set TNS_CRYPTOPANIC_TOKEN",
        "rss": "RSS feeds are public but shared; space out requests with TNS_RATE_LIMIT",
        "bingnews": "Bing News search is shared per-IP; lower TNS_RATE_LIMIT",
    }
    return known.get(source.split(":", 1)[0], GENERIC_HINT)


class RateLimiter:
    """A thread-safe token-bucket limiter, one instance per source.

    ``rpm`` is requests-per-minute. When a source documents a stricter limit,
    pass that instead and it takes precedence over the config default.
    """

    __slots__ = ("_interval", "_lock", "_next_allowed", "name", "rpm")

    def __init__(self, rpm: int, *, name: str = "default") -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.name = name
        self.rpm = rpm
        self._interval = 60.0 / rpm
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until the next request slot is free."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._interval


class RetryPolicy:
    """Exponential backoff with full jitter."""

    __slots__ = ("base_delay", "max_after", "max_delay", "max_retries")

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        max_after: float = MAX_RETRY_AFTER,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_after = max_after

    @classmethod
    def from_config(cls, config: Config) -> RetryPolicy:
        return cls(
            max_retries=config.max_retries,
            base_delay=config.retry_base_delay,
            max_delay=config.retry_max_delay,
            max_after=config.retry_max_after,
        )

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Backoff before ``attempt`` (0-based), jittered in ``[0, backoff)``."""
        source = rng or random
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        return source.uniform(0.0, ceiling)


class HttpClient:
    """An ``httpx.Client`` wrapper applying rate limiting, retries and a breaker.

    One of these is created per source (``source`` names it). After a genuine
    throttle the client opens a **circuit breaker**: for ``cooldown`` seconds
    every call raises :class:`RateLimitExceeded` immediately instead of
    spending real quota. Without it, an orchestrator fanning out over several
    feeds would fire every one of them at an already-exhausted source and
    collect the same 429 several times over.
    """

    __slots__ = (
        "_blocked_until",
        "_client",
        "_limiter",
        "_lock",
        "_owns_client",
        "_policy",
        "source",
    )

    def __init__(
        self,
        *,
        config: Config | None = None,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        client: httpx.Client | None = None,
        source: str = "http",
        timeout: float | None = None,
    ) -> None:
        if config is None and (limiter is None or retry is None):
            raise ValueError("config is required unless limiter and retry are given")
        self._policy = retry or RetryPolicy.from_config(config)  # type: ignore[arg-type]
        if limiter is not None:
            self._limiter = limiter
        else:
            assert config is not None
            self._limiter = RateLimiter(config.rate_limit_rpm, name="http")
        self._owns_client = client is None
        self.source = source
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout if config else (timeout or 15.0)),
            follow_redirects=True,
            headers={
                "User-Agent": "ticker-news-signals/0.2 (+https://github.com/Cairnstew/ticker-news-signals)"
            },
        )

    def view(self, source: str) -> HttpClient:
        """A per-source view sharing this client's transport, limiter and policy.

        Only the circuit breaker is private to the view, so one throttled feed
        never blocks the others. Ownership stays with the parent, so closing
        the parent closes the single underlying connection pool.
        """
        return HttpClient(
            limiter=self._limiter,
            retry=self._policy,
            client=self._client,
            source=source,
        )

    @property
    def is_throttled(self) -> bool:
        return time.monotonic() < self._blocked_until

    def _open_breaker(self, until: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, until)

    # -- request surface -----------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET ``url`` with retries and rate limiting.

        Raises :class:`RateLimitExceeded` for a throttled source and
        ``httpx.HTTPStatusError`` for any other non-success status.
        """
        if self.is_throttled:
            raise RateLimitExceeded(
                self.source,
                f"{self.source} rate limit still in effect; request suppressed",
                retry_after=max(0.0, self._blocked_until - time.monotonic()),
                hint=_limit_hint(self.source),
                suppressed=True,
            )

        last_exc: httpx.HTTPError | None = None
        for attempt in range(self._policy.max_retries + 1):
            self._limiter.acquire()
            # Signalled in-band: an exception raised inside an `except` block is
            # not caught by sibling handlers of the same `try`.
            delay: float | None = None
            try:
                response = self._client.get(url, params=params, headers=headers)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    delay = self._throttle_delay(exc, attempt)  # raises if spent
                elif status not in RETRYABLE_STATUS or attempt >= self._policy.max_retries:
                    raise
                else:
                    last_exc = exc
                    delay = self._policy.delay_for(attempt)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._policy.max_retries:
                    raise
                last_exc = exc
                delay = self._policy.delay_for(attempt)

            assert delay is not None  # every path above sets it or raises
            time.sleep(delay)
            self._blocked_until = 0.0  # cooldown elapsed; let traffic resume
        assert last_exc is not None  # pragma: no cover - loop always sets or raises
        raise last_exc

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET ``url`` and decode JSON."""
        return self.get(url, params=params, headers=headers).json()

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """GET ``url`` and return the decoded body text."""
        return self.get(url, params=params, headers=headers).text

    def get_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """GET ``url`` and return the raw body (feed parsers want bytes)."""
        return self.get(url, params=params, headers=headers).content

    def _throttle_delay(self, exc: httpx.HTTPStatusError, attempt: int) -> float:
        """How long to wait before retrying a 429, or raise if retries are spent."""
        headers = exc.response.headers
        advertised = parse_retry_after(headers, ceiling=self._policy.max_after)
        raw_advertised = parse_retry_after(headers)  # uncapped, for reporting
        if attempt >= self._policy.max_retries:
            cooldown = advertised if advertised is not None else self._policy.delay_for(attempt)
            if advertised is not None:
                self._open_breaker(time.monotonic() + min(cooldown, self._policy.max_after))
            raise RateLimitExceeded(
                self.source,
                f"{self.source} returned HTTP 429 after {attempt + 1} attempt(s)",
                retry_after=raw_advertised,
                limit=_header_int(
                    headers, "x-ratelimit-limit", "ratelimit-limit", "x-rate-limit-limit"
                ),
                remaining=_header_int(
                    headers,
                    "x-ratelimit-remaining",
                    "ratelimit-remaining",
                    "x-rate-limit-remaining",
                ),
                hint=_limit_hint(self.source),
            ) from exc
        delay = self._policy.delay_for(attempt)
        if advertised is not None:
            delay = max(delay, min(advertised, self._policy.max_after))
        self._open_breaker(time.monotonic() + delay)
        return delay

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["RETRYABLE_STATUS", "HttpClient", "RateLimiter", "RetryPolicy", "parse_retry_after"]
