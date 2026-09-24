"""Firestore access for the admission controller.

The hot path is deliberately narrow: one ordered query for READY work, one read
per pool inside the admission transaction, and nothing else. Every wider sweep
(dependency promotion, prewarm) is bounded and runs once per drain, not once per
task.

Composite indexes this module relies on (owned by the terraform track). Both of
these exist today as `tasks-state-priority-created` and
`tasks-tenant-state-priority-created`:

    tasks:  state ASC, priority DESC, created_at ASC
    tasks:  tenant_id ASC, state ASC, priority DESC, created_at ASC

`parked_tasks` and `task_states` use equality filters only, which Firestore
serves from single-field indexes by merge join, so they need no composite index.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import (
    AdmissionConfig,
    acquire_lease_in_transaction,
    release_lease_in_transaction,
)
from swarm_common.models import (
    Attempt,
    Lease,
    QuotaState,
    SlotPool,
    Task,
    TaskEvent,
    Tenant,
    new_id,
    retries_exhausted,
    utcnow,
)
from swarm_common.states import EventType, ParkReason, TaskState, assert_transition

from .codec import pool_from_dict, quota_from_dict, task_from_dict, tenant_from_dict

log = logging.getLogger(__name__)

TASKS = "tasks"
TENANTS = "tenants"
POOLS = "pools"
QUOTA = "quota"
LEASES = "leases"
ATTEMPTS = "attempts"
CONTROL = "control"
EVENTS = "events"
CONTROL_DOC = "dispatch"


class SchedulerStore:
    def __init__(self, db: Any, *, now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._now = now

    @property
    def db(self) -> Any:
        return self._db

    # -- control ----------------------------------------------------------

    def dispatch_paused(self) -> bool:
        snap = self._db.collection(CONTROL).document(CONTROL_DOC).get()
        if not snap.exists:
            return False
        return bool(snap.to_dict().get("dispatch_paused", False))

    # -- queries ----------------------------------------------------------

    def ready_tasks(self, limit: int) -> list[Task]:
        """READY work, highest priority first and oldest first within a priority.

        Ordering here is the DATABASE's contribution to fairness; the
        round-robin interleave in `fairness.py` is the scheduler's. Both are
        needed: this query decides which slice of a large backlog we look at,
        the interleave decides the order we try that slice in.
        """
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("state", "==", TaskState.READY.value))
            .order_by("priority", direction=firestore.Query.DESCENDING)
            .order_by("created_at", direction=firestore.Query.ASCENDING)
            .limit(limit)
        )
        return [task_from_dict(snap.to_dict()) for snap in query.stream()]

    def ready_tasks_for_tenant(self, tenant_id: str, limit: int) -> list[Task]:
        """One tenant's own READY work, in the same order as the global query.

        This is the escape hatch from a priority flood. `ready_tasks` returns the
        globally highest-priority slice, so a tenant holding more than
        `candidate_batch_size` high-priority tasks fills that slice completely
        and every other tenant vanishes from it -- at which point the
        round-robin interleave has nothing of theirs to interleave and the aging
        bonus has nothing to lift. Asking per tenant is what makes "one tenant
        cannot starve others" true of the QUEUE rather than only of the slice.

        Served by the `tasks-tenant-state-priority-created` composite index.
        """
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("state", "==", TaskState.READY.value))
            .order_by("priority", direction=firestore.Query.DESCENDING)
            .order_by("created_at", direction=firestore.Query.ASCENDING)
            .limit(limit)
        )
        return [task_from_dict(snap.to_dict()) for snap in query.stream()]

    def enabled_tenant_ids(self, limit: int) -> list[str]:
        """Tenants that may currently be admitted, oldest registration first.

        One small document per tenant, read once per drain rather than once per
        pass. A disabled tenant is skipped here so the top-up never spends a
        query discovering work it would refuse to admit anyway.
        """
        out: list[str] = []
        for snap in self._db.collection(TENANTS).limit(limit).stream():
            data = snap.to_dict() or {}
            if data.get("enabled", True):
                out.append(str(data.get("tenant_id") or snap.id))
        return out

    def parked_tasks(self, reason: ParkReason, limit: int) -> list[Task]:
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
            .where(filter=FieldFilter("park_reason", "==", reason.value))
            .limit(limit)
        )
        return [task_from_dict(snap.to_dict()) for snap in query.stream()]

    def get_task(self, task_id: str) -> Task | None:
        snap = self._db.collection(TASKS).document(task_id).get()
        if not snap.exists:
            return None
        return task_from_dict(snap.to_dict())

    def task_states(self, task_ids: Sequence[str]) -> dict[str, TaskState]:
        """States of a task's parents. `depends_on` is capped at the workflow
        step limit, so this is a bounded number of point reads, not a scan."""
        states: dict[str, TaskState] = {}
        for task_id in dict.fromkeys(task_ids):
            snap = self._db.collection(TASKS).document(task_id).get()
            if snap.exists:
                states[task_id] = TaskState(snap.to_dict()["state"])
        return states

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        if not snap.exists:
            return None
        return tenant_from_dict(snap.to_dict())

    def pools(self) -> dict[str, SlotPool]:
        return {
            snap.id: pool_from_dict(snap.id, snap.to_dict())
            for snap in self._db.collection(POOLS).stream()
        }

    def active_by_tenant(self) -> dict[str, int]:
        """Live lease count per tenant, read from the tenant pools.

        The pool's `active` is the authoritative count -- it is only ever
        mutated inside the admission and release transactions -- so this needs
        no aggregation query over tasks.
        """
        out: dict[str, int] = {}
        for name, pool in self.pools().items():
            if name.startswith("tenant:") and ":tenant:" not in name:
                out[name.split(":", 1)[1]] = pool.active
        return out

    def quota_states(self, limit: int = 500) -> list[QuotaState]:
        return [
            quota_from_dict(snap.to_dict())
            for snap in self._db.collection(QUOTA).limit(limit).stream()
        ]

    # -- transitions ------------------------------------------------------

    def record_blockers(self, task: Task, blockers: list[dict[str, Any]]) -> None:
        """Surface why a READY task was not admitted, without changing state.

        The task stays READY. Parking it would be wrong -- the platform being
        busy is not a durable condition -- and blocking the loop on it would let
        one full pool stall every other tenant's work.
        """
        self._db.collection(TASKS).document(task.id).update(
            {"blocked_by": blockers, "updated_at": self._now()}
        )

    def park(self, task: Task, reason: ParkReason, *, next_eligible_at: datetime | None = None,
             detail: dict[str, Any] | None = None) -> None:
        assert_transition(task.state, TaskState.PARKED)
        patch: dict[str, Any] = {
            "state": TaskState.PARKED.value,
            "park_reason": reason.value,
            "updated_at": self._now(),
        }
        if next_eligible_at is not None:
            patch["next_eligible_at"] = next_eligible_at
        self._db.collection(TASKS).document(task.id).update(patch)
        self.append_event(task, EventType.PARKED, {"reason": reason.value, **(detail or {})})

    def promote_to_ready(self, task: Task, *, detail: dict[str, Any] | None = None) -> None:
        assert_transition(task.state, TaskState.READY)
        self._db.collection(TASKS).document(task.id).update(
            {
                "state": TaskState.READY.value,
                "park_reason": None,
                "next_eligible_at": None,
                "blocked_by": [],
                "updated_at": self._now(),
            }
        )
        self.append_event(task, EventType.READY, detail or {})

    def mark_dispatched(self, task: Task, lease: Lease, execution_name: str, backend: str) -> None:
        assert_transition(TaskState.LEASED, TaskState.DISPATCHED)
        now = self._now()
        self._db.collection(TASKS).document(task.id).update(
            {"state": TaskState.DISPATCHED.value, "updated_at": now}
        )
        self._db.collection(LEASES).document(lease.lease_id).update(
            {"state": TaskState.DISPATCHED.value}
        )
        self._db.collection(ATTEMPTS).document(lease.attempt_id).update(
            {"execution_name": execution_name}
        )
        self.append_event(
            task,
            EventType.DISPATCHED,
            {"execution_name": execution_name, "backend": backend},
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
            generation=lease.generation,
        )

    def return_to_ready_after_failed_dispatch(
        self,
        task: Task,
        lease: Lease,
        error_code: str,
        *,
        correlation_id: str | None = None,
        retry_delay_seconds: int = 30,
    ) -> None:
        """Dispatch failed after the lease was taken: give the capacity back.

        Leaving the lease in place would hold a slot that no container will ever
        occupy, and the reconciler would not reclaim it until the dispatch
        deadline passed -- minutes of a pool sitting idle but full.

        `next_eligible_at` is pushed out by a short backoff so the same drain
        does not immediately re-lease the task and fail again: a backend that is
        refusing one dispatch is usually about to refuse the next, and a tight
        retry would burn the whole run's lease budget on one broken task.

        `error_code` is a STABLE CODE, not the upstream exception text. This
        field is returned to the tenant by `codec.task_to_api`, and a Cloud Run
        or Kubernetes error echoes the resource it was given -- the tenant
        service account email, the job name, the secret names in the manifest.
        The correlation id (the attempt id) is what ties the tenant's copy to the
        operator's log line, which has the full message.
        """
        now = self._now()
        reference = correlation_id or lease.attempt_id
        public = f"{error_code} (attempt {reference})"
        self.release_lease(lease.lease_id, reason=f"dispatch_failed: {error_code}"[:200])

        # THE CAP IS ENFORCED HERE TOO, and its absence was a real outage.
        # This path returns a task to READY after a dispatch failure, and it
        # used to do so unconditionally -- so a task whose dispatch kept failing
        # retried for ever. The backoff below spaces the attempts out and the
        # docstring reasons about it carefully; nothing counted them.
        # `task_d18d8d8b044d469cb43c` reached 83 attempts against a cap of 3 on
        # 2026-09-23, re-dispatching every 30s for hours on
        # gke_create_job_failed, and was stopped by hand.
        #
        # The rule lives in swarm_common so the reconciler's repair path and
        # this one cannot disagree again -- they are separate images and cannot
        # import each other, so a shared predicate is the only home that is not
        # a restatement.
        exhausted = retries_exhausted(task.attempt_count, task.max_attempts)
        target = TaskState.FAILED if exhausted else TaskState.READY
        assert_transition(TaskState.LEASED, target)
        payload: dict[str, Any] = {
            "state": target.value,
            "current_lease_id": None,
            "last_error": public[:1000],
            "updated_at": now,
        }
        if exhausted:
            # A terminal task needs a completion time; it will never be
            # eligible again, so a next_eligible_at would be a lie about a
            # retry that is not coming.
            payload["completed_at"] = now
        else:
            payload["next_eligible_at"] = now + timedelta(seconds=max(0, retry_delay_seconds))
        self._db.collection(TASKS).document(task.id).update(payload)
        self.append_event(
            task,
            EventType.LEASE_RELEASED,
            {
                "reason": "dispatch_failed",
                "error_code": error_code,
                "correlation_id": reference,
            },
            lease_id=lease.lease_id,
            generation=lease.generation,
        )

    def cancel(self, task: Task, reason: str, detail: dict[str, Any] | None = None) -> None:
        """Terminate a task that can never become runnable.

        Only called for tasks that hold NO capacity, so there is no lease to
        release: a task holding a lease is cancelled through the worker or the
        reconciler, which are the only components that know whether a container
        is still running.
        """
        assert_transition(task.state, TaskState.CANCELLED)
        now = self._now()
        self._db.collection(TASKS).document(task.id).update(
            {
                "state": TaskState.CANCELLED.value,
                "park_reason": None,
                "blocked_by": [],
                "completed_at": now,
                "last_error": reason[:1000],
                "updated_at": now,
            }
        )
        self.append_event(task, EventType.CANCELLED, {"reason": reason, **(detail or {})})

    def append_event(
        self,
        task: Task,
        type: EventType,
        detail: dict[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
        lease_id: str | None = None,
        generation: int | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=new_id("ev"),
            task_id=task.id,
            tenant_id=task.tenant_id,
            type=type,
            at=self._now(),
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            detail=dict(detail or {}),
        )
        payload = {
            "event_id": event.event_id,
            "task_id": event.task_id,
            "tenant_id": event.tenant_id,
            "type": event.type.value,
            "at": event.at,
            "attempt_id": event.attempt_id,
            "lease_id": event.lease_id,
            "generation": event.generation,
            "detail": event.detail,
        }
        (
            self._db.collection(TASKS)
            .document(task.id)
            .collection(EVENTS)
            .document(event.event_id)
            .set(payload)
        )
        return event

    def create_attempt(self, task: Task, lease: Lease, backend: str) -> Attempt:
        attempt = Attempt(
            attempt_id=lease.attempt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            generation=lease.generation,
            lease_id=lease.lease_id,
            backend=backend,
            created_at=self._now(),
        )
        self._db.collection(ATTEMPTS).document(attempt.attempt_id).set(
            {
                "attempt_id": attempt.attempt_id,
                "task_id": attempt.task_id,
                "tenant_id": attempt.tenant_id,
                "generation": attempt.generation,
                "lease_id": attempt.lease_id,
                "backend": attempt.backend,
                "created_at": attempt.created_at,
                "execution_name": None,
                "started_at": None,
                "completed_at": None,
                "exit_code": None,
                "error": None,
                "checkpoints": [],
                "oom_near_miss": False,
            }
        )
        return attempt

    # -- admission --------------------------------------------------------

    def acquire_lease(
        self,
        task: Task,
        *,
        units: int,
        backend: str,
        config: AdmissionConfig,
    ) -> Lease:
        """Admit one task. Raises AdmissionDenied if it cannot be admitted.

        The frozen `acquire_lease_in_transaction` is called INSIDE
        `firestore.transactional`, which is what makes the last-slot race
        resolve to exactly one winner: if another scheduler mutated a pool this
        function read, Firestore aborts and re-runs the whole body against fresh
        reads rather than committing a stale increment.
        """
        transaction = self._db.transaction()

        @firestore.transactional
        def _acquire(txn: Any) -> Lease:
            return acquire_lease_in_transaction(
                txn,
                db=self._db,
                task=task,
                units=units,
                backend=backend,
                config=config,
            )

        return _acquire(transaction)

    def release_lease(self, lease_id: str, *, reason: str) -> bool:
        transaction = self._db.transaction()

        @firestore.transactional
        def _release(txn: Any) -> bool:
            return release_lease_in_transaction(
                txn, db=self._db, lease_id=lease_id, reason=reason
            )

        return _release(transaction)

    # -- misc -------------------------------------------------------------

    def iter_chunks(self, items: Iterable[Any], size: int) -> Iterable[list[Any]]:
        chunk: list[Any] = []
        for item in items:
            chunk.append(item)
            if len(chunk) >= size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk
