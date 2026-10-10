"""CryptoPanic provider — optional, gated on a free auth token.

CryptoPanic aggregates crypto news and lets you filter by currency code. The
public API needs a (free) auth token, so this provider is *skipped* with a
recorded reason unless ``TNS_CRYPTOPANIC_TOKEN`` is set — the credential-gating
pattern: a missing key degrades coverage, it never fails the run.

API docs: https://cryptopanic.com/developers/api/
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..capabilities import Capability
from ..models import NewsArticle
from ..resolve import ResolvedTicker
from .base import NewsProvider

API_BASE = "https://cryptopanic.com/api/v1/posts/"


class CryptoPanicProvider(NewsProvider):
    """Fetches currency-filtered headlines from CryptoPanic."""

    name: ClassVar[str] = "cryptopanic"
    supports: ClassVar[frozenset[Capability]] = frozenset({Capability.NEWS})

    @classmethod
    def available(cls, config: Any) -> tuple[bool, str | None]:
        if not config.cryptopanic_token:
            return False, "set TNS_CRYPTOPANIC_TOKEN to enable CryptoPanic"
        return True, None

    @classmethod
    def rate_limit_rpm(cls, config: Any) -> int:
        # Free tier is ~100 requests/day; keep the per-minute rate gentle.
        return min(config.rate_limit_rpm, 30)

    def fetch_news(self, asset: ResolvedTicker, *, max_results: int) -> tuple[NewsArticle, ...]:
        self.ctx.throttle()
        payload = self.ctx.http.get_json(
            API_BASE,
            params={
                "auth_token": self.ctx.config.cryptopanic_token,
                "currencies": asset.base,
                "public": "true",
            },
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        articles = [
            _article_from(entry, source=self.name)
            for entry in (results or [])
            if isinstance(entry, dict)
        ][:max_results]
        self.ctx.record(
            f"{self.name}:news",
            source=self.name,
            url=API_BASE,
            article_count=len(articles),
        )
        return tuple(articles)


def _article_from(entry: dict[str, Any], *, source: str) -> NewsArticle:
    origin = entry.get("source")
    publisher = str(origin.get("title") or "") if isinstance(origin, dict) else str(origin or "")
    return NewsArticle(
        title=str(entry.get("title") or ""),
        description="",
        published_date=str(entry.get("published_at") or entry.get("created_at") or ""),
        url=str(entry.get("url") or ""),
        publisher=publisher,
        source=source,
    )


__all__ = ["API_BASE", "CryptoPanicProvider"]
