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

A log line is not the only way out, which is why `scrub_text`, `scrub_value` and
`scrub_file` are public. The worker uploads `stdout.log`, `stderr.log` and the
runner's artifacts to GCS and writes `result_summary` into Firestore, and a CLI
that echoes its own configuration or a tool error quoting an `Authorization`
header would otherwise land verbatim in both. The lifecycle runs every one of
those channels through this same registered-secret set before it leaves the pod.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, TextIO

from .redact import REDACTED, MIN_SECRET_LENGTH, scrub_file as _scrub_file, scrub_text as _scrub

__all__ = ["REDACTED", "StructuredLogger", "build_logger"]

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
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._secrets.add(value)

    @property
    def has_secrets(self) -> bool:
        return bool(self._secrets)

    # -- redaction ---------------------------------------------------------
    def _scrub_text(self, text: str) -> str:
        return _scrub(text, self._secrets)

    def scrub_text(self, text: str) -> str:
        """Public: redact registered secrets from text bound for GCS or Firestore."""
        return self._scrub_text(text)

    def scrub_value(self, value: Any) -> Any:
        """Public: the same redaction, applied recursively to a JSON-able value."""
        return self._scrub(value)

    def scrub_file(self, path: Any, *, max_bytes: int = 64 * 1024 * 1024) -> bool:
        """Rewrite `path` in place with every registered secret redacted.

        Returns True when the file was rewritten. Binary files and files larger
        than `max_bytes` are left alone and reported as not rewritten: a partial
        rewrite of a large or non-text artifact would corrupt it, and corrupting
        a tenant's artifact to protect a key that is probably not in it is the
        wrong trade. Uploading is still the caller's decision.
        """
        from pathlib import Path as _Path

        return _scrub_file(_Path(path), self._secrets, max_bytes=max_bytes)

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
