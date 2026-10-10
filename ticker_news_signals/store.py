"""The user-facing query store: save/load whole fetches by ticker key.

This is the "database output infrastructure" counterpart of tickerkit's
:mod:`tickerkit.store`. It layers ticker-shaped keys and full model
round-tripping (:class:`SignalResult` / :class:`HistoryResult`) over the
low-level :class:`~ticker_news_signals.cache.SQLiteCache`, and it *also*
speaks the :class:`~ticker_news_signals.cache.Cache` protocol, so
``pipeline(cache=QueryCache("..."))`` persists provider calls automatically
on the same file you save queries to.

Save a pull, load it back as a real model — no re-fetching, no rate-limit
spend:

    from ticker_news_signals.pipeline import fetch_result
    from ticker_news_signals.store import QueryCache

    result = fetch_result("ETH/USD")
    qc = QueryCache("queries.sqlite3")          # git-trackable, same SQLite layout
    qc.save_signal("ETH", result)
    again = qc.load_signal("ETH")                # a real SignalResult

The file is the whole store — one ``entries`` table with ``query:...`` keys —
so ``ticker-news-signals cache stats`` on the same path works unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache import CacheLimits, SQLiteCache
from .models import HistoryResult, SignalResult


class QueryCache:
    """The user-facing query store for whole ticker queries.

    Same SQLite backing as :class:`SQLiteCache`, but aimed at application
    code: you open one with a path inside your own repo, save complete
    signal/history results under their natural ticker keys, and load them
    back as real model objects (:class:`SignalResult` /
    :class:`HistoryResult`) — no re-fetching, no rate-limit spend. The file
    is git-trackable, so a repo can commit it and replay queries offline.

    It *also* speaks the low-level :class:`Cache` protocol
    (``get``/``set``/``clear``), so passing it where a cache is expected
    persists provider calls automatically on the same file.

    Default TTL for a saved query is **never expires** (``ttl=-1``) — that is
    what you want for a committed artifact. Pass a positive ``ttl`` to expire
    it later.

    Usable as a context manager: ``with QueryCache("c.sqlite3") as qc:``
    closes the underlying database on exit.
    """

    __slots__ = ("_db",)

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        limits: CacheLimits | None = None,
    ) -> None:
        self._db = SQLiteCache(path=path, limits=limits)

    # -- Cache protocol (so pipeline(cache=QueryCache(...)) works too) ------

    def get(self, key: str) -> Any | None:
        return self._db.get(key)

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._db.set(key, value, ttl)

    def clear(self) -> None:
        self._db.clear()

    # -- whole-query API ----------------------------------------------------

    @staticmethod
    def _signal_key(ticker: str) -> str:
        return f"query:signal:{ticker.strip().upper()}"

    @staticmethod
    def _history_key(ticker: str, granularity: str, start: str | None, end: str | None) -> str:
        return (
            f"query:history:{ticker.strip().upper()}:{granularity.strip().lower()}"
            f":{start or 'any'}:{end or 'any'}"
        )

    def save_signal(
        self,
        ticker: str,
        signal: SignalResult | dict[str, Any],
        *,
        ttl: float = -1.0,
    ) -> None:
        """Persist a signal pull (or its ``to_dict()`` form) under ``ticker``."""
        data = signal.to_dict() if isinstance(signal, SignalResult) else signal
        self.set(self._signal_key(ticker), data, ttl)

    def load_signal(self, ticker: str) -> SignalResult | None:
        """Return the saved pull as a real :class:`SignalResult`, or ``None``."""
        data = self.get(self._signal_key(ticker))
        return SignalResult.from_dict(data) if data is not None else None

    def save_history(
        self,
        ticker: str,
        granularity: str,
        *,
        start: str | None = None,
        end: str | None = None,
        history: HistoryResult | dict[str, Any] | None = None,
        ttl: float = -1.0,
    ) -> None:
        """Persist a history result (or its ``to_dict()`` form) under its key.

        ``start``/``end`` identify the window in the key; pass the same values
        to :meth:`load_history`. ``history`` is the result to store; it is
        required.
        """
        if history is None:
            raise ValueError("save_history requires history=<result>")
        data = history.to_dict() if isinstance(history, HistoryResult) else history
        self.set(self._history_key(ticker, granularity, start, end), data, ttl)

    def load_history(
        self,
        ticker: str,
        granularity: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> HistoryResult | None:
        """Return the saved history for that window as a :class:`HistoryResult`."""
        data = self.get(self._history_key(ticker, granularity, start, end))
        return HistoryResult.from_dict(data) if data is not None else None

    # -- administration -----------------------------------------------------

    def prune(self) -> dict[str, Any]:
        """Evict expired/beyond-limit entries now. Returns stats after pruning."""
        return self._db.prune()

    def stats(self) -> dict[str, Any]:
        """Current state: size, bounds, expiry breakdown. Cheap, no pruning."""
        return self._db.stats()

    def vacuum(self) -> None:
        """Shrink the file (auto_vacuum is already on; this is the explicit path)."""
        self._db.vacuum()

    def close(self) -> None:
        """Close the underlying database."""
        self._db.close()

    def __len__(self) -> int:
        return len(self._db)

    def __enter__(self) -> QueryCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["QueryCache"]
