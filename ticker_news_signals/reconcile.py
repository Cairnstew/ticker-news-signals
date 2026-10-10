"""Cross-source reconciliation.

The same story is republished across many feeds, often with tracking
parameters on the URL. Reconciliation collapses those duplicates to one
article while *remembering* every source that carried it, so the resulting
signal can report how broadly a story was covered instead of how many copies
each feed happened to publish.

Unlike price reconciliation there is no numeric consensus to compute — the
"truth" of a headline is its existence, and the interesting derived quantity is
source coverage and inter-headline sentiment dispersion.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import NewsArticle


@dataclass(slots=True)
class DedupeResult:
    """The outcome of collapsing duplicates across sources."""

    articles: list[NewsArticle] = field(default_factory=list)
    #: dedupe key -> every source that reported that story (first-seen order).
    sources_by_key: dict[str, tuple[str, ...]] = field(default_factory=dict)
    duplicates_removed: int = 0

    def sources_for(self, article: NewsArticle) -> tuple[str, ...]:
        """All sources that carried ``article`` (its own, plus duplicates')."""
        return self.sources_by_key.get(article.dedupe_key, (article.source,))


def dedupe(articles: Iterable[NewsArticle]) -> DedupeResult:
    """Collapse articles by :attr:`NewsArticle.dedupe_key`, tracking source coverage.

    First occurrence wins (order is preserved); later occurrences of the same
    key increment :attr:`DedupeResult.duplicates_removed` and add their source
    to the story's source list. Articles with no URL and no title are dropped
    as unidentifiable.
    """
    result = DedupeResult()
    sources: dict[str, list[str]] = defaultdict(list)
    for article in articles:
        key = article.dedupe_key
        if not key:
            continue
        if key not in result.sources_by_key and key not in sources:
            result.articles.append(article)
            sources[key].append(article.source)
        else:
            result.duplicates_removed += 1
            if article.source and article.source not in sources[key]:
                sources[key].append(article.source)
    result.sources_by_key = {key: tuple(vals) for key, vals in sources.items()}
    return result


def sentiment_dispersion(scores: list[float]) -> float:
    """Population standard deviation of per-headline scores (0.0 for <2 items)."""
    if len(scores) < 2:
        return 0.0
    return statistics.pstdev(scores)


def coverage_summary(result: DedupeResult) -> dict[str, Any]:
    """A compact coverage view for the report: source counts across unique stories."""
    per_source: dict[str, int] = defaultdict(int)
    multi_source = 0
    for sources in result.sources_by_key.values():
        for source in sources:
            per_source[source] += 1
        if len(sources) > 1:
            multi_source += 1
    return {
        "unique": len(result.articles),
        "duplicates_removed": result.duplicates_removed,
        "multi_source_stories": multi_source,
        "per_source": dict(sorted(per_source.items())),
    }


__all__ = ["DedupeResult", "coverage_summary", "dedupe", "sentiment_dispersion"]
