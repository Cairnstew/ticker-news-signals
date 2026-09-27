"""Offline tests for VADER sentiment scoring."""

from __future__ import annotations

from ticker_news_signals.sentiment import score_batch, score_headline


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