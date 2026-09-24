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

EVERY WRITE THAT MOVES A TASK IS GUARDED, and that costs reads on purpose.
`park`, `promote_to_ready`, `cancel`, `cancel_if_not_started`,
`record_blockers`, `mark_dispatched` and
`return_to_ready_after_failed_dispatch` each decide from a snapshot the drain
read earlier -- the READY slice, a sweep's PARKED rows, the lease admission
handed back. They used to write with a blind `update`, so anything another
process committed in between was overwritten by a decision about a document
that no longer existed: a user's cancel parked or promoted back into the queue,
another scheduler's lease cancelled or promoted over (and then leased a second
time), a RUNNING worker rewound to DISPATCHED, a re-leased task returned to
READY with its new lease orphaned (incident wf_ebb3ab2d65664707a559, F-9).

Each is now ONE transaction that re-reads the task and writes only if it is
still in the state the decision was made against. When it is not, the write is
SKIPPED -- never forced -- and the caller gets a `GuardedWrite` saying so, which
the loop logs and counts (`DrainReport.stale_writes`,
`swarm_scheduler_stale_writes_total{write, reason}`). That is one extra read per
park, promotion, cancel or denial and three per dispatch, against a hot path
that was otherwise "one query and the pool reads": the price of not
overwriting, and small beside the admission transaction each dispatch already
pays.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    CONCURRENCY_STATES,
    PENDING_STATES,
    TERMINAL_STATES,
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

#: Why a guarded write was skipped. Stable codes: they are the `reason` label on
#: `swarm_scheduler_stale_writes_total`, so a dashboard can tell "somebody
#: cancelled it" from "the reconciler fenced this attempt".
#:
#:   task_missing         the document is gone;
#:   state_changed        it is no longer in the state the decision was made
#:                        from (cancelled, leased by another scheduler, advanced
#:                        by its worker, finished);
#:   park_reason_changed  still PARKED, but for a different reason than the one
#:                        a promotion was decided on;
#:   lease_superseded     the task has moved to a newer generation or another
#:                        lease -- the reconciler fenced this attempt.
TASK_MISSING = "task_missing"
STATE_CHANGED = "state_changed"
PARK_REASON_CHANGED = "park_reason_changed"
LEASE_SUPERSEDED = "lease_superseded"

#: States in which the task's next move belongs to its WORKER. A failed dispatch
#: that finds its own lease here must not release it: the create call reported
#: failure (a timeout, say) but the execution exists and is running under it.
_WORKER_OWNED = frozenset(
    {TaskState.DISPATCHED.value, TaskState.STARTING.value, TaskState.RUNNING.value}
)
_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)
_HOLDS_CAPACITY = frozenset(state.value for state in CONCURRENCY_STATES)
#: States a step has "not started" in: they hold no capacity (invariant 1), so
#: `cancel_if_not_started` may cancel from any of them. `_NOT_STARTED` names
#: that precondition in a skip's log line and in its `GuardedWrite.expected`.
_NOT_STARTED_VALUES = frozenset(state.value for state in PENDING_STATES)
_NOT_STARTED = "/".join(sorted(_NOT_STARTED_VALUES))

#: The event a task's return is announced with when this store finishes a
#: reclaim the reconciler left half done -- the same mapping the reconciler's
#: own repair announces with (`reconciler/repair.py`, `_EVENT_FOR_REPAIR`).
_EVENT_FOR_RETURN = {
    TaskState.READY: EventType.READY,
    TaskState.FAILED: EventType.FAILED,
    TaskState.CANCELLED: EventType.CANCELLED,
}

#: What `return_to_ready_after_failed_dispatch` found when the reconciler had
#: fenced this attempt and the task still points at its lease.
_RECLAIM_PENDING = "pending"    # lease unreleased: left for the reconciler
_RECLAIM_FINISHED = "finished"  # lease released, task not returned: finished here


@dataclass(frozen=True)
class GuardedWrite:
    """What one guarded transition did.

    `applied` False means nothing was written to the task: it had moved on
    since the drain read it, and the decision made from that read no longer
    applies. `reason` is one of the codes above, `expected` the state the write
    required and `found` the state the transaction read (None when the document
    is gone). `target` is the state written, when one was.
    """

    write: str
    applied: bool
    expected: str
    found: str | None
    reason: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class _Settled:
    """`return_to_ready_after_failed_dispatch`'s transaction, as data, so the
    event and the log line are written once, after it commits -- never once per
    retried attempt of the body."""

    outcome: GuardedWrite
    released: bool
    attempt_count: int = 0
    max_attempts: int = 0
    exhausted: bool = False
    #: Set only when the reconciler had fenced this attempt and the task still
    #: points at its lease: `_RECLAIM_PENDING` or `_RECLAIM_FINISHED`.
    reclaim: str | None = None
    #: The task's generation as the transaction read it, for the log line.
    task_generation: int | None = None


def _lease_refusal(stored: dict[str, Any] | None, lease: Lease) -> str | None:
    """Why a write made on behalf of `lease` no longer applies, or None.

    The two writes that follow admission require the task to be exactly what
    admission left: LEASED, pointing at this lease, at this generation. The
    generation is checked first because it is what the reconciler moves FIRST
    (`invalidate_generation`, then release, then repair), so a reclaim in
    progress reads as superseded rather than as a state it has not reached yet.

    Superseded is therefore NOT the same as "this lease backs nothing". A task
    still pointing at the lease at a newer generation is a reclaim that has not
    finished, and `return_to_ready_after_failed_dispatch` must not release it
    on this code alone -- see its docstring.
    """
    if stored is None:
        return TASK_MISSING
    held = stored.get("current_lease_id") or None
    if int(stored.get("current_generation", 0)) != lease.generation:
        return LEASE_SUPERSEDED
    if held is not None and held != lease.lease_id:
        return LEASE_SUPERSEDED
    if stored.get("state") != TaskState.LEASED.value:
        return STATE_CHANGED
    if held is None:
        return LEASE_SUPERSEDED
    return None


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
    #
    # Every method below returns a `GuardedWrite`. None of them forces a write
    # onto a task that has moved on since the drain read it; see the module
    # docstring for what used to happen when they did.

    def _write_if_unchanged(
        self,
        write: str,
        task: Task,
        patch: dict[str, Any],
        *,
        same_park_reason: bool = False,
    ) -> GuardedWrite:
        """Apply `patch` only if the task is still in `task.state`.

        One transaction: the re-read and the write commit together or not at
        all. A concurrent writer either commits first -- and this re-runs, sees
        it, and skips -- or commits after, and sees this. `same_park_reason`
        also requires the stored park reason to be the one the caller read: a
        promotion is a decision about WHY the task was parked, not only that it
        was.

        `cancel_requested` is deliberately NOT part of this precondition. The
        API never flags a SUBMITTED/QUEUED/READY/PARKED task -- it cancels it
        outright (`swarm_api.store.request_cancel`) -- so a cancel landing
        between the drain's read and these writes changes the STATE, which is
        checked. A PARKED task that does carry the flag -- a worker that hits a
        quota park before its next poll of the flag would leave one; the worker's
        park does not clear it -- must still be promotable: READY is where the
        drain's `_admit_one` cancels it, and refusing the promotion would leave
        it PARKED for ever.
        """
        task_ref = self._db.collection(TASKS).document(task.id)
        expected = task.state.value
        expected_reason = task.park_reason.value if task.park_reason else None
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> GuardedWrite:
            snap = _snapshot(txn.get(task_ref))
            stored = (snap.to_dict() or {}) if snap.exists else None
            found = stored.get("state") if stored is not None else None
            if stored is None:
                reason = TASK_MISSING
            elif found != expected:
                reason = STATE_CHANGED
            elif same_park_reason and (stored.get("park_reason") or None) != expected_reason:
                reason = PARK_REASON_CHANGED
            else:
                txn.update(task_ref, patch)
                return GuardedWrite(write, True, expected, found, None, patch.get("state"))
            return GuardedWrite(write, False, expected, found, reason)

        outcome = _apply(transaction)
        if not outcome.applied:
            self._log_skip(outcome, task)
        return outcome

    def _log_skip(
        self,
        outcome: GuardedWrite,
        task: Task,
        *,
        lease: Lease | None = None,
        level: int = logging.INFO,
        consequence: str = "nothing written",
    ) -> None:
        """One line per skipped write, naming what was expected and found.

        INFO by default: under concurrency a skip is the guard doing its job (a
        user cancelled, another scheduler got there first). The two writes that
        follow admission log WARNING, because a skip there means a container was
        started or refused for an attempt that is no longer current.
        """
        log.log(
            level,
            "skipped %s task=%s%s: expected %s, found %s (%s); %s",
            outcome.write,
            task.id,
            f" lease={lease.lease_id} generation={lease.generation}" if lease else "",
            outcome.expected,
            outcome.found or "no document",
            outcome.reason,
            consequence,
        )

    def record_blockers(self, task: Task, blockers: list[dict[str, Any]]) -> GuardedWrite:
        """Surface why a READY task was not admitted, without changing state.

        The task stays READY. Parking it would be wrong -- the platform being
        busy is not a durable condition -- and blocking the loop on it would let
        one full pool stall every other tenant's work.

        Guarded like the transitions: a denial of `not_ready` is admission
        telling this drain that another scheduler has just LEASED the task, and
        written unconditionally it became that LEASED task's `blocked_by` -- the
        field the console shows as why a task is waiting.
        """
        return self._write_if_unchanged(
            "record_blockers", task, {"blocked_by": blockers, "updated_at": self._now()}
        )

    def park(self, task: Task, reason: ParkReason, *, next_eligible_at: datetime | None = None,
             detail: dict[str, Any] | None = None) -> GuardedWrite:
        assert_transition(task.state, TaskState.PARKED)
        patch: dict[str, Any] = {
            "state": TaskState.PARKED.value,
            "park_reason": reason.value,
            "updated_at": self._now(),
        }
        if next_eligible_at is not None:
            patch["next_eligible_at"] = next_eligible_at
        outcome = self._write_if_unchanged("park", task, patch)
        if outcome.applied:
            self.append_event(task, EventType.PARKED, {"reason": reason.value, **(detail or {})})
        return outcome

    def promote_to_ready(self, task: Task, *, detail: dict[str, Any] | None = None) -> GuardedWrite:
        assert_transition(task.state, TaskState.READY)
        outcome = self._write_if_unchanged(
            "promote",
            task,
            {
                "state": TaskState.READY.value,
                "park_reason": None,
                "next_eligible_at": None,
                "blocked_by": [],
                "updated_at": self._now(),
            },
            same_park_reason=True,
        )
        if outcome.applied:
            self.append_event(task, EventType.READY, detail or {})
        return outcome

    def mark_dispatched(
        self, task: Task, lease: Lease, execution_name: str, backend: str
    ) -> GuardedWrite:
        """Record that the backend accepted the execution for `lease`.

        THE TASK moves LEASED -> DISPATCHED only if it is still LEASED on this
        lease at this generation. Two writers can get there first:

          * its WORKER. A container that starts before this write lands finds
            the task at LEASED and walks it to RUNNING itself
            (`ControlPlane.advance_to_running`). DISPATCHED over RUNNING was not
            just a wrong label: DISPATCHED -> SUCCEEDED is illegal, so the
            worker could no longer finish its own task;
          * the RECONCILER, when the create call outlived the dispatch deadline:
            it fenced the generation, released the lease and returned the task
            to READY. DISPATCHED over that is a task claiming capacity it does
            not hold, that no drain will ever look at again.

        THE LEASE moves on its own condition -- unreleased and still LEASED --
        and so it moves in the first case too. After a lease's first heartbeat
        the reconciler treats one still at LEASED past its dispatch deadline as
        overdue (`reconciler/detect.py`, `overdue_dispatch`), so leaving it
        there would reap a healthy worker. A released lease is never relabelled.

        THE ATTEMPT always records its execution when the attempt exists: it is
        a fact about this attempt, which only this admission minted.

        `cancel_requested` does not stop this write. The API flags a LEASED task
        and waits for "the worker or the reconciler"; the execution now exists,
        and its worker is what honours the flag. The update touches only
        `state`, so the flag survives.
        """
        assert_transition(TaskState.LEASED, TaskState.DISPATCHED)
        now = self._now()
        task_ref = self._db.collection(TASKS).document(task.id)
        lease_ref = self._db.collection(LEASES).document(lease.lease_id)
        attempt_ref = self._db.collection(ATTEMPTS).document(lease.attempt_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> GuardedWrite:
            # Every read before any write: Firestore refuses a read after a
            # write inside one transaction.
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            attempt_snap = _snapshot(txn.get(attempt_ref))
            stored = (task_snap.to_dict() or {}) if task_snap.exists else None
            found = stored.get("state") if stored is not None else None
            reason = _lease_refusal(stored, lease)
            if reason is None:
                txn.update(task_ref, {"state": TaskState.DISPATCHED.value, "updated_at": now})
            held = (lease_snap.to_dict() or {}) if lease_snap.exists else None
            if (
                held is not None
                and held.get("released_at") is None
                and held.get("state") == TaskState.LEASED.value
            ):
                txn.update(lease_ref, {"state": TaskState.DISPATCHED.value})
            if attempt_snap.exists:
                txn.update(attempt_ref, {"execution_name": execution_name})
            return GuardedWrite(
                "mark_dispatched",
                reason is None,
                TaskState.LEASED.value,
                found,
                reason,
                TaskState.DISPATCHED.value if reason is None else None,
            )

        outcome = _apply(transaction)
        if outcome.applied:
            self.append_event(
                task,
                EventType.DISPATCHED,
                {"execution_name": execution_name, "backend": backend},
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
            )
        elif outcome.reason == STATE_CHANGED and outcome.found in _WORKER_OWNED:
            # The ordinary race, and a benign one now that it is not overwritten.
            self._log_skip(
                outcome,
                task,
                lease=lease,
                consequence=(
                    f"its worker got there first; execution {execution_name} is recorded "
                    "on the attempt and the task is left to the worker"
                ),
            )
        else:
            self._log_skip(
                outcome,
                task,
                lease=lease,
                level=logging.WARNING,
                consequence=(
                    f"execution {execution_name} was started for an attempt that is no "
                    "longer current; the worker's fencing check stops it unless it has "
                    "already finished"
                ),
            )
        return outcome

    def return_to_ready_after_failed_dispatch(
        self,
        task: Task,
        lease: Lease,
        error_code: str,
        *,
        correlation_id: str | None = None,
        retry_delay_seconds: int = 30,
    ) -> GuardedWrite:
        """Dispatch failed after the lease was taken: give the capacity back.

        The returned `GuardedWrite` is `applied` with `target == "FAILED"`
        exactly when this write left the task FAILED: its last attempt, or a
        reclaim finished here on the last attempt. The drain needs that answer:
        a FAILED workflow step under `on_step_failure: fail_workflow` must stop
        its siblings in the SAME drain, and a verdict the drain read before
        this write would say the workflow was healthy.

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

        THE TASK IS WRITTEN ONLY IF IT IS STILL WHAT ADMISSION LEFT: LEASED, on
        this lease, at this generation. This used to read the task inside its
        transaction and then write READY whatever it read (PR #31's review; F-9).
        A dispatch can take minutes to fail, and in that time:

          * the reconciler fences the attempt past its dispatch deadline and a
            second admission re-leases the task. READY with
            `current_lease_id=None` over that orphaned the NEW lease -- its slot
            held by nothing -- and the next drain leased the task again;
          * the create call reports failure (a timeout) although the execution
            exists, and its worker walks the task to RUNNING. READY over RUNNING
            admits a second worker onto the same attempt;
          * that worker finishes, or an operator cancels the task by hand.
            READY over SUCCEEDED runs finished work again; over CANCELLED it
            un-cancels it.

        AND THE LEASE IS DECIDED IN THE SAME TRANSACTION, on the same read. It
        used to be released first, unconditionally, so the RUNNING case above
        also handed back a slot a live container occupied (CONTRACT invariants
        1 and 3). Now, by what the transaction finds:

          task found                         lease          task
          ---------------------------------  -------------  ------------------
          LEASED on this lease, this         released       READY / FAILED /
            generation                                        CANCELLED
          DISPATCHED/STARTING/RUNNING on     kept           left: its worker
            this lease, this generation                       releases it
          on this lease, generation fenced,  kept           left: the
            lease unreleased                                  reconciler's
                                                              reclaim is running
                                                              or stopped short
          on this lease, generation fenced,  (released      READY / FAILED /
            lease released                     already)       CANCELLED
          anything else: another lease,      released       left
            none, terminal, gone

        THE TWO FENCED ROWS ARE A RECLAIM THE RECONCILER HAS NOT FINISHED. Only
        its `invalidate_generation` bumps a generation without also minting a
        new lease, and only its `repair_task_state` then clears
        `current_lease_id`, so a task still pointing at this lease at a newer
        generation is between those two steps -- running, or stopped by an
        unconfirmed kill or a dead instance. PR #45's first version released
        here and walked away, and a task in a concurrency state on a released
        lease is invisible to every reconciler rule: its snapshot reads only
        unreleased leases. Main recovered from the same interleaving by writing
        READY blind.

          * lease UNRELEASED: kept, and the task is left. It is the reconciler's
            evidence -- `detect_stale_leases` reports a superseded, unreleased
            lease with nothing running under it as ORPHAN_LEASE, and an
            execution still running under it as OBSOLETE_GENERATION -- and the
            reconciler releases it only once it has confirmed the kill. This
            store cannot confirm anything: releasing here is what handed back
            the slots of a container the reconciler had deliberately kept ("did
            NOT release: termination was not confirmed").
          * lease RELEASED: the reconciler got as far as its own release, which
            it makes only after the kill is confirmed or when nothing was
            running, and no rule of its will ever bring it back for step 4. So
            this finishes step 4 with `repair_task_state`'s rule, from whatever
            concurrency state the task is in.

        `release_lease_in_transaction` is idempotent, so a lease the reconciler
        already released is not decremented twice. The event is still written
        after the commit, so an event write that fails cannot keep the pools.

        `cancel_requested` IS part of the decision. The API flags a LEASED task
        and leaves it to "the worker or the reconciler"; when the dispatch
        fails there is no worker, and READY was a second hop through the next
        drain -- one that recorded FAILED instead whenever the attempt was the
        last. It ends CANCELLED here, as the reconciler's `repair_task_state`
        ends the same case: the user's decision survives exhausted attempts.
        """
        now = self._now()
        reference = correlation_id or lease.attempt_id
        public = f"{error_code} (attempt {reference})"
        release_reason = f"dispatch_failed: {error_code}"[:200]

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
        lease_ref = self._db.collection(LEASES).document(lease.lease_id)
        transaction = self._db.transaction()

        def _returned(
            txn: Any,
            stored: dict[str, Any],
            found: str,
            reclaim: str | None,
            *,
            released: bool,
        ) -> _Settled:
            """Write the task's way off this lease: READY, or CANCELLED when a
            cancel was requested, or FAILED when retries are spent. One rule for
            the ordinary return and for finishing a reclaim, as
            `repair_task_state` applies one rule to every state it repairs."""
            attempt_count = int(stored.get("attempt_count", 0))
            max_attempts = int(stored.get("max_attempts", task.max_attempts))
            exhausted = retries_exhausted(attempt_count, max_attempts)
            if stored.get("cancel_requested"):
                target = TaskState.CANCELLED
            elif exhausted:
                target = TaskState.FAILED
            else:
                target = TaskState.READY
            assert_transition(TaskState(found), target)
            payload: dict[str, Any] = {
                "state": target.value,
                "current_lease_id": None,
                "last_error": (
                    f"cancelled on request; {public}" if target is TaskState.CANCELLED else public
                )[:1000],
                "updated_at": now,
            }
            if target.value in _TERMINAL:
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
            return _Settled(
                GuardedWrite(
                    "return_to_ready", True, TaskState.LEASED.value, found, None, target.value
                ),
                released,
                attempt_count,
                max_attempts,
                exhausted,
                reclaim,
                int(stored.get("current_generation", 0)),
            )

        @firestore.transactional
        def _settle(txn: Any) -> _Settled:
            # Every read before any write: the task, then its lease, then --
            # inside `release_lease_in_transaction` -- the lease again and its
            # pools. Firestore refuses a read after a write in one transaction.
            snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            stored = (snap.to_dict() or {}) if snap.exists else None
            found = stored.get("state") if stored is not None else None
            refusal = _lease_refusal(stored, lease)
            points_here = (
                stored is not None
                and (stored.get("current_lease_id") or None) == lease.lease_id
            )

            if (
                stored is not None
                and found in _HOLDS_CAPACITY
                and refusal == LEASE_SUPERSEDED
                and points_here
            ):
                # Still on this lease, so what superseded it is the GENERATION:
                # the reconciler fenced this attempt and has not returned the
                # task. See the docstring's two fenced rows.
                held = (lease_snap.to_dict() or {}) if lease_snap.exists else None
                if held is not None and held.get("released_at") is None:
                    return _Settled(
                        GuardedWrite(
                            "return_to_ready", False, TaskState.LEASED.value, found, refusal
                        ),
                        False,
                        reclaim=_RECLAIM_PENDING,
                        task_generation=int(stored.get("current_generation", 0)),
                    )
                return _returned(txn, stored, found, _RECLAIM_FINISHED, released=False)

            worker_owns_it = refusal == STATE_CHANGED and found in _WORKER_OWNED and points_here
            released = False
            if not worker_owns_it:
                released = release_lease_in_transaction(
                    txn, db=self._db, lease_id=lease.lease_id, reason=release_reason, now=now
                )
            if refusal is not None or stored is None or found is None:
                return _Settled(
                    GuardedWrite("return_to_ready", False, TaskState.LEASED.value, found, refusal),
                    released,
                )
            return _returned(txn, stored, found, None, released=released)

        settled = _settle(transaction)
        outcome = settled.outcome
        failure = {
            "reason": "dispatch_failed",
            "error_code": error_code,
            "correlation_id": reference,
        }
        if settled.reclaim == _RECLAIM_FINISHED:
            # Not a lease_released event: the reconciler released it, and said
            # so in this timeline when it did. What nothing has said yet is that
            # the task came off the lease, which is what its own repair would
            # have announced.
            target = TaskState(outcome.target)
            detail: dict[str, Any] = {
                **failure,
                "completed_reclaim": True,
                "from_state": outcome.found,
                "fenced_generation": lease.generation,
                "task_generation": settled.task_generation,
                "attempt_count": settled.attempt_count,
                "max_attempts": settled.max_attempts,
                "retries_exhausted": settled.exhausted,
            }
            if target is TaskState.CANCELLED:
                detail["phase"] = "cancelled"
            self.append_event(
                task,
                _EVENT_FOR_RETURN[target],
                detail,
                lease_id=lease.lease_id,
                generation=lease.generation,
            )
            log.warning(
                "finished a reclaim for task=%s lease=%s generation=%s: the task is at "
                "generation %s on this lease, which the reconciler released without "
                "returning the task; %s -> %s",
                task.id,
                lease.lease_id,
                lease.generation,
                settled.task_generation,
                outcome.found,
                outcome.target,
            )
            return outcome

        if outcome.applied:
            self.append_event(
                task,
                EventType.LEASE_RELEASED,
                {
                    **failure,
                    # The numbers the cap was decided on, so a task that went
                    # FAILED here says why in its own timeline.
                    "attempt_count": settled.attempt_count,
                    "max_attempts": settled.max_attempts,
                    "retries_exhausted": settled.exhausted,
                    "to_state": outcome.target,
                },
                lease_id=lease.lease_id,
                generation=lease.generation,
            )
            if outcome.target == TaskState.CANCELLED.value:
                # The API's flag-only cancel wrote `cancelled` with
                # phase=cancel_requested; this is the one that says it happened.
                self.append_event(
                    task,
                    EventType.CANCELLED,
                    {**failure, "phase": "cancelled", "from_state": TaskState.LEASED.value},
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                )
            return outcome

        if settled.released and outcome.reason != TASK_MISSING:
            # The capacity really came back here, so the task's own timeline
            # says so -- and that its state was left alone.
            self.append_event(
                task,
                EventType.LEASE_RELEASED,
                {
                    **failure,
                    "task_state_written": False,
                    "skip_reason": outcome.reason,
                    "found_state": outcome.found,
                },
                lease_id=lease.lease_id,
                generation=lease.generation,
            )
        if settled.reclaim == _RECLAIM_PENDING:
            consequence = (
                f"the reconciler fenced this attempt (the task is at generation "
                f"{settled.task_generation} on this lease) and has not released it; the "
                "lease and the task are left for the reconciler, which releases only once "
                "it has confirmed nothing runs under the lease"
            )
        elif settled.released:
            consequence = "its lease was released (it backed nothing); the task was left alone"
        elif outcome.found in _WORKER_OWNED:
            consequence = (
                "the task advanced on this lease, so its execution exists and a worker "
                "holds it; the lease is left for that worker to release"
            )
        else:
            consequence = "its lease had already been released; the task was left alone"
        self._log_skip(outcome, task, lease=lease, level=logging.WARNING, consequence=consequence)
        return outcome

    def cancel(self, task: Task, reason: str, detail: dict[str, Any] | None = None) -> GuardedWrite:
        """Terminate a task that can never become runnable.

        Only called for tasks that hold NO capacity, so there is no lease to
        release: a task holding a lease is cancelled through the worker or the
        reconciler, which are the only components that know whether a container
        is still running.

        Which is exactly why it is guarded. The drain decided from a READY or
        PARKED snapshot; if another scheduler admitted the task since, CANCELLED
        over LEASED is a terminal task holding a live lease -- the case the
        paragraph above rules out -- and if the user cancelled it since, this
        rewrote why it ended and recorded a second cancellation.
        """
        assert_transition(task.state, TaskState.CANCELLED)
        now = self._now()
        outcome = self._write_if_unchanged(
            "cancel",
            task,
            {
                "state": TaskState.CANCELLED.value,
                "park_reason": None,
                "blocked_by": [],
                "completed_at": now,
                "last_error": reason[:1000],
                "updated_at": now,
            },
        )
        if outcome.applied:
            self.append_event(task, EventType.CANCELLED, {"reason": reason, **(detail or {})})
        return outcome

    def cancel_if_not_started(
        self, task: Task, reason: str, detail: dict[str, Any] | None = None
    ) -> GuardedWrite:
        """Cancel a step only if it STILL has not started, in whatever pending state it is now.

        `applied` True means it was cancelled. False means it had started, had
        finished or was gone when the transaction read it: nothing is written,
        the skip is logged like every other guarded write, and the loop counts
        it in `stale_writes` (`write="cancel_if_not_started"`).

        WHY NOT `cancel`. `cancel` is guarded too (PR #45), but on the EXACT
        state the caller read. That is the right precondition for a decision
        about one task: "this READY task was cancel-requested", "this PARKED
        task's parent failed". The workflow sweep's decision is about the
        WORKFLOW -- every step that has not started goes -- so a step the sweep
        read READY that a concurrent drain has since parked (a missing key, a
        quota park) is still a step to cancel. `cancel` would skip it as
        `state_changed` and leave it to a later touch point, which for a
        MANUAL_PAUSE park may never come. So the precondition here is "still in
        a state that holds no capacity", not "still in the state I read".

        What that precondition rules out is the race the sweep sits beside. The
        sweep cancels READY steps, and a concurrent drain can lease one between
        the sweep's query and its write. CANCELLED over a LEASED task is wrong
        in two ways. First, the lease stays counted in every pool it reserved
        until the reconciler's `detect_orphan_leases` finds a terminal task
        holding it. Second, the drain that took the lease goes on to dispatch,
        so a step of a failed workflow starts after all.

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
        def _cancel(txn: Any) -> GuardedWrite:
            snap = _snapshot(txn.get(task_ref))
            stored = (snap.to_dict() or {}) if snap.exists else None
            found = stored.get("state") if stored is not None else None
            if stored is None:
                return GuardedWrite(
                    "cancel_if_not_started", False, _NOT_STARTED, None, TASK_MISSING
                )
            if found not in _NOT_STARTED_VALUES:
                return GuardedWrite(
                    "cancel_if_not_started", False, _NOT_STARTED, found, STATE_CHANGED
                )
            assert_transition(TaskState(found), TaskState.CANCELLED)
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
            return GuardedWrite(
                "cancel_if_not_started",
                True,
                _NOT_STARTED,
                found,
                None,
                TaskState.CANCELLED.value,
            )

        outcome = _cancel(transaction)
        if not outcome.applied:
            self._log_skip(
                outcome,
                task,
                consequence="it started or finished after the sweep read it; left alone",
            )
            return outcome
        self.append_event(
            task,
            EventType.CANCELLED,
            {"reason": reason, "from_state": outcome.found, **(detail or {})},
        )
        return outcome

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
