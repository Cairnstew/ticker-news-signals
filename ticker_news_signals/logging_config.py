"""Logging infrastructure for the ticker-news-signals package.

This module provides structured logging that can be consumed by the CLI
or by any embedding application.  It follows the Python library convention:
the ``ticker_news_signals`` package logger has a ``NullHandler`` by default
(safe when no logging is configured).  Call :func:`setup_logging` to attach
real handlers — typically done once from the CLI entry-point.

Loggers
-------
``ticker_news_signals``          root package logger
``ticker_news_signals.client``   GNews backend fetches (retries, result counts)
``ticker_news_signals.sentiment`` sentiment scoring batches
``ticker_news_signals.pipeline`` aggregation steps (bucketing, signal output)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import stat
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sensitive-data patterns
# ---------------------------------------------------------------------------

# Each entry is a compiled regex.  The redaction filter replaces matched spans
# with ``[REDACTED]``.  Patterns are ordered so that more-specific ones run
# first.  GNews needs no API key today, but a subscription key or a long
# opaque token would still be masked if one is ever added.
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?secret)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(BEARER)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(token|secret)\s*[=:]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),
]

# ---------------------------------------------------------------------------
# Redaction filter
# ---------------------------------------------------------------------------

# Fields on LogRecord that are set by logging internals — never extra data.
_LOG_RECORD_BUILTINS = frozenset(logging.LogRecord(
    "", 0, "", 0, "", (), None,
).__dict__.keys())


class RedactionFilter(logging.Filter):
    """Log filter that masks sensitive data in messages, exc_info, and ``extra``.

    The filter operates on the *formatted* record (``record.getMessage()``)
    and on exception text / tracebacks so that secrets never reach the
    output.  It mutates the record in place so downstream handlers see
    clean text.
    """

    _PASSWORD_PLACEHOLDER = "[REDACTED]"

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Scrub the main message.
        record.msg = self._redact(record.getMessage())
        record.args = None  # prevent double-formatting

        # Generate exc_text from exc_info if the handler hasn't done so yet,
        # then redact it.  (Python's Formatter normally sets exc_text, but
        # filters run *before* the formatter.)
        if record.exc_info and record.exc_info[0] is not None:
            if record.exc_text is None:
                record.exc_text = "".join(
                    traceback.format_exception(*record.exc_info)
                )
            record.exc_text = self._redact(record.exc_text)

        if record.stack_info is not None:
            record.stack_info = self._redact(record.stack_info)

        # Scrub any string values in extra fields that made it onto the record.
        for key in list(vars(record)):
            if key in _LOG_RECORD_BUILTINS or key.startswith("_"):
                continue
            val = getattr(record, key, None)
            setattr(record, key, self._redact_value(val))

        return True

    def _redact_value(self, val: Any) -> Any:
        """Recursively redact sensitive data in a value."""
        if isinstance(val, str):
            return self._redact(val)
        if isinstance(val, (list, tuple)):
            return type(val)(self._redact_value(v) for v in val)
        if isinstance(val, dict):
            return {k: self._redact_value(v) for k, v in val.items()}
        return val

    @classmethod
    def _redact(cls, text: str) -> str:
        if not text:
            return text
        for pattern in _SENSITIVE_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text


# ---------------------------------------------------------------------------
# Structured JSON formatter
# ---------------------------------------------------------------------------


class StructuredJsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line with UTC timestamps.

    Custom fields passed via ``extra=`` are serialised alongside the
    standard record attributes.  Non-serialisable values are coerced to
    strings.
    """

    _RESERVED = {
        "name", "msg", "args", "created", "relativeCreated", "exc_info",
        "exc_text", "stack_info", "levelname", "levelno", "pathname",
        "filename", "module", "funcName", "lineno", "msecs", "message",
        "thread", "threadName", "process", "processName",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.") + f"{record.msecs:03.0f}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        # Attach any extra fields the caller passed via log_event(..., extra=...).
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in self._RESERVED or key in _LOG_RECORD_BUILTINS:
                continue
            doc[key] = _json_safe(val)

        # Append exception info if present.
        if record.exc_info and record.exc_info[0] is not None:
            exc_doc: dict[str, Any] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
            }
            tb_text = record.exc_text
            if tb_text is None:
                tb_text = "".join(traceback.format_exception(*record.exc_info))
            if tb_text:
                exc_doc["traceback"] = tb_text
            doc["exception"] = exc_doc

        return json.dumps(doc, ensure_ascii=False, default=str)


def _json_safe(val: Any) -> Any:
    """Coerce *val* to something json.dumps can serialise."""
    if isinstance(val, (str, int, float, bool, type(None))):
        return val
    if isinstance(val, (list, tuple)):
        return [_json_safe(v) for v in val]
    if isinstance(val, dict):
        return {k: _json_safe(v) for k, v in val.items()}
    if hasattr(val, "to_dict"):
        return _json_safe(val.to_dict())
    if hasattr(val, "__str__") and not isinstance(val, type):
        s = str(val)
        if s and not s.startswith("<"):
            return s
    if hasattr(val, "__dict__"):
        return {k: _json_safe(v) for k, v in val.__dict__.items() if not k.startswith("_")}
    return str(val)


# ---------------------------------------------------------------------------
# Human-readable console formatter
# ---------------------------------------------------------------------------


class HumanReadableFormatter(logging.Formatter):
    """Short, colourless, one-line format for stderr console output."""

    _LEVEL_WIDTH = 8

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        name = record.name.replace("ticker_news_signals.", "tns.")
        if name == "ticker_news_signals":
            name = "tns"
        level = record.levelname[: self._LEVEL_WIDTH].ljust(self._LEVEL_WIDTH)
        msg = record.getMessage()
        line = f"[{ts}] {level} {name}  {msg}"
        if record.exc_info and record.exc_info[0] is not None:
            line += f"  ({record.exc_info[0].__name__}: {record.exc_info[1]})"
        return line


# ---------------------------------------------------------------------------
# log_event helper
# ---------------------------------------------------------------------------

_LOGGERS: dict[str, logging.Logger] = {}


def _get_logger(name: str) -> logging.Logger:
    if name not in _LOGGERS:
        _LOGGERS[name] = logging.getLogger(name)
    return _LOGGERS[name]


def log_event(
    logger: str | logging.Logger,
    event: str,
    *,
    level: int | str = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured log event with arbitrary extra fields.

    Parameters
    ----------
    logger:
        Logger name (string) or a ``logging.Logger`` instance.
    event:
        Human-readable event description (becomes ``record.getMessage()``).
    level:
        Log level for the record — an int (``logging.DEBUG``) or a level
        name (``"DEBUG"``).  Defaults to ``INFO``.
    **fields:
        Arbitrary key-value pairs attached to the record via ``extra=``.
        These are serialised by :class:`StructuredJsonFormatter` and
        ignored by the human-readable formatter.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    if isinstance(logger, str):
        logger_obj = _get_logger(logger)
    else:
        logger_obj = logger
    logger_obj.log(level, event, extra=fields)


# ---------------------------------------------------------------------------
# XDG state directory
# ---------------------------------------------------------------------------


def _xdg_state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/ticker-news-signals/logs``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "ticker-news-signals" / "logs"


def _ensure_log_dir(path: Path) -> None:
    """Create the parent directory with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass  # best-effort on platforms that don't support chmod


def _create_restricted_file(path: Path) -> None:
    """Create the log file with 0600 permissions if it doesn't exist."""
    if not path.exists():
        path.touch()
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def setup_logging(
    *,
    level: int | str = "WARNING",
    log_file: str | Path | None = None,
    file_level: int | str = "INFO",
    console: bool = True,
) -> None:
    """Configure logging for the ``ticker_news_signals`` package.

    Call this **once** from the CLI entry-point or application startup.
    When not called, the package logger keeps only its ``NullHandler``
    (library-safe default).

    Parameters
    ----------
    level:
        Console handler log level.  Accepts a level name or int.
    log_file:
        Path to the JSON log file.  ``None`` disables file logging.
    file_level:
        File handler log level.  Defaults to ``"INFO"``.
    console:
        Whether to attach a console (stderr) handler.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.WARNING)
    if isinstance(file_level, str):
        file_level = getattr(logging, file_level.upper(), logging.INFO)

    pkg_logger = logging.getLogger("ticker_news_signals")

    # Remove any pre-existing handlers (idempotent re-calls).
    for h in list(pkg_logger.handlers):
        pkg_logger.removeHandler(h)

    pkg_logger.setLevel(min(level, file_level))
    pkg_logger.propagate = False

    redaction = RedactionFilter()

    # -- Console handler (stderr) --
    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(HumanReadableFormatter())
        console_handler.addFilter(redaction)
        pkg_logger.addHandler(console_handler)

    # -- File handler (rotating JSON) --
    if log_file is not None:
        log_path = Path(log_file)
        _ensure_log_dir(log_path)
        _create_restricted_file(log_path)

        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(StructuredJsonFormatter())
        file_handler.addFilter(redaction)
        pkg_logger.addHandler(file_handler)

    # Silence chatty HTTP libraries at DEBUG.
    logging.getLogger("urllib3").setLevel(logging.WARNING)