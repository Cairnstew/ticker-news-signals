"""Offline tests for the concrete providers: RSS, CryptoPanic, GNews.

Network is mocked with ``respx`` (httpx); GNews uses a mocked NewsClient.
No test touches a real feed.
"""

from __future__ import annotations

import httpx
import pytest
from ticker_news_signals.cache import MemoryCache
from ticker_news_signals.capabilities import Capability
from ticker_news_signals.config import Config, FeedSpec
from ticker_news_signals.errors import RateLimitExceeded
from ticker_news_signals.http import HttpClient
from ticker_news_signals.models import NewsArticle
from ticker_news_signals.providers.base import ProviderContext
from ticker_news_signals.providers.bingnews import SEARCH_URL, BingNewsProvider
from ticker_news_signals.providers.cryptopanic import API_BASE, CryptoPanicProvider
from ticker_news_signals.providers.gnews import GNewsProvider
from ticker_news_signals.providers.rss import RssProvider
from ticker_news_signals.resolve import resolve_ticker

ASSET = resolve_ticker("ETH")

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>CoinPulse</title>
    <link>https://coinpulse.example</link>
    <item>
      <title>Ethereum ETF approval fuels rally</title>
      <link>https://coinpulse.example/eth-rally</link>
      <description>ETH climbs on ETF news</description>
      <pubDate>Fri, 09 Oct 2026 14:20:00 GMT</pubDate>
    </item>
    <item>
      <title>Bitcoin slides on macro jitters</title>
      <link>https://coinpulse.example/btc-slide</link>
      <description>BTC slips</description>
      <pubDate>Fri, 09 Oct 2026 13:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def _ctx(config: Config, http: HttpClient) -> ProviderContext:
    return ProviderContext(config=config, http=http, cache=MemoryCache())


def _fast_config(**overrides) -> Config:
    return Config(cache_enabled=True, cache_ttl=300, rate_limit_rpm=1000, **overrides)


class TestRssProvider:
    @pytest.mark.respx()
    def test_filters_to_ticker_and_parses(self, respx_mock) -> None:
        respx_mock.get("https://coinpulse.example/rss").mock(
            return_value=httpx.Response(200, content=_RSS.encode("utf-8"))
        )
        config = _fast_config(rss_feeds=(FeedSpec("coinpulse", "https://coinpulse.example/rss"),))
        http = HttpClient(config=config)
        provider = RssProvider(_ctx(config, http))

        articles = provider.fetch_news(ASSET, max_results=10)
        assert len(articles) == 1
        article = articles[0]
        assert article.title == "Ethereum ETF approval fuels rally"
        assert article.source == "rss:coinpulse"
        assert article.publisher == "CoinPulse"
        assert article.published_dt is not None
        assert article.url == "https://coinpulse.example/eth-rally"

        # provenance recorded
        assert "rss:coinpulse" in provider.ctx.provenance
        assert provider.ctx.provenance["rss:coinpulse"].cached is False
        http.close()

    @pytest.mark.respx()
    def test_trending_returns_all_entries(self, respx_mock) -> None:
        respx_mock.get("https://coinpulse.example/rss").mock(
            return_value=httpx.Response(200, content=_RSS.encode("utf-8"))
        )
        config = _fast_config(rss_feeds=(FeedSpec("coinpulse", "https://coinpulse.example/rss"),))
        http = HttpClient(config=config)
        provider = RssProvider(_ctx(config, http))
        articles = provider.fetch_trending(max_results=10)
        assert len(articles) == 2  # no ticker filter
        http.close()

    @pytest.mark.respx()
    def test_response_is_cached_second_call_is_free(self, respx_mock) -> None:
        route = respx_mock.get("https://coinpulse.example/rss").mock(
            return_value=httpx.Response(200, content=_RSS.encode("utf-8"))
        )
        config = _fast_config(rss_feeds=(FeedSpec("coinpulse", "https://coinpulse.example/rss"),))
        http = HttpClient(config=config)
        provider = RssProvider(_ctx(config, http))
        first = provider.fetch_news(ASSET, max_results=10)
        second = provider.fetch_news(ASSET, max_results=10)
        assert len(second) == len(first) == 1
        assert route.call_count == 1  # second read came from the TTL cache
        assert provider.ctx.provenance["rss:coinpulse"].cached is True
        http.close()

    @pytest.mark.respx()
    def test_dead_feed_is_recorded_not_fatal(self, respx_mock) -> None:
        respx_mock.get("https://dead.example/rss").mock(return_value=httpx.Response(500))
        config = _fast_config(rss_feeds=(FeedSpec("dead", "https://dead.example/rss"),))
        http = HttpClient(config=config)
        provider = RssProvider(_ctx(config, http))
        assert provider.fetch_news(ASSET, max_results=10) == ()
        assert len(provider.ctx.errors) == 1
        assert provider.ctx.errors[0].source == "rss:dead"
        assert any(m.field == "rss.dead" for m in provider.ctx.missing)
        http.close()

    def test_available_without_feeds(self) -> None:
        ok, reason = RssProvider.available(Config(rss_feeds=()))
        assert ok is False
        assert reason is not None and "TNS_RSS_FEEDS" in reason

    @pytest.mark.respx()
    def test_one_throttled_feed_does_not_suppress_the_others(self, respx_mock) -> None:
        """Per-feed breakers: a 429 on one feed must not short-circuit the rest."""
        respx_mock.get("https://throttled.example/rss").mock(return_value=httpx.Response(429))
        healthy = respx_mock.get("https://healthy.example/rss").mock(
            return_value=httpx.Response(200, content=_RSS.encode("utf-8"))
        )
        config = _fast_config(
            max_retries=0,
            rss_feeds=(
                FeedSpec("throttled", "https://throttled.example/rss"),
                FeedSpec("healthy", "https://healthy.example/rss"),
            ),
        )
        http = HttpClient(config=config)
        provider = RssProvider(_ctx(config, http))
        articles = provider.fetch_news(ASSET, max_results=10)

        # The healthy feed was still fetched (not suppressed by the other's breaker)…
        assert healthy.call_count == 1
        assert any(a.source == "rss:healthy" for a in articles)
        # …and only the throttled feed is recorded as the failure.
        assert {e.source for e in provider.ctx.errors} == {"rss:throttled"}
        assert any(rl.source == "rss:throttled" for rl in provider.ctx.rate_limited)
        assert not any(e.source == "rss:healthy" for e in provider.ctx.errors)
        http.close()

    @pytest.mark.respx()
    def test_trending_feed_error_is_labelled_trending(self, respx_mock) -> None:
        respx_mock.get("https://dead.example/rss").mock(return_value=httpx.Response(500))
        config = _fast_config(
            max_retries=0, rss_feeds=(FeedSpec("dead", "https://dead.example/rss"),)
        )
        http = HttpClient(config=config)
        provider = RssProvider(_ctx(config, http))
        provider.fetch_trending(max_results=10)
        assert provider.ctx.errors[0].capability == "trending"
        http.close()


_BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>ethereum - BingNews</title>
    <link>https://www.bing.com/news/search?q=ethereum&amp;format=rss</link>
    <item>
      <title>Ethereum ETF flows turn positive</title>
      <link>https://bing.example/eth-etf</link>
      <description>ETH sees record inflows as investors rotate back in</description>
      <pubDate>Fri, 09 Oct 2026 14:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Global markets digest rate decision</title>
      <link>https://bing.example/markets</link>
      <description>Stocks mixed, bonds steady ahead of the next FOMC</description>
      <pubDate>Fri, 09 Oct 2026 13:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


class TestBingNewsProvider:
    def test_available_keyless(self) -> None:
        ok, reason = BingNewsProvider.available(Config())
        assert ok is True
        assert reason is None

    def test_supports_news_and_trending(self) -> None:
        assert Capability.NEWS in BingNewsProvider.supports
        assert Capability.TRENDING in BingNewsProvider.supports

    @pytest.mark.respx()
    def test_fetch_news_filters_to_ticker_and_parses(self, respx_mock) -> None:
        respx_mock.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, content=_BING_RSS.encode())
        )
        config = _fast_config()
        http = HttpClient(config=config)
        provider = BingNewsProvider(_ctx(config, http))
        articles = provider.fetch_news(ASSET, max_results=10)
        assert len(articles) == 1
        article = articles[0]
        assert article.title == "Ethereum ETF flows turn positive"
        assert article.source == "bingnews"
        assert article.published_dt is not None
        assert provider.ctx.provenance["bingnews:news"].cached is False
        http.close()

    @pytest.mark.respx()
    def test_fetch_trending_uses_default_query_and_returns_all(self, respx_mock) -> None:
        respx_mock.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, content=_BING_RSS.encode())
        )
        config = _fast_config()
        http = HttpClient(config=config)
        provider = BingNewsProvider(_ctx(config, http))
        articles = provider.fetch_trending(max_results=10)
        assert len(articles) == 2  # trending has no ticker filter
        http.close()

    @pytest.mark.respx()
    def test_response_is_cached_second_call_is_free(self, respx_mock) -> None:
        route = respx_mock.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, content=_BING_RSS.encode())
        )
        config = _fast_config()
        http = HttpClient(config=config)
        provider = BingNewsProvider(_ctx(config, http))
        first = provider.fetch_news(ASSET, max_results=10)
        second = provider.fetch_news(ASSET, max_results=10)
        assert len(second) == len(first) == 1
        assert route.call_count == 1
        assert provider.ctx.provenance["bingnews:news"].cached is True
        http.close()

    @pytest.mark.respx()
    def test_dead_source_raises_for_direct_call(self, respx_mock) -> None:
        # A failing query raises out of the provider; the *orchestrator* is what
        # records it as a tolerated error (see test_pipeline partial-failure tests).
        respx_mock.get(SEARCH_URL).mock(return_value=httpx.Response(500))
        config = _fast_config(max_retries=0)
        http = HttpClient(config=config)
        provider = BingNewsProvider(_ctx(config, http))
        with pytest.raises(httpx.HTTPStatusError):
            provider.fetch_news(ASSET, max_results=10)
        http.close()

    @pytest.mark.respx()
    def test_429_propagates_as_rate_limit(self, respx_mock) -> None:
        respx_mock.get(SEARCH_URL).mock(return_value=httpx.Response(429))
        config = _fast_config(max_retries=0)
        http = HttpClient(config=config)
        provider = BingNewsProvider(_ctx(config, http))
        with pytest.raises(RateLimitExceeded):
            provider.fetch_news(ASSET, max_results=10)
        http.close()


class TestCryptoPanicProvider:
    def test_requires_token(self) -> None:
        ok, reason = CryptoPanicProvider.available(Config(cryptopanic_token=None))
        assert ok is False
        assert reason is not None and "TNS_CRYPTOPANIC_TOKEN" in reason

    @pytest.mark.respx()
    def test_fetch_news_builds_articles(self, respx_mock) -> None:
        payload = {
            "results": [
                {
                    "title": "Ethereum killer app launches",
                    "published_at": "2026-10-09T14:20:00Z",
                    "url": "https://cryptopanic.example/1",
                    "source": {"title": "CoinStar"},
                }
            ]
        }
        respx_mock.get(API_BASE).mock(return_value=httpx.Response(200, json=payload))
        config = _fast_config(cryptopanic_token="test-token")
        http = HttpClient(config=config)
        provider = CryptoPanicProvider(_ctx(config, http))
        articles = provider.fetch_news(ASSET, max_results=10)
        assert len(articles) == 1
        assert articles[0].source == "cryptopanic"
        assert articles[0].publisher == "CoinStar"
        assert articles[0].published_dt is not None
        http.close()

    @pytest.mark.respx()
    def test_429_propagates_as_rate_limit(self, respx_mock) -> None:
        respx_mock.get(API_BASE).mock(return_value=httpx.Response(429))
        config = _fast_config(cryptopanic_token="test-token", max_retries=0)
        http = HttpClient(config=config)
        provider = CryptoPanicProvider(_ctx(config, http))
        with pytest.raises(RateLimitExceeded):
            provider.fetch_news(ASSET, max_results=10)
        http.close()


class TestGNewsProvider:
    def test_fetch_news_uses_search_and_records(self, monkeypatch) -> None:
        class FakeClient:
            def __init__(self, **kwargs):
                self.calls: list[str] = []

            def search(self, query, max_results=100):
                self.calls.append(f"search:{query}")
                return [NewsArticle(title="Ethereum rally", source="gnews", url="https://g/1")]

            def search_by_topic(self, topic, max_results=100):
                self.calls.append(f"topic:{topic}")
                return []

        config = _fast_config()
        http = HttpClient(config=config)
        provider = GNewsProvider(_ctx(config, http), client=FakeClient())  # type: ignore[arg-type]
        articles = provider.fetch_news(ASSET, max_results=10)
        assert len(articles) == 1
        assert articles[0].source == "gnews"
        assert provider.ctx.provenance["gnews:news"].article_count == 1
        http.close()

    def test_supports_news_and_trending(self) -> None:
        assert Capability.NEWS in GNewsProvider.supports
        assert Capability.TRENDING in GNewsProvider.supports


class TestRegistry:
    def test_bingnews_is_registered_by_default(self) -> None:
        from ticker_news_signals.providers import registry, select

        names = {cls.name for cls in registry()}
        assert "bingnews" in names
        # select("") means "every registered provider" — bingnews is on by default.
        assert BingNewsProvider in select(())

    def test_select_by_name(self) -> None:
        from ticker_news_signals.providers import select

        assert select(("bingnews",)) == (BingNewsProvider,)


class TestDefaultFeeds:
    def test_default_feed_set_is_broad_and_unique(self) -> None:
        from ticker_news_signals.config import DEFAULT_RSS_FEEDS

        names = [f.name for f in DEFAULT_RSS_FEEDS]
        urls = [f.url for f in DEFAULT_RSS_FEEDS]
        assert len(names) == len(set(names))  # no duplicate feed names
        assert len(urls) == len(set(urls))  # no duplicate URLs
        # The original six plus the live-verified Tier-1 additions.
        assert len(DEFAULT_RSS_FEEDS) >= 12
        for added in ("beincrypto", "newsbtc", "bitcoinist", "utoday", "forkast"):
            assert added in names
        for feed in DEFAULT_RSS_FEEDS:
            assert feed.url.startswith("https://")
