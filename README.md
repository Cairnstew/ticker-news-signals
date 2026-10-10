# ticker-news-signals

A **standalone, multi-source crypto news-sentiment pipeline**: pull headlines
from many keyless sources at once, de-duplicate them across feeds, score
sentiment locally, and aggregate into per-ticker, per-hour **signal vectors**
written as NDJSON — ready to feed a trading strategy or a dashboard.

No API keys required. No daemon. One command, one file — or a library call
that returns the vectors *and* the story of how they were built.

```console
$ ticker-news-signals pull --ticker ETH/USD --report
```

## Features

- **Multi-source by default.** Headlines are gathered concurrently from
  GNews (keyless Google News RSS), a per-ticker **Bing News** search feed,
  and a curated set of ~18 keyless crypto RSS feeds (CoinDesk,
  Cointelegraph, Decrypt, The Block, CryptoSlate, Bitcoin Magazine,
  BeInCrypto, NewsBTC, Bitcoinist, U.Today, ZyCrypto, CryptoNews, CoinGape,
  Bitcoin.com News, Coinspeaker, CryptoBriefing, TheNewCrypto, Forkast).
  Redundant independent publishers are deliberate: a story covered by several
  sources is reconciled into one well-covered article, and a single feed
  going down never empties an hour. Optional providers light up when a
  credential is present (CryptoPanic via `TNS_CRYPTOPANIC_TOKEN`); a missing
  credential **skips with a recorded reason**, never fails the run.
- **Cross-source reconciliation.** The same story re-published across feeds
  collapses into one article (URL + tracking-parameter aware), while *every*
  source that carried it is remembered — so each hour's vector reports how
  broadly a story was covered, not how many copies were fetched.
- **Historical reconstruction.** `history` answers the pipeline's question one
  axis in the past, rebuilding per-bucket signal vectors from the Wayback
  Machine's archived RSS feeds (keyless) with honest per-source coverage —
  a sparse archive says how many buckets it could answer instead of pretending
  to a full graph.
- **A query store.** `QueryCache` saves whole pulls (`SignalResult`) and
  history results (`HistoryResult`) to a git-trackable SQLite file keyed by
  ticker, and loads them back as real model objects — replay offline without
  burning rate-limit quota. It also speaks the cache protocol, so one file can
  hold both the provider cache and the saved queries.
- **Nothing is silent.** Every result carries a `data_quality` block:
  provenance (which source, when, cached?), tolerated errors, rate-limit
  records with a remedy hint, skipped sources, missing-field explanations,
  and duplicate counts. A dead feed annotates the result; it never empties it.
- **Reliability plumbing.** Per-source rate limiters, jittered exponential
  retry for only `429/5xx`/timeouts, `Retry-After` honouring (capped), a
  per-source **circuit breaker** so one throttled feed never triggers a
  stampede, and a TTL cache (in-memory or a git-trackable SQLite file).
- **Local sentiment, pluggable.** VADER by default; a crypto-domain lexicon
  backend for vocabulary VADER reads as neutral ("hack", "rally",
  "liquidation"). An `ensemble` backend averages the independent scorers and
  reports how far they disagreed on each headline (`scorer_std` in the
  extended record) — the sentiment-side mirror of cross-source news
  reconciliation, so a *contested* hour is visible instead of silently
  averaged away. A dependency-gated `cryptobert` transformer backend (MIT,
  crypto-tuned) is available via the optional `[ml]` extra. Backends are a
  one-line registry entry.
- **Canonical output, preserved.** The JSONL schema behind the consumer
  contract (`ticker`, `timestamp`, `sentiment_score`, `article_count`,
  `novelty_flag`) is unchanged. Richer fields (cross-source counts,
  sentiment spread, sources, top headlines) are available via `--extended`
  or the full result envelope.
- **Offline test suite.** 130+ tests, all mocked — CI and `pytest` never
  touch the network.

## Setup

No credentials are needed. GNews and the RSS feeds are unconditionally public.

### Nix (recommended here)

```bash
nix develop                # python + all deps + pytest + ruff
```

### Or plain pip

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11+. Runtime dependencies: `gnews`, `vaderSentiment`, `httpx`,
`feedparser`. The `dev` extra adds pytest, respx, ruff, pyright.

## Quickstart

```python
from ticker_news_signals.pipeline import fetch_signals, fetch_result

# The canonical vectors only (the documented consumer contract).
vectors = fetch_signals("ETH/USD", lookback_hours=3)
for vector in vectors:
    print(vector.to_dict())
```

```json
{"article_count": 7, "novelty_flag": false, "sentiment_score": 0.42, "ticker": "ETH_USD", "timestamp": "2026-09-27T14:00:00Z"}
```

`ticker` accepts any spelling — `ETH/USD`, `eth-usd`, or a bare `ETH`.

### The full result (vectors + quality)

```python
result = fetch_result("BTC", lookback_hours=3)

result.resolved_name              # "Bitcoin"
result.vectors[0].to_record()     # canonical + source_count, sentiment_std, sources, top_headlines
result.data_quality.sources_used  # ("gnews", "rss:coindesk", ...)
result.data_quality.errors        # tolerated failures, provenance, skips, throttles
result.articles                   # the unique deduped headlines behind the signals
```

### Lower-level pieces

```python
from ticker_news_signals.sentiment import available_backends, score_headline
from ticker_news_signals.export import extract_envelope, write_jsonl

available_backends()                        # ("ensemble", "lexicon", "vader")
score_headline("Ethereum hits record high") # 0.65-ish compound score
score_headline("Rally halted after exchange halt", backend="ensemble")  # contested: scorer_std > 0

env = extract_envelope(None, "signals", ticker="ETH/USD", lookback_hours=3)
write_jsonl("signals.jsonl", env["resources"]["signals"]["signals"])
```

## CLI

```console
ticker-news-signals pull --ticker ETH/USD                  # canonical JSONL to stdout
ticker-news-signals pull --ticker BTC/USD --output signals.jsonl --append
ticker-news-signals pull --ticker BTC,ETH --report         # human report, multiple tickers
ticker-news-signals pull --ticker SOL --markdown           # fenced markdown block
ticker-news-signals pull --ticker ETH --metrics            # one flat JSON row (pipelines)
ticker-news-signals pull --ticker ETH --json               # full envelope + data_quality
ticker-news-signals pull --ticker ETH --extended           # richer JSONL records
ticker-news-signals pull --ticker ETH --sources gnews,rss --sentiment lexicon
ticker-news-signals pull --ticker ETH --show-errors        # quality block on stderr
ticker-news-signals history --ticker ETH --from 2026-01-01 --to 2026-01-07  # archived/EOD vectors
ticker-news-signals history --ticker BTC --granularity hour --from 2026-01-08 --to 2026-01-09 --json
ticker-news-signals sources                                # providers + availability + rpm
ticker-news-signals cache stats                            # disk-cache size / bounds (prune, clear, vacuum)
ticker-news-signals version
```

`--output` is **replaced** by each pull unless `--append` is given — a timer
that wants a growing log MUST pass `--append`, because the default
`--lookback-hours 1` means a replace-mode timer leaves the file one hour deep
forever. Replacing a file that already holds records prints a warning naming
how many were dropped.

### Historical (`history`)

`history` reconstructs **past** signal vectors from the Internet Archive's
captured copies of the configured RSS feeds (the Wayback Machine): `CDX`
lists the captures, the nearest one per bucket is replayed with `id_`, and the
archived feed bytes go through the same RSS parser and scoring as a live pull.
No API key; the entanglement with the live pipeline is deliberate — same
providers, same reconciliation, same sentiment backends.

```console
$ ticker-news-signals history --ticker ETH --granularity day --from 2026-01-01 --to 2026-01-03 --report
# Ethereum — historical signals
queried: ETH  granularity: day
window: 2026-01-01 00:00:00+00:00 .. 2026-01-03 00:00:00+00:00

bucket (UTC)          sentiment  spread  n  src
--------------------  ---------  ------  -  ---
2026-01-01T00:00:00Z  +0.000     -       1  1
2026-01-02T00:00:00Z  +0.340     -       1  1

## Source coverage
- wayback:coindesk: 2/3 buckets (… ) — archive covered 2/3 buckets; complete reconstruction is not possible from captured feeds
```

Coverage is honest by design, mirroring tickerkit's history contract: a sparse
archive reports exactly how many buckets it could answer (`buckets/expected`,
`missing`, gaps), never pretending to a full graph. The Wayback crawl rate
varies per feed (CoinDesk is captured ~240 minutes/day, U.Today more like a
few times a month), so `--granularity hour` works well for densely-archived
feeds and degrades gracefully everywhere else.

### Query store (`QueryCache`)

`QueryCache` is a git-trackable SQLite store for whole queries — the
"database output" companion to the JSONL files. You point it at a file inside
your own repo, save complete pulls and history results under their natural
ticker keys, and load them back as real model objects:

```python
from ticker_news_signals import QueryCache
from ticker_news_signals.pipeline import fetch_result
from ticker_news_signals.history import fetch_history

qc = QueryCache("queries.sqlite3")          # git-trackable, same SQLite layout as the cache
result = fetch_result("ETH/USD")
qc.save_signal("ETH", result)               # never-expiring by default (ttl=-1)
again = qc.load_signal("eth")               # case-insensitive; a real SignalResult

qc.save_history("ETH", "day", start="2026-01-01", end="2026-01-03",
                history=fetch_history("ETH", start="2026-01-01", end="2026-01-03"))
qc.load_history("ETH", "day", start="2026-01-01", end="2026-01-03")
```

The window (`start`/`end`) is part of the history key, so different windows
do not collide. `QueryCache` also implements the cache protocol, so you can
pass it anywhere a cache is expected — one file then holds both the automatic
provider-cache entries and your saved queries, and `ticker-news-signals cache
stats <path>` reports both. Round-trips are faithful: save a result, close the
file, reopen, and `loaded.to_dict() == original.to_dict()` (vectors, articles,
coverage, provenance and every quality record survive).

A pair of example scripts live in [`examples/`](examples/): `quickstart.py`
(one ticker, report/json/markdown) and `pipeline.py` (many tickers → CSV/JSON
metrics rows).

## Notebook

A guided, fully-live notebook lives in [`notebooks/pipeline.ipynb`](notebooks/pipeline.ipynb):
multi-source fetches, cross-source dedupe, sentiment backends, resolution,
export round-trips, metrics rows, caching, and the reliability story. It is
generated programmatically (like tickerkit's notebook tooling) so it stays a
deterministic source of truth:

```bash
uv run python tools/build_notebook.py              # regenerate notebooks/pipeline.ipynb
uv run python tools/validate_notebook.py           # execute it against the live feeds
```

Run it interactively with `jupyter lab` or open it in Zed (see below). The
`dev` extra installs `nbformat`/`nbclient`/`ipykernel`; the notebook depends
only on the package itself — no pandas required.

### Opening it in Zed

Zed discovers notebook kernels through `jupyter kernelspec list`, so a
notebook with no registered kernel just reports "no kernel".
`tools/install_kernel.py` writes a `ticker-news-signals` kernelspec whose
`argv` names the project interpreter **by absolute path** (`.venv/bin/python`,
or the `nix develop` python). The venv's own `python3` spec is not enough: its
`argv` is a bare `python`, resolved through `PATH`, so which interpreter
actually runs depends on how the editor was launched — and a cell can end up
importing a different `ticker-news-signals`, or none, than the one you are
testing.

```bash
python tools/install_kernel.py          # from a venv or `nix develop`
nix run .#kernel                        # same thing, via the flake
```

[`.zed/settings.json`](.zed/settings.json) then pins Python cells to that
kernel via `jupyter.kernel_selections`. Re-run `install_kernel.py` after any
recreation of the virtualenv, and `nix run .#notebook` (or
`tools/validate_notebook.py`) executes the notebook under that same kernel so
what Zed runs is exactly what CI tested.

## Output shapes

The **canonical JSONL record** is the stable contract documented in
[`INTEGRATION.md`](INTEGRATION.md):

```json
{"ticker": "ETH_USD", "timestamp": "2026-09-27T20:00:00Z", "sentiment_score": -0.1132, "article_count": 2, "novelty_flag": true}
```

`--extended` / `to_record()` add the derived fields: `source_count` (how many
sources covered the hour), `sentiment_std` (dispersion across the hour's
headlines — low = agreement, high = contested), `sources`, and
`top_headlines` (the most polarising headlines that drove the mean).

`--json` and `fetch_result().to_dict()` return the full self-describing
envelope: resolved identity, the extended signals, and the `data_quality`
block. `--metrics` returns one flat scalar row per ticker with nulls for
missing values — append rows straight into a DataFrame or a CSV.

Timestamps are UTC-hour-floored (`HH:00:00Z`); no records are emitted for
empty hours; every article time is snapped with `floor_to_hour()`.

## Reliability

- **Per-provider rate limiters** — GNews/RSS share a safety-net ceiling;
  CryptoPanic self-throttles to a gentle 30/min (its free tier is ~100/day).
- **Throttling is identified, not just retried.** A 429 becomes a structured
  `RateLimitExceeded` carrying the source, the advertised `Retry-After`, quota
  headers and a concrete remedy. It surfaces on the result as
  `data_quality.rate_limited`, so a pipeline can react without
  string-matching exception names.
- **A per-source circuit breaker.** Once a source is genuinely throttled its
  remaining calls short-circuit immediately (no doomed requests), and the
  breaker is private per source — one throttled feed never blocks the others.
- **Retry is narrow on purpose** — only `408/425/429/5xx` and transport
  timeouts, with jittered backoff. A `400/401/403/404` is a caller or
  credential problem; retrying it only burns quota.
- **Partial-failure tolerance.** Every `(source, capability)` result is
  independent and concurrent. A failure becomes a `SourceErrorRecord` on the
  quality block; it never propagates.
- **TTL cache** — MemoryCache by default, SQLiteCache when
  `TNS_CACHE_PATH` is set (single file, git-trackable, bounded by entries and
  bytes, deterministic JSON bytes, `cache stats/prune/vacuum` to manage).

## Configuration

Everything is environment-driven with sane defaults (prefixed `TNS_`; the
legacy `TICKER_NEWS_LANGUAGE`/`COUNTRY`/`PERIOD` still shadow for back-compat).
See [`.env.example`](.env.example) for the full list. The important ones:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TNS_SOURCES` | all | comma-separated provider names to use |
| `TNS_SENTIMENT` | `vader` | sentiment backend (`vader`, `lexicon`, `ensemble`; plus `cryptobert` with `[ml]`) |
| `TNS_CRYPTOPANIC_TOKEN` | — | enables the CryptoPanic provider |
| `TNS_RSS_FEEDS` | curated set | `name=url` feeds for the rss provider |
| `TNS_LOOKBACK_HOURS` | `1` | default aggregation window |
| `TNS_NOVELTY_MINUTES` | `15` | age that marks an hour as novel |
| `TNS_RATE_LIMIT` | `60` | per-source requests/minute safety net |
| `TNS_CACHE_PATH` | — | git-trackable SQLite cache file |

Programmatic overrides use the immutable `Config`:

```python
from ticker_news_signals.config import Config

config = Config.from_env().with_overrides(sentiment_backend="lexicon", sources=("gnews", "rss"))
result = fetch_result("ETH", lookback_hours=3, config=config)
```

## Symbol resolution

Tickers are normalised to a base asset (`ETH/USD`, `eth-usd`, `ETH:USD` and
`ETH` all resolve to `ETH`), and a built-in symbol map gives the majors a
human name and search terms (`BTC` → `Bitcoin`, `BCH` → `Bitcoin Cash`, …).
Unknown symbols resolve to their bare base — that is not an error, the RSS
filter just falls back to the symbol itself. `resolve_ticker()` /
`ResolvedTicker` are public.

## Extending — adding a source

The provider registry is the seam; the orchestrator discovers capabilities, so
nothing in `pipeline.py` changes:

```python
from ticker_news_signals.capabilities import Capability
from ticker_news_signals.providers import register
from ticker_news_signals.providers.base import NewsProvider


def _article_from(entry, *, source):
    # normalise API JSON into a models.NewsArticle
    ...

class MyFeed(NewsProvider):
    name = "myfeed"
    supports = frozenset({Capability.NEWS})

    @classmethod
    def available(cls, config):
        if not config.extra.get("MYFEED_KEY"):
            return False, "set MYFEED_KEY to enable"
        return True, None

    def fetch_news(self, asset, *, max_results):
        data = self.ctx.http.get_json("https://api.myfeed/headlines", params={"q": asset.symbol})
        self.ctx.record("myfeed:news", source=self.name, url="https://api.myfeed/headlines",
                        article_count=len(data))
        return tuple(_article_from(entry, source=self.name) for entry in data[:max_results])

register(MyFeed)
```

Inside an adapter, `self.ctx` provides `config`, `http` (rate-limited,
retrying, with a circuit breaker, and `get_json`/`get_text`/`get_bytes`),
`cache`, `throttle()`, `record()`, `note_missing()` and `note_error()`. Call
`self.ctx.throttle()` before a request if you declare a tighter
`rate_limit_rpm`.

Sentiment backends are equally pluggable:

```python
from ticker_news_signals.sentiment import register_backend

register_backend("mybert", lambda text: float(predict(text)))
```

The optional `cryptobert` backend (Hugging Face `ElKulako/cryptobert`, MIT) is
dependency-gated the way credential-gated providers are: install
`pip install -e ".[ml]"` and it will appear in `available_backends()`;
otherwise it is simply absent — nothing breaks. Model weights (~500 MB) are
downloaded lazily on first use, never at import.

## Tests

```bash
pytest                  # offline: everything is mocked (no network)
nix run --no-write-lock-file nixpkgs#ruff -- check ticker_news_signals/ cli.py tests/
nix run --no-write-lock-file nixpkgs#pyright -- --pythonpath .venv/bin/python ticker_news_signals
```

`tests/` covers the orchestrator (fake providers, partial failures, coverage
counts), the HTTP layer (retry semantics, `Retry-After` parsing, circuit
breaker), cache TTL/LRU/bytes determinism, the providers (respx-mocked RSS /
CryptoPanic / GNews / Bing News), reconciliation, config, render and CLI.

## Integration

The canonical signal vectors are consumed by the RL pipeline repo
`kraken-trading-bot` via the seam documented in
[`INTEGRATION.md`](./INTEGRATION.md): the output schema, how the hourly
timestamps align to OHLCV bar timestamps, and the full wiring plan into
`train`/`backtest`. The canonical JSONL shape is unchanged in this release;
`--extended`/`--metrics` add fields the consumer's merge simply passes
through.

## Project layout

```
ticker_news_signals/
    __init__.py      # public API surface
    cli.py           # pull / sources / cache / version commands
    config.py        # env-driven immutable Config (+ FeedSpec)
    errors.py        # exception hierarchy (RateLimitExceeded carries structured detail)
    http.py          # per-source rate limiter, retry, circuit breaker
    cache.py         # Cache protocol: Memory / SQLite (git-trackable) / Null
    models.py        # NewsArticle, SignalVector, DataQuality, SignalResult
    sentiment.py     # scorer registry: vader + lexicon + ensemble, register_backend()
    sentiment_ml.py  # optional cryptoBERT transformer backend (dependency-gated, [ml] extra)
    resolve.py       # ticker -> ResolvedTicker (name, search terms)
    reconcile.py     # cross-source dedupe + source coverage + dispersion
    history.py       # archived/wayback pipeline: coverage + per-source series
    pipeline.py      # orchestrator: concurrent providers -> vectors + quality
    render.py        # human report / markdown / flat metrics
    export.py        # extraction registry + JSONL/json writers (append-aware)
    store.py         # QueryCache: git-trackable whole-query store (save/load pulls)
    providers/
        base.py          # NewsProvider contract + ProviderContext
        gnews.py         # keyless Google News RSS
        bingnews.py      # keyless Bing News search RSS (per-ticker + trending)
        rss.py           # configurable keyless feeds, ticker-filtered
        wayback.py       # HISTORY: reconstruct headlines from archived RSS
        cryptopanic.py   # optional, credential-gated
tests/               # offline suite (fakes + respx)
examples/            # quickstart.py, pipeline.py
notebooks/           # pipeline.ipynb (generated by tools/build_notebook.py)
tools/               # build_notebook.py, validate_notebook.py, install_kernel.py
.zed/settings.json   # pins the notebook kernel for the Zed editor
nix/                 # vendored gnews/vader derivations + package
flake.nix            # dev shell + package + kernel/notebook apps
```

## License

MIT