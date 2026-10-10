"""Offline tests for the Wayback history provider and the history pipeline.

Network is mocked with ``respx``: the CDX search endpoint returns capture
timestamps and the ``{ts}id_`` replay URL returns raw archived RSS bytes.
No test touches archive.org or a real feed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from ticker_news_signals.cache import MemoryCache
from ticker_news_signals.capabilities import Capability
from ticker_news_signals.config import Config, FeedSpec
from ticker_news_signals.history import (
    build_history_coverage,
    fetch_history,
)
from ticker_news_signals.http import HttpClient
from ticker_news_signals.providers.base import ProviderContext
from ticker_news_signals.providers.wayback import CDX_URL, WaybackProvider
from ticker_news_signals.resolve import resolve_ticker

ASSET = resolve_ticker("ETH")


def _dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>CoinPulse</title><link>https://coinpulse.example</link>
<item><title>Ethereum ETF approval fuels rally</title><link>https://coinpulse.example/eth</link>
<description>ETH climbs on ETF news</description><pubDate>Fri, 02 Jan 2026 14:20:00 GMT</pubDate></item>
<item><title>Bitcoin slides on macro jitters</title><link>https://coinpulse.example/btc</link>
<description>BTC slips</description><pubDate>Fri, 02 Jan 2026 13:00:00 GMT</pubDate></item>
</channel></rss>
"""

_CDX_RESPONSE = [
    ["timestamp", "statuscode"],
    ["20260101004050", "200"],
    ["20260102004050", "200"],
    ["20260103004050", "200"],
]


def _fast_config(**overrides) -> Config:
    return Config(cache_enabled=True, cache_ttl=300, rate_limit_rpm=1000, **overrides)


def _ctx(config: Config, http: HttpClient) -> ProviderContext:
    return ProviderContext(config=config, http=http, cache=MemoryCache())


class TestWaybackProvider:
    def test_available_requires_feeds(self) -> None:
        ok, reason = WaybackProvider.available(Config(rss_feeds=()))
        assert ok is False
        assert reason is not None and "TNS_RSS_FEEDS" in reason

    def test_supports_history_capability(self) -> None:
        assert Capability.HISTORY in WaybackProvider.supports
        assert Capability.NEWS not in WaybackProvider.supports

    @pytest.mark.respx()
    def test_fetch_history_reconstructs_from_archived_feed(self, respx_mock) -> None:
        """CDX lists captures; the nearest one per day is fetched and parsed."""
        respx_mock.get(CDX_URL).mock(return_value=httpx.Response(200, json=_CDX_RESPONSE))
        for ts in ("20260101004050", "20260102004050", "20260103004050"):
            url = f"https://web.archive.org/web/{ts}id_/https://coinpulse.example/rss"
            respx_mock.get(url).mock(return_value=httpx.Response(200, content=_RSS.encode("utf-8")))

        config = _fast_config(rss_feeds=(FeedSpec("coinpulse", "https://coinpulse.example/rss"),))
        http = HttpClient(config=config)
        provider = WaybackProvider(_ctx(config, http))
        stale_articles = provider.fetch_history(
            ASSET,
            start="2026-01-01T00:00:00Z",
            end="2026-01-03T00:00:00Z",
            granularity="day",
        )
        articles = [a for a in stale_articles if a.title.startswith("Ethereum")]
        assert articles  # the ticker-relevant headline survived
        assert all(a.source == "wayback:coinpulse" for a in stale_articles)
        # provenance recorded for each captured snapshot
        assert any(k.startswith("wayback:coinpulse:") for k in provider.ctx.provenance)
        http.close()

    @pytest.mark.respx()
    def test_ticker_filter_drops_irrelevant_headlines(self, respx_mock) -> None:
        respx_mock.get(CDX_URL).mock(return_value=httpx.Response(200, json=_CDX_RESPONSE))
        for ts in ("20260101004050", "20260102004050", "20260103004050"):
            url = f"https://web.archive.org/web/{ts}id_/https://coinpulse.example/rss"
            respx_mock.get(url).mock(return_value=httpx.Response(200, content=_RSS.encode("utf-8")))

        config = _fast_config(rss_feeds=(FeedSpec("coinpulse", "https://coinpulse.example/rss"),))
        http = HttpClient(config=config)
        provider = WaybackProvider(_ctx(config, http))
        articles = provider.fetch_history(
            ASSET,
            start="2026-01-01T00:00:00Z",
            end="2026-01-03T00:00:00Z",
            granularity="day",
        )
        assert all("ethereum" in (a.title or a.description).lower() for a in articles)
        http.close()

    @pytest.mark.respx()
    def test_partial_failure_is_recorded_not_fatal(self, respx_mock) -> None:
        respx_mock.get(CDX_URL).mock(return_value=httpx.Response(500))
        config = _fast_config(
            max_retries=0, rss_feeds=(FeedSpec("dead", "https://dead.example/rss"),)
        )
        http = HttpClient(config=config)
        provider = WaybackProvider(_ctx(config, http))
        assert (
            provider.fetch_history(ASSET, start="2026-01-01", end="2026-01-03", granularity="day")
            == ()
        )
        assert len(provider.ctx.errors) == 1
        assert provider.ctx.errors[0].source == "rss:dead"
        http.close()


class TestHistoryCoverage:
    def test_complete_window(self) -> None:
        cov = build_history_coverage(
            source="wayback:a",
            published=[_dt("2026-01-01"), _dt("2026-01-02"), _dt("2026-01-03")],
            start=_dt("2026-01-01"),
            end=_dt("2026-01-03"),
            granularity="day",
        )
        assert cov.complete is True
        assert cov.missing == 0
        assert cov.buckets == 3

    def test_gap_reports_missing(self) -> None:
        cov = build_history_coverage(
            source="wayback:a",
            published=[_dt("2026-01-01"), _dt("2026-01-03")],  # 01-02 missing
            start=_dt("2026-01-01"),
            end=_dt("2026-01-03"),
            granularity="day",
        )
        assert cov.complete is False
        assert cov.missing == 1
        gaps = cov.to_dict()["gaps"]
        assert len(gaps) == 1
        assert gaps[0]["missing"] == "1"


class TestHistoryRunner:
    @pytest.mark.respx()
    def test_fetch_history_builds_vectors_and_series(self, respx_mock) -> None:
        """End-to-end: CDX -> snapshots -> dedupe -> per-bucket vectors + series."""
        respx_mock.get(CDX_URL).mock(return_value=httpx.Response(200, json=_CDX_RESPONSE))
        for ts in ("20260101004050", "20260102004050", "20260103004050"):
            url = f"https://web.archive.org/web/{ts}id_/https://coinpulse.example/rss"
            respx_mock.get(url).mock(return_value=httpx.Response(200, content=_RSS.encode("utf-8")))

        config = Config(
            cache_enabled=False,
            cache_ttl=300,
            rate_limit_rpm=1000,
            rss_feeds=(FeedSpec("coinpulse", "https://coinpulse.example/rss"),),
        )
        result = fetch_history(
            "ETH",
            granularity="day",
            start="2026-01-01",
            end="2026-01-03",
            config=config,
        )
        assert result.ticker == "ETH"
        assert result.vectors  # at least one bucket had the ETH headline
        assert all(v.article_count >= 1 for v in result.vectors)
        assert any(s.source == "wayback:coinpulse" for s in result.series)
        covered = [s for s in result.series if s.coverage]
        assert covered
        assert result.data_quality.sources_used  # provenance lists wayback feeds
        # canonical schema preserved
        assert set(result.vectors[0].to_dict()) == {
            "ticker",
            "timestamp",
            "sentiment_score",
            "article_count",
            "novelty_flag",
        }
