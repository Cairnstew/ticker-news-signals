"""Offline tests for the multi-source orchestrator.

Fake providers hand fixed headlines to :func:`fetch_result` so assertions
about cross-source dedupe, coverage counts, partial-failure tolerance and the
quality block are fully deterministic. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.fakes import (
    BadRpmProvider,
    BrokenProvider,
    ExplodingInitProvider,
    UnavailableProvider,
    article,
    provider_with,
)
from ticker_news_signals.config import Config
from ticker_news_signals.pipeline import fetch_result, fetch_signals

NOW = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)

_RALLY = article(
    "Ethereum ETF approval sparks a massive rally",
    "Fri, 09 Oct 2026 14:20:00 GMT",
    "https://ex.com/rally",
    source="alpha",
)
_HACK = article(
    "Ethereum hack drains cross-chain bridge",
    "Fri, 09 Oct 2026 13:10:00 GMT",
    "https://ex.com/hack",
    source="alpha",
)
_NEW = article(
    "Ethereum launches mainnet upgrade",
    "Fri, 09 Oct 2026 14:05:00 GMT",
    "https://ex.com/upgrade",
    source="cryptowire",
)


def _config(**overrides) -> Config:
    return Config(cache_enabled=False, **overrides)


def test_merges_sources_and_tracks_cross_source_coverage() -> None:
    """The same story read from two sources is one article with source_count 2."""
    dup = article(
        "Ethereum ETF approval sparks a massive rally (update)",
        "Fri, 09 Oct 2026 14:22:00 GMT",
        "https://ex.com/rally?utm_source=feed",
        source="beta",
    )
    alpha = provider_with("alpha", (_RALLY, _HACK))
    beta = provider_with("beta", (dup,))
    result = fetch_result(
        "ETH/USD", lookback_hours=3, now=NOW, config=_config(), providers=(alpha, beta)
    )

    assert result.resolved_name == "Ethereum"
    assert len(result.vectors) == 2
    assert result.data_quality.articles_fetched == 3
    assert result.data_quality.duplicates_removed == 1

    hour14 = next(v for v in result.vectors if v.timestamp == "2026-10-09T14:00:00Z")
    assert hour14.article_count == 1  # one unique story
    assert hour14.source_count == 2  # but two sources carried it
    assert hour14.sources == ("alpha", "beta")
    assert hour14.novelty_flag is True


def test_partial_failure_is_tolerated_and_recorded() -> None:
    """A provider that explodes is recorded, never fatal."""
    alpha = provider_with("alpha", (_RALLY, _HACK))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=3,
        now=NOW,
        config=_config(),
        providers=(alpha, BrokenProvider),
    )

    assert len(result.vectors) == 2  # alpha's articles still landed
    errors = result.data_quality.errors
    assert len(errors) == 1
    assert errors[0].source == "broken"
    assert errors[0].exception_type == "RuntimeError"
    assert result.data_quality.sources_used == ("alpha",)


def test_missing_field_when_nothing_lands() -> None:
    """Empty coverage is explained, not silently returned as an empty list."""
    alpha = provider_with("alpha", ())
    result = fetch_result(
        "ETH/USD", lookback_hours=1, now=NOW, config=_config(), providers=(alpha,)
    )
    assert result.vectors == []
    assert any(m.field == "news" for m in result.data_quality.missing)


def test_skipped_provider_is_recorded_with_reason() -> None:
    """A credential-gated provider reports why it did not run."""
    from ticker_news_signals.providers.cryptopanic import CryptoPanicProvider

    result = fetch_result(
        "ETH/USD",
        lookback_hours=1,
        now=NOW,
        config=_config(cryptopanic_token=None),
        providers=(CryptoPanicProvider,),
    )
    skipped = result.data_quality.skipped
    assert len(skipped) == 1
    assert skipped[0].source == "cryptopanic"
    assert "token" in skipped[0].reason.lower()


def test_availability_check_failure_is_recorded_not_fatal() -> None:
    """A provider whose ``available()`` raises is recorded, not fatal."""
    alpha = provider_with("alpha", (_RALLY,))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=3,
        now=NOW,
        config=_config(),
        providers=(alpha, UnavailableProvider),
    )
    assert len(result.vectors) == 1  # alpha still landed
    errors = {e.source: e for e in result.data_quality.errors}
    assert errors["unavailable"].exception_type == "RuntimeError"
    skipped = {s.source: s for s in result.data_quality.skipped}
    assert "availability check failed" in skipped["unavailable"].reason
    assert result.data_quality.sources_used == ("alpha",)


def test_provider_init_failure_is_recorded_not_fatal() -> None:
    """A provider raising in ``__init__`` is skipped; the rest still run."""
    alpha = provider_with("alpha", (_RALLY,))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=3,
        now=NOW,
        config=_config(),
        providers=(alpha, ExplodingInitProvider),
    )
    assert len(result.vectors) == 1
    errors = {e.source: e for e in result.data_quality.errors}
    assert errors["exploding"].exception_type == "RuntimeError"
    skipped = {s.source: s for s in result.data_quality.skipped}
    assert "initialisation failed" in skipped["exploding"].reason
    assert result.data_quality.sources_used == ("alpha",)


def test_invalid_rate_limit_is_recorded_not_fatal() -> None:
    """A provider declaring a zero rpm is skipped, not a crash."""
    alpha = provider_with("alpha", (_RALLY,))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=3,
        now=NOW,
        config=_config(),
        providers=(alpha, BadRpmProvider),
    )
    assert len(result.vectors) == 1
    errors = {e.source: e for e in result.data_quality.errors}
    assert errors["badrpm"].exception_type == "ValueError"
    assert result.data_quality.sources_used == ("alpha",)


def test_scoring_uses_configured_backend() -> None:
    """The lexicon backend catches domain words VADER reads as neutral."""
    from ticker_news_signals.sentiment import score_lexicon

    headline = article(
        "Ethereum hacked in record bridge exploit",
        "Fri, 09 Oct 2026 14:20:00 GMT",
        "https://ex.com/x",
        source="alpha",
    )
    alpha = provider_with("alpha", (headline,))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=1,
        now=NOW,
        config=_config(sentiment_backend="lexicon"),
        providers=(alpha,),
    )
    assert len(result.vectors) == 1
    assert result.vectors[0].sentiment_score < 0
    assert score_lexicon("Ethereum hacked in record bridge exploit") < 0


def test_old_articles_are_dropped_future_dated_are_clamped() -> None:
    """Outside the window => no vector; future timestamps bucket at now."""
    old = article(
        "Ethereum history from yesterday",
        "Fri, 09 Oct 2026 05:00:00 GMT",
        "https://ex.com/old",
        source="alpha",
    )
    future = article(
        "Ethereum scheduled announcement",
        "Sat, 10 Oct 2026 02:00:00 GMT",
        "https://ex.com/future",
        source="alpha",
    )
    alpha = provider_with("alpha", (old, future))
    result = fetch_result(
        "ETH/USD", lookback_hours=3, now=NOW, config=_config(), providers=(alpha,)
    )
    assert len(result.vectors) == 1
    assert result.vectors[0].timestamp == "2026-10-09T14:00:00Z"


def test_fetch_signals_returns_only_vectors() -> None:
    """The legacy entry point still returns the canonical list."""
    alpha = provider_with("alpha", (_RALLY,))
    vectors = fetch_signals(
        "ETH/USD", lookback_hours=1, now=NOW, config=_config(), providers=(alpha,)
    )
    assert isinstance(vectors, list)
    assert vectors[0].to_dict().keys() == {
        "ticker",
        "timestamp",
        "sentiment_score",
        "article_count",
        "novelty_flag",
    }


def test_vector_to_record_includes_extended_fields() -> None:
    alpha = provider_with("alpha", (_RALLY, _HACK))
    result = fetch_result(
        "ETH/USD", lookback_hours=3, now=NOW, config=_config(), providers=(alpha,)
    )
    record = result.vectors[0].to_record()
    assert set(record) >= {
        "source_count",
        "sentiment_std",
        "scorer_std",
        "sources",
        "top_headlines",
    }
    assert len(record["top_headlines"]) <= 3


def test_single_backend_has_zero_scorer_std() -> None:
    """Without the ensemble backend the scorer agrees with itself (std 0)."""
    alpha = provider_with("alpha", (_RALLY, _HACK))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=3,
        now=NOW,
        config=_config(sentiment_backend="vader"),
        providers=(alpha,),
    )
    assert all(v.scorer_std == 0.0 for v in result.vectors)
    # The canonical JSONL schema must NOT grow this field.
    assert "scorer_std" not in result.vectors[0].to_dict()


def test_ensemble_backend_populates_scorer_std() -> None:
    """Ensemble exposes per-headline disagreement as a non-zero scorer_std on
    headlines the two constituent scorers read differently."""
    contradictory = article(
        # VADER reads the headline as neutral (0.000); the crypto lexicon sees
        # "halt" as strongly negative (-0.333) — the scorers disagree.
        "Rally halted after exchange halt",
        "Fri, 09 Oct 2026 14:20:00 GMT",
        "https://ex.com/mixed",
        source="alpha",
    )
    alpha = provider_with("alpha", (contradictory,))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=3,
        now=NOW,
        config=_config(sentiment_backend="ensemble"),
        providers=(alpha,),
    )
    assert result.vectors
    assert all(v.scorer_std >= 0.0 for v in result.vectors)
    # At least one headline should have a measurable scorer disagreement.
    assert any(v.scorer_std > 0.0 for v in result.vectors)


def test_result_dict_is_json_safe() -> None:
    import json

    alpha = provider_with("alpha", (_RALLY,))
    result = fetch_result(
        "ETH/USD", lookback_hours=1, now=NOW, config=_config(), providers=(alpha,)
    )
    json.dumps(result.to_dict())


def test_trending_only_provider_populates_sources() -> None:
    from tests.fakes import TrendProvider

    class CryptoWire(TrendProvider):
        name = "cryptowire"
        articles = (_NEW,)

    result = fetch_result(
        "ETH/USD", lookback_hours=1, now=NOW, config=_config(), providers=(CryptoWire,)
    )
    assert len(result.vectors) == 1
    assert result.data_quality.sources_used == ("cryptowire",)


def test_selected_sources_from_config() -> None:
    """Config.sources restricts which providers run."""
    alpha = provider_with("alpha", (_RALLY,))
    beta = provider_with("beta", (_NEW,))
    result = fetch_result(
        "ETH/USD",
        lookback_hours=1,
        now=NOW,
        config=_config(sources=("alpha",)),
        providers=(alpha, beta),
    )
    assert result.data_quality.sources_used == ("alpha",)
    assert len(result.data_quality.provenance) == 1
