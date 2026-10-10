"""Offline tests for cross-source reconciliation and symbol resolution."""

from __future__ import annotations

from tests.fakes import article
from ticker_news_signals.models import NewsArticle
from ticker_news_signals.reconcile import coverage_summary, dedupe, sentiment_dispersion
from ticker_news_signals.resolve import ResolvedTicker, resolve_ticker


class TestDedupe:
    def test_exact_url_collapses(self) -> None:
        a = article("same story", "Fri, 09 Oct 2026 14:00:00 GMT", "https://x.com/a", "one")
        b = article("same story", "Fri, 09 Oct 2026 14:00:01 GMT", "https://x.com/a", "two")
        result = dedupe([a, b])
        assert len(result.articles) == 1
        assert result.duplicates_removed == 1
        assert result.sources_for(result.articles[0]) == ("one", "two")

    def test_tracking_params_are_stripped(self) -> None:
        a = article("story", "Fri, 09 Oct 2026 14:00:00 GMT", "https://x.com/story?id=1", "one")
        b = article(
            "story", "Fri, 09 Oct 2026 14:00:00 GMT", "https://x.com/story?id=1&utm=feed", "two"
        )
        result = dedupe([a, b])
        assert len(result.articles) == 1
        assert result.duplicates_removed == 1

    def test_title_fallback_when_url_missing(self) -> None:
        a = NewsArticle(title="No URL here", source="one")
        b = NewsArticle(title="No URL here", source="two")
        result = dedupe([a, b])
        assert len(result.articles) == 1
        assert result.duplicates_removed == 1

    def test_blank_articles_dropped(self) -> None:
        blank = NewsArticle(title="", url="", source="one")
        result = dedupe([blank])
        assert result.articles == []

    def test_coverage_summary_counts_per_source(self) -> None:
        a = article("story", "Fri, 09 Oct 2026 14:00:00 GMT", "https://x.com/a", "one")
        b = article("story", "Fri, 09 Oct 2026 14:00:01 GMT", "https://x.com/a", "two")
        c = article("other", "Fri, 09 Oct 2026 14:00:00 GMT", "https://x.com/c", "one")
        summary = coverage_summary(dedupe([a, b, c]))
        assert summary["unique"] == 2
        assert summary["duplicates_removed"] == 1
        assert summary["multi_source_stories"] == 1
        assert summary["per_source"]["one"] == 2
        assert summary["per_source"]["two"] == 1


class TestDispersion:
    def test_single_score_is_zero(self) -> None:
        assert sentiment_dispersion([0.5]) == 0.0

    def test_two_scores_spread(self) -> None:
        assert sentiment_dispersion([0.9, -0.9]) > 0.8

    def test_identical_scores_zero(self) -> None:
        assert sentiment_dispersion([0.4, 0.4, 0.4]) == 0.0


class TestResolve:
    def test_normalises_pair_spellings(self) -> None:
        expected: dict[str, str] = {
            "ETH/USD": "ETH_USD",
            "eth-usd": "ETH_USD",
            "ETH:USD": "ETH_USD",
        }
        for ticker, symbol in expected.items():
            asset = resolve_ticker(ticker)
            assert asset.base == "ETH"
            assert asset.symbol == symbol
        assert resolve_ticker("ETH").symbol == "ETH"  # no quote -> bare key

    def test_known_symbol_gains_name_and_terms(self) -> None:
        asset = resolve_ticker("BTC")
        assert asset.name == "Bitcoin"
        assert "btc" in asset.terms
        assert "bitcoin" in asset.terms

    def test_unknown_symbol_uses_bare_base(self) -> None:
        asset = resolve_ticker("FOOBARCOIN")
        assert asset.name is None
        assert asset.terms == ("foobarcoin",)

    def test_matches_is_whole_word(self) -> None:
        asset = resolve_ticker("ETH")
        assert asset.matches("Ethereum sets a record")
        assert asset.matches("ether developers meet")
        assert not asset.matches("together we can")
        assert not asset.matches("whether it rains")

    def test_matches_multiword_phrase(self) -> None:
        asset = resolve_ticker("BCH")
        assert asset.matches("Bitcoin Cash network upgrade")
        assert not asset.matches("cash is king")

    def test_search_query_prefers_name(self) -> None:
        assert resolve_ticker("SOL").search_query == "Solana"
        assert resolve_ticker("FOOBARCOIN").search_query == "FOOBARCOIN"

    def test_with_name_adds_term(self) -> None:
        asset = ResolvedTicker(query="X", symbol="X", base="X", terms=("x",))
        renamed = asset.with_name("My Coin")
        assert renamed.name == "My Coin"
        assert "my coin" in renamed.terms
