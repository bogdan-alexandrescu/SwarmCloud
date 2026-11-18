"""Logging configuration for EVERY swarm service. Without it, logs go nowhere.

Every module in every service does `logging.getLogger(__name__)` and then logs,
but nothing ever configured the root logger, so INFO records were dropped
entirely and even WARNING records did not reach Cloud Logging. Only uvicorn's
access log appeared, because uvicorn configures its own handlers. The practical
effect was that an operator debugging a 401 could see the request happen and
nothing about why it was refused.

Cloud Run captures stdout, and Cloud Logging parses a JSON line into structured
fields when it recognises `severity` and `message`. So records are emitted as
single-line JSON with those exact keys, which makes them filterable in the log
explorer by severity, logger and any field a caller attaches via `extra=`.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

#: Python level name -> the severity string Cloud Logging understands.
_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}

#: Attributes LogRecord always carries; anything else came from `extra=`.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Structured fields passed as `log.info("...", extra={"task_id": ...})`.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install a single stdout handler on the root logger. Idempotent.

    Idempotence matters because `create_app()` is called once per worker
    process and again in tests; adding a handler per call would multiply every
    line by the number of calls.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_swarm_configured", False):
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter())
    handler._swarm_configured = True  # type: ignore[attr-defined]

    # Replace rather than append: uvicorn may already have installed its own,
    # and two handlers means every record is emitted twice.
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn's loggers propagate to root once their own handlers are cleared,
    # so access logs arrive in the same JSON shape as everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
