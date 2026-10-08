"""Stop a cancelled task's execution the moment it is cancelled (#627).

A cancel used to wait for the worker's next control poll, or for this service's
pass to find the worker silent; some executions ran 7-13 h past a cancel (four
mock steps for 12.8 h on 2026-10-04, two browser steps for 7.6 h on
2026-09-24). Now swarm-api publishes the cancelled attempt to
EXECUTION_CANCEL_TOPIC on the task's FIRST cancel, and Pub/Sub pushes it to
`/stop-execution` here.

WHY HERE. This service already holds the stop permissions (swarmJobReaper,
swarmGkeReaper, the namespaced swarm-reaper Role) and the backend clients that
use them; swarm-api holds none, by design. The stop is the same call a repair
makes -- `Backend.terminate` -- after the same by-name read a repair makes
before it (`probe`), so every check those carry applies here unchanged: a
Cloud Run execution must belong to a platform-managed `swarm-` Job in this
project and region, a GKE Job must sit in a `swarm-tenant-` namespace and carry
the managed-by label, and the dispatcher's `pending-` placeholder is never
read as a name.

WHAT THE MESSAGE IS TRUSTED FOR: ids, nothing more. The task and the attempt
are re-read from Firestore, and the execution stopped is the one the ATTEMPT
recorded. The task must be the message's tenant's, must carry the cancel flag,
and the attempt must be that task's and not yet ended; the execution the
backend returns must not name another task or attempt. A request that fails
any of these stops nothing.

WHAT THIS DOES NOT DO: release a lease, decrement a pool, fence or write the
task. The SIGTERMed worker ends the task CANCELLED with its spend; if it
cannot, the next pass finds the execution gone and repairs it with the full
release rules (invariants 1-3 and 5 stay where they were kept).
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Mapping

from .backends import ProbeOutcome
from .detect import sanitised

#: Outcomes in which the execution is stopped or was already over.
STOPPED = frozenset({"stopped", "gone", "finished"})


def parse_push(envelope: Any) -> dict[str, str] | None:
    """The tenant, task and attempt ids of a Pub/Sub push, or None if malformed.

    Read from the message's JSON body, falling back to its attributes, the two
    places `swarm_api.executioncancel` writes them.
    """
    if not isinstance(envelope, Mapping):
        return None
    message = envelope.get("message")
    if not isinstance(message, Mapping):
        return None
    fields: dict[str, Any] = {}
    attributes = message.get("attributes")
    if isinstance(attributes, Mapping):
        fields.update(attributes)
    data = message.get("data")
    if isinstance(data, str) and data:
        try:
            body = json.loads(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError):
            return None
        if isinstance(body, Mapping):
            fields.update(body)
    ids = {key: fields.get(key) for key in ("tenant_id", "task_id", "attempt_id")}
    if not all(isinstance(value, str) and value.strip() for value in ids.values()):
        return None
    return {key: str(value).strip() for key, value in ids.items()}


class ExecutionStopper:
    """Stops one cancelled attempt's execution through the reconciler's backends."""

    def __init__(self, *, store: Any, backends: Mapping[str, Any], logger: Any) -> None:
        self._store = store
        self._backends = dict(backends)
        self._log = logger

    def stop(self, *, tenant_id: str, task_id: str, attempt_id: str) -> str:
        """The outcome word. Raises only when a read or the stop call failed,
        so the push is retried; every decision is an outcome and is acked."""
        outcome, execution = self._decide(tenant_id, task_id, attempt_id)
        log = self._log.info if outcome in STOPPED else self._log.warning
        log(
            "stop request for a cancelled task's execution",
            tenant_id=tenant_id,
            task_id=task_id,
            attempt_id=attempt_id,
            execution=execution,
            outcome=outcome,
        )
        return outcome

    def _decide(self, tenant_id: str, task_id: str, attempt_id: str) -> tuple[str, str | None]:
        task, attempt = self._store.task_and_attempt_docs(task_id, attempt_id)
        if task is None:
            return "no_task", None
        if str(task.get("tenant_id") or "") != tenant_id:
            return "refused_tenant", None
        if not task.get("cancel_requested"):
            # Only a cancelled task is stopped from here; anything else is the
            # pass's to judge, with its own evidence.
            return "not_cancelled", None
        if attempt is None:
            return "no_attempt", None
        if str(attempt.get("task_id") or "") != task_id:
            return "refused_attempt", None
        attempt_tenant = attempt.get("tenant_id")
        if attempt_tenant and str(attempt_tenant) != tenant_id:
            return "refused_tenant", None
        if attempt.get("completed_at") is not None:
            return "attempt_ended", None
        name = attempt.get("execution_name")
        if not isinstance(name, str) or not name:
            return "no_execution", None
        backend = self._backends.get(str(attempt.get("backend") or ""))
        if backend is None:
            return "backend_unavailable", name

        probe = backend.probe(name)
        if probe.outcome is ProbeOutcome.ABSENT:
            return "gone", name
        if probe.outcome is ProbeOutcome.FINISHED:
            return "finished", name
        if probe.outcome is not ProbeOutcome.ACTIVE or probe.execution is None:
            # Not ours by label, outside the prefix, a placeholder, or not
            # readable: nothing proven, nothing stopped. The pass is the backstop.
            return "unreadable", name
        view = probe.execution
        if view.task_id and view.task_id != task_id:
            return "refused_mismatch", name
        if view.attempt_id and view.attempt_id != attempt_id:
            return "refused_mismatch", name
        if view.tenant_id and sanitised(str(view.tenant_id)) != sanitised(tenant_id):
            return "refused_tenant", name
        return ("stopped" if backend.terminate(view) else "unconfirmed"), name


__all__ = ["ExecutionStopper", "STOPPED", "parse_push"]
