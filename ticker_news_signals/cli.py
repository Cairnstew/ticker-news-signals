"""CLI entry point for ticker-news-signals.

This module lives inside the package so setuptools can register it as a
console_scripts entry point (see pyproject.toml).  The repo-root cli.py
just delegates here, so `python cli.py ...` and `ticker-news-signals ...`
behave identically.

Usage (from the dev shell):

    python cli.py pull ETH/USD --output signals.jsonl
    python cli.py pull ETH/USD --lookback-hours 3 --json
    python cli.py version

``pull`` fetches news for --ticker from GNews (keyless), scores sentiment
with VADER, and writes one JSON signal vector per line (NDJSON) to --output
or stdout.  ``--json`` switches to a pretty self-describing envelope.
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
        "-v", "--verbose",
        action="store_true",
        help="enable debug logging (both console and file)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="path to the JSON log file (default: $XDG_STATE_HOME/ticker-news-signals/logs/ticker-news-signals.log)",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="disable file logging entirely",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pull", help="fetch signals for one ticker and write JSONL")
    p.add_argument("--ticker", required=True, help="market or symbol, e.g. ETH/USD")
    p.add_argument("--output", default=None, help="output file path (default: stdout)")
    p.add_argument("--lookback-hours", type=int, default=1, help="hours to look back (default: 1)")
    p.add_argument("--json", action="store_true", help="pretty JSON output (envelope) instead of JSONL")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("version", help="print the package version")
    p.set_defaults(func=cmd_version)

    return parser


def _emit_json(payload: dict, pretty: bool = False) -> None:
    if pretty:
        print(_json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(_json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cmd_pull(args: argparse.Namespace) -> None:
    from ticker_news_signals.export import extract_envelope, write_json, write_jsonl

    # The source is omitted: fetch_signals creates a NewsClient and sizes
    # its GNews period to cover --lookback-hours.
    env = extract_envelope(
        None, "signals",
        ticker=args.ticker,
        lookback_hours=args.lookback_hours,
    )
    signals = env["resources"]["signals"]["signals"]

    if args.output:
        path = write_json(env, args.output) if args.json else write_jsonl(args.output, signals)
        print(f"signals -> {path}")
        return
    if args.json:
        _emit_json(env, pretty=True)
        return
    if not signals:
        print("no signals in window", file=sys.stderr)
    for record in signals:
        _emit_json(record, pretty=False)


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