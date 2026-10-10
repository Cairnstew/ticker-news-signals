"""Capabilities a news source can supply.

A *capability* is one unit of data a provider may be able to deliver. The
orchestrator asks every registered provider which capabilities it declares
(:attr:`~ticker_news_signals.providers.base.NewsProvider.supports`) and only
ever calls the matching methods, so adding a provider is a registry entry and
an adapter module — no orchestrator change.

Two capabilities are defined today:

``NEWS``
    Ticker-relevant headlines (a keyword/symbol query). Every real source
    implements this.

``TRENDING``
    Broad, ticker-agnostic headlines for the domain (a "top stories" or
    section feed). Providers that only offer a search endpoint simply do not
    declare it; its absence never invalidates a result.

``HISTORY``
    Ticker-relevant headlines *as of a past time*, reconstructed from an
    archive (e.g. Wayback Machine feed snapshots). History providers answer
    the same question the live pipeline answers, one axis over — seeing them
    in ``sources`` is informative but the live pipeline never calls them.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """A unit of data a news provider may be able to supply."""

    NEWS = "news"
    """Ticker-relevant headlines for a caller-supplied query."""

    TRENDING = "trending"
    """Broad, ticker-agnostic headlines for the whole news domain."""

    HISTORY = "history"
    """Ticker-relevant headlines reconstructed as of a past time."""


#: Capabilities whose absence annotates a result but never invalidates it.
OPTIONAL_CAPABILITIES: frozenset[Capability] = frozenset({Capability.TRENDING, Capability.HISTORY})


__all__ = ["OPTIONAL_CAPABILITIES", "Capability"]
