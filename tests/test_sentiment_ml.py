"""Offline tests for the optional transformer sentiment backend.

No ``transformers``/``torch`` is required for these tests: the lazily-loaded
model pipeline is replaced with a fake via :func:`set_loader`, so the scoring
logic (label -> amplitude * probability) is exercised without the ML stack.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from ticker_news_signals import sentiment_ml as ml


def _fake(rows: list[dict]) -> Callable[[], object]:
    def loader():
        class FakePipe:
            def __call__(self, text, **kwargs):
                return rows

        return FakePipe()

    return loader


class TestScoreCryptobert:
    def test_bullish_amplitude_weighted(self) -> None:
        ml.set_loader(_fake([{"label": "Bullish", "score": 0.9}]))
        try:
            assert ml.score_cryptobert("Bitcoin moon rally to the moon!") == pytest.approx(0.9)
        finally:
            ml.set_loader(None)

    def test_bearish_negative(self) -> None:
        ml.set_loader(_fake([{"label": "Bearish", "score": 0.7}]))
        try:
            assert ml.score_cryptobert("crypto crash") == pytest.approx(-0.7)
        finally:
            ml.set_loader(None)

    def test_neutral_zero(self) -> None:
        ml.set_loader(_fake([{"label": "Neutral", "score": 0.8}]))
        try:
            assert ml.score_cryptobert("nothing to see here") == 0.0
        finally:
            ml.set_loader(None)

    def test_blank_never_calls_the_model(self) -> None:
        calls: list[str] = []

        def loader():
            def pipe(*args, **kwargs):
                calls.append("called")
                return [{"label": "Bullish", "score": 1.0}]

            return pipe

        ml.set_loader(loader)
        try:
            assert ml.score_cryptobert("") == 0.0
            assert ml.score_cryptobert("   ") == 0.0
            assert calls == []
        finally:
            ml.set_loader(None)


class TestDependencyGate:
    def test_register_returns_false_without_transformers(self) -> None:
        # In this repo's default dev environment transformers is NOT installed,
        # so the gate must refuse without raising.
        try:
            import transformers  # noqa: F401

            installed = True
        except Exception:
            installed = False
        if installed:
            pytest.skip("transformers is installed; dependency gate not exercisable")
        assert ml.transformers_available() is False
        assert ml.register_ml_backends() is False

    def test_cryptobert_not_advertised_without_ml_stack(self) -> None:
        """The optional backend is absent from available_backends until installed."""
        from ticker_news_signals.sentiment import available_backends

        if ml.transformers_available():
            # With the ML stack present the backend IS advertised; assert that.
            assert "cryptobert" in available_backends()
        else:
            assert "cryptobert" not in available_backends()
