"""Offline tests for sentiment backends: VADER, lexicon, ensemble, scoring API."""

from __future__ import annotations

from ticker_news_signals.sentiment import (
    ENSEMBLE_BACKENDS,
    available_backends,
    score_batch,
    score_batch_detail,
    score_ensemble,
    score_headline,
    score_lexicon,
    score_vader,
)


class TestScoreHeadline:
    def test_positive_headline_scores_positive(self) -> None:
        assert score_headline("This is amazing!") > 0.3

    def test_negative_headline_scores_negative(self) -> None:
        assert score_headline("This is terrible, awful, horrible news.") < -0.3

    def test_blank_headline_scores_zero(self) -> None:
        assert score_headline("") == 0.0
        assert score_headline(None) == 0.0
        assert score_headline("   ") == 0.0

    def test_mixed_headline_is_neutralish(self) -> None:
        # Roughly balanced positive/negative: |compound| should be small.
        score = score_headline("The market goes up and then it goes down.")
        assert -0.4 < score < 0.4

    def test_score_is_float_in_range(self) -> None:
        for text in ("worst thing ever", "best day of my life", "a plain statement"):
            score = score_headline(text)
            assert isinstance(score, float)
            assert -1.0 <= score <= 1.0


class TestScoreBatch:
    def test_batch_preserves_order_and_length(self) -> None:
        texts = ["Great news!", "Terrible news!", ""]
        scores = score_batch(texts)
        assert len(scores) == 3
        assert scores[0] > 0
        assert scores[1] < 0
        assert scores[2] == 0.0

    def test_batch_accepts_generator(self) -> None:
        scores = score_batch(text for text in ["Amazing", "Awful"])
        assert len(scores) == 2


class TestEnsemble:
    def test_registered_and_in_backends(self) -> None:
        assert "ensemble" in available_backends()
        assert set(ENSEMBLE_BACKENDS) <= set(available_backends())

    def test_ensemble_is_constituent_mean(self) -> None:
        text = "Ethereum ETF approval sparks a massive rally"
        expected = (score_vader(text) + score_lexicon(text)) / 2
        assert score_ensemble(text) == expected

    def test_blank_scores_zero(self) -> None:
        assert score_ensemble("") == 0.0

    def test_agrees_with_vader_on_plain_sentiment(self) -> None:
        # Ensemble inherits the polarity of both constituents on unambiguous text.
        assert score_ensemble("This is terrible and horrible news.") < -0.2
        assert score_ensemble("Fantastic bullish rally.") > 0.1


class TestScoreBatchDetail:
    def test_single_backend_has_length_one_components(self) -> None:
        pairs = score_batch_detail(["Great news!"], backend="vader")
        sample, components = pairs[0]
        assert len(components) == 1
        assert components[0] == sample

    def test_ensemble_exposes_constituents(self) -> None:
        text = "Ethereum ETF approval sparks a massive rally"
        pairs = score_batch_detail([text], backend="ensemble")
        sample, components = pairs[0]
        assert len(components) == len(ENSEMBLE_BACKENDS)
        assert sum(components) / len(components) == sample
        assert score_batch([text], backend="ensemble")[0] == sample

    def test_generator_is_consumed_once(self) -> None:
        pairs = score_batch_detail((t for t in ["A", "B", "C"]), backend="ensemble")
        assert len(pairs) == 3
