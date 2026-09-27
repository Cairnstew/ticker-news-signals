"""Offline tests for the aggregation pipeline.

The GNews backend is mocked with ``unittest.mock.patch`` so no test ever
touches the network.  Articles are fabricated with fixed RFC 2822 publish
times so bucketing, means, counts, and novelty are fully deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from ticker_news_signals.client import NewsClient
from ticker_news_signals import pipeline
from ticker_news_signals.sentiment import score_headline

NOW = datetime(2026, 9, 27, 14, 30, tzinfo=timezone.utc)

_ENTRY_A = {
    "title": "This is amazing news for Ethereum",
    "description": "ETH climbs on ETF inflows",
    "published date": "Sun, 27 Sep 2026 14:20:00 GMT",
    "url": "https://example.com/a",
    "publisher": "CoinDesk",
}
_ENTRY_B = {
    "title": "This is terrible awful horrendous news for Ethereum",
    "published date": "Sun, 27 Sep 2026 14:05:00 GMT",
    "url": "https://example.com/b",
    "publisher": "The Block",
}
_ENTRY_C = {
    "title": "Ethereum continues to change the world for the better",
    "published date": "Sun, 27 Sep 2026 13:15:00 GMT",
    "url": "https://example.com/c",
    "publisher": "Decrypt",
}


def _client_with(*, search=None, topic=None):
    """A NewsClient backed by a mocked GNews that returns pre-built entries."""
    mock_news = MagicMock()
    mock_news.get_news.return_value = search if search is not None else []
    mock_news.get_news_by_topic.return_value = topic if topic is not None else []
    with patch("ticker_news_signals.client.GNews", return_value=mock_news):
        return NewsClient()


class TestFetchSignals:
    def test_groups_by_hour_with_mean_sentiment_and_novelty(self) -> None:
        client = _client_with(search=[_ENTRY_A], topic=[_ENTRY_B, _ENTRY_C])
        signals = pipeline.fetch_signals("ETH/USD", lookback_hours=3, client=client, now=NOW)

        assert [s.ticker for s in signals] == ["ETH_USD", "ETH_USD"]
        assert [s.timestamp for s in signals] == [
            "2026-09-27T13:00:00Z",
            "2026-09-27T14:00:00Z",
        ]
        assert [s.article_count for s in signals] == [1, 2]

        # The 13:00 bucket has a single positive article.
        assert signals[0].sentiment_score == round(score_headline(_ENTRY_C["title"]), 4)
        assert signals[0].novelty_flag is False

        # The 14:00 bucket averages both, and 14:20 is within the 15-min
        # novelty window of "now" (14:30).
        expected_mean = round(
            (score_headline(_ENTRY_A["title"]) + score_headline(_ENTRY_B["title"])) / 2, 4
        )
        assert signals[1].sentiment_score == expected_mean
        assert signals[1].novelty_flag is True

    def test_normalizes_ticker_in_vectors(self) -> None:
        client = _client_with(topic=[_ENTRY_A])
        signals = pipeline.fetch_signals("ETH/USD", lookback_hours=1, client=client, now=NOW)
        assert signals and signals[0].ticker == "ETH_USD"

    def test_no_articles_returns_empty(self) -> None:
        client = _client_with(search=[], topic=[])
        signals = pipeline.fetch_signals("ETH/USD", lookback_hours=1, client=client, now=NOW)
        assert signals == []

    def test_articles_older_than_lookback_are_dropped(self) -> None:
        old = {
            "title": "Ancient Ethereum history",
            "published date": "Sun, 27 Sep 2026 05:00:00 GMT",
            "url": "https://example.com/old",
        }
        client = _client_with(topic=[old])
        signals = pipeline.fetch_signals("ETH/USD", lookback_hours=3, client=client, now=NOW)
        assert signals == []

    def test_undated_article_is_counted_at_now(self) -> None:
        client = _client_with(topic=[{"title": "Fresh but undated", "url": "https://example.com/x"}])
        signals = pipeline.fetch_signals("ETH/USD", lookback_hours=1, client=client, now=NOW)
        assert len(signals) == 1
        assert signals[0].timestamp == "2026-09-27T14:00:00Z"
        assert signals[0].novelty_flag is True  # treated as published now

    def test_output_records_are_json_safe(self) -> None:
        import json

        client = _client_with(topic=[_ENTRY_A])
        signals = pipeline.fetch_signals("ETH/USD", lookback_hours=1, client=client, now=NOW)
        for vector in signals:
            json.dumps(vector.to_dict())


class TestDedupe:
    def test_dedupes_by_url(self) -> None:
        from ticker_news_signals.models import NewsArticle

        a = NewsArticle(title="Same", url="https://example.com/same")
        b = NewsArticle(title="Same", url="https://example.com/same")
        c = NewsArticle(title="Different", url="https://example.com/diff")
        assert len(pipeline._dedupe([a, b, c])) == 2

    def test_dedupes_by_title_when_url_missing(self) -> None:
        from ticker_news_signals.models import NewsArticle

        a = NewsArticle(title="No URL here")
        b = NewsArticle(title="No URL here")
        assert len(pipeline._dedupe([a, b])) == 1


class TestClientRetry:
    def test_client_retries_transient_failure(self) -> None:
        mock_news = MagicMock()
        mock_news.get_news.side_effect = [
            RuntimeError("connection reset"),
            [_ENTRY_A],
        ]
        with (
            patch("ticker_news_signals.client.GNews", return_value=mock_news),
            patch("ticker_news_signals.client.time.sleep") as fake_sleep,
        ):
            client = NewsClient(retry_attempts=3)
            articles = client.search("ETH")

        assert len(articles) == 1
        assert fake_sleep.call_count == 1

    def test_client_gives_up_after_retries(self) -> None:
        from ticker_news_signals.errors import NewsFetchError

        mock_news = MagicMock()
        mock_news.get_news.side_effect = RuntimeError("connection reset")
        with (
            patch("ticker_news_signals.client.GNews", return_value=mock_news),
            patch("ticker_news_signals.client.time.sleep"),
        ):
            client = NewsClient(retry_attempts=3)
            try:
                client.search("ETH")
            except NewsFetchError:
                pass
            else:
                raise AssertionError("expected NewsFetchError")

    def test_client_normalizes_entries_into_articles(self) -> None:
        mock_news = MagicMock()
        mock_news.search = None  # placate type checkers; unused
        mock_news.get_news.return_value = [_ENTRY_A]
        with patch("ticker_news_signals.client.GNews", return_value=mock_news):
            client = NewsClient()
        articles = client.search("ETH")
        assert articles[0].title == _ENTRY_A["title"]
        assert articles[0].publisher == "CoinDesk"

    def test_default_topic_is_digital_currencies(self) -> None:
        mock_news = MagicMock()
        mock_news.get_news_by_topic.return_value = []
        with patch("ticker_news_signals.client.GNews", return_value=mock_news):
            client = NewsClient()
        client.search_by_topic()
        # The gnews 0.8 section display name carries a space.
        mock_news.get_news_by_topic.assert_called_once_with("DIGITAL CURRENCIES")