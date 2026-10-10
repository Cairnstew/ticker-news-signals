"""Offline tests for configuration: env parsing, feed parsing, overrides."""

from __future__ import annotations

from pathlib import Path

from ticker_news_signals.config import Config, FeedSpec, parse_feeds


class TestEnv:
    def test_defaults(self, monkeypatch) -> None:
        for var in [
            "TNS_SOURCES",
            "TNS_SENTIMENT",
            "TNS_LANGUAGE",
            "TICKER_NEWS_LANGUAGE",
            "TNS_COUNTRY",
            "TNS_PERIOD",
            "TNS_LOOKBACK_HOURS",
            "TNS_NOVELTY_MINUTES",
            "TNS_MAX_RESULTS",
            "TNS_CACHE_PATH",
            "TNS_CRYPTOPANIC_TOKEN",
            "TNS_RSS_FEEDS",
        ]:
            monkeypatch.delenv(var, raising=False)
        config = Config.from_env()
        assert config.sources == ()
        assert config.sentiment_backend == "vader"
        assert config.language == "en"
        assert config.country == "US"
        assert config.lookback_hours == 1
        assert config.novelty_minutes == 15
        assert config.max_results == 100
        assert config.cache_enabled is True
        assert config.cryptopanic_token is None
        assert len(config.rss_feeds) >= 3

    def test_sources_and_sentiment(self, monkeypatch) -> None:
        monkeypatch.setenv("TNS_SOURCES", "gnews,rss")
        monkeypatch.setenv("TNS_SENTIMENT", "lexicon")
        config = Config.from_env()
        assert config.sources == ("gnews", "rss")
        assert config.sentiment_backend == "lexicon"

    def test_legacy_env_vars_still_read(self, monkeypatch) -> None:
        monkeypatch.setenv("TICKER_NEWS_LANGUAGE", "de")
        monkeypatch.setenv("TICKER_NEWS_COUNTRY", "DE")
        config = Config.from_env()
        assert config.language == "de"
        assert config.country == "DE"

    def test_tns_shadows_legacy(self, monkeypatch) -> None:
        monkeypatch.setenv("TICKER_NEWS_LANGUAGE", "de")
        monkeypatch.setenv("TNS_LANGUAGE", "fr")
        config = Config.from_env()
        assert config.language == "fr"

    def test_cache_path_parses(self, monkeypatch) -> None:
        monkeypatch.setenv("TNS_CACHE_PATH", "/tmp/foo.sqlite3")
        config = Config.from_env()
        assert config.cache_path == Path("/tmp/foo.sqlite3")

    def test_with_overrides_only_replaces_non_none(self) -> None:
        config = Config(sentiment_backend="vader", lookback_hours=1)
        changed = config.with_overrides(sentiment_backend="lexicon", lookback_hours=None)
        assert changed.sentiment_backend == "lexicon"
        assert changed.lookback_hours == 1  # None did not clobber

    def test_from_env_accepts_constructor_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("TNS_MAX_RESULTS", "50")
        config = Config.from_env(max_results=20)
        assert config.max_results == 20


class TestFeeds:
    def test_parse_name_url_pairs(self) -> None:
        feeds = parse_feeds("a=https://a.example/rss, b = https://b.example/rss")
        assert [f.name for f in feeds] == ["a", "b"]
        assert feeds[0].url == "https://a.example/rss"

    def test_parse_bare_url_derives_host(self) -> None:
        feeds = parse_feeds("https://www.coindesk.com/feed")
        assert len(feeds) == 1
        assert feeds[0].name == "coindesk"
        assert feeds[0].url.startswith("https://")

    def test_parse_drops_empty_entries(self) -> None:
        feeds = parse_feeds(",,?bad=1,ok=https://ok.example/rss,")
        assert feeds == (FeedSpec(name="ok", url="https://ok.example/rss"),)

    def test_provenance_source_keys_off_name(self) -> None:
        feeds = parse_feeds("coindesk=https://coindesk.com/feed")
        assert feeds[0].provenance_source() == "rss:coindesk"
