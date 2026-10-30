"""The engine's event stream: `--json-events` and the human view (design §2.5).

Every face of the installer (the CLI, `/sc:install`, the console) learns what
the engine is doing from these events. They carry step ids, states, reasons and
times, **never a value**: `/sc:install` reads them inside a Claude session,
where a secret must never appear (`docs/security.md`, "no value in any tool
schema"). So an event refuses a field that is a `SecretStr`/`SecretBytes` or
raw bytes, accepts only JSON scalars and lists/dicts of them, and passes every
string through `swarm_redaction.redact` before it is written.

`--json-events` writes one JSON object per line:
`{"v": 1, "event": <kind>, "at": <epoch seconds>, ...fields}`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from pydantic import SecretBytes, SecretStr
from swarm_redaction import redact

#: The schema version of one event line.
EVENT_VERSION = 1

#: Every kind the engine emits. A face that does not know a kind shows it as
#: text; nothing else is promised.
KINDS = frozenset(
    {
        "run_start",
        "stage_start",
        "stage_refused",
        "step_check",
        "step_skip",
        "step_drift",
        "step_plan",
        "step_apply",
        "step_done",
        "step_failed",
        "step_paused",
        "step_triggered",
        "wait_countdown",
        "wait_gave_up",
        "lease",
        "run_end",
    }
)


class EventValueError(TypeError):
    """An event was given a field that could carry a value (a secret)."""


def _clean(name: str, value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes, bytes, bytearray, memoryview)):
        raise EventValueError(f"event field {name!r} holds a secret or raw bytes; events carry no value")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, (list, tuple)):
        return [_clean(name, v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clean(f"{name}.{k}", v) for k, v in value.items()}
    raise EventValueError(f"event field {name!r} is a {type(value).__name__}, not a JSON value")


def build(kind: str, at: float, fields: dict[str, Any]) -> dict[str, Any]:
    """One event as a dict, checked and redacted."""
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    event: dict[str, Any] = {"v": EVENT_VERSION, "event": kind, "at": at}
    for key, value in fields.items():
        if key in event:
            raise ValueError(f"event field {key!r} is reserved")
        event[key] = _clean(key, value)
    return event


def _human_line(event: dict[str, Any]) -> str:
    kind = event["event"]
    step = event.get("step")
    if kind == "wait_countdown":
        return (
            f"  waiting on {step}: {event.get('reason')} -- "
            f"{int(event.get('remaining_s', 0))} s left of the bound "
            f"(usually {event.get('expected')})"
        )
    if kind == "wait_gave_up":
        return f"  gave up waiting on {step} after {event.get('bound_s')} s. Look at: {event.get('look_at')}"
    if kind == "stage_start":
        return f"Stage {event.get('stage')}"
    if kind == "run_end":
        return f"{event.get('outcome')} (exit {event.get('exit_code')})" + (
            f": {event['message']}" if event.get("message") else ""
        )
    parts = [f"  {kind.removeprefix('step_')}"]
    if step:
        parts.append(str(step))
    for key in ("state", "detail", "message", "explain", "plan"):
        if event.get(key):
            parts.append(f"-- {event[key]}")
    line = " ".join(parts)
    if kind == "step_plan":
        # A human step's instructions (what to type on the page, which path
        # to take) live in its plan's actions; the person reads them here.
        for action in event.get("actions") or ():
            line += "\n" + "\n".join(f"      {text}" for text in str(action).splitlines())
    return line


class Events:
    """Emits events to a sink: JSON lines for `--json-events`, else text.

    `record` keeps every event emitted, so a test (or the engine's summary)
    can read them back.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        *,
        json_lines: bool = False,
        out: TextIO | None = None,
    ) -> None:
        self._clock = clock
        self._json = json_lines
        self._out = out
        self.record: list[dict[str, Any]] = []

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = build(kind, self._clock(), fields)
        self.record.append(event)
        out = self._out if self._out is not None else sys.stdout
        if self._json:
            out.write(json.dumps(event, sort_keys=True) + "\n")
        else:
            out.write(_human_line(event) + "\n")
        out.flush()
        return event

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.record if e["event"] == kind]
