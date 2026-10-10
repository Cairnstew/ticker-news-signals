"""Provider registry.

Adding a provider is a one-line change here plus the adapter module. The
orchestrator discovers capabilities from the registry, so nothing else needs
editing.
"""

from __future__ import annotations

from .base import NewsProvider, ProviderContext
from .bingnews import BingNewsProvider
from .cryptopanic import CryptoPanicProvider
from .gnews import GNewsProvider
from .rss import RssProvider

#: Default provider set. Order is resolution-priority / display order only —
#: the orchestrator fans out over all of them concurrently.
_DEFAULT: list[type[NewsProvider]] = [
    GNewsProvider,
    RssProvider,
    BingNewsProvider,
    CryptoPanicProvider,
]

#: The live registry. Mutated in place by :func:`register`.
PROVIDERS: list[type[NewsProvider]] = list(_DEFAULT)

_BY_NAME: dict[str, type[NewsProvider]] = {cls.name: cls for cls in _DEFAULT}


def registry() -> tuple[type[NewsProvider], ...]:
    """All registered providers."""
    return tuple(PROVIDERS)


def get(name: str) -> type[NewsProvider]:
    """Look up a provider class by name."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(_BY_NAME)}") from None


def register(cls: type[NewsProvider]) -> None:
    """Register (or replace) a provider class at runtime."""
    PROVIDERS[:] = [p for p in PROVIDERS if p.name != cls.name] + [cls]
    _BY_NAME[cls.name] = cls


def select(names: tuple[str, ...]) -> tuple[type[NewsProvider], ...]:
    """Return the registered providers named in ``names``.

    An empty ``names`` means "all registered providers". Unknown names raise
    :class:`KeyError` so a typo in ``TNS_SOURCES`` is caught, not ignored.
    """
    if not names:
        return registry()
    return tuple(get(name) for name in names)


__all__ = [
    "PROVIDERS",
    "BingNewsProvider",
    "CryptoPanicProvider",
    "GNewsProvider",
    "NewsProvider",
    "ProviderContext",
    "RssProvider",
    "get",
    "register",
    "registry",
    "select",
]
