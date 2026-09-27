# Integration with `kraken-trading-bot`

This document is the integration seam between **ticker-news-signals** (this
repo, the producer) and the RL pipeline repo
**`kraken-trading-bot`** (the consumer). It pins down the exact data shape,
how the two time models line up, how the consumer currently reads us, and
what a full wiring pass looks like.

## Output schema

`cli.py pull` writes JSONL — one record per **per-(ticker, UTC hour)**
signal vector. Every record round-trips through `json.dumps` (see
`SignalVector.to_dict()` in `ticker_news_signals/models.py`):

```json
{
  "ticker": "ETH_USD",
  "timestamp": "2026-09-27T20:00:00Z",
  "sentiment_score": -0.1132,
  "article_count": 2,
  "novelty_flag": true
}
```

| Field             | Type    | Meaning                                                                         |
| ----------------- | ------- | ------------------------------------------------------------------------------- |
| `ticker`          | string  | Normalised pair key (`ETH/USD` → `ETH_USD`). Always the underscore form.         |
| `timestamp`       | string  | Start of the UTC hour bucket, ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`). One per line.   |
| `sentiment_score` | float   | Mean VADER compound score (−1.0 … +1.0) across that hour's headlines (0.0 when none). |
| `article_count`   | int     | Number of headlines bucketed into that hour.                                   |
| `novelty_flag`    | bool    | True if the hour contains an article newer than ~15 minutes (recency signal).   |

Timestamps are produced by `floor_to_hour()` in
`ticker_news_signals/utils.py`: every article time is snapped to the start
of its UTC hour (`HH:00:00`). No records are emitted for empty hours, so a
file is a *sparse* hourly series per ticker.

## Alignment to OHLCV bar timestamps

`kraken-trading-bot` builds its OHLCV frames in
`kraken_trading_bot/rl/data.py`: `fetch_ohlc_dataframe()` returns a pandas
DataFrame **indexed by UTC bar time** (bar open time, from
`candles_to_dataframe()`, `pd.to_datetime(time, unit="s", utc=True)`).

With the default `ohlcv_interval_minutes: 60`, bars open exactly on the
hour — so an OHLCV bar indexed at `2026-09-27 20:00:00` and a signal vector
with `timestamp` `2026-09-27T20:00:00Z` refer to the **same instant**, and a
plain index-to-index left-join on the (already aligned) datetimes is
correct.

For any other interval, floor the bar index to the hour before joining:
`df.index.floor("h")`. Because both sides are TZ-aware UTC datetimes, no
timezone shifting is needed; if a bar ever arrives with a naive index,
cast it to UTC first.

## How the consumer uses this today (integration STUB)

The consumer currently ships a **seam**, not a full implementation. In
`kraken-trading-bot`:

1. **Config flag** — `configs/default.yaml` gains an optional
   `extra_features_file` key:

   ```yaml
   extra_features_file: null   # path to a signal JSONL, or null to disable
   ```

2. **Adapter stub** — `kraken_trading_bot/rl/data.py` adds
   `merge_extra_features(df, extra_features_file=None)` on the tail of
   `fetch_ohlc_dataframe()`. With `extra_features_file=None` it returns the
   frame unchanged; the merge logic is a `TODO(next pass)` note. When a
   path is set, `fetch_ohlc_dataframe(...)` accepts it and routes the
   frame through the stub before returning.

3. **Plumbing** — `kraken_trading_bot/rl/train.py` reads
   `cfg.get("extra_features_file")` from the training config and passes it
   into `fetch_ohlc_dataframe(...)`.

Today the flag defaults to `null`, so the pipeline behaviour is unchanged —
this is purely the interface contract the full implementation will fill.

## Full wiring (follow-up pass)

To go from seam to working feature, the next pass in `kraken-trading-bot`
would implement the TODO inside `merge_extra_features`:

1. **Read** the JSONL lazily (`pd.read_json(path, lines=True)`), matching
   records to the frame's ticker (`ETH/USD` ↔ `ETH_USD` via the existing
   `normalize_ticker_id()`).
2. **Join** by floored timestamp: parse `timestamp` to TZ-aware UTC, set it
   as the index, `df.index = df.index.floor("h")`, then
   `df.join(signals, how="left")` (or `merge` on the index). Missing hours
   become `NaN` columns (or forward-fill for late/absent pulls).
3. **Flow through features** — `FeaturePipeline.compute()` already
   tolerates extra columns (microstructure checks `spread`/`bid`/`ask`
   presence; unknown columns pass through untouched), so
   `sentiment_score`, `article_count`, `novelty_flag` become additional
   observation inputs without changing group dimensions.
4. **Normalization** — because `prepare_episode()`/`FeaturePipeline.fit()`
   compute per-ticker mean/std over the *full merged frame*,
   `normalization.npz` automatically includes the new columns' stats, so
   training and backtest/paper-trade normalization stay consistent.
5. **Config** — a per-model override `extra_features_file: signals/eth_usd.jsonl`
   in `models/ETH_USD/{model_name}/config.yaml` selects the file per run.

## Refresh cadence

`cli.py pull` is a one-shot command — there is no daemon. Common patterns:

```bash
# Manual
python cli.py pull --ticker ETH/USD --output signals/eth_usd.jsonl

# Hourly cron (appends: the file is JSONL, so subsequent pulls may be
# concatenated; the consumer dedupes by join key)
0 * * * * cd ~/Projects/ticker-news-signals && \
  python cli.py pull --ticker ETH/USD --output signals/eth_usd.jsonl >> signals/eth_usd.jsonl
```

Because records are keyed by `(ticker, timestamp)`, re-pulling a given
hour is idempotent for the consumer even if the same hour is written twice;
the left-join simply keeps the last value.