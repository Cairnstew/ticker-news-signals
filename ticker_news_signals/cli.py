"""CLI entry point for ticker-news-signals.

This module lives inside the package so setuptools can register it as a
console_scripts entry point (see pyproject.toml).  The repo-root cli.py
just delegates here, so `python cli.py ...` and `ticker-news-signals ...`
behave identically.

Usage (from the dev shell):

    python cli.py pull --ticker ETH/USD --output signals.jsonl --append
    python cli.py pull --ticker ETH/USD --report
    python cli.py pull --ticker BTC/USD,ETH/USD --metrics
    python cli.py history --ticker ETH --from 2026-01-01 --to 2026-01-07 --granularity day
    python cli.py sources
    python cli.py cache stats

``pull`` fetches news for ``--ticker`` from every available source, scores
sentiment, and writes one JSON signal vector per line (NDJSON) to ``--output``
or stdout.  ``--json`` switches to a pretty self-describing envelope and
``--report`` / ``--markdown`` / ``--metrics`` select human or pipeline output.

``history`` reconstructs *historical* per-ticker signal vectors for a past
window from archived news (the Wayback Machine's captured RSS feeds).  It
reuses the same scoring, reconciliation and coverage machinery as ``pull`` and
reports per-source coverage so a sparse archive is visible rather than silent.

``--output`` is REPLACED by each pull unless ``--append`` is given, in which
case the pull's records are added to whatever the file already holds.  A
timer wants ``--append``: the default lookback is one hour, so a replace-mode
timer leaves the file one hour deep forever.  Replacing a file that already
holds records prints a warning on stderr.
"""

from __future__ import annotations

import argparse
import json as _json
import sys

from ticker_news_signals import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticker-news-signals",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging (both console and file)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help=(
            "path to the JSON log file (default: "
            "$XDG_STATE_HOME/ticker-news-signals/logs/ticker-news-signals.log)"
        ),
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="disable file logging entirely",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pull", help="fetch signals for one or more tickers and write JSONL")
    p.add_argument(
        "--ticker",
        required=True,
        help="market or symbol, e.g. ETH/USD (comma-separate for several)",
    )
    p.add_argument("--output", default=None, help="output file path (default: stdout)")
    p.add_argument("--lookback-hours", type=int, default=1, help="hours to look back (default: 1)")
    p.add_argument(
        "--append",
        action="store_true",
        help=(
            "append to --output instead of replacing it. Default is OFF, so "
            "an existing caller keeps today's replace behaviour; a timer that "
            "wants a growing log MUST pass it, because an hourly pull with "
            "--lookback-hours 1 leaves the file one hour deep otherwise."
        ),
    )
    p.add_argument(
        "--sources",
        default=None,
        help="comma-separated source names to use (default: all available). See `sources`.",
    )
    p.add_argument(
        "--sentiment",
        default=None,
        help="sentiment backend: vader (default) or lexicon",
    )
    p.add_argument("--no-cache", action="store_true", help="disable the response cache")
    p.add_argument(
        "--extended",
        action="store_true",
        help=(
            "emit extended JSONL records (adds source_count, sentiment_std, sources, top_headlines)"
        ),
    )
    p.add_argument(
        "--json", action="store_true", help="pretty JSON result envelope instead of JSONL"
    )
    p.add_argument("--report", action="store_true", help="human-readable report")
    p.add_argument("--markdown", action="store_true", help="report as a fenced markdown block")
    p.add_argument(
        "--metrics",
        action="store_true",
        help="one flat JSON metrics row per ticker (pipeline friendly)",
    )
    p.add_argument(
        "--show-errors",
        action="store_true",
        help="print tolerated errors, throttles, skips and missing fields to stderr",
    )
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("sources", help="list configured news sources and their availability")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser(
        "history",
        help="reconstruct historical signal vectors from archived news (Wayback)",
    )
    p.add_argument(
        "--ticker",
        required=True,
        help="market or symbol, e.g. ETH/USD (comma-separate for several)",
    )
    p.add_argument(
        "--granularity",
        default="day",
        choices=["hour", "day"],
        help="bucket size for the reconstructed vectors (default: day)",
    )
    p.add_argument(
        "--from",
        dest="start",
        required=True,
        help="inclusive start date, e.g. 2026-01-01 (or ISO datetime)",
    )
    p.add_argument(
        "--to",
        dest="end",
        required=True,
        help="inclusive end date, e.g. 2026-01-07 (or ISO datetime)",
    )
    p.add_argument(
        "--sources",
        default=None,
        help="comma-separated history source names (default: all available). See `sources`.",
    )
    p.add_argument("--no-cache", action="store_true", help="disable the response cache")
    p.add_argument(
        "--json", action="store_true", help="pretty JSON result envelope instead of JSONL"
    )
    p.add_argument("--report", action="store_true", help="human-readable report")
    p.add_argument(
        "--show-errors",
        action="store_true",
        help="print tolerated errors, throttles, skips and missing fields to stderr",
    )
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("cache", help="inspect or maintain the disk (SQLite) cache")
    p.add_argument(
        "action",
        nargs="?",
        default="stats",
        choices=["stats", "prune", "clear", "vacuum"],
        help="what to do (default: stats)",
    )
    p.add_argument("--path", default=None, help="cache file (default: TNS_CACHE_PATH env)")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("version", help="print the package version")
    p.set_defaults(func=cmd_version)

    return parser


def _emit_json(payload: object, pretty: bool = False) -> None:
    if pretty:
        print(_json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(_json.dumps(payload, ensure_ascii=False, sort_keys=True))


# --------------------------------------------------------------------------- commands


def _config_from_args(args: argparse.Namespace):
    from ticker_news_signals.config import Config

    config = Config.from_env()
    overrides: dict[str, object] = {}
    if args.no_cache:
        overrides["cache_enabled"] = False
    if getattr(args, "sources", None):
        overrides["sources"] = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    if getattr(args, "sentiment", None):
        overrides["sentiment_backend"] = args.sentiment
    return config.with_overrides(**overrides)


def _tickers(args: argparse.Namespace) -> list[str]:
    return [t.strip() for t in args.ticker.split(",") if t.strip()]


def cmd_pull(args: argparse.Namespace) -> None:
    from ticker_news_signals.pipeline import fetch_result
    from ticker_news_signals.render import (
        flat_metrics,
        quality_block,
        render_markdown,
        render_report,
    )

    config = _config_from_args(args)
    tickers = _tickers(args)
    results = [
        fetch_result(ticker, lookback_hours=args.lookback_hours, config=config)
        for ticker in tickers
    ]

    if args.show_errors:
        for result in results:
            print(f"# {result.ticker}", file=sys.stderr)
            print(quality_block(result), file=sys.stderr)

    if args.output:
        from ticker_news_signals.export import write_json, write_jsonl

        if args.json:
            payload = _envelope(results)
            path = write_json(payload, args.output)
        else:
            records = _records(results, extended=args.extended)
            path = write_jsonl(args.output, records, append=args.append)
        print(f"signals -> {path}")
        return

    if args.report:
        print("\n\n".join(render_report(result) for result in results))
    elif args.markdown:
        print("\n\n".join(render_markdown(result) for result in results))
    elif args.metrics:
        for result in results:
            _emit_json(flat_metrics(result), pretty=False)
    elif args.json:
        _emit_json(_envelope(results), pretty=True)
    else:
        for result in results:
            if not result.vectors:
                print(f"no signals in window for {result.ticker}", file=sys.stderr)
            for record in _records([result], extended=args.extended):
                _emit_json(record, pretty=False)


def _envelope(results: list) -> dict:
    """A self-describing pretty envelope for one or more results."""
    from ticker_news_signals.export import SCHEMA_VERSION
    from ticker_news_signals.utils import now_utc

    return {
        "schema": SCHEMA_VERSION,
        "exported_at": now_utc().isoformat(),
        "results": [r.to_dict() for r in results],
    }


def _records(results: list, *, extended: bool) -> list[dict]:
    records: list[dict] = []
    for result in results:
        for vector in result.vectors:
            records.append(vector.to_record() if extended else vector.to_dict())
    return records


def _parse_iso(value: str):
    from datetime import UTC, datetime

    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            continue
    raise ValueError(f"cannot parse date {value!r} (use YYYY-MM-DD or ISO datetime)")


def cmd_history(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from ticker_news_signals.history import HISTORY_PROVIDERS, fetch_history
    from ticker_news_signals.providers import get as get_provider
    from ticker_news_signals.render import quality_block, render_history_report

    config = _config_from_args(args)
    if args.sources:
        wanted = tuple(s.strip() for s in args.sources.split(",") if s.strip())
        try:
            providers = tuple(get_provider(name) for name in wanted)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    else:
        providers = HISTORY_PROVIDERS

    start = _parse_iso(args.start)
    end = _parse_iso(args.end)
    if start > end:
        print("error: --from must be <= --to", file=sys.stderr)
        raise SystemExit(1)

    tickers = [t.strip() for t in args.ticker.split(",") if t.strip()]
    results = [
        fetch_history(
            ticker,
            granularity=args.granularity,
            start=start,
            end=end,
            config=config,
            providers=providers,
        )
        for ticker in tickers
    ]

    if args.show_errors:
        for result in results:
            print(f"# {result.ticker}", file=sys.stderr)
            print(quality_block(result), file=sys.stderr)

    if args.report:
        print("\n\n".join(render_history_report(result) for result in results))
    elif args.json:
        from datetime import UTC, datetime

        from ticker_news_signals.export import SCHEMA_VERSION

        _emit_json(
            {
                "schema": SCHEMA_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "history": [r.to_dict() for r in results],
            },
            pretty=True,
        )
    else:
        # canonical NDJSON: one vector per line (same schema as `pull`)
        for result in results:
            for vector in result.vectors:
                _emit_json(vector.to_dict(), pretty=False)


def cmd_sources(args: argparse.Namespace) -> None:
    from ticker_news_signals.config import Config
    from ticker_news_signals.history import HISTORY_PROVIDERS
    from ticker_news_signals.providers import registry

    config = Config.from_env()
    print(f"{'source':<14} {'available':<10} {'rpm':<5} capabilities")
    print("-" * 60)
    for cls in (*registry(), *HISTORY_PROVIDERS):
        ok, reason = cls.available(config)
        status = "yes" if ok else "no"
        caps = ",".join(sorted(str(c) for c in cls.supports))
        print(f"{cls.name:<14} {status:<10} {cls.rate_limit_rpm(config):<5} {caps}")
        if not ok:
            print(f"{'':<14} reason: {reason}")
    print()
    print(f"selected: {', '.join(config.sources) if config.sources else 'all available'}")
    if config.rss_feeds:
        print(f"rss feeds: {', '.join(f.name for f in config.rss_feeds)}")


def cmd_cache(args: argparse.Namespace) -> None:
    from pathlib import Path

    from ticker_news_signals.cache import SQLiteCache
    from ticker_news_signals.config import Config

    config = Config.from_env()
    raw = args.path or (str(config.cache_path) if config.cache_path else None)
    path = Path(raw) if raw else Path("ticker_news_signals_cache.sqlite3")
    cache = SQLiteCache(path)
    try:
        if args.action == "clear":
            cache.clear()
            print(f"cleared {path}")
        elif args.action == "vacuum":
            cache.vacuum()
            print(f"vacuumed {path}")
        else:
            if args.action == "prune":
                cache.prune()
            stats = cache.stats()
            print(
                f"{stats['entries']} entries, {stats['bytes'] / 1024:.1f} KiB "
                f"(max {stats['max_entries']} / {stats['max_bytes'] / (1024 * 1024):.1f} MiB) "
                f"at {path}"
            )
    finally:
        cache.close()


def cmd_version(args: argparse.Namespace) -> None:
    print(f"ticker-news-signals {__version__}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # -- Logging setup (precedence: CLI flag > env var > default) ----------
    from ticker_news_signals.logging_config import _xdg_state_dir, setup_logging

    log_level = "DEBUG" if args.verbose else "WARNING"
    file_level = "DEBUG" if args.verbose else "INFO"

    if args.no_log_file:
        log_file = None
    elif args.log_file is not None:
        log_file = args.log_file
    else:
        log_file = str(_xdg_state_dir() / "ticker-news-signals.log")

    setup_logging(
        level=log_level,
        log_file=log_file,
        file_level=file_level,
        console=True,
    )

    # -- Run the command ---------------------------------------------------
    try:
        args.func(args)
    except Exception as exc:  # surface friendly errors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
