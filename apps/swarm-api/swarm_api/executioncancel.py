"""Stopping a cancelled task's execution as soon as it is cancelled (#627).

WHY. A cancel used to be a flag: the worker would see it at its next control
poll, or the reconciler would stop the execution once the worker went silent.
Some executions took 7-13 hours to stop that way (four mock steps for 12.8 h on
2026-10-04, two browser steps for 7.6 h on 2026-09-24), all the while holding
their leases and pools. So the first cancel of a task whose attempt may be
executing now asks for that execution to be stopped at once.

WHY NOT FROM HERE. swarm-api holds no compute permission -- no run.*, no
container.*, no swarm-reaper RoleBinding; tests/terraform/iam.tftest.hcl
asserts it -- and it is the service a browser talks to. The reconciler holds
exactly the grants a stop needs, and the backend clients that use them with
their name and label checks. So this module publishes the attempt to
EXECUTION_CANCEL_TOPIC and the reconciler's `/stop-execution` route
(`reconciler.stopexec`) stops it: Cloud Run's `CancelExecution`, or a delete of
the GKE Job with Background propagation.

WHAT IS SENT: the tenant, task and attempt ids, and nothing else. The
reconciler re-reads the task and the attempt and stops the execution the
ATTEMPT recorded, so neither a caller nor this service names a resource -- the
project is shared with another team's cluster, VPC, buckets and service
accounts (scripts/lib/common.sh deny-list).

WHAT THIS DOES NOT DO: release a lease, decrement a pool or write the task.
Stopping the execution sends the worker a SIGTERM; the worker sees the cancel
and ends the task CANCELLED with its spend (`agent_worker.lifecycle.
_handle_interruption`), and if it cannot, the reconciler finds the execution
gone and finishes it (invariants 1-3 are kept where they were kept before).

Never raises: a cancel whose stop request could not be published is still a
cancel, and the reconciler's pass remains the backstop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: The backends an attempt records (`swarm_common.profiles.Backend`), by value.
CLOUD_RUN_JOB = "CLOUD_RUN_JOB"
GKE_AUTOPILOT = "GKE_AUTOPILOT"

#: The publish's bound. The route has answered by the time this runs (it is a
#: background task), but a hung publish would still hold a worker thread.
TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class ExecutionTarget:
    """The execution a cancel should stop: what the attempt document recorded."""

    tenant_id: str
    task_id: str
    attempt_id: str
    backend: str
    execution_name: str


class ExecutionCanceller(Protocol):
    def cancel(self, target: ExecutionTarget) -> str:
        """Ask for it to be stopped. Returns the outcome word; never raises."""


class NoExecutionCanceller:
    """Asks nothing: a deployment with execution cancel turned off."""

    def cancel(self, target: ExecutionTarget) -> str:
        return "disabled"


class PubSubExecutionCanceller:
    """Publishes the attempt to stop on EXECUTION_CANCEL_TOPIC.

    `publisher` is anything with `publish(topic, data, **attributes)` returning
    a future, the way `google.cloud.pubsub_v1.PublisherClient` has it; built on
    first use, so `create_app()` builds no client and a test injects a fake.
    """

    def __init__(self, topic: str, *, publisher: Any | None = None) -> None:
        self._topic = topic
        self._publisher = publisher

    def cancel(self, target: ExecutionTarget) -> str:
        # Ids only: the reconciler re-reads the attempt and stops what IT
        # recorded. The execution name and backend are logged, never sent.
        message = {
            "tenant_id": target.tenant_id,
            "task_id": target.task_id,
            "attempt_id": target.attempt_id,
        }
        try:
            future = self._client().publish(
                self._topic, json.dumps(message).encode("utf-8"), **message
            )
            future.result(timeout=TIMEOUT_SECONDS)
            outcome = "requested"
        except Exception as exc:  # never raises: the reconciler is the backstop
            outcome = f"error:{type(exc).__name__}"
        log.log(
            logging.INFO if outcome == "requested" else logging.WARNING,
            "asked the reconciler to stop a cancelled task's execution",
            extra={
                "tenant_id": target.tenant_id,
                "task_id": target.task_id,
                "attempt_id": target.attempt_id,
                "backend": target.backend,
                "execution": target.execution_name,
                "outcome": outcome,
            },
        )
        return outcome

    def _client(self) -> Any:
        if self._publisher is None:
            from google.cloud import pubsub_v1

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher


__all__ = [
    "CLOUD_RUN_JOB",
    "ExecutionCanceller",
    "ExecutionTarget",
    "GKE_AUTOPILOT",
    "NoExecutionCanceller",
    "PubSubExecutionCanceller",
]
