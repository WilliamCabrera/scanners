"""
Centralised logging configuration for scanner.

Call setup_logging() once at startup. After that, every module uses:

    import logging
    logger = logging.getLogger(__name__)
    logger.info("...")
    logger.error("...", exc_info=True)

Output
------
  Console       → human-readable, coloured by level
  logs/app.log    → JSON, all levels (DEBUG+), rotating 10 MB × 5 files
  logs/errors.log → JSON, ERROR+ only,          rotating 10 MB × 5 files
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import traceback as tb
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path("logs")

# ── JSON formatter ─────────────────────────────────────────────────────────────

_SKIP_ATTRS = frozenset(
    {
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs",
        "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Serialise a LogRecord to a single-line JSON string."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()

        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
            "level":   record.levelname,
            "logger":  record.name,
            "module":  record.module,
            "line":    record.lineno,
            "message": record.message,
        }

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
            entry["traceback"] = "".join(tb.format_exception(*record.exc_info))

        if record.stack_info:
            entry["stack_info"] = self.formatStack(record.stack_info)

        # Attach any extra= fields passed by the caller
        for key, value in record.__dict__.items():
            if key in _SKIP_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                entry[key] = value
            except (TypeError, ValueError):
                entry[key] = str(value)

        return json.dumps(entry, ensure_ascii=False)


# ── Console formatter (coloured) ───────────────────────────────────────────────

_COLOURS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[35m",   # magenta
}
_RESET = "\033[0m"


class ColourFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        colour = _COLOURS.get(record.levelname, "")
        record.levelname = f"{colour}{record.levelname:<8}{_RESET}"
        return super().format(record)


# ── Public setup function ──────────────────────────────────────────────────────

def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure the root logger. Safe to call multiple times — handlers are
    only added once (idempotent).
    """
    LOGS_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(logging.DEBUG)

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # ── Console ───────────────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(
        ColourFormatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # ── app.log — all levels, JSON, rotating ──────────────────────────────────
    all_handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "app.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    all_handler.setLevel(logging.DEBUG)
    all_handler.setFormatter(JsonFormatter())

    # ── errors.log — ERROR+ only, JSON, rotating ──────────────────────────────
    error_handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "errors.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(JsonFormatter())

    root.addHandler(console)
    root.addHandler(all_handler)
    root.addHandler(error_handler)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "watchfiles", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging initialised — level=%s  logs_dir=%s", log_level, LOGS_DIR.resolve()
    )
