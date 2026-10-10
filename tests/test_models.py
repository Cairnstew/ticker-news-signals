"""Offline tests for the typed models."""

from __future__ import annotations

import json

from ticker_news_signals.models import NewsArticle, SignalVector


class TestSignalVector:
    def test_round_trip_to_dict_from_dict(self) -> None:
        vector = SignalVector(
            ticker="ETH_USD",
            timestamp="2026-09-27T14:00:00Z",
            sentiment_score=0.42,
            article_count=7,
            novelty_flag=False,
        )
        data = vector.to_dict()
        assert SignalVector.from_dict(data) == vector

    def test_to_dict_is_json_safe(self) -> None:
        vector = SignalVector(
            ticker="ETH_USD",
            timestamp="2026-09-27T14:00:00Z",
            sentiment_score=-0.1234,
            article_count=3,
            novelty_flag=True,
        )
        # Must round-trip through json.dumps with no coercion helper.
        text = json.dumps(vector.to_dict())
        assert '"ticker": "ETH_USD"' in text or '"ticker":"ETH_USD"' in text
        assert "2026-09-27T14:00:00Z" in text

    def test_shape(self) -> None:
        data = SignalVector(
            ticker="BTC_USD",
            timestamp="2026-09-27T13:00:00Z",
            sentiment_score=0.0,
            article_count=1,
            novelty_flag=False,
        ).to_dict()
        assert sorted(data) == [
            "article_count",
            "novelty_flag",
            "sentiment_score",
            "ticker",
            "timestamp",
        ]

    def test_from_dict_coerces_json_scalars(self) -> None:
        vector = SignalVector.from_dict(
            {
                "ticker": "ETH_USD",
                "timestamp": "2026-09-27T14:00:00Z",
                "sentiment_score": "0.42",
                "article_count": "7",
                "novelty_flag": 1,
            }
        )
        assert vector.sentiment_score == 0.42
        assert vector.article_count == 7
        assert vector.novelty_flag is True


class TestNewsArticle:
    def test_from_gnews_string_publisher(self) -> None:
        article = NewsArticle.from_gnews(
            {
                "title": "Ethereum rallies",
                "description": "Ether climbs on ETF news",
                "published date": "Sun, 27 Sep 2026 14:05:00 GMT",
                "url": "https://example.com/1",
                "publisher": "CoinDesk",
            }
        )
        assert article.title == "Ethereum rallies"
        assert article.publisher == "CoinDesk"
        assert article.url == "https://example.com/1"
        assert article.published_dt is not None
        assert article.published_dt.tzinfo is not None

    def test_from_gnews_dict_publisher(self) -> None:
        article = NewsArticle.from_gnews(
            {
                "title": "Ethereum rallies",
                "published date": "Sun, 27 Sep 2026 14:05:00 GMT",
                "url": "https://example.com/1",
                "publisher": {"title": "CoinDesk", "href": "https://coindesk.com"},
            }
        )
        assert article.publisher == "CoinDesk"

    def test_unparsable_published_date(self) -> None:
        article = NewsArticle.from_gnews(
            {"title": "T", "published date": "2 hours ago", "url": "https://example.com/2"}
        )
        assert article.published_dt is None
        # Falls back to the raw string in the JSON output, never a datetime.
        data = article.to_dict()
        assert data["published_date"] == "2 hours ago"
        json.dumps(data)

    def test_to_dict_is_json_safe(self) -> None:
        article = NewsArticle.from_gnews(
            {
                "title": "Ethereum rallies",
                "description": "Ether climbs on ETF news",
                "url": "https://example.com/1",
            }
        )
        json.dumps(article.to_dict())
