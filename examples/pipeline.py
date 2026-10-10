#!/usr/bin/env python3
"""Pipeline output: one flat metrics row per ticker, ready for a DataFrame/CSV.

    uv run python examples/pipeline.py BTC ETH SOL
    uv run python examples/pipeline.py BTC ETH --csv > signals.csv
    uv run python examples/pipeline.py BTC,ETH --hours 3

Every row is produced by :func:`ticker_news_signals.render.flat_metrics`:
missing measurements are ``null`` (never ``0``) and tolerated provider errors
are surfaced by ``error_count`` instead of being hidden.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from typing import Any

from ticker_news_signals.config import Config
from ticker_news_signals.pipeline import fetch_result
from ticker_news_signals.render import flat_metrics


def to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in columns})
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--hours", type=int, default=1, help="lookback hours per pull")
    parser.add_argument("--sources", default=None, help="comma-separated provider names")
    parser.add_argument("--csv", action="store_true", help="emit CSV instead of JSON")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between tickers")
    args = parser.parse_args()

    config = Config.from_env(sources=tuple(args.sources.split(",")) if args.sources else None)
    rows: list[dict[str, Any]] = []
    for i, ticker in enumerate(args.tickers):
        if i:
            time.sleep(args.delay)
        result = fetch_result(ticker, lookback_hours=args.hours, config=config)
        rows.append(flat_metrics(result))

    if args.csv:
        print(to_csv(rows), end="")
    else:
        print(json.dumps(rows, indent=2, default=str))

    degraded = [r for r in rows if r["error_count"]]
    if degraded:
        print(
            f"{len(degraded)} of {len(rows)} pulls had tolerated provider errors; "
            "use the --json envelope to inspect data_quality",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
