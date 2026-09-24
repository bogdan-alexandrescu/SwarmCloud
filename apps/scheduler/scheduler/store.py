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
So do `workflow_steps_in_state` (tenant_id, workflow_id, state, all `==`), the
point read in `workflow_on_step_failure`, and `tasks_in_state` (state `==`),
which only the read-only `on_step_failure_audit` uses.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import (
    AdmissionConfig,
    _snapshot,
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
    Workflow,
    new_id,
    retries_exhausted,
    utcnow,
)
from swarm_common.states import (
    PENDING_STATES,
    EventType,
    ParkReason,
    TaskState,
    assert_transition,
)

from .codec import (
    as_datetime,
    pool_from_dict,
    quota_from_dict,
    task_from_dict,
    tenant_from_dict,
)

log = logging.getLogger(__name__)

TASKS = "tasks"
TENANTS = "tenants"
POOLS = "pools"
QUOTA = "quota"
LEASES = "leases"
ATTEMPTS = "attempts"
CONTROL = "control"
EVENTS = "events"
WORKFLOWS = "workflows"
CONTROL_DOC = "dispatch"

#: What a workflow document with no `on_step_failure` field means: the frozen
#: dataclass default, read from the dataclass rather than restated. The API
#: decodes a missing field the same way (`swarm_api.codec.workflow_from_dict`),
#: and the scheduler must act on the value the tenant is shown.
_ON_STEP_FAILURE_DEFAULT: str = Workflow.__dataclass_fields__["on_step_failure"].default


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

    def workflow_on_step_failure(
        self,
        tenant_id: str,
        workflow_id: str,
        *,
        enforced_since: datetime | None = None,
    ) -> str | None:
        """The workflow's `on_step_failure`, or None when there is no policy to act on.

        None when the document is missing, and when it belongs to a different
        tenant than the step asking. The API never writes either, so both mean a
        corrupt or forged task. The caller falls back to the rule that needs no
        policy (cancel the dependents of a failed parent) rather than guessing
        `fail_workflow`, because a cancel cannot be undone.

        None as well, under a cutoff (`enforced_since`, from
        ON_STEP_FAILURE_ENFORCED_SINCE), for a workflow created before it. Such
        a workflow was submitted while the setting was documented as not
        honoured, so it keeps the dependency rule it was submitted under. A
        document with no readable `created_at` is treated the same way under a
        cutoff: when it was submitted cannot be told, and a cancel cannot be
        undone.

        A document that exists but has no field reads as the frozen default,
        which is what the API shows the tenant for the same document.
        """
        snap = self._db.collection(WORKFLOWS).document(workflow_id).get()
        if not snap.exists:
            log.warning(
                "workflow %s named by a step of tenant %s has no document; "
                "on_step_failure cannot be applied", workflow_id, tenant_id,
            )
            return None
        data = snap.to_dict() or {}
        if data.get("tenant_id") != tenant_id:
            log.warning(
                "workflow %s belongs to tenant %r, not %r; its on_step_failure is "
                "not applied to that tenant's step", workflow_id, data.get("tenant_id"),
                tenant_id,
            )
            return None
        if enforced_since is not None:
            try:
                created_at = as_datetime(data.get("created_at"))
            except (TypeError, ValueError):
                created_at = None
            if created_at is None or created_at < enforced_since:
                log.debug(
                    "workflow %s was created at %s, before on_step_failure was "
                    "enforced (%s); only the dependency rule applies to it",
                    workflow_id, created_at, enforced_since.isoformat(),
                )
                return None
        value = data.get("on_step_failure")
        if value is None:
            return _ON_STEP_FAILURE_DEFAULT
        return str(value).strip().lower()

    def tasks_in_state(self, state: TaskState, limit: int) -> list[Task]:
        """Every tenant's tasks in one state, up to `limit`. Equality filter only.

        Used by the read-only `on_step_failure_audit`, never by a drain: a drain
        reaches waiting work through the queries above, each scoped to the one
        thing it is looking for.
        """
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("state", "==", state.value))
            .limit(limit)
        )
        return [task_from_dict(snap.to_dict()) for snap in query.stream()]

    def workflow_steps_in_state(
        self, tenant_id: str, workflow_id: str, state: TaskState, limit: int
    ) -> list[Task]:
        """This tenant's steps of one workflow in one state.

        Scoped by `tenant_id` as well as `workflow_id`, so nothing a workflow
        sweep does can reach another tenant's task, even one whose document names
        this workflow (invariant 9). Equality filters only, so no composite
        index is needed. The query is bounded by the workflow step limit and
        does not grow with history.
        """
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("workflow_id", "==", workflow_id))
            .where(filter=FieldFilter("state", "==", state.value))
            .limit(limit)
        )
        return [task_from_dict(snap.to_dict()) for snap in query.stream()]

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
    ) -> bool:
        """Dispatch failed after the lease was taken: give the capacity back.

        Returns True when this was the task's last attempt and it is now FAILED.
        The drain needs that answer: a FAILED workflow step under
        `on_step_failure: fail_workflow` must stop its siblings in the SAME
        drain, and a verdict the drain read before this write would say the
        workflow was healthy.

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
        #
        # AND THE COUNT IS READ FROM THE DOCUMENT, inside a transaction, never
        # from `task`. `task` is the snapshot the drain loop read while the task
        # was still READY; `acquire_lease_in_transaction` then incremented
        # `attempt_count` in Firestore and nowhere else. Checking the cap
        # against `task.attempt_count` was therefore always one attempt behind:
        # the third failed dispatch of three read 2, returned the task to
        # READY, and the next drain leased it a FOURTH time.
        # `task_b568a623be8645eb87c6` ended FAILED at attempt_count 4 against a
        # cap of 3 on 2026-09-24 exactly this way. The reconciler's repair path
        # has always read the document (reconciler/store.py); this one now does
        # too, and the unit tests that fed it the post-increment count -- and so
        # agreed with the bug -- feed it what the loop actually holds.
        task_ref = self._db.collection(TASKS).document(task.id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _settle(txn: Any) -> tuple[bool, int, int]:
            snap = _snapshot(txn.get(task_ref))
            stored = (snap.to_dict() or {}) if snap.exists else {}
            attempt_count = int(stored.get("attempt_count", 0))
            max_attempts = int(stored.get("max_attempts", task.max_attempts))
            exhausted = retries_exhausted(attempt_count, max_attempts)
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
                # retry that is not coming. CLEARED, not merely omitted: every
                # task that reaches its last attempt through this path carries
                # the retry time the PREVIOUS failure wrote, and an update that
                # leaves the field alone leaves that promise on a FAILED task.
                payload["completed_at"] = now
                payload["next_eligible_at"] = None
            else:
                payload["next_eligible_at"] = now + timedelta(
                    seconds=max(0, retry_delay_seconds)
                )
            txn.update(task_ref, payload)
            return exhausted, attempt_count, max_attempts

        exhausted, attempt_count, max_attempts = _settle(transaction)
        self.append_event(
            task,
            EventType.LEASE_RELEASED,
            {
                "reason": "dispatch_failed",
                "error_code": error_code,
                "correlation_id": reference,
                # The numbers the cap was decided on, so a task that went FAILED
                # here says why in its own timeline.
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "retries_exhausted": exhausted,
            },
            lease_id=lease.lease_id,
            generation=lease.generation,
        )
        return exhausted

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

    def cancel_if_not_started(
        self, task: Task, reason: str, detail: dict[str, Any] | None = None
    ) -> bool:
        """Cancel a step only if it STILL has not started. Returns True if it did.

        `cancel` above is a blind write. That is safe for a step the caller has
        just read PARKED behind a failed parent, because nothing can lease a
        PARKED task. It is not safe for the workflow sweep. The sweep cancels
        READY steps, and a concurrent drain can lease one between the sweep's
        query and its write. A blind CANCELLED over a LEASED task is wrong in
        two ways. First, the lease stays counted in every pool it reserved
        until the reconciler's `detect_orphan_leases` finds a terminal task
        holding it. Second, the drain that took the lease goes on to dispatch
        and `mark_dispatched` writes DISPATCHED over the CANCELLED, so a step of
        a failed workflow starts after all, and its record goes backwards out
        of a terminal state.

        So the state is re-read INSIDE the transaction. The write happens only
        while the step is still SUBMITTED, QUEUED, READY or PARKED, which are
        exactly the states that hold no capacity (invariant 1).
        `acquire_lease_in_transaction` reads the same document in its own
        transaction and refuses anything that is not READY, so whichever commits
        second sees the first. The two can never both win.
        """
        task_ref = self._db.collection(TASKS).document(task.id)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _cancel(txn: Any) -> TaskState | None:
            snap = _snapshot(txn.get(task_ref))
            if not snap.exists:
                return None
            stored = snap.to_dict() or {}
            try:
                current = TaskState(stored.get("state"))
            except ValueError:
                return None
            if current not in PENDING_STATES:
                return None
            assert_transition(current, TaskState.CANCELLED)
            txn.update(
                task_ref,
                {
                    "state": TaskState.CANCELLED.value,
                    "park_reason": None,
                    "blocked_by": [],
                    "next_eligible_at": None,
                    "completed_at": now,
                    "last_error": reason[:1000],
                    "updated_at": now,
                },
            )
            return current

        prior = _cancel(transaction)
        if prior is None:
            return False
        self.append_event(
            task,
            EventType.CANCELLED,
            {"reason": reason, "from_state": prior.value, **(detail or {})},
        )
        return True

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
