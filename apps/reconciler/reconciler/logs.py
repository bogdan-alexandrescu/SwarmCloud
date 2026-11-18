"""Structured logging for the reconciler.

Same JSON-lines shape the worker uses, so one Cloud Logging query covers the
whole execution plane. The reconciler's lines are the ones an operator reads
after an incident -- "why did my task restart?" -- so every repair logs what it
invalidated, what it terminated and what it released, in that order.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, TextIO


class StructuredLogger:
    def __init__(self, *, stream: TextIO | None = None, component: str = "reconciler") -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._component = component
        self._lock = threading.Lock()

    def log(self, severity: str, message: str, **fields: Any) -> None:
        record = {
            "severity": severity,
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "component": self._component,
            "message": message,
        }
        record.update(fields)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def debug(self, message: str, **fields: Any) -> None:
        self.log("DEBUG", message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self.log("INFO", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self.log("WARNING", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.log("ERROR", message, **fields)

    def exception(self, message: str, exc: BaseException, **fields: Any) -> None:
        self.log(
            "ERROR",
            message,
            error_type=type(exc).__name__,
            error=str(exc),
            stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:],
            **fields,
        )


def build_logger(stream: TextIO | None = None) -> StructuredLogger:
    return StructuredLogger(stream=stream)
