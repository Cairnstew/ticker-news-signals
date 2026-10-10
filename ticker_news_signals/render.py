"""Clean, composable output.

Two audiences:

* **Humans** — aligned plain-text sections and tables (``render_report`` /
  ``render_markdown``). No colour codes, so they paste cleanly into a terminal,
  a log, an issue or a code review.
* **Pipelines** — :func:`flat_metrics` returns one flat ``dict`` of scalars per
  fetch, appendable straight into a DataFrame or a CSV.

Every formatter degrades predictably: a missing value renders as ``-``, never
as ``0``, so an absent measurement can never be mistaken for a real one.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .models import SignalResult, SignalVector

#: Rendered in place of a missing value.
NA = "-"


def _iso(value: datetime | None) -> str:
    if value is None:
        return NA
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mean_nonzero(values: Iterable[float]) -> float | None:
    """Mean of *values*, counting only non-zero ones; ``None`` if none are."""
    nonzero = [v for v in values if v]
    if not nonzero:
        return None
    return round(sum(nonzero) / len(nonzero), 4)


def fmt_score(value: float | None) -> str:
    return NA if value is None else f"{value:+.3f}"


def fmt_pct(value: float | None, *, signed: bool = True, digits: int = 1) -> str:
    if value is None:
        return NA
    return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"


def table(headers: list[str], rows: list[list[str]], *, indent: str = "") -> str:
    """Render a simple aligned text table."""
    if not rows:
        return f"{indent}(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = [indent + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append(indent + "  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- metrics


def flat_metrics(result: SignalResult) -> dict[str, Any]:
    """One flat dict of scalars per fetch — the pipeline-friendly shape."""
    vectors = result.vectors
    scores = [v.sentiment_score for v in vectors]
    articles = sum(v.article_count for v in vectors)
    sources = sorted({s for v in vectors for s in v.sources})
    quality = result.data_quality
    return {
        "ticker": result.ticker,
        "query": result.query,
        "resolved_symbol": result.resolved_symbol,
        "resolved_name": result.resolved_name,
        "generated_at": _iso(result.generated_at),
        "hours": len(vectors),
        "articles": articles,
        "articles_fetched": quality.articles_fetched,
        "duplicates_removed": quality.duplicates_removed,
        "mean_sentiment": round(sum(scores) / len(scores), 4) if scores else None,
        "max_sentiment": max(scores) if scores else None,
        "min_sentiment": min(scores) if scores else None,
        "novel_hours": sum(1 for v in vectors if v.novelty_flag),
        "source_count": len(sources),
        "sources": ",".join(sources),
        # Scorer disagreement (ensemble backends): how contested the hours
        # were on average / at their most — a pipeline filter for anomaly risk.
        "mean_scorer_std": _mean_nonzero(v.scorer_std for v in vectors),
        "max_scorer_std": max((v.scorer_std for v in vectors), default=None)
        if any(v.scorer_std for v in vectors)
        else None,
        "error_count": len(quality.errors),
        "rate_limited_count": len(quality.rate_limited),
        "skipped_count": len(quality.skipped),
        "missing_count": len(quality.missing),
    }


# --------------------------------------------------------------------------- sections


def _vector_rows(vectors: list[SignalVector]) -> list[list[str]]:
    rows: list[list[str]] = []
    for v in vectors:
        rows.append(
            [
                v.timestamp,
                fmt_score(v.sentiment_score),
                f"{v.sentiment_std:.3f}" if v.sentiment_std else NA,
                str(v.article_count),
                str(v.source_count),
                "yes" if v.novelty_flag else "",
            ]
        )
    return rows


def signals_table(result: SignalResult, *, indent: str = "") -> str:
    return table(
        ["hour (UTC)", "sentiment", "spread", "n", "src", "novel"],
        _vector_rows(result.vectors),
        indent=indent,
    )


def sources_table(result: SignalResult, *, indent: str = "") -> str:
    provenance = result.data_quality.provenance
    if not provenance:
        return f"{indent}(no sources recorded)"
    rows = [
        [key, prov.source, str(prov.article_count), "yes" if prov.cached else "", prov.url or ""]
        for key, prov in sorted(provenance.items())
    ]
    return table(["key", "source", "articles", "cached", "url"], rows, indent=indent)


def render_history_report(result: Any, *, indent: str = "") -> str:
    """Human report for a :class:`HistoryResult`: buckets + per-source coverage."""
    from .models import HistoryResult

    if not isinstance(result, HistoryResult):
        return quality_block(result, indent=indent)

    lines = [
        f"{indent}# {result.resolved_name or result.ticker} — historical signals",
        f"{indent}queried: {result.query}  granularity: {result.granularity}",
        f"{indent}window: {result.start} .. {result.end}  (generated {result.generated_at})",
        "",
    ]
    if result.vectors:
        rows = [
            [
                v.timestamp,
                fmt_score(v.sentiment_score),
                f"{v.sentiment_std:.3f}" if v.sentiment_std else NA,
                str(v.article_count),
                str(v.source_count),
            ]
            for v in result.vectors
        ]
        lines.append(
            table(["bucket (UTC)", "sentiment", "spread", "n", "src"], rows, indent=indent)
        )
    else:
        lines.append(f"{indent}(no reconstructed vectors in this window)")

    if result.series:
        lines += ["", f"{indent}## Source coverage"]
        for series in result.series:
            cov = series.coverage
            if cov is None:
                lines.append(f"{indent}- {series.source}: —")
                continue
            complete = "complete" if cov.complete else f"{cov.buckets}/{cov.expected} buckets"
            lines.append(
                f"{indent}- {series.source}: {complete}"
                + (f" ({cov.first} .. {cov.last})" if cov.first else "")
                + (f" — {cov.note}" if cov.note else "")
            )

    quality_text = quality_block(result, indent=indent)
    if quality_text.strip():
        lines += ["", f"{indent}## Quality", quality_text]
    return "\n".join(lines)


def quality_block(result: Any, *, indent: str = "") -> str:
    """Tolerated errors, throttles, skipped sources and missing-field explanations.

    Accepts any result carrying a ``data_quality`` block — live
    (:class:`SignalResult`) or historical (:class:`HistoryResult`).
    """
    quality = result.data_quality
    lines: list[str] = []
    if quality.skipped:
        lines.append(f"{indent}skipped:")
        for item in quality.skipped:
            lines.append(f"{indent}  - {item.source}: {item.reason}")
    if quality.errors:
        lines.append(f"{indent}tolerated errors:")
        for err in quality.errors:
            lines.append(
                f"{indent}  - [{err.source}/{err.capability}] {err.exception_type}: {err.message}"
            )
    if quality.rate_limited:
        lines.append(f"{indent}rate limited:")
        for item in quality.rate_limited:
            retry = f" retry in {item.retry_after:.0f}s" if item.retry_after else ""
            lines.append(f"{indent}  - {item.source}/{item.capability}:{retry} {item.message}")
            if item.remedy:
                lines.append(f"{indent}    remedy: {item.remedy}")
    if quality.missing:
        lines.append(f"{indent}missing data:")
        for item in quality.missing:
            lines.append(f"{indent}  - {item.field}: {item.reason}")
    if not lines:
        lines.append(f"{indent}no errors, throttles, skips or missing fields")
    return "\n".join(lines)


def render_report(result: SignalResult, *, indent: str = "") -> str:
    """The full human-readable report."""
    quality = result.data_quality
    name = f" ({result.resolved_name})" if result.resolved_name else ""
    lines = [
        f"{result.ticker}{name}",
        f"  window    {len(result.vectors)} non-empty hour(s)"
        f"  {quality.articles_fetched} fetched  {len(result.articles)} unique"
        f"  {quality.duplicates_removed} duplicate(s) removed",
        f"  sources   {len(quality.sources_used)}"
        + (f"  {', '.join(quality.sources_used)}" if quality.sources_used else ""),
        f"  generated {_iso(result.generated_at)}",
        "",
        "signals",
        signals_table(result, indent=indent + "  "),
        "",
        "sources",
        sources_table(result, indent=indent + "  "),
        "",
        "quality",
        quality_block(result, indent=indent + "  "),
    ]
    return "\n".join(lines)


def render_markdown(result: SignalResult) -> str:
    """The report as a fenced markdown block."""
    quality = result.data_quality
    lines = [
        f"# {result.ticker} — news signals",
        "",
        f"- **unique headlines**: {len(result.articles)} "
        f"({quality.articles_fetched} fetched, {quality.duplicates_removed} duplicates removed)",
        f"- **sources**: {', '.join(quality.sources_used) if quality.sources_used else NA}",
        f"- **generated**: {_iso(result.generated_at)}",
        "",
        "| hour (UTC) | sentiment | spread | n | src | novel |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for v in result.vectors:
        spread = f"{v.sentiment_std:.3f}" if v.sentiment_std else NA
        lines.append(
            f"| {v.timestamp} | {fmt_score(v.sentiment_score)} | {spread} | "
            f"{v.article_count} | {v.source_count} | {'yes' if v.novelty_flag else ''} |"
        )
    if not result.vectors:
        lines.append("| - | - | - | - | - | - |")
    if quality.missing or quality.errors or quality.skipped:
        lines += ["", "## Quality", "```", quality_block(result), "```"]
    return "\n".join(lines)


__all__ = [
    "NA",
    "flat_metrics",
    "fmt_pct",
    "fmt_score",
    "quality_block",
    "render_history_report",
    "render_markdown",
    "render_report",
    "signals_table",
    "sources_table",
    "table",
]
