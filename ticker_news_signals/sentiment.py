"""VADER sentiment scoring for news headlines.

Exposes a tiny surface: :func:`score_headline` returns the VADER compound
score (``-1.0`` … ``1.0``) for one headline, :func:`score_batch` scores many
at once.  The analyzer is created once at import time — the package keeps a
``NullHandler`` logger by default so importing it never configures logging.
"""

from __future__ import annotations

from collections.abc import Iterable

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Module-level analyzer: vaderSentiment loads its lexicon on construction,
# so share a single instance across all callers.
_ANALYZER = SentimentIntensityAnalyzer()


def score_headline(text: str) -> float:
    """Return the VADER compound sentiment score for *text*.

    The compound score is a normalised value in ``[-1.0, 1.0]``: strongly
    negative headlines score near ``-1``, strongly positive near ``+1``, and
    a blank/invalid headline scores ``0.0``.
    """
    if not text:
        return 0.0
    return float(_ANALYZER.polarity_scores(text)["compound"])


def score_batch(texts: Iterable[str]) -> list[float]:
    """Score every headline in *texts*, preserving order."""
    return [score_headline(text) for text in texts]