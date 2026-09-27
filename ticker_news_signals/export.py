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
"""

from __future__ import annotations

import json
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


def write_jsonl(path: str | Path, documents: list[dict[str, Any]] | Any) -> Path:
    """Write an iterable of documents as NDJSON (one compact object per line)."""
    path = Path(path)
    with path.open("w", encoding="utf-8") as fh:
        for document in documents:
            fh.write(dumps(document) + "\n")
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