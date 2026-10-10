"""Optional transformer-based sentiment backends (dependency-gated).

This module follows the credential-gating pattern used for providers — but
instead of gating on a token it gates on the **ML stack**: ``cryptobert`` is
only advertised in :func:`ticker_news_signals.sentiment.available_backends`
when ``transformers`` (and torch behind it) are importable on this machine.
Model weights are downloaded lazily on first use (~500 MB), never at import.

Backends
--------
``cryptobert``
    Hugging Face `ElKulako/cryptobert` (MIT licence) — a RoBERTa/BERTweet
    variant continued-pretrained on 3.2M crypto social posts and fine-tuned on
    2M labelled StockTwits posts, classes ``Bearish``/``Neutral``/``Bullish``.
    This is a genuinely different *type* of scorer from the lexicon backends:
    contextual, trained on crypto vocabulary, and tuned for short social/news
    text rather than word counting.

    The raw class probability is amplitude-weighted to a continuous score in
    ``[-1.0, 1.0]``: ``Bullish -> +p``, ``Bearish -> -p``, ``Neutral -> 0.0``.

    - Model: https://huggingface.co/ElKulako/cryptobert
    - Paper: https://ieeexplore.ieee.org/document/10223689

Requires the optional extra: ``pip install -e ".[ml]"`` (or a Nix shell with
``transformers``/``torch`` on ``PYTHONPATH``). Without it, nothing breaks —
the backend simply is not listed.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Callable
from typing import Any

from .sentiment import register_backend

#: Hugging Face model id used by :func:`score_cryptobert`.
MODEL_NAME = "ElKulako/cryptobert"

#: Class-label -> amplitude. Labels are case- and strip-normalised before lookup.
_AMPLITUDE: dict[str, float] = {
    "bullish": 1.0,
    "bearish": -1.0,
    "neutral": 0.0,
}

#: Injection seam for offline tests: replaces the model loader with a fake.
_loader: Callable[[], Any] | None = None


def transformers_available() -> bool:
    """True when the ``transformers`` package can be imported on this machine."""
    try:
        importlib.import_module("transformers")
        return True
    except Exception:  # not installed, or a broken wheel
        return False


def _default_loader() -> Any:
    """Build the text-classification pipeline (imports transformers lazily)."""
    transformers = importlib.import_module("transformers")
    pipe = transformers.pipeline
    return pipe(
        "text-classification",
        model=MODEL_NAME,
        tokenizer=MODEL_NAME,
    )


def set_loader(loader: Callable[[], Any] | None) -> None:
    """Override the lazy model loader (test seam); ``None`` restores default."""
    global _loader
    _loader = loader


def score_cryptobert(text: str) -> float:
    """Crypto-domain transformer score in ``[-1.0, 1.0]``.

    Blank text scores ``0.0`` (never calls the model). A headline's score is
    the probability of its predicted class, sign-amplitude-weighted, so a
    confident ``Bullish`` (p=0.9) ≈ +0.9 while an ambiguous tweet lands near 0.
    """
    if not text.strip():
        return 0.0
    loader = _loader if _loader is not None else _default_loader
    pipe = loader()
    result = pipe(text, truncation=True, max_length=128)[0]
    label = str(result.get("label") or "").strip().lower()
    probability = float(result.get("score") or 0.0)
    return float(_AMPLITUDE.get(label, 0.0) * probability)


def register_ml_backends() -> bool:
    """Register transformer backends if the stack is present.

    Returns ``True`` on registration (or if already registered), ``False``
    when ``transformers`` is unavailable — the caller records the skip.
    """
    if not transformers_available():
        return False
    register_backend("cryptobert", score_cryptobert)
    return True


def ensure_registered() -> None:
    """Best-effort registration; a machine without the stack just skips."""
    with contextlib.suppress(Exception):
        register_ml_backends()


__all__ = [
    "MODEL_NAME",
    "ensure_registered",
    "register_ml_backends",
    "score_cryptobert",
    "set_loader",
    "transformers_available",
]
