"""Offline tests for render (reports / metrics) and the sentiment registry."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ticker_news_signals.models import DataQuality, SignalResult, SignalVector
from ticker_news_signals.render import (
    flat_metrics,
    quality_block,
    render_markdown,
    render_report,
)
from ticker_news_signals.sentiment import (
    available_backends,
    register_backend,
    score_batch,
    score_headline,
    score_lexicon,
)


def _result() -> SignalResult:
    quality = DataQuality(
        sources_used=("alpha", "beta"),
        duplicates_removed=1,
        articles_fetched=3,
        skipped=(
            __import__("ticker_news_signals.models", fromlist=["SkippedSource"]).SkippedSource(
                "cmc", "no key"
            ),
        ),
    )
    return SignalResult(
        ticker="ETH_USD",
        vectors=[
            SignalVector(
                ticker="ETH_USD",
                timestamp="2026-10-09T14:00:00Z",
                sentiment_score=0.42,
                article_count=2,
                novelty_flag=True,
                source_count=2,
                sentiment_std=0.11,
                scorer_std=0.03,
                sources=("alpha", "beta"),
                top_headlines=("Ethereum rallies", "ETF approval"),
            )
        ],
        data_quality=quality,
        generated_at=datetime(2026, 10, 9, 14, 30, tzinfo=UTC),
        query="ETH/USD",
        resolved_symbol="ETH",
        resolved_name="Ethereum",
    )


class TestFlatMetrics:
    def test_shape_and_scalars(self) -> None:
        metrics = flat_metrics(_result())
        assert metrics["ticker"] == "ETH_USD"
        assert metrics["resolved_name"] == "Ethereum"
        assert metrics["hours"] == 1
        assert metrics["articles"] == 2
        assert metrics["mean_sentiment"] == 0.42
        assert metrics["novel_hours"] == 1
        assert metrics["source_count"] == 2
        assert metrics["duplicates_removed"] == 1
        assert metrics["skipped_count"] == 1
        assert metrics["mean_scorer_std"] == 0.03
        assert metrics["max_scorer_std"] == 0.03

    def test_scorer_std_null_when_absent(self) -> None:
        plain = SignalResult(
            ticker="X",
            vectors=[
                SignalVector(
                    ticker="X",
                    timestamp="2026-10-09T14:00:00Z",
                    sentiment_score=0.1,
                    article_count=1,
                )
            ],
            data_quality=DataQuality(),
            generated_at=datetime.now(UTC),
        )
        metrics = flat_metrics(plain)
        assert metrics["mean_scorer_std"] is None
        assert metrics["max_scorer_std"] is None

    def test_empty_result_has_null_mean(self) -> None:
        empty = SignalResult(
            ticker="X", vectors=[], data_quality=DataQuality(), generated_at=datetime.now(UTC)
        )
        metrics = flat_metrics(empty)
        assert metrics["hours"] == 0
        assert metrics["mean_sentiment"] is None

    def test_json_serialisable(self) -> None:
        json.dumps(flat_metrics(_result()))


class TestReports:
    def test_report_mentions_sources_and_quality(self) -> None:
        report = render_report(_result())
        assert "ETH_USD" in report
        assert "Ethereum" in report
        assert "alpha, beta" in report
        assert "skipped" in report.lower()

    def test_markdown_is_fenced(self) -> None:
        markdown = render_markdown(_result())
        assert markdown.startswith("# ETH_USD")
        assert "| hour (UTC) |" in markdown
        assert "alpha, beta" in markdown

    def test_quality_block_renders_skips(self) -> None:
        block = quality_block(_result())
        assert "cmc" in block
        assert "no key" in block


class TestSentimentRegistry:
    def test_default_is_vader(self) -> None:
        assert "vader" in available_backends()
        assert score_headline("This is terrible awful news.") < -0.3

    def test_lexicon_backend(self) -> None:
        assert score_lexicon("Ethereum hacked in a record exploit") < 0
        assert score_lexicon("Bitcoin ETF approval sparks rally") > 0
        assert score_lexicon("a plain neutral statement") == 0.0

    def test_backend_arg_selects(self) -> None:
        assert score_headline("rally", backend="lexicon") > 0
        assert score_batch(["rally", "hack"], backend="lexicon")[1] < 0

    def test_register_backend_extension(self) -> None:
        register_backend("constant", lambda text: 0.5)
        assert score_headline("anything", backend="constant") == 0.5
        assert "constant" in available_backends()
