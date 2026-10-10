"""Offline tests for the cache implementations."""

from __future__ import annotations

import json

from ticker_news_signals.cache import CacheLimits, MemoryCache, NullCache, SQLiteCache


class TestMemoryCache:
    def test_get_missing(self) -> None:
        assert MemoryCache().get("nope") is None

    def test_round_trip(self) -> None:
        cache = MemoryCache()
        cache.set("k", {"a": [1, 2]}, 60)
        assert cache.get("k") == {"a": [1, 2]}

    def test_expiry(self) -> None:
        cache = MemoryCache()
        cache.set("k", "v", -1)  # already expired
        assert cache.get("k") is None

    def test_clear(self) -> None:
        cache = MemoryCache()
        cache.set("k", "v", 60)
        cache.clear()
        assert cache.get("k") is None


class TestNullCache:
    def test_stores_nothing(self) -> None:
        cache = NullCache()
        cache.set("k", "v", 60)
        assert cache.get("k") is None
        cache.clear()


class TestSQLiteCache:
    def test_round_trip_and_ttl(self, tmp_path) -> None:
        path = tmp_path / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.set("key", {"nested": [1, 2, 3]}, 60)
        assert cache.get("key") == {"nested": [1, 2, 3]}
        cache.set("expired", "x", 0)
        assert cache.get("expired") is None
        cache.close()

    def test_negative_ttl_never_expires(self, tmp_path) -> None:
        cache = SQLiteCache(tmp_path / "c.sqlite3")
        cache.set("immortal", {"keep": True}, -1)
        assert cache.get("immortal") == {"keep": True}
        cache.close()

    def test_single_file_no_sidecars(self, tmp_path) -> None:
        path = tmp_path / "c.sqlite3"
        cache = SQLiteCache(path)
        cache.set("k", "v", 60)
        cache.close()
        siblings = [p.name for p in tmp_path.iterdir()]
        assert siblings == ["c.sqlite3"]

    def test_deterministic_bytes(self, tmp_path) -> None:
        """Canonical JSON: identical data is identical payload bytes."""
        path = tmp_path / "c.sqlite3"
        cache = SQLiteCache(path)
        cache.set("k", {"b": 1, "a": 2}, 60)
        cache.close()
        conn = __import__("sqlite3").connect(str(path))
        payload = conn.execute("SELECT payload FROM entries WHERE key='k'").fetchone()[0]
        assert payload == json.dumps({"a": 2, "b": 1}, sort_keys=True, separators=(",", ":"))
        conn.close()

    def test_size_bounds_and_lru(self, tmp_path) -> None:
        cache = SQLiteCache(
            tmp_path / "c.sqlite3",
            limits=CacheLimits(max_entries=2, max_bytes=1024 * 1024),
        )
        for i in range(4):
            cache.set(f"k{i}", {"i": i}, 60)
        # least-recently-used shed first; only the two newest remain
        assert len(cache) == 2
        assert cache.get("k0") is None
        assert cache.get("k1") is None
        assert cache.get("k2") == {"i": 2}
        assert cache.get("k3") == {"i": 3}
        cache.close()

    def test_stats_and_prune(self, tmp_path) -> None:
        import sqlite3

        cache = SQLiteCache(tmp_path / "c.sqlite3")
        cache.set("live", "y", 3600)
        # Insert an expired entry directly (a ttl=0 set would prune it instantly).
        with sqlite3.connect(str(cache._path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entries(key, payload, payload_format, ttl, stored_ms, last_used_ms)"
                " VALUES ('expired', 'x', 'json', 1.0, ?, ?)",
                (0.0, 0.0),
            )
        stats = cache.stats()
        assert stats["entries"] == 2
        assert stats["expired_now"] == 1
        cache.prune()
        assert cache.stats()["entries"] == 1
        cache.close()

    def test_clear_and_vacuum(self, tmp_path) -> None:
        cache = SQLiteCache(tmp_path / "c.sqlite3")
        cache.set("k", "v", 60)
        cache.vacuum()
        cache.clear()
        assert len(cache) == 0
        cache.close()
