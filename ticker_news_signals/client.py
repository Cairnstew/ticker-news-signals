"""Thin wrapper around GNews (keyless Google News RSS).

The :class:`NewsClient` hides the GNews backend behind two query methods
(:meth:`search` / :meth:`search_by_topic`) and normalises every raw RSS
entry dict into a typed :class:`~ticker_news_signals.models.NewsArticle`.

Retry / backoff
---------------
gnews 0.8 already retries HTTP 429 responses internally with exponential
backoff (``max_retries`` / ``retry_backoff_base`` / ``retry_backoff_max``),
so those knobs are passed straight through when the installed version
supports them.  On top of that, this client keeps its own small retry loop
for transient connection-level failures, with a short exponential backoff
that is easy to test offline.
"""

from __future__ import annotations

import time
from inspect import signature
from typing import Any

from gnews import GNews
from gnews.exceptions import InvalidConfigError

from .errors import NewsFetchError
from .logging_config import log_event
from .models import NewsArticle

# Constructor parameters that may or may not exist depending on the installed
# gnews minor version; they are filtered against the real signature.
_BACKEND_PARAMS = (
    "language",
    "country",
    "period",
    "max_results",
    "max_retries",
    "retry_backoff_base",
    "retry_backoff_max",
)


def _accepted_kwargs(fn: Any, candidates: dict[str, Any]) -> dict[str, Any]:
    """Return only *candidates* keys that *fn* actually accepts."""
    params = signature(fn).parameters
    return {name: value for name, value in candidates.items() if name in params}


class NewsClient:
    """Fetches and normalises news headlines from the GNews backend."""

    def __init__(
        self,
        *,
        language: str = "en",
        country: str = "US",
        period: str = "1d",
        max_results: int = 100,
        max_retries: int = 3,
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 60.0,
        retry_attempts: int = 3,
        retry_backoff: float = 0.1,
        **backend_kwargs: Any,
    ) -> None:
        """Create a client.

        ``language`` / ``country`` / ``period`` / ``max_results`` and the
        retry knobs map directly onto GNews's own init arguments (filtered to
        what the installed version supports).  Any extra ``backend_kwargs``
        (``proxy``, ``exclude_websites``, ``start_date``, ...) are forwarded
        to the GNews constructor as-is.
        """
        self._language = language
        self._country = country
        self._period = period
        self.max_results = int(max_results)
        self.retry_attempts = int(retry_attempts)
        self.retry_backoff = float(retry_backoff)

        candidates: dict[str, Any] = {
            "language": language,
            "country": country,
            "period": period,
            "max_results": self.max_results,
            "max_retries": int(max_retries),
            "retry_backoff_base": float(retry_backoff_base),
            "retry_backoff_max": float(retry_backoff_max),
        }
        accepted = _accepted_kwargs(GNews.__init__, candidates)
        self._backend = GNews(**accepted, **backend_kwargs)

    # ------------------------------------------------------------------ #
    # Public query surface
    # ------------------------------------------------------------------ #

    def search(self, query: str, max_results: int = 100) -> list[NewsArticle]:
        """Search headlines for *query* (a keyword or ticker symbol)."""
        return self._fetch(
            lambda: self._backend.get_news(query),
            max_results=max_results,
            query=query,
        )

    def search_by_topic(
        self, topic: str = "DIGITAL CURRENCIES", max_results: int = 100
    ) -> list[NewsArticle]:
        """Fetch headlines for a Google News *topic*.

        Defaults to ``DIGITAL CURRENCIES`` (note the space — it is the
        section's display name), the crypto-relevant section.  ``BITCOIN``
        and ``CRYPTOCURRENCIES`` are also available.
        """
        return self._fetch(
            lambda: self._backend.get_news_by_topic(topic),
            max_results=max_results,
            topic=topic,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _fetch(
        self,
        call: Any,
        *,
        max_results: int,
        **meta: Any,
    ) -> list[NewsArticle]:
        """Run a backend call with retry, then normalise the raw entries."""
        self._backend.max_results = int(max_results)
        last_exc: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                raw = call() or []
            except InvalidConfigError as exc:
                # Programming/config error (bad topic, bad params) — never
                # transient, so surface it immediately.
                last_exc = exc
                break
            except Exception as exc:  # connection-level errors
                last_exc = exc
                if attempt < self.retry_attempts - 1:
                    delay = self.retry_backoff * (2 ** attempt)
                    log_event(
                        "ticker_news_signals.client",
                        "gnews request failed; retrying",
                        level="WARNING",
                        attempt=attempt + 1,
                        delay=delay,
                        error=str(exc),
                        **meta,
                    )
                    time.sleep(delay)
                    continue
                break
            else:
                last_exc = None
                break

        if last_exc is not None:
            log_event(
                "ticker_news_signals.client",
                "gnews request failed after retries",
                level="ERROR",
                error=str(last_exc),
                **meta,
            )
            raise NewsFetchError(f"GNews request failed: {last_exc}") from last_exc

        articles = [NewsArticle.from_gnews(entry) for entry in raw]
        log_event(
            "ticker_news_signals.client",
            "gnews query returned articles",
            level="INFO",
            count=len(articles),
            **meta,
        )
        return articles