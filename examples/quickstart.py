#!/usr/bin/env python3
"""Quickstart: fetch one ticker and print its report.

uv run python examples/quickstart.py ETH/USD        # (or `.venv/bin/python`)
uv run python examples/quickstart.py BTC --json
"""

from __future__ import annotations

import argparse
import json
import sys

from ticker_news_signals import __version__
from ticker_news_signals.pipeline import fetch_result
from ticker_news_signals.render import quality_block, render_markdown, render_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", default="ETH/USD")
    parser.add_argument("--lookback-hours", type=int, default=3)
    parser.add_argument("--sources", default=None, help="comma-separated provider names")
    parser.add_argument("--json", action="store_true", help="print the full JSON result")
    parser.add_argument("--markdown", action="store_true", help="print the report as markdown")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--show-errors", action="store_true", help="also print the quality block")
    args = parser.parse_args()

    from ticker_news_signals.config import Config

    config = Config.from_env(
        sources=tuple(args.sources.split(",")) if args.sources else None,
        cache_enabled=not args.no_cache,
    )
    result = fetch_result(args.ticker, lookback_hours=args.lookback_hours, config=config)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    elif args.markdown:
        print(render_markdown(result))
    else:
        print(render_report(result))

    if args.show_errors and (result.data_quality.errors or result.data_quality.skipped):
        print(quality_block(result))

    if result.vectors:
        return 0
    print("no signals in window — see the quality block", file=sys.stderr)
    return 1


if __name__ == "__main__":
    print(f"ticker-news-signals {__version__}")
    raise SystemExit(main())
