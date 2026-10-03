"""Data extraction infra: pull signal vectors as clean JSON.

Mirrors the ``kraken_api.export`` registry pattern: one registry mapping a
resource name to a fetch function that returns a JSON-serialisable dict,
plus :func:`extract` / :func:`extract_envelope` entry points and the
:func:`write_json` / :func:`write_jsonl` persistence helpers.

Everything returned here round-trips through ``json.dumps``: no datetimes,
no Decimals, no surprise keys.

    from ticker_news_signals.client import NewsClient
    from ticker_news_signals.export import extract_envelope, write_jsonl

    client = NewsClient()
    doc = extract(client, "signals", ticker="ETH/USD", lookback_hours=1)
    env = extract_envelope(client, "signals", ticker="ETH/USD")
    write_jsonl("signals.jsonl", env["resources"]["signals"]["signals"])
    # ...or into a growing history log (what the hourly timer does):
    write_jsonl(
        "signals.jsonl", env["resources"]["signals"]["signals"], append=True
    )
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .utils import normalize_ticker

SCHEMA_VERSION = "ticker-news-signals/1"

# An extractor: (source, **opts) -> JSON-ready dict.
Extractor = Callable[..., dict[str, Any]]


# ---------------------------------------------------------------------- #
# Per-resource extractors
# ---------------------------------------------------------------------- #


def _signals(client: Any, ticker: str = "", lookback_hours: int = 1, **opts: Any) -> dict[str, Any]:
    from .pipeline import fetch_signals

    vectors = fetch_signals(ticker, lookback_hours=lookback_hours, client=client)
    return {
        "ticker": normalize_ticker(ticker),
        "lookback_hours": int(lookback_hours),
        "signals": [v.to_dict() for v in vectors],
    }


# ---------------------------------------------------------------------- #
# Registry
# ---------------------------------------------------------------------- #

# name -> (extractor, one-line help).
EXTRACTORS: dict[str, tuple[Extractor, str]] = {
    "signals": (_signals, "per-ticker per-hour sentiment vectors (ticker=, lookback_hours=)"),
}


# ---------------------------------------------------------------------- #
# Entry points
# ---------------------------------------------------------------------- #


def extract(source: Any, resource: str, **opts: Any) -> dict[str, Any]:
    """Pull one resource as a clean JSON-ready dict.

    ``opts`` are passed to the extractor (see the registry entries above);
    unsupported options are ignored by the extractor's ``**opts``.
    """
    if resource not in EXTRACTORS:
        known = ", ".join(sorted(EXTRACTORS))
        raise KeyError(f"unknown resource {resource!r}; known resources: {known}")
    fn, _help = EXTRACTORS[resource]
    return fn(source, **opts)


def extract_envelope(source: Any, resource: str, **opts: Any) -> dict[str, Any]:
    """Pull one resource and wrap it in the self-describing envelope."""
    if resource not in EXTRACTORS:
        known = ", ".join(sorted(EXTRACTORS))
        raise KeyError(f"unknown resource {resource!r}; known resources: {known}")
    data: dict[str, Any] = {}
    data[resource] = EXTRACTORS[resource][0](source, **opts)
    return _envelope(data)


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "resources": data,
    }


# ---------------------------------------------------------------------- #
# Persistence / streaming
# ---------------------------------------------------------------------- #


def dumps(document: dict[str, Any], *, pretty: bool = False, sort_keys: bool = True) -> str:
    """JSON-dump a document with the extraction layer's defaults."""
    return json.dumps(
        document,
        indent=2 if pretty else None,
        sort_keys=sort_keys,
        ensure_ascii=False,
        separators=None if pretty else (",", ":"),
    )


def write_json(document: dict[str, Any], path: str | Path) -> Path:
    """Write a document (usually an envelope from :func:`extract_envelope`) to ``path``."""
    path = Path(path)
    path.write_text(dumps(document, pretty=True) + "\n", encoding="utf-8")
    return path


def _existing_records(path: Path) -> tuple[int, bool]:
    """``(record_count, ends_with_newline)`` for a file about to be appended.

    Read once, and only when the file is already there: the append path is
    the hot one (a timer fires it hourly) and re-stat-ing per record would
    be silly, while the replace path pays one read it is about to throw
    away anyway.

    ``ends_with_newline`` is what keeps an append from gluing its first
    record onto a half-written last line.  A file cut short mid-record
    would otherwise yield ONE line holding TWO records, which every
    consumer's ``json.loads`` loop then skips -- losing both while
    reporting neither.
    """
    if not path.is_file():
        return 0, True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # unreadable: treat as empty rather than lose the write
        return 0, True
    if not text:
        return 0, True
    return len(text.splitlines()), text.endswith("\n")


def write_jsonl(
    path: str | Path,
    documents: list[dict[str, Any]] | Any,
    *,
    append: bool = False,
) -> Path:
    """Write an iterable of documents as NDJSON (one compact object per line).

    ``append`` is the difference between a **log** and a **snapshot**, and
    the consumer's shape makes it load-bearing.  This project is pulled by
    a timer with a one-hour ``--lookback-hours``, so:

    * ``append=True`` — one hour-floored record per pull, forever.  The file
      is a forward-only log and the file's coverage grows with uptime.
    * ``append=False`` — the file is *replaced* by this pull's window, so
      an hourly pull leaves the file one hour deep forever.  That is a
      correct answer for "give me the last hour" and **data destruction** for
      "keep a history", which is why it stays the default (back-compat for
      every existing caller) rather than becoming the append default.

    Replacing a file that already holds records is therefore never silent:
    it prints a stderr warning naming how many records were dropped and
    telling the caller about ``append=True``.  The consumer's de-duplication
    means a duplicate hour is harmless, so the *only* way to lose history
    here is the silent truncate — and it is invisible until someone reads
    the file weeks later and finds last Tuesday missing.

    Args:
        path: Destination file path.
        documents: Records to write; any iterable of JSON-ready dicts.
        append: Append to an existing file instead of replacing it.

    Returns:
        The destination path, so callers can chain or report it.
    """
    path = Path(path)
    records = list(documents)
    dropped, ends_clean = _existing_records(path)
    mode = "a" if append else "w"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        if append and dropped and not ends_clean:
            fh.write("\n")
        for document in records:
            fh.write(dumps(document) + "\n")
    if dropped and not append:
        print(
            f"warning: {path} already held {dropped} record(s); this pull "
            f"REPLACED them with {len(records)} record(s). Pass --append to "
            f"keep the history — the consumer dedupes repeated hours, so "
            f"appending is always safe.",
            file=sys.stderr,
        )
    return path


__all__ = [
    "SCHEMA_VERSION",
    "EXTRACTORS",
    "extract",
    "extract_envelope",
    "dumps",
    "write_json",
    "write_jsonl",
]