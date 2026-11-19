"""Unified structured logging for every service.

    from clipper_core.logs import get_logger, log_context, setup_logging

    setup_logging(settings)                      # once, at process start
    log = get_logger(__name__)
    log.info("chunk saved", chunk_id=5, seq=3)   # extra keyword args become fields
    with log_context(channel="xqc", stream_id=7):
        log.info("joined")                       # ...and context fields are added too

- Built on stdlib `logging`, so library logs (sqlalchemy, botocore, alembic, streamlink, ...)
  come out in the same format.
- `json` format: one JSON object per line on stdout. Container log drivers / CloudWatch Logs /
  Loki ingest it as-is; CloudWatch Logs Insights discovers the fields automatically. Metrics can
  later be emitted the same way (CloudWatch Embedded Metric Format is just a JSON log line).
- `console` format: readable lines for a terminal. `auto` picks console for a TTY, else json.
- Context lives in a contextvar, so every asyncio task keeps its own (per channel, per job).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import PurePath
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clipper_core.config import Settings

# Core keys are always present and can't be overridden by fields.
CORE_KEYS = ("timestamp", "level", "service", "worker", "logger", "message")

# Chatty libraries: quiet by default, override with CLIPPER_LOG_LEVELS="botocore=DEBUG,...".
DEFAULT_LEVELS = {
    "asyncio": "WARNING",
    "boto3": "WARNING",
    "botocore": "WARNING",
    "faster_whisper": "WARNING",  # one INFO line per transcribe call
    "httpcore": "WARNING",
    "httpcore2": "WARNING",
    "httpx": "WARNING",
    "httpx2": "WARNING",  # huggingface_hub's client: one INFO line per request
    "s3transfer": "WARNING",
    "sqlalchemy.engine": "WARNING",
    "urllib3": "WARNING",
    "websockets": "WARNING",
}

_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "clipper_log_context", default=MappingProxyType({})
)
_process: dict[str, Any] = {"service": "unknown", "worker": None}


# --------------------------------------------------------------------------- API


class StructuredLogger(logging.LoggerAdapter):
    """Logger that turns extra keyword arguments into structured fields."""

    _STD_KWARGS = ("exc_info", "stack_info", "stacklevel", "extra")

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        std = {k: kwargs.pop(k) for k in self._STD_KWARGS if k in kwargs}
        if kwargs:
            std["extra"] = {**(std.get("extra") or {}), "clipper_fields": dict(kwargs)}
        return msg, std

    # Positional-only, so fields may be named `level` or `msg` without clashing.
    def log(self, lvl: int, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(lvl):
            msg, kwargs = self.process(msg, kwargs)
            self.logger.log(lvl, msg, *args, **kwargs)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name), {})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields to every log line inside this block (and tasks started inside it)."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def bind_context(**fields: Any) -> Token:
    """Like log_context, for the rest of the current task (e.g. a per-channel loop)."""
    return _context.set({**_context.get(), **fields})


def current_context() -> dict[str, Any]:
    return dict(_context.get())


def setup_logging(
    settings: Settings | None = None,
    *,
    service: str | None = None,
    level: str | None = None,
    fmt: str | None = None,
    levels: str | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Configure the root logger once per process. Explicit arguments override settings."""
    service = service or (settings.service_name if settings else "unknown")
    level = (level or (settings.log_level if settings else "INFO")).upper()
    fmt = fmt or (settings.log_format if settings else "auto")
    levels = levels if levels is not None else (settings.log_levels if settings else "")
    stream = stream or sys.stdout

    _process.update(service=service, worker=settings.worker_id if settings else None)
    if fmt == "auto":
        fmt = "console" if getattr(stream, "isatty", lambda: False)() else "json"
    if fmt not in ("json", "console"):
        raise ValueError(f"unknown log format {fmt!r} (use json, console or auto)")

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name, lvl in {**DEFAULT_LEVELS, **parse_levels(levels)}.items():
        logging.getLogger(name).setLevel(lvl)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught


def parse_levels(spec: str) -> dict[str, str]:
    """'sqlalchemy.engine=INFO, botocore=DEBUG' -> {'sqlalchemy.engine': 'INFO', ...}"""
    result: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, sep, lvl = part.partition("=")
        if not sep or not name.strip() or not lvl.strip():
            raise ValueError(f"bad log level spec {part!r}, expected logger=LEVEL")
        result[name.strip()] = lvl.strip().upper()
    return result


# --------------------------------------------------------------------------- formatting


class _ContextFilter(logging.Filter):
    """Capture the task's context when the record is created (also safe for queued handlers)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "clipper_context"):
            record.clipper_context = _context.get()
        return True


def record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Context + call-site fields + exception info, without the core keys."""
    fields: dict[str, Any] = {
        **getattr(record, "clipper_context", {}),
        **getattr(record, "clipper_fields", {}),
    }
    for key in CORE_KEYS:
        fields.pop(key, None)
    if record.exc_info and record.exc_info[0] is not None:
        fields["exc_type"] = record.exc_info[0].__name__
        fields["exception"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
    if record.stack_info:
        fields["stack"] = record.stack_info
    return fields


def _timestamp(record: logging.LogRecord) -> str:
    ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
    return ts.replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "service": _process["service"],
            "worker": _process["worker"],
            "logger": record.name,
            "message": record.getMessage(),
        }
        doc.update(record_fields(record))
        return json.dumps(doc, default=_json_default, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = record_fields(record)
        exception = fields.pop("exception", None)
        fields.pop("exc_type", None)
        line = (
            f"{_timestamp(record)[11:]} {record.levelname:<7} {_process['service']} "
            f"{record.name}: {record.getMessage()}"
        )
        if fields:
            line += "  " + " ".join(f"{k}={_console_value(v)}" for k, v in fields.items())
        if exception:
            line += "\n" + exception
        return line


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):  # pydantic
        return value.model_dump(mode="json")
    if isinstance(value, PurePath | bytes):
        return str(value)
    return repr(value)


def _console_value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=_json_default)
    return f'"{text}"' if isinstance(value, str) and (" " in text or not text) else text


def _log_uncaught(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("uncaught").critical("uncaught exception", exc_info=(exc_type, exc, tb))
