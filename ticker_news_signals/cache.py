"""TTL caches behind a single pluggable interface.

Three implementations of the synchronous :class:`Cache` protocol:

* :class:`MemoryCache` — the default. Fast, per-process, ephemeral.
* :class:`SQLiteCache` — a single-file disk cache that can be committed to
  git. Values are stored as canonical JSON (deterministic bytes for identical
  data), the database is bounded by :class:`CacheLimits`, and eviction is
  TTL-first then LRU. The file is the whole cache — no WAL/shm sidecars — so
  it stays a single small artifact on disk.
* :class:`NullCache` — stores nothing; used when caching is disabled.

The cache exists purely so repeated runs do not re-hit public feeds.
"""

from __future__ import annotations

import base64
import json
import pickle
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CacheEntry:
    value: Any
    expires_at: float


@runtime_checkable
class Cache(Protocol):
    """Minimal cache contract."""

    def get(self, key: str) -> Any | None:
        """Return the cached value, or ``None`` when absent or expired."""
        ...

    def set(self, key: str, value: Any, ttl: float) -> None:
        """Store ``value`` for ``ttl`` seconds."""
        ...

    def clear(self) -> None:
        """Drop everything."""
        ...


class MemoryCache:
    """TTL cache guarded by a lock.

    The lock matters: the orchestrator fetches many sources concurrently and
    two threads must not both miss and both fetch.
    """

    __slots__ = ("_data", "_lock")

    def __init__(self) -> None:
        self._data: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._data.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = CacheEntry(value=value, expires_at=time.monotonic() + ttl)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class NullCache:
    """A cache that stores nothing; used when caching is disabled."""

    __slots__ = ()

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        return None

    def clear(self) -> None:
        return None


# --------------------------------------------------------------------------- disk


@dataclass(frozen=True, slots=True)
class CacheLimits:
    """Hard bounds that keep the cache "not too big".

    ``max_bytes`` counts JSON payload bytes only (row overhead is a few hundred
    bytes per entry at most). Eviction runs after every write: expired entries
    first, then least-recently-used until both bounds hold.
    """

    max_entries: int = 2000
    max_bytes: int = 10 * 1024 * 1024  # 10 MiB


def _now_ms() -> float:
    # Wall clock, not monotonic: the file outlives processes, and monotonic resets.
    return time.time() * 1000.0


def _encode(value: Any) -> tuple[str, str]:
    """Canonical JSON for git-friendly, deterministic bytes."""
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True),
            "json",
        )
    except (TypeError, ValueError):
        blob = base64.b64encode(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)).decode(
            "ascii"
        )
        return blob, "pickle"


def _decode(payload: str, fmt: str) -> Any:
    if fmt == "json":
        return json.loads(payload)
    # Pickle payloads are only created by this process from trusted in-process
    # values; never point the cache at a file you do not control.
    return pickle.loads(base64.b64decode(payload.encode("ascii")))


class SQLiteCache:
    """TTL cache persisted to one SQLite file — git-trackable and bounded.

    Layout (single ``entries`` table, no sidecar files):

    * ``key``            TEXT PRIMARY KEY — source-assigned, e.g. ``rss:coindesk``.
    * ``payload``        TEXT — canonical JSON (or base64 pickle in the fallback).
    * ``payload_format`` TEXT — ``json`` or ``pickle``.
    * ``ttl``            REAL — seconds. Negative means **never expire**, zero
      expires immediately, positive expires ``ttl`` seconds after write.
    * ``stored_ms`` / ``last_used_ms`` — wall-clock millis; ``last_used_ms`` feeds LRU.
    """

    __slots__ = ("_conn", "_limits", "_lock", "_path")

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        limits: CacheLimits | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else Path("ticker_news_signals_cache.sqlite3")
        self._limits = limits if limits is not None else CacheLimits()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA auto_vacuum=FULL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS entries(
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    payload_format TEXT NOT NULL,
                    ttl REAL NOT NULL,
                    stored_ms REAL NOT NULL,
                    last_used_ms REAL NOT NULL
                )"""
            )
            self._conn.commit()

    # -- protocol -----------------------------------------------------------

    def get(self, key: str) -> Any | None:
        now = _now_ms()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, payload_format, ttl, stored_ms FROM entries WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if row["ttl"] >= 0 and row["stored_ms"] + row["ttl"] * 1000.0 <= now:
                self._conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                self._conn.commit()
                return None
            self._conn.execute("UPDATE entries SET last_used_ms = ? WHERE key = ?", (now, key))
            self._conn.commit()
            return _decode(row["payload"], row["payload_format"])

    def set(self, key: str, value: Any, ttl: float) -> None:
        payload, fmt = _encode(value)
        now = _now_ms()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO entries(
                    key, payload, payload_format, ttl, stored_ms, last_used_ms
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (key, payload, fmt, ttl, now, now),
            )
            self._prune_locked(now, protected_key=key)
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM entries")
            self._conn.commit()

    # -- administration -----------------------------------------------------

    def prune(self) -> dict[str, Any]:
        """Evict expired/beyond-limit entries now. Returns stats after pruning."""
        now = _now_ms()
        with self._lock:
            self._prune_locked(now)
            self._conn.commit()
        return self.stats()

    def stats(self) -> dict[str, Any]:
        """Current state: size, bounds, expiry breakdown. Cheap, no pruning."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(LENGTH(payload)), 0) AS bytes,
                          MIN(stored_ms) AS oldest,
                          MAX(stored_ms) AS newest
                   FROM entries"""
            ).fetchone()
            expiring = self._conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE ttl >= 0"
            ).fetchone()["n"]
            now = _now_ms()
            expired = self._conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE ttl >= 0"
                " AND stored_ms + ttl * 1000.0 <= ?",
                (now,),
            ).fetchone()["n"]
        return {
            "path": str(self._path),
            "entries": row["n"],
            "bytes": row["bytes"],
            "max_entries": self._limits.max_entries,
            "max_bytes": self._limits.max_bytes,
            "oldest_ms": row["oldest"],
            "newest_ms": row["newest"],
            "expired_now": expired,
            "expiring_entries": expiring,
        }

    def vacuum(self) -> None:
        """Reclaim file pages. Mostly useful before committing a large cache to git."""
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()
        return row["n"]

    # -- internals ----------------------------------------------------------

    def _prune_locked(self, now: float, protected_key: str | None = None) -> None:
        self._conn.execute(
            "DELETE FROM entries WHERE ttl >= 0 AND stored_ms + ttl * 1000.0 <= ?",
            (now,),
        )
        while True:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(payload)), 0) AS b FROM entries"
            ).fetchone()
            if row["n"] <= self._limits.max_entries and row["b"] <= self._limits.max_bytes:
                return
            if row["n"] > self._limits.max_entries:
                shed = row["n"] - self._limits.max_entries
            else:
                # Over the byte budget with the count fine: shed an eighth (at least
                # one) per loop rather than one row at a time.
                shed = max(1, row["n"] // 8)
            query = (
                "DELETE FROM entries WHERE key IN ("
                "SELECT key FROM entries"
                + (" WHERE key != ?" if protected_key is not None else "")
                + " ORDER BY last_used_ms ASC LIMIT ?)"
            )
            params: tuple[Any, ...] = (
                (protected_key, shed) if protected_key is not None else (shed,)
            )
            cur = self._conn.execute(query, params)
            # A single oversized entry cannot be shed; stop rather than loop forever.
            if cur.rowcount == 0:
                return


__all__ = [
    "Cache",
    "CacheEntry",
    "CacheLimits",
    "MemoryCache",
    "NullCache",
    "SQLiteCache",
]
