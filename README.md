# ticker-news-signals

Pull crypto news headlines via [GNews](https://github.com/ranahaani/GNews)
(the keyless Google News RSS wrapper), score each headline's sentiment with
[VADER](https://github.com/cjhutto/vaderSentiment), and aggregate it into
per-ticker, per-hour **signal vectors** written as JSONL — one clean JSON
object per line, ready to feed a trading strategy or a dashboard.

No API keys. No long-running service. One command, one file.

## Features

- **Keyless by design** — GNews reads Google News RSS without an API key;
  VADER is a local rule-based lexicon scorer. Nothing to provision.
- **Typed models** — `NewsArticle` and `SignalVector` dataclasses with
  `to_dict()` JSON output and `from_dict()` reads, so records round-trip
  through `json.dumps` with no datetimes, Decimals, or surprise keys.
- **Per-ticker, per-hour signals** — articles are bucketed by UTC hour and
  each bucket collapses into a mean sentiment score, an article count, and a
  novelty flag (the hour contains an article newer than 15 minutes).
- **JSON extraction registry** — the `ticker_news_signals.export` module
  mirrors the *kraken-python* registry pattern: `EXTRACTORS`, `extract()` /
  `extract_envelope()`, and `write_json` / `write_jsonl` (NDJSON) writers.
- **Structured logging** — `logging_config.py` ships structured JSON logging
  (UTC timestamps, extra fields, secret redaction) plus a human-readable
  stderr formatter; the library keeps a `NullHandler` until you opt in.
- **Resilient fetching** — the `NewsClient` passes gnews's built-in HTTP-429
  retry/backoff through and adds its own short retry loop for transient
  connection errors.
- **Batteries included** — a tiny CLI (`cli.py` / `ticker-news-signals`), a
  Nix dev shell + package, and a fully offline pytest suite.

## Setup

No credentials are needed. GNews is unconditionally public and regionalised
over plain RSS.

### Nix (recommended here)

```bash
nix develop                # drops you into a shell with python + deps
```

### Or plain pip

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Quickstart

```python
from ticker_news_signals.pipeline import fetch_signals

# One SignalVector per non-empty UTC hour in the last 3 hours.
vectors = fetch_signals("ETH/USD", lookback_hours=3)

for vector in vectors:
    print(vector.to_dict())
```

```json
{"article_count": 7, "novelty_flag": false, "sentiment_score": 0.42, "ticker": "ETH_USD", "timestamp": "2026-09-27T14:00:00Z"}
```

`ticker` accepts any spelling — `ETH/USD`, `eth-usd`, or a bare `ETH` — the
vector's `ticker` field is always the normalised underscore form `ETH_USD`.

### Lower-level pieces

```python
from ticker_news_signals.client import NewsClient
from ticker_news_signals.sentiment import score_headline
from ticker_news_signals.export import extract_envelope, write_jsonl

client = NewsClient(period="3h")          # keyless GNews wrapper
articles = client.search("Ethereum")      # list[NewsArticle]
crypto = client.search_by_topic()         # "DIGITAL CURRENCIES" by default

score_headline("Ethereum hits record high")   # 0.65-ish compound score

env = extract_envelope(client, "signals", ticker="ETH/USD", lookback_hours=3)
write_jsonl("signals.jsonl", env["resources"]["signals"]["signals"])
```

The extractor returns only strings, ints, bools, lists and dicts, so every
record is `json.dumps`-able by construction.

## CLI

`pull` fetches, scores, and aggregates one ticker. Output defaults to stdout
as JSONL (one signal vector per line); `--json` switches to pretty output.

```bash
python cli.py pull --ticker ETH/USD
python cli.py pull --ticker ETH/USD --lookback-hours 6
python cli.py pull --ticker BTC/USD --output signals.jsonl
python cli.py pull --ticker SOL/USD --lookback-hours 3 --json
python cli.py version
```

`--output` writes NDJSON to a file and echoes the path; with `--json` it
writes the pretty self-describing envelope (schema + exported_at + signals).

Every invocation also accepts `-v/--verbose` (debug logging), `--log-file`
(default `$XDG_STATE_HOME/ticker-news-signals/logs/ticker-news-signals.log`),
and `--no-log-file`.

## Project layout

```
ticker_news_signals/
    __init__.py    # version string, NullHandler, re-exports
    client.py      # NewsClient — thin GNews wrapper (search / search_by_topic,
                   #   retries/backoff, entry normalisation)
    models.py      # NewsArticle + SignalVector dataclasses (to_dict / from_dict)
    sentiment.py   # score_headline / score_batch (VADER compound)
    pipeline.py    # fetch_signals — gather, bucket by hour, score, aggregate
    export.py      # extraction registry: EXTRACTORS / extract / extract_envelope,
                   #   write_json / write_jsonl
    errors.py      # exception hierarchy
    logging_config.py  # structured JSON logging + secret redaction
    utils.py       # timestamp helpers (hour-bucketing, ISO conversion),
                   #   ticker normalisation
tests/             # no-network unit tests (mocked GNews)
cli.py             # tiny terminal tool (delegates to the package CLI)
flake.nix          # Nix dev shell + package
nix/               # vendored gnews / vaderSentiment derivations + package
pyproject.toml     # pip-installable package metadata
```

## Extending

The three stages — *fetch*, *score*, *aggregate* — are separate modules with
narrow interfaces, so each can be swapped:

- **new news source**: implement something with `search()` /
  `search_by_topic()` returning `NewsArticle`s (or normalise it into
  `NewsArticle.from_gnews`-style entries) and pass it as `client=` to
  `fetch_signals`.
- **new scoring model**: replace `score_headline` / `score_batch` with any
  function returning a float in the same range (FinBERT, a fine-tuned model,
  cross-lingual scorers); the pipeline never inspects per-article internals.
- **new aggregation** (topic-aware signals, weighted means, per-publisher
  trust, extra buckets): add another extractor to `EXTRACTORS` in
  `ticker_news_signals/export.py` and it is immediately available to the
  CLI's registry surface.
- **new ticker dialects**: `normalize_ticker` / `base_symbol` in `utils.py`
  own the market-spelling mapping (add e.g. `XETHZUSD` → `ETH_USD`).

## Tests

```bash
pytest                              # offline unit tests (fast, no network)
```

`tests/test_pipeline.py` mocks the GNews backend with `unittest.mock.patch`,
so every assertion about bucketing, means, counts, and novelty runs against
fixed RFC 2822 publish times — no live feed required.

## Integration

These signal vectors are consumed by the RL pipeline repo
`kraken-trading-bot` (a sibling checkout, e.g. at `~/Projects/kraken-trading-bot`)
via the seam documented in [`INTEGRATION.md`](./INTEGRATION.md): the output
schema, how the hourly timestamps align to OHLCV bar timestamps, the current
adapter stub (`extra_features_file` config flag + `merge_extra_features` in
`kraken_trading_bot/rl/data.py`), and the full wiring plan into
`train`/`backtest`.

## License

MIT