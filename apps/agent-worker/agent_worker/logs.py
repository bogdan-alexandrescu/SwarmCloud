"""Structured logging for the worker.

Everything the worker says goes to stdout as one JSON object per line, in the
shape Cloud Logging parses natively (`severity`, `message`, `time`, plus a flat
`labels` map). There is no second log sink: Cloud Run and GKE both capture
stdout, and a worker that tried to ship its own logs would be one more thing
that can fail while the interesting failure is happening.

Two properties matter more than prettiness:

* **Every line carries the attempt's identity.** A worker is one of hundreds and
  its logs are read after the fact, interleaved with everyone else's. task,
  attempt, tenant and generation are bound once at construction.
* **Secrets are scrubbed on the way out.** The worker is the only component that
  ever holds a tenant's provider key, so it is the only component that can leak
  one into a log line. Registered secret values are replaced with a redaction
  marker in both the message and the structured fields.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, TextIO

REDACTED = "***REDACTED***"

#: Field names whose values are never printed, no matter where they appear.
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "anthropic_api_key",
        "openai_api_key",
        "authorization",
        "token",
        "secret",
        "password",
        "credentials",
    }
)


def _isoformat(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StructuredLogger:
    """JSON-lines logger with bound context and secret scrubbing."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        labels: dict[str, Any] | None = None,
        component: str = "agent-worker",
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._labels = dict(labels or {})
        self._component = component
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    # -- context -----------------------------------------------------------
    def bind(self, **labels: Any) -> "StructuredLogger":
        """Return a child logger carrying additional labels.

        The secret set is shared by reference: a key registered anywhere is
        scrubbed everywhere, including by loggers created before it was known.
        """
        child = StructuredLogger(
            stream=self._stream, labels={**self._labels, **labels}, component=self._component
        )
        child._secrets = self._secrets
        child._lock = self._lock
        return child

    def register_secret(self, value: str | None) -> None:
        """Mark a literal string as unloggable. Short values are ignored.

        Scrubbing a 3-character value would corrupt ordinary text far more often
        than it would protect anything, and no real provider key is that short.
        """
        if value and len(value) >= 8:
            self._secrets.add(value)

    # -- redaction ---------------------------------------------------------
    def _scrub_text(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def _scrub(self, value: Any, key: str | None = None) -> Any:
        if key is not None and key.lower() in _SENSITIVE_KEYS:
            return REDACTED
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, dict):
            return {k: self._scrub(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(v) for v in value]
        if isinstance(value, datetime):
            return _isoformat(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return self._scrub_text(str(value))

    # -- emission ----------------------------------------------------------
    def log(self, severity: str, message: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "severity": severity,
            "time": _isoformat(datetime.now(timezone.utc)),
            "component": self._component,
            "message": self._scrub_text(message),
        }
        if self._labels:
            record["labels"] = self._scrub(self._labels)
        for key, value in fields.items():
            record[key] = self._scrub(value, key)
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
            stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            **fields,
        )


def build_logger(
    *,
    task_id: str,
    attempt_id: str,
    tenant_id: str,
    generation: int,
    runner_profile: str,
    stream: TextIO | None = None,
) -> StructuredLogger:
    return StructuredLogger(
        stream=stream,
        labels={
            "task_id": task_id,
            "attempt_id": attempt_id,
            "tenant_id": tenant_id,
            "generation": generation,
            "runner_profile": runner_profile,
        },
    )
