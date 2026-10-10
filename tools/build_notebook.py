#!/usr/bin/env python3
"""Build ``notebooks/pipeline.ipynb`` — a guided tour of the pipeline.

The notebook is constructed programmatically (like tickerkit's notebook
tooling) so it stays a deterministic source of truth: edit the cell sources
here, re-run the build, and the committed ``.ipynb`` regenerates identically.
The output cells are left empty — run the notebook (Kernel -> Restart & Run
All) or ``tools/validate_notebook.py`` to fill them.

    uv run python tools/build_notebook.py
    uv run python tools/build_notebook.py --output notebooks/pipeline.ipynb
"""

from __future__ import annotations

import argparse
from pathlib import Path

import nbformat as nbf

NOTEBOOK_TITLE = "ticker-news-signals — crypto news in, sentiment signal vectors out"


def _md(source: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(source.strip())


def _code(source: str) -> nbf.NotebookNode:
    return nbf.v4.new_code_cell(source.strip())


def cells() -> list[nbf.NotebookNode]:
    return [
        # ── Header ──────────────────────────────────────────────────────────
        _md(
            f"""
# {NOTEBOOK_TITLE}

A guided tour through the [ticker-news-signals](https://github.com/Cairnstew/ticker-news-signals)
pipeline: pull crypto headlines from many keyless sources at once, de-duplicate
them across feeds, score sentiment locally, and aggregate into per-ticker,
per-hour **signal vectors**.

This notebook is fully live — every fetch hits real public feeds (GNews +
Bing News + curated RSS). The pipeline is **partial-failure tolerant**: a
source that is down or throttled is recorded on the result's `data_quality`
block, never an exception. No API keys are required.

```python
# install once, if you have not already
# pip install -e ".[dev]"
```
"""
        ),
        # ── Imports ─────────────────────────────────────────────────────────
        _code(
            """
from ticker_news_signals import __version__
from ticker_news_signals.config import Config
from ticker_news_signals.pipeline import fetch_result, fetch_signals
from ticker_news_signals.sentiment import available_backends, score_batch_detail, score_headline
from ticker_news_signals.render import flat_metrics, quality_block, render_markdown, render_report
from ticker_news_signals.resolve import resolve_ticker
"""
        ),
        _code(
            """
print("ticker-news-signals", __version__)
print("sentiment backends:", available_backends())
print()
print(fetch_result.__module__)
# The 'sources' CLI surfaces the same list:
from ticker_news_signals.providers import registry
for cls in registry():
    print(f"  {cls.name:<14}", ",".join(sorted(str(c) for c in cls.supports)))
"""
        ),
        # ── 1. canonical signals ───────────────────────────────────────────
        _md(
            """
## 1 — The canonical signal: `fetch_signals`

The library entry point you know from the README. One call returns one
`SignalVector` per non-empty UTC hour — the exact five-field schema that the
`kraken-trading-bot` consumer joins against OHLCV bars.
"""
        ),
        _code(
            """
vectors = fetch_signals("ETH/USD", lookback_hours=2)
print(f"{len(vectors)} signal vector(s) in the window")

for vector in vectors:
    print(vector)
"""
        ),
        _code(
            """
if vectors:
    first = vectors[0]
    print("canonical record (the consumer contract):")
    import json
    print(json.dumps(first.to_dict(), indent=2))
    print()
    print("extended record (source coverage, dispersion, driving headlines):")
    print(json.dumps(first.to_record(), indent=2))
"""
        ),
        # ── 2. full result ─────────────────────────────────────────────────
        _md(
            """
## 2 — The full result: `fetch_result`

`fetch_signals` hides the plumbing. `fetch_result` gives you the vectors
*plus* everything behind them: the resolved asset, the unique headlines, and
the `data_quality` block that answers "which source, when, any errors?".
"""
        ),
        _code(
            """
result = fetch_result("BTC", lookback_hours=2)
quality = result.data_quality

print(f"resolved: {result.resolved_symbol} ({result.resolved_name})")
print(f"{len(result.vectors)} hour(s), {len(result.articles)} unique headlines "
      f"({quality.articles_fetched} fetched, {quality.duplicates_removed} duplicates removed)")
print(f"sources used: {quality.sources_used}")
print(f"errors: {len(quality.errors)}  throttled: {len(quality.rate_limited)}  "
      f"skipped: {len(quality.skipped)}")
"""
        ),
        _code(
            """
# Something went wrong? The block explains it instead of raising.
print(quality_block(result))
"""
        ),
        # ── 3. cross-source reconciliation ─────────────────────────────────
        _md(
            """
## 3 — Cross-source reconciliation

The same story is republished across feeds, often with tracking parameters.
Reconciliation collapses those duplicates while remembering **every** source
that carried the story — so an hour can say "1 article, covered by 3 sources".
"""
        ),
        _code(
            """
from collections import Counter

coverage = Counter()
for article in result.articles:
    coverage[article.source] += 1

print("unique headlines per source (after cross-source dedupe):")
for source, count in sorted(coverage.items()):
    print(f"  {source:<28} {count}")
print(f"{len(result.articles)} unique stories total")
print("stories covered by more than one source are visible as the 'sources' tuple in each hour's vector")
"""
        ),
        _code(
            """
print("per-hour source coverage from the signal vectors:")
for vector in result.vectors:
    print(f"  {vector.timestamp}  n={vector.article_count:<3} sources={vector.sources}")
"""
        ),
        # ── 4. sentiment backends ──────────────────────────────────────────
        _md(
            """
## 4 — Sentiment backends

All shipped scorers are local and keyless. VADER is the default
(general-purpose, robust); the `lexicon` backend carries crypto-domain
vocabulary that VADER reads as neutral (`hack`, `rally`, `liquidation`,
`ETF approval`). The `ensemble` backend averages the independent scorers and
reports how far they disagreed on each headline — a *contested* headline is a
signal in itself. An optional `cryptobert` transformer backend is
dependency-gated to the `[ml]` extra and appears here only when installed.
"""
        ),
        _code(
            """
headlines = [
    "Ethereum ETF approval sparks massive rally",
    "Exchange hacked: $200M drained in bridge exploit",
    "Bitcoin faces liquidation cascade as funding flips negative",
    "Rally halted after exchange halt",
]

for text in headlines:
    vader = score_headline(text, backend="vader")
    lex = score_headline(text, backend="lexicon")
    ens, ens_components = score_batch_detail([text], backend="ensemble")[0]
    print(f"  vader {vader:+.3f}  lex {lex:+.3f}  ensemble {ens:+.3f}  comps {[round(c,3) for c in ens_components]}  {text}")
"""
        ),
        # ── 5. symbol resolution ───────────────────────────────────────────
        _md(
            """
## 5 — Symbol resolution

`ETH/USD`, `eth-usd`, `ETH:USD` and `ETH` all resolve to the same base asset,
and the majors gain a human name plus whole-word search terms (so `ETH` never
matches "together").
"""
        ),
        _code(
            """
for ticker in ("ETH/USD", "eth-usd", "BCH", "BTC", "MADEUPCOIN"):
    asset = resolve_ticker(ticker)
    print(f"{ticker:<10} -> base={asset.base:<4} symbol={asset.symbol:<10} "
          f"name={asset.name or '-':<12} terms={asset.terms}")

btc = resolve_ticker("BTC")
print()
print("BTC matches 'Bitcoin hits record'? ", btc.matches("Bitcoin hits record"))
print("ETH matches 'together we stand'?  ", resolve_ticker("ETH").matches("together we stand"))
"""
        ),
        # ── 6. export + JSONL round-trip ───────────────────────────────────
        _md(
            """
## 6 — Export & JSONL round-trip

The extraction registry (`ticker_news_signals.export`) takes the same objects
the CLI writes. A JSONL file is a forward-only log when pulled with
`--append`; here we round-trip one in-memory.
"""
        ),
        _code(
            """
import json
import tempfile
from pathlib import Path

from ticker_news_signals.export import extract_envelope, write_jsonl

env = extract_envelope(None, "signals", ticker="ETH/USD", lookback_hours=1)
records = env["resources"]["signals"]["signals"]
print(f"{len(records)} canonical record(s) in the envelope; schema {env['schema']}")

tmpdir = Path(tempfile.mkdtemp(prefix="tns_notebook_"))
path = write_jsonl(tmpdir / "signals.jsonl", records, append=True)
print("wrote", path)
with path.open() as fh:
    line = fh.readline()
print("first line:", line.strip())
loaded = json.loads(line)
assert loaded.keys() == {"ticker", "timestamp", "sentiment_score", "article_count", "novelty_flag"}
print("round-trip OK — keys match the consumer contract")
"""
        ),
        # ── 7. pipeline-friendly rows ──────────────────────────────────────
        _md(
            """
## 7 — Pipeline-friendly metrics

`flat_metrics` turns one pull into one flat JSON row of scalars (`null` for
missing measurements, never `0`). Collect rows per ticker and you have a tidy
dataset without pandas.
"""
        ),
        _code(
            """
import time

rows = []
for ticker in ("BTC", "ETH", "SOL"):
    time.sleep(1)  # stay polite to free feeds
    rows.append(flat_metrics(fetch_result(ticker, lookback_hours=1)))

print(f"{'ticker':<10} {'hours':<6} {'articles':<9} {'mean':>6} {'novel':<6} {'sources':<6} {'errors':<7}")
for row in rows:
    mean = f"{row['mean_sentiment']:+.3f}" if row["mean_sentiment"] is not None else "-"
    print(f"{row['ticker']:<10} {row['hours']:<6} {row['articles']:<9} {mean:>6} "
          f"{row['novel_hours']:<6} {row['source_count']:<6} {row['error_count']:<7}")
"""
        ),
        # ── 8. caching ─────────────────────────────────────────────────────
        _md(
            """
## 8 — Caching

Every provider response passes through a TTL cache so repeated runs don't
re-hit feeds. Default is in-memory; point `TNS_CACHE_PATH` at a file to make it
git-trackable and inspect it with the `cache` CLI.
"""
        ),
        _code(
            """
from ticker_news_signals.cache import CacheLimits, SQLiteCache

cache = SQLiteCache(str(tmpdir / "cache.sqlite3"), limits=CacheLimits())
cache.set("demo:btc", {"price": 100_000}, ttl=3600)
print("cached value:", cache.get("demo:btc"))
print("stats:", {k: cache.stats()[k] for k in ("entries", "bytes")})
cache.close()

# Same file the CLI manages:
#   ticker-news-signals cache stats --path notebooks/cache.sqlite3
"""
        ),
        # ── 9. reliability ─────────────────────────────────────────────────
        _md(
            """
## 9 — Reliability in practice

Sources that are unavailable (no key) or throttled are **recorded**, never
fatal. Ask the registry what runs in your environment — then take any pull's
`data_quality` block as the audit trail.
"""
        ),
        _code(
            """
from ticker_news_signals.config import Config

config = Config.from_env()
print("configured sources:", config.sources or "all available")
for cls in registry():
    ok, reason = cls.available(config)
    flag = " available" if ok else f" skipped — {reason}"
    print(f"  {cls.name:<14}{flag}")
"""
        ),
        # ── 10. query store ────────────────────────────────────────────────
        _md(
            """
## 10 — The query store (`QueryCache`)

`QueryCache` is the database-shaped output: a git-trackable SQLite file that
saves whole pulls (`SignalResult`) and history results (`HistoryResult`) under
their ticker keys and loads them back as real model objects. A pull saved here
is identical to the JSONL line a consumer would read — but replayable offline
without re-fetching. `save`/`load` also works on the same file where the
provider cache lives, so one committed file can hold both.
"""
        ),
        _code(
            """
from ticker_news_signals import QueryCache

with QueryCache(str(tmpdir / "queries.sqlite3")) as qc:
    qc.save_signal("BTC", result)                 # never-expiring by default
    replay = qc.load_signal("btc")                # case-insensitive ticker
    assert replay is not None
    assert replay.to_dict() == result.to_dict()   # faithful round-trip
    print(f"saved + replayed BTC: {len(replay.vectors)} vector(s), "
          f"{len(replay.articles)} articles — byte-identical")

# The same file shows up in `ticker-news-signals cache stats`:
from ticker_news_signals.cache import SQLiteCache
from pathlib import Path as _P
qc_path = _P(tmpdir) / "queries.sqlite3"
print("store entries:", SQLiteCache(str(qc_path)).stats()["entries"])
SQLiteCache(str(qc_path)).close()
"""
        ),
        # ── 11. historical reconstruction ─────────────────────────────────
        _md(
            """
## 11 — Historical reconstruction (`history`)

The `history` capability rebuilds *past* per-bucket signal vectors from the
Wayback Machine's archived copies of the configured RSS feeds — no API key.
Coverage is honest: a sparse archive reports exactly how many buckets it could
answer (`buckets/expected`, `missing`, `complete`), never a fake full graph.
"""
        ),
        _code(
            """
from ticker_news_signals import fetch_history

# Bound the CDX fan-out to one densely-archived feed so this cell stays fast
# (a full 18-feed history call crawls the Wayback index and can take minutes).
config = Config().with_overrides(rss_feeds=config.rss_feeds[:1])

hist = fetch_history(
    "ETH",
    granularity="day",
    start="2026-01-01",
    end="2026-01-03",
    config=config,
)
print(f"{len(hist.vectors)} reconstructed bucket(s) from {len(hist.series)} archived source(s)")
for series in hist.series:
    cov = series.coverage
    if cov is None:
        print(f"  {series.source}: no coverage")
        continue
    print(f"  {series.source}: {cov.buckets}/{cov.expected} buckets "
          f"complete={cov.complete}  {cov.first} .. {cov.last}")

from ticker_news_signals.render import render_history_report
print(render_history_report(hist))
"""
        ),
        # ── footer ─────────────────────────────────────────────────────────
        _md(
            """
## Next steps

- `ticker-news-signals pull --ticker SOL --report` — the same data as a
  terminal report.
- `ticker-news-signals pull --ticker BTC,ETH --metrics` — one flat row per
  ticker for your own pipeline.
- `ticker-news-signals history --ticker ETH --from 2026-01-01 --to 2026-01-07`
  — archived vectors from the Wayback Machine.
- `ticker-news-signals sources` — what will run.
- `ticker-news-signals cache stats` — how big your disk cache is.

The canonical JSONL schema, the hourly timestamp alignment to OHLCV bars and
the full wiring into `kraken-trading-bot` are pinned down in
[`INTEGRATION.md`](INTEGRATION.md).
"""
        ),
    ]


def build(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    notebook = nbf.v4.new_notebook()
    notebook.metadata = {
        "kernelspec": {
            "display_name": "Python 3 (ticker-news-signals)",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3.11"},
    }
    notebook.cells = cells()
    with path.open("w", encoding="utf-8") as fh:
        nbf.write(notebook, fh)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "notebooks" / "pipeline.ipynb"),
    )
    args = parser.parse_args()
    out = build(args.output)
    print(f"built {out} ({len(cells())} cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
