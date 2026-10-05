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


def topic_path(project_id: str, topic: str) -> str:
    """`projects/<project>/topics/<name>` from a bare name; a full path as given.

    terraform renders DISPATCH_TOPIC as the bare name (terraform/infra/locals.tf
    `wake_topic`), and Pub/Sub's publish accepts only the full path. Handed the
    bare name, every wake was refused -- counted in
    `swarm_api_scheduler_wake_failures_total` and otherwise silent, because the
    safety tick drained the queue anyway, a minute later (#636).
    """
    topic = (topic or "").strip()
    if not topic or topic.startswith("projects/"):
        return topic
    return f"projects/{project_id}/topics/{topic}"


class PubSubWaker:
    def __init__(
        self, topic: str, *, project_id: str = "", publisher: Any | None = None
    ) -> None:
        self._topic = topic_path(project_id, topic) if project_id else topic.strip()
        self._publisher = publisher

    @property
    def topic(self) -> str:
        return self._topic

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


def waker_for(settings: Any) -> SchedulerWaker:
    """The deployment's waker: Pub/Sub on DISPATCH_TOPIC, or none without one."""
    topic = str(getattr(settings, "dispatch_topic", "") or "").strip()
    if not topic:
        return NullWaker()
    return PubSubWaker(topic, project_id=str(getattr(settings, "project_id", "") or ""))
