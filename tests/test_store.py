"""QueryCache: save/load whole signal & history results, and model round-trips.

The round-trip asserts are the real safety net for the new ``from_dict``
logic: every field (including datetimes, nested articles, coverage objects,
provenance dicts, every quality record) must survive
``to_dict -> from_dict -> to_dict`` unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ticker_news_signals import (
    Cache,
    DataQuality,
    HistoryCoverage,
    HistoryResult,
    HistorySeries,
    MissingField,
    NewsArticle,
    Provenance,
    QueryCache,
    RateLimitInfo,
    SignalResult,
    SignalVector,
    SkippedSource,
    SourceErrorRecord,
)

DT = datetime(2024, 1, 15, 12, 30, 0, tzinfo=UTC)


def _article(title: str, url: str, source: str = "rss:coindesk") -> NewsArticle:
    return NewsArticle(
        title=title,
        description=f"about {title}",
        published_date="Mon, 15 Jan 2024 12:00:00 GMT",
        url=url,
        publisher="CoinDesk",
        source=source,
    )


def _vector(ticker: str, ts: str, score: float, n: int) -> SignalVector:
    return SignalVector(
        ticker=ticker,
        timestamp=ts,
        sentiment_score=score,
        article_count=n,
        novelty_flag=n > 0,
        source_count=2,
        sentiment_std=0.05,
        scorer_std=0.1,
        sources=("rss:coindesk", "gnews"),
        top_headlines=("headline",),
    )


def _quality() -> DataQuality:
    return DataQuality(
        provenance={
            "rss:coindesk:news": Provenance(
                source="rss:coindesk",
                fetched_at=DT,
                url="https://example.com/rss",
                cached=False,
                article_count=3,
                notes=("ok",),
            )
        },
        errors=(
            SourceErrorRecord(
                source="rss:busted",
                capability="NEWS",
                exception_type="RuntimeError",
                message="feed exploded",
            ),
        ),
        rate_limited=(
            RateLimitInfo(
                source="gnews",
                capability="NEWS",
                message="429",
                retry_after=30.0,
                limit=50,
                remaining=0,
                remedy="wait",
                suppressed=False,
            ),
        ),
        skipped=(SkippedSource(source="cryptopanic", reason="no key"),),
        missing=(MissingField(field="scorer", reason="ml extra not installed"),),
        sources_used=("rss:coindesk", "gnews"),
        duplicates_removed=2,
        articles_fetched=5,
    )


def _signal_result() -> SignalResult:
    return SignalResult(
        ticker="BTC",
        vectors=[
            _vector("BTC", "2024-01-15T12:00:00Z", 0.25, 2),
            _vector("BTC", "2024-01-15T13:00:00Z", -0.1, 1),
        ],
        data_quality=_quality(),
        generated_at=DT,
        query="BTC",
        resolved_symbol="BTC",
        resolved_name="Bitcoin",
        articles=(
            _article("Bitcoin tops 41k", "https://example.com/a/1"),
            _article("Ethereum trails", "https://example.com/a/2"),
        ),
    )


def _history_result() -> HistoryResult:
    series = HistorySeries(
        source="wayback:coindesk",
        ticker="ETH",
        granularity="day",
        articles=(
            _article("Ethereum roadmap", "https://example.com/e/1"),
            _article("ETH 2.0", "https://example.com/e/2"),
        ),
        coverage=HistoryCoverage(
            source="wayback:coindesk",
            requested_start=DT,
            requested_end=DT + timedelta(days=1),
            first=DT,
            last=DT + timedelta(days=1),
            buckets=2,
            expected=2,
            missing=0,
            truncated=False,
            complete=True,
            gaps=(),
            note="complete",
        ),
        provenance=Provenance(
            source="wayback:coindesk",
            fetched_at=DT,
            url="https://web.archive.org/web/20240115123000id_/x",
            cached=False,
            article_count=2,
        ),
    )
    return HistoryResult(
        ticker="ETH",
        query="ETH",
        granularity="day",
        start=DT,
        end=DT + timedelta(days=1),
        vectors=(
            _vector("ETH", "2024-01-15T00:00:00Z", 0.5, 1),
            _vector("ETH", "2024-01-16T00:00:00Z", -0.05, 1),
        ),
        series=(series,),
        data_quality=_quality(),
        generated_at=DT,
        resolved_symbol="ETH",
        resolved_name="Ethereum",
    )


# --------------------------------------------------------------------------- model round-trips


def test_signal_roundtrip() -> None:
    original = _signal_result()
    rebuilt = SignalResult.from_dict(original.to_dict())
    assert rebuilt == original
    assert rebuilt.to_dict() == original.to_dict()
    assert isinstance(rebuilt.vectors[0], SignalVector)
    assert rebuilt.articles[0].title == original.articles[0].title


def test_signal_json_roundtrip() -> None:
    original = _signal_result()
    rebuilt = SignalResult.from_json(original.to_json())
    assert rebuilt == original
    # datetime + nested quality survived the string path
    assert rebuilt.generated_at == DT
    assert rebuilt.data_quality.rate_limited[0].retry_after == 30.0


def test_history_roundtrip() -> None:
    original = _history_result()
    rebuilt = HistoryResult.from_dict(original.to_dict())
    assert rebuilt == original
    assert rebuilt.to_dict() == original.to_dict()
    cov = rebuilt.series[0].coverage
    assert cov is not None and cov.complete is True


def test_history_json_roundtrip() -> None:
    original = _history_result()
    rebuilt = HistoryResult.from_json(original.to_json())
    assert rebuilt == original
    # datetimes survived the string path
    assert rebuilt.start == DT


def test_vector_roundtrip_extended_and_canonical() -> None:
    v = _vector("BTC", "2024-01-15T12:00:00Z", 0.25, 2)
    rebuilt_ext = SignalVector.from_dict(v.to_record())
    assert rebuilt_ext == v
    rebuilt_canon = SignalVector.from_json(v.to_json())
    assert rebuilt_canon == v


# --------------------------------------------------------------------------- QueryCache


def test_query_cache_save_load_signal(tmp_path) -> None:
    original = _signal_result()
    qc = QueryCache(tmp_path / "queries.sqlite3")
    try:
        qc.save_signal("BTC", original)
        loaded = qc.load_signal("BTC")
        assert loaded is not None
        assert loaded == original
        assert loaded.to_dict() == original.to_dict()
        # case-insensitive ticker, immortal default TTL
        assert qc.load_signal("btc") == loaded
        assert qc.load_signal("ETH") is None
    finally:
        qc.close()


def test_query_cache_save_signal_accepts_dict(tmp_path) -> None:
    qc = QueryCache(tmp_path / "queries.sqlite3")
    try:
        data = _signal_result().to_dict()
        data["ticker"] = "SOL"
        data["resolved"]["symbol"] = "SOL"
        qc.save_signal("SOL", data)
        loaded = qc.load_signal("SOL")
        assert loaded is not None and loaded.ticker == "SOL"
        # the stored signal keeps the payload's identity, not the key's
        assert loaded.to_dict()["ticker"] == "SOL"
    finally:
        qc.close()


def test_query_cache_save_load_history(tmp_path) -> None:
    qc = QueryCache(tmp_path / "queries.sqlite3")
    try:
        hist = _history_result()
        qc.save_history("ETH", "day", start="2024-01-15", history=hist)
        loaded = qc.load_history("ETH", "day", start="2024-01-15")
        assert loaded is not None and loaded == hist
        # window is part of the key
        assert qc.load_history("ETH", "day", start="2023-01-15") is None
        # dict form is accepted
        qc.save_history("ETH", "day", end="2024-02-01", history=hist.to_dict())
        assert qc.load_history("ETH", "day", end="2024-02-01") == hist
    finally:
        qc.close()


def test_save_history_requires_result(tmp_path) -> None:
    qc = QueryCache(tmp_path / "q.sqlite3")
    try:
        with pytest.raises(ValueError):
            qc.save_history("BTC", "day")
    finally:
        qc.close()


def test_query_cache_is_a_cache_protocol(tmp_path) -> None:
    qc = QueryCache(tmp_path / "q.sqlite3")
    try:
        assert isinstance(qc, Cache)
        qc.set("k", {"a": 1}, 60)
        assert qc.get("k") == {"a": 1}
        assert len(qc) == 1
        qc.clear()
        assert len(qc) == 0
    finally:
        qc.close()


def test_context_manager(tmp_path) -> None:
    with QueryCache(tmp_path / "q.sqlite3") as qc:
        qc.save_signal("BTC", _signal_result())
        assert qc.load_signal("BTC") is not None


def test_query_cache_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "queries.sqlite3"
    qc = QueryCache(path)
    qc.save_signal("BTC", _signal_result())
    qc.close()
    qc2 = QueryCache(path)
    try:
        assert qc2.load_signal("BTC") is not None
        assert isinstance(qc2.stats()["entries"], int)
    finally:
        qc2.close()
