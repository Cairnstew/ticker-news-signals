"""Offline tests for the HTTP layer: rate limiting, retry, circuit breaker.

Network calls are mocked with ``respx`` (httpx mocks), so nothing touches the
wire and the retry/backoff knobs can be made small enough to run fast.
"""

from __future__ import annotations

import time

import httpx
import pytest
from ticker_news_signals.config import Config
from ticker_news_signals.errors import RateLimitExceeded
from ticker_news_signals.http import (
    MAX_RETRY_AFTER,
    HttpClient,
    RateLimiter,
    RetryPolicy,
    parse_retry_after,
)

URL = "https://example.test/data"


def _client(**kwargs: object) -> HttpClient:
    return HttpClient(
        config=Config(
            max_retries=1,
            retry_base_delay=0.01,
            retry_max_delay=0.01,
            retry_max_after=0.5,
            rate_limit_rpm=1000,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


class TestRetryAfterParsing:
    def test_delta_seconds(self) -> None:
        assert parse_retry_after(httpx.Headers({"Retry-After": "30"})) == 30.0

    def test_http_date_form_clamps(self) -> None:
        headers = httpx.Headers({"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})
        assert parse_retry_after(headers) == MAX_RETRY_AFTER

    def test_epoch_reset_header(self) -> None:
        now = 1_000_000_000.0
        headers = httpx.Headers({"x-ratelimit-reset": str(now + 42)})
        assert parse_retry_after(headers, now=now) == pytest.approx(42, abs=1)

    def test_relative_reset_header(self) -> None:
        assert parse_retry_after(httpx.Headers({"x-ratelimit-reset": "45"})) == 45.0

    def test_absent_is_none(self) -> None:
        assert parse_retry_after(httpx.Headers()) is None

    def test_hostile_value_clamped(self) -> None:
        assert parse_retry_after(httpx.Headers({"Retry-After": "999999"})) == MAX_RETRY_AFTER


class TestRateLimiter:
    def test_intervals_spaced_by_rpm(self) -> None:
        limiter = RateLimiter(60, name="test")  # 1 request / second
        started = time.monotonic()
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        elapsed = time.monotonic() - started
        assert elapsed >= 2.0

    def test_zero_rpm_rejected(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(0)


class TestRetryPolicy:
    def test_jittered_backoff_in_range(self) -> None:
        policy = RetryPolicy(max_retries=3, base_delay=1.0, max_delay=8.0)
        for attempt in range(3):
            delay = policy.delay_for(attempt, rng=__import__("random").Random(0))
            assert 0.0 <= delay < min(8.0, 1.0 * (2**attempt))


class TestThrottle:
    @pytest.mark.respx()
    def test_exhausted_429_raises_structured_error(self, respx_mock) -> None:
        route = respx_mock.get(URL).mock(
            return_value=httpx.Response(
                429,
                headers={
                    "Retry-After": "30",
                    "X-RateLimit-Limit": "25",
                    "X-RateLimit-Remaining": "0",
                },
            )
        )
        client = _client()
        with pytest.raises(RateLimitExceeded) as excinfo:
            client.get_json(URL)

        err = excinfo.value
        assert err.limit == 25
        assert err.remaining == 0
        assert err.retry_after == pytest.approx(30, abs=1)
        assert err.hint
        assert not err.suppressed
        assert route.call_count == 2  # initial attempt + 1 retry
        client.close()

    @pytest.mark.respx()
    def test_retry_honours_server_delay_not_local_backoff(self, respx_mock) -> None:
        """A 429 asking for a long wait must not be retried on our tiny backoff."""
        respx_mock.get(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "60"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        client = _client()
        started = time.monotonic()
        result = client.get_json(URL)
        elapsed = time.monotonic() - started
        assert result == {"ok": True}
        assert elapsed >= 0.3  # the advertised delay (capped at retry_max_after) was honoured
        client.close()

    @pytest.mark.respx()
    def test_400_is_not_retried(self, respx_mock) -> None:
        route = respx_mock.get(URL).mock(return_value=httpx.Response(404))
        client = _client()
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json(URL)
        assert route.call_count == 1  # caller/credential problems are not transient
        client.close()

    @pytest.mark.respx()
    def test_503_is_retried(self, respx_mock) -> None:
        route = respx_mock.get(URL).mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
        )
        client = _client()
        assert client.get_json(URL) == {"ok": True}
        assert route.call_count == 2
        client.close()


class TestCircuitBreaker:
    @pytest.mark.respx()
    def test_suppressed_calls_after_throttle(self, respx_mock) -> None:
        """After a real 429 the source is short-circuited: no network, structured error."""
        route = respx_mock.get(URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        client = _client()
        with pytest.raises(RateLimitExceeded):
            client.get_json(URL)
        calls_after_first_burst = route.call_count
        assert calls_after_first_burst == 2  # initial attempt + 1 retry, never more

        with pytest.raises(RateLimitExceeded) as excinfo:
            client.get_json(URL)
        assert excinfo.value.suppressed is True
        assert route.call_count == calls_after_first_burst  # breaker stopped the stampede
        client.close()

    def test_view_isolates_breakers(self) -> None:
        """Each source view may be throttled without blocking others."""
        parent = _client()
        view_a = parent.view("source-a")
        view_b = parent.view("source-b")
        view_a._blocked_until = time.monotonic() + 100
        assert view_a.is_throttled
        assert not view_b.is_throttled
        parent.close()
