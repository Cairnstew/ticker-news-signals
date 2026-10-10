"""Append semantics for the news signal file (AC3 / AC4).

The consumer (`kraken-trading-bot`'s `merge_extra_features`) left-joins this
file by floored hour and de-duplicates repeated hours, so *appending* a
duplicate hour is free while *replacing* the file destroys every hour that
is not in the current pull. With `--lookback-hours` defaulting to 1, an
hourly pull without `--append` rewrites the whole history to a one-hour
window every hour — and the loss is invisible until someone reads the file.

These tests therefore assert on **content**, not on a line count. A count
assertion passes in the exact case that matters: 2 records in, 2 records
out, the sentinel gone.
"""

from __future__ import annotations

import json
from pathlib import Path

from ticker_news_signals.export import write_jsonl

# Two records so `lines_before` is 2 and the *second* record can also be
# checked: a truncate-and-rewrite to the same length is the failure a count
# cannot see.
_SENTINEL = "NEWS_SENTINEL"


def _record(hour: int, *, sentinel: bool = False) -> dict[str, object]:
    """One real-shaped news signal vector; `sentinel` is the marker line."""
    return {
        "ticker": _SENTINEL if sentinel else "ETH_USD",
        "timestamp": f"2026-10-03T{hour:02d}:00:00Z",
        "sentiment_score": 0.5 if sentinel else 0.1 * hour,
        "article_count": 5,
        "novelty_flag": False,
    }


def _lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _sentinel_present(path: Path) -> bool:
    return any(_SENTINEL in line for line in _lines(path))


def test_two_appends_keep_the_first_file_intact(tmp_path: Path) -> None:
    """AC3: the first file's records survive a second append, content and all.

    The failure this exists to catch is ``path.open("w")`` under an accepted
    ``--append``: the flag says "keep", the code says "replace". Asserting
    the count would not notice, because the second write happens to emit
    the same number of records as the first.
    """
    out = tmp_path / "news.jsonl"

    write_jsonl(out, [_record(1), _record(2, sentinel=True)])
    before = _lines(out)
    assert len(before) == 2
    assert _sentinel_present(out)

    write_jsonl(out, [_record(3), _record(4)], append=True)
    after = _lines(out)

    lines_before, lines_after = len(before), len(after)
    if not _sentinel_present(out):
        raise AssertionError(
            f"sentinel record '{_SENTINEL}' was destroyed by a second "
            f"write_jsonl(append=True) — lines_before={lines_before}, "
            f"lines_after={lines_after}, sentinel_present=False"
        )
    # Content, not just "still there": the original lines must be
    # byte-identical, in order, at the head of the file.
    assert after[: len(before)] == before, (
        "append rewrote the existing records; the first write's lines must "
        f"survive verbatim at the head — before={before!r} head={after[: len(before)]!r}"
    )
    assert lines_after == lines_before + 2
    assert json.loads(after[-1])["timestamp"] == "2026-10-03T04:00:00Z"


def test_append_creates_the_file_when_absent(tmp_path: Path) -> None:
    """`append=True` on a missing file is a plain first write, not an error."""
    out = tmp_path / "fresh.jsonl"
    write_jsonl(out, [_record(5)], append=True)
    assert len(_lines(out)) == 1


def test_replace_is_loud_on_stderr(tmp_path: Path, capsys) -> None:
    """AC4: a truncate that drops records says so, on stderr, naming --append.

    Chosen over refusing: replacing is the documented back-compat default
    and a legitimate answer for "give me the last hour", so it stays legal —
    it just is not allowed to be quiet about the cost.
    """
    out = tmp_path / "news.jsonl"
    write_jsonl(out, [_record(1), _record(2, sentinel=True)], append=True)
    capsys.readouterr()  # drop the first write's output

    write_jsonl(out, [_record(3)])

    captured = capsys.readouterr()
    assert captured.out == "", "the warning belongs on stderr, not stdout"
    assert "--append" in captured.err
    assert "2 record(s)" in captured.err, (
        "the warning must name the number of records dropped, so the "
        f"operator can tell a 2-record loss from a 2-record no-op: {captured.err!r}"
    )
    assert _lines(out) == [
        json.dumps(_record(3), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    ]


def test_replace_of_an_empty_or_missing_file_is_silent(tmp_path: Path, capsys) -> None:
    """Nothing was dropped, so there is nothing to warn about."""
    out = tmp_path / "news.jsonl"
    write_jsonl(out, [_record(1)])
    captured = capsys.readouterr()
    assert captured.err == ""

    out.write_text("", encoding="utf-8")
    write_jsonl(out, [_record(2)])
    captured = capsys.readouterr()
    assert captured.err == "", "an empty file drops zero records — no warning"


def test_append_repairs_a_file_whose_last_line_was_cut_short(tmp_path: Path) -> None:
    """A truncated final line must not swallow the next appended record.

    ``{"a": 1`` with no newline, then an append that writes ``{"b": 2}``
    yields ONE line holding a JSON fragment, which every consumer's
    ``json.loads`` loop skips — two records lost, none reported.
    """
    out = tmp_path / "torn.jsonl"
    out.write_text('{"ticker": "ETH_USD", "sentiment_score": 0.1', encoding="utf-8")

    write_jsonl(out, [_record(1)], append=True)

    lines = _lines(out)
    assert len(lines) == 2, f"the torn line must be closed off, not glued: {lines!r}"
    assert lines[0] == '{"ticker": "ETH_USD", "sentiment_score": 0.1', (
        "the torn line must be left exactly as it was, on a line of its own"
    )
    assert json.loads(lines[1])["timestamp"] == "2026-10-03T01:00:00Z"


def test_cli_accepts_append_and_defaults_off() -> None:
    """The flag exists, defaults to False, and the parser wires it through.

    Structural rather than behavioural on purpose: `cmd_pull` needs the
    network, and the thing worth pinning here is the default — a silent
    flip to True would change every existing caller's file from a snapshot
    to a log, and a silent flip to a *removed* flag would make the timer
    units on the consumer side fail their ExecStart.
    """
    from ticker_news_signals.cli import _build_parser

    args = _build_parser().parse_args(["pull", "--ticker", "ETH/USD", "--output", "x.jsonl"])
    assert args.append is False, "back-compat: replace stays the default"

    args = _build_parser().parse_args(
        ["pull", "--ticker", "ETH/USD", "--output", "x.jsonl", "--append"]
    )
    assert args.append is True
