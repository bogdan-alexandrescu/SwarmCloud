"""Nudge the scheduler after a submission.

The scheduler is woken by Pub/Sub and drains until no admissible work remains,
so this message is a latency optimisation, not a correctness requirement: Cloud
Scheduler's 1-minute safety tick drains the queue regardless. That is why every
failure here is logged and counted rather than failing the caller's submission,
which has already been durably written.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


class SchedulerWaker(Protocol):
    def wake(self, reason: str, **attributes: str) -> bool: ...


class PubSubWaker:
    def __init__(self, topic: str, *, publisher: Any | None = None) -> None:
        self._topic = topic
        self._publisher = publisher

    @property
    def enabled(self) -> bool:
        return bool(self._topic)

    def _client(self) -> Any:
        if self._publisher is None:
            from google.cloud import pubsub_v1

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def wake(self, reason: str, **attributes: str) -> bool:
        if not self._topic:
            return False
        payload = json.dumps({"reason": reason, **attributes}).encode("utf-8")
        try:
            future = self._client().publish(self._topic, payload, reason=reason, **attributes)
            # Block briefly: a submission that returns before the wake message
            # is durable would make the latency win imaginary.
            future.result(timeout=5)
            return True
        except Exception as exc:  # pragma: no cover - transport level
            log.warning("scheduler wake failed reason=%s: %r", reason, exc)
            return False


class NullWaker:
    """Used when no topic is configured. The safety tick still drains."""

    enabled = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def wake(self, reason: str, **attributes: str) -> bool:
        self.calls.append((reason, dict(attributes)))
        return False
