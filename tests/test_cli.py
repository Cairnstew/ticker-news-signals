"""Offline tests for the CLI: parser surface, sources command, cache command.

Commands that would hit the network (``pull``) are only exercised structurally
(sort of): `sources` and `cache` are fully offline.
"""

from __future__ import annotations

from datetime import UTC, datetime

import ticker_news_signals.cli as cli
from ticker_news_signals.cli import _build_parser


def _pull_args(*extra: str):
    return _build_parser().parse_args(
        ["pull", "--ticker", "ETH/USD", "--output", "x.jsonl", *extra]
    )


class TestParser:
    def test_append_defaults_off(self) -> None:
        assert _pull_args().append is False

    def test_append_flag_on(self) -> None:
        assert _pull_args("--append").append is True

    def test_ticker_comma_list(self) -> None:
        args = _build_parser().parse_args(["pull", "--ticker", "BTC/USD,ETH/USD"])
        assert args.ticker == "BTC/USD,ETH/USD"

    def test_new_flags_present(self) -> None:
        args = _build_parser().parse_args(
            [
                "pull",
                "--ticker",
                "ETH",
                "--sources",
                "gnews,rss",
                "--sentiment",
                "lexicon",
                "--extended",
            ]
        )
        assert args.sources == "gnews,rss"
        assert args.sentiment == "lexicon"
        assert args.extended is True

    def test_output_modes_are_mutually_acceptable(self) -> None:
        for flag in ("--report", "--markdown", "--metrics", "--json", "--show-errors"):
            _build_parser().parse_args(["pull", "--ticker", "ETH", flag])

    def test_sources_command(self) -> None:
        args = _build_parser().parse_args(["sources"])
        assert args.command == "sources"

    def test_cache_command_actions(self) -> None:
        for action in ("stats", "prune", "clear", "vacuum"):
            args = _build_parser().parse_args(["cache", action])
            assert args.command == "cache"
            assert args.action == action


class TestSourcesCommand:
    def test_lists_registered_providers(self, capsys) -> None:
        from ticker_news_signals.providers import GNewsProvider, RssProvider

        exit_code = cli.main(["--no-log-file", "sources"])
        assert exit_code == 0
        captured = capsys.readouterr().out
        assert GNewsProvider.name in captured
        assert RssProvider.name in captured
        assert "cryptopanic" in captured


class TestCacheCommand:
    def test_stats_on_missing_file(self, tmp_path) -> None:
        path = tmp_path / "cache.sqlite3"
        exit_code = cli.main(["--no-log-file", "cache", "--path", str(path)])
        assert exit_code == 0
        assert path.exists()  # the cache file is created by stats

    def test_clear_and_vacuum(self, tmp_path) -> None:
        from ticker_news_signals.cache import SQLiteCache

        path = tmp_path / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.set("k", {"a": 1}, 60)
        cache.close()
        assert cli.main(["--no-log-file", "cache", "stats", "--path", str(path)]) == 0
        assert cli.main(["--no-log-file", "cache", "vacuum", "--path", str(path)]) == 0
        assert cli.main(["--no-log-file", "cache", "clear", "--path", str(path)]) == 0
        cache = SQLiteCache(path)
        assert len(cache) == 0
        cache.close()


class TestPullWithoutNetwork:
    def test_empty_stdout_when_no_articles(self, monkeypatch, capsys) -> None:
        """A pull whose providers return nothing exits 0 and prints nothing on stdout."""

        from ticker_news_signals.models import DataQuality, SignalResult

        empty = SignalResult(
            ticker="NOTAREALCOIN",
            vectors=[],
            data_quality=DataQuality(),
            generated_at=datetime.now(UTC),
            query="NOTAREALCOIN",
            resolved_symbol="NOTAREALCOIN",
        )
        monkeypatch.setattr("ticker_news_signals.pipeline.fetch_result", lambda *a, **k: empty)

        exit_code = cli.main(["--no-log-file", "pull", "--ticker", "NOTAREALCOIN"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == ""


class TestVersion:
    def test_version_prints(self, capsys) -> None:
        from ticker_news_signals import __version__

        assert cli.main(["--no-log-file", "version"]) == 0
        assert __version__ in capsys.readouterr().out
