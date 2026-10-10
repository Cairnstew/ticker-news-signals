"""Sentiment scoring for news headlines, behind a small pluggable registry.

Three local backends ship in the box, all fully local (no key, no network):

``vader`` (default)
    VADER compound score (``-1.0`` … ``1.0``) from the
    :mod:`vaderSentiment` lexicon. General-purpose, well-validated on social
    and news text.

``lexicon``
    A tiny crypto-domain keyword scorer. Markets have vocabulary VADER reads
    as neutral ("rally", "hack", "ETF approval", "liquidation"), so this
    backend counts polarity-bearing domain terms. It is deliberately simple,
    fast and auditable — a second opinion to cross-check VADER, not a
    replacement.

``ensemble``
    The mean of the independent backends (:data:`ENSEMBLE_BACKENDS`). The
    point of combining *different types* of scorer is agreement: a headline
    that VADER reads one way and the crypto lexicon reads another is
    *contested*, and ``score_batch_detail`` exposes the per-headline component
    values so the orchestrator can surface how far the scorers disagreed
    (``SignalVector.scorer_std`` in the extended record) — the sentiment-side
    mirror of cross-source news reconciliation.

An optional ``cryptobert`` backend (Hugging Face `ElKulako/cryptobert`, MIT)
is dependency-gated the way credential-gated providers are: it is only
advertised when ``transformers``/``torch`` are importable, and weights are
downloaded lazily on first use (see :mod:`.sentiment_ml`).

Backends are registered by name; :func:`score_headline` / :func:`score_batch`
accept a ``backend=`` argument and default to the config default. Callers may
register their own backend with :func:`register_backend`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .errors import UnknownSentimentBackend

# Module-level analyzer: vaderSentiment loads its lexicon on construction, so
# share a single instance across all callers.
_ANALYZER = SentimentIntensityAnalyzer()

#: Default backend used when none is named.
DEFAULT_BACKEND = "vader"

Scorer = Callable[[str], float]


# --------------------------------------------------------------------------- backends


def score_vader(text: str) -> float:
    """VADER compound sentiment in ``[-1.0, 1.0]``; blank text scores ``0.0``."""
    if not text:
        return 0.0
    return float(_ANALYZER.polarity_scores(text)["compound"])


#: Crypto-domain polarity terms. Kept small and explicit so the score is auditable.
_POSITIVE_TERMS: frozenset[str] = frozenset(
    {
        "adoption",
        "approval",
        "approved",
        "bull",
        "bullish",
        "breakout",
        "buyback",
        "gain",
        "gains",
        "green",
        "halving",
        "inflow",
        "inflows",
        "integration",
        "launch",
        "launches",
        "listing",
        "mainnet",
        "moon",
        "partnership",
        "rally",
        "rallies",
        "recovery",
        "record",
        "rise",
        "rises",
        "soar",
        "soars",
        "surge",
        "surges",
        "upgrade",
        "upside",
        "win",
        "wins",
    }
)
_NEGATIVE_TERMS: frozenset[str] = frozenset(
    {
        "ban",
        "banned",
        "bear",
        "bearish",
        "breach",
        "bug",
        "crash",
        "crashes",
        "delist",
        "delisting",
        "dump",
        "exploit",
        "exploited",
        "fear",
        "fraud",
        "hack",
        "hacked",
        "halt",
        "halted",
        "lawsuit",
        "liquidated",
        "liquidation",
        "loss",
        "losses",
        "outage",
        "outflow",
        "outflows",
        "plunge",
        "plunges",
        "recession",
        "red",
        "rug",
        "scam",
        "sell-off",
        "slump",
        "sue",
        "sued",
        "tumble",
        "tumbles",
        "warning",
    }
)

_WORD_RE = re.compile(r"[a-z][a-z\-]+")


def score_lexicon(text: str) -> float:
    """A crypto-domain keyword score in ``[-1.0, 1.0]``.

    Score is ``(pos - neg) / (pos + neg)`` over matched terms, so a headline
    mentioning both sides nets toward neutral rather than saturating.
    """
    if not text:
        return 0.0
    words = _WORD_RE.findall(text.lower())
    pos = sum(1 for word in words if word in _POSITIVE_TERMS)
    neg = sum(1 for word in words if word in _NEGATIVE_TERMS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


#: Constituent backends averaged by the ``ensemble`` backend. Intentionally a
#: *set of different types* — a general lexicon (VADER) and a domain keyword
#: scorer — so agreement means something rather than correlating scores.
ENSEMBLE_BACKENDS: tuple[str, ...] = ("vader", "lexicon")


def score_ensemble(text: str) -> float:
    """The mean of :data:`ENSEMBLE_BACKENDS` scores, in ``[-1.0, 1.0]``."""
    if not text:
        return 0.0
    scores = [_resolve(backend)(text) for backend in ENSEMBLE_BACKENDS]
    return sum(scores) / len(scores)


# --------------------------------------------------------------------------- registry

_BACKENDS: dict[str, Scorer] = {
    "vader": score_vader,
    "lexicon": score_lexicon,
    "ensemble": score_ensemble,
}


def register_backend(name: str, scorer: Scorer) -> None:
    """Register (or replace) a sentiment backend by name."""
    _BACKENDS[name.strip().lower()] = scorer


def available_backends() -> tuple[str, ...]:
    """Names of every registered backend, sorted."""
    return tuple(sorted(_BACKENDS))


def _resolve(backend: str | None) -> Scorer:
    name = (backend or DEFAULT_BACKEND).strip().lower()
    try:
        return _BACKENDS[name]
    except KeyError:
        known = ", ".join(available_backends())
        raise UnknownSentimentBackend(
            f"unknown sentiment backend {name!r}; known backends: {known}"
        ) from None


def score_headline(text: str, *, backend: str | None = None) -> float:
    """Score one headline in ``[-1.0, 1.0]`` with the named backend."""
    return _resolve(backend)(text)


def score_batch_detail(
    texts: Iterable[str], *, backend: str | None = None
) -> list[tuple[float, tuple[float, ...]]]:
    """Score every headline, returning ``(score, components)`` pairs.

    ``score`` is what :func:`score_batch` returns. ``components`` holds the
    per-backend values that produced it: length ``1`` for single backends, the
    constituent scores for ``ensemble``. The orchestrator uses the components
    to compute per-headline scorer disagreement without re-running scorers.
    """
    name = (backend or DEFAULT_BACKEND).strip().lower()
    scorers = (
        tuple(_resolve(b) for b in ENSEMBLE_BACKENDS) if name == "ensemble" else (_resolve(name),)
    )
    out: list[tuple[float, tuple[float, ...]]] = []
    for text in texts:
        components = tuple(scorer(text) for scorer in scorers)
        out.append((sum(components) / len(components), components))
    return out


def score_batch(texts: Iterable[str], *, backend: str | None = None) -> list[float]:
    """Score every headline in *texts*, preserving order."""
    return [score for score, _components in score_batch_detail(texts, backend=backend)]


# --------------------------------------------------------------------------- optional ML backends


def _register_ml_backends() -> None:
    """Advertise transformer backends only when their stack is installed.

    Mirrors the credential-gating of providers: a machine without
    ``transformers``/``torch`` simply does not see ``cryptobert`` in
    :func:`available_backends`. Model weights are downloaded lazily on first
    use, never at import.
    """
    try:
        from .sentiment_ml import ensure_registered

        ensure_registered()
    except Exception:  # pragma: no cover - the module is self-guarding
        pass


_register_ml_backends()


__all__ = [
    "DEFAULT_BACKEND",
    "ENSEMBLE_BACKENDS",
    "available_backends",
    "register_backend",
    "score_batch",
    "score_batch_detail",
    "score_ensemble",
    "score_headline",
    "score_lexicon",
    "score_vader",
]
