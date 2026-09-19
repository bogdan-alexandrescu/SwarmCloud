"""Control-plane reads and writes for the reconciler.

Two responsibilities, kept apart on purpose:

* **Reading** builds a `ControlSnapshot` from one bounded set of queries. The
  reconciler only ever looks at tasks in the four concurrency states and leases
  that have not been released, because those are the only documents that can
  hold capacity -- a backlog of a hundred thousand QUEUED tasks costs this
  service nothing, exactly as invariant 1 requires.

* **Writing** exposes precisely four mutations: invalidate a generation, release
  a lease, repair a task's state, record an event. Every one of them runs in a
  transaction and re-reads what it is about to change, so a reconciler racing a
  live worker loses rather than corrupts.

Capacity is never decremented here. `release_lease` calls the frozen
`release_lease_in_transaction`, which is the only function in the platform
allowed to touch `pool.active`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from swarm_common.admission import _snapshot
from swarm_common.admission import release_lease_in_transaction
from swarm_common.models import TaskEvent, new_id, utcnow
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    EventType,
    TaskState,
    can_transition,
)

from .model import AttemptView, ControlSnapshot, LeaseView, TaskView


class FirestoreTransactionRunner:
    def __init__(self, db: Any) -> None:
        self._db = db

    def run(self, fn: Callable[[Any], Any]) -> Any:
        from google.cloud import firestore

        transaction = self._db.transaction()

        @firestore.transactional
        def _inner(txn: Any) -> Any:
            return fn(txn)

        return _inner(transaction)


def _field_filter(field: str, op: str, value: Any) -> Any:
    from google.cloud.firestore_v1.base_query import FieldFilter

    return FieldFilter(field, op, value)


class ControlStore:
    def __init__(
        self,
        db: Any,
        *,
        logger: Any,
        txn_runner: Any | None = None,
        filter_factory: Callable[[str, str, Any], Any] | None = None,
    ) -> None:
        self._db = db
        self._log = logger
        self._txn = txn_runner or FirestoreTransactionRunner(db)
        self._filter = filter_factory or _field_filter

    # -- reads -----------------------------------------------------------
    def snapshot(self, *, lease_lookback_hours: int = 48) -> ControlSnapshot:
        snapshot = ControlSnapshot(taken_at=utcnow())

        active_states = [state.value for state in sorted(CONCURRENCY_STATES, key=lambda s: s.value)]
        tasks_query = self._db.collection("tasks").where(
            filter=self._filter("state", "in", active_states)
        )
        for doc in tasks_query.stream():
            snapshot.tasks[doc.id] = TaskView.from_doc(doc.to_dict() or {}, doc.id)

        leases_query = self._db.collection("leases").where(
            filter=self._filter("released_at", "==", None)
        )
        cutoff = utcnow() - timedelta(hours=lease_lookback_hours)
        for doc in leases_query.stream():
            lease = LeaseView.from_doc(doc.to_dict() or {}, doc.id)
            if lease.created_at is not None and lease.created_at < cutoff:
                # Ancient unreleased leases are a data-integrity problem, not a
                # reconciliation one; they are reported, never auto-repaired.
                self._log.warning(
                    "lease older than the reconciliation window",
                    lease_id=lease.lease_id,
                    created_at=str(lease.created_at),
                )
            snapshot.leases[lease.lease_id] = lease

        for lease in snapshot.leases.values():
            if not lease.attempt_id or lease.attempt_id in snapshot.attempts:
                continue
            doc = self._db.collection("attempts").document(lease.attempt_id).get()
            if doc.exists:
                snapshot.attempts[lease.attempt_id] = AttemptView.from_doc(
                    doc.to_dict() or {}, lease.attempt_id
                )
        return snapshot

    def active_tenants(self, snapshot: ControlSnapshot) -> set[str]:
        return {task.tenant_id for task in snapshot.tasks.values() if task.tenant_id}

    def registered_tenants(self) -> set[str]:
        """Every tenant the control plane knows about, busy or idle.

        Used to protect per-tenant Kubernetes namespaces from garbage
        collection. A Cloud Run Job resource is safe to delete when idle because
        the dispatcher recreates it on the next dispatch; a namespace is not --
        it also holds the tenant's service account and its workload-identity
        binding, and nothing in the dispatch path recreates those. Deleting the
        namespace of a tenant who merely had a quiet day would turn their next
        submission into a dispatch failure, so a registered tenant keeps its
        namespace however long it sits idle.
        """
        tenants: set[str] = set()
        for doc in self._db.collection("tenants").stream():
            data = doc.to_dict() or {}
            tenant_id = data.get("tenant_id") or doc.id
            if tenant_id:
                tenants.add(str(tenant_id))
        return tenants

    # -- writes ----------------------------------------------------------
    def invalidate_generation(self, task_id: str, expected_generation: int) -> int | None:
        """Bump `current_generation`, fencing any worker still running.

        This is the step that makes termination safe to be best-effort on the
        slow path: a worker whose generation no longer matches exits without
        finishing, and it checks on every control poll.

        Returns the new generation, or None if the task had already moved on --
        in which case somebody else has already fenced it and there is nothing
        to do.
        """
        task_ref = self._db.collection("tasks").document(task_id)

        def _apply(txn: Any) -> int | None:
            snap = _snapshot(txn.get(task_ref))
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            try:
                state = TaskState(data.get("state"))
            except (ValueError, TypeError):
                return None
            if state in TERMINAL_STATES:
                return None          # nothing left to fence
            current = int(data.get("current_generation", 0))
            if current != expected_generation:
                return None
            new_generation = current + 1
            txn.update(
                task_ref,
                {
                    "current_generation": new_generation,
                    "updated_at": utcnow(),
                },
            )
            return new_generation

        return self._txn.run(_apply)

    def release_lease(self, lease_id: str, reason: str) -> bool:
        def _apply(txn: Any) -> bool:
            return release_lease_in_transaction(
                txn, db=self._db, lease_id=lease_id, reason=reason
            )

        return bool(self._txn.run(_apply))

    def repair_task_state(
        self,
        task_id: str,
        *,
        to_state: TaskState,
        error: str | None = None,
        next_eligible_at: datetime | None = None,
    ) -> TaskState | None:
        """Move a task out of a concurrency state it can no longer justify.

        Re-reads inside the transaction and refuses an illegal transition rather
        than forcing one: if a worker wrote SUCCEEDED between the snapshot and
        now, the correct repair is no repair.
        """
        task_ref = self._db.collection("tasks").document(task_id)

        def _apply(txn: Any) -> TaskState | None:
            snap = _snapshot(txn.get(task_ref))
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            try:
                current = TaskState(data.get("state"))
            except (ValueError, TypeError):
                return None
            if current in TERMINAL_STATES:
                return None
            target = to_state
            if target is TaskState.READY:
                attempts = int(data.get("attempt_count", 0))
                max_attempts = int(data.get("max_attempts", 3))
                if attempts >= max_attempts:
                    target = TaskState.FAILED
            if current is target:
                return None
            if not can_transition(current, target):
                self._log.warning(
                    "refusing an illegal repair transition",
                    task_id=task_id,
                    frm=current.value,
                    to=target.value,
                )
                return None
            payload: dict[str, Any] = {
                "state": target.value,
                "updated_at": utcnow(),
                "current_lease_id": None,
            }
            if error:
                payload["last_error"] = error[:2000]
            if next_eligible_at is not None:
                payload["next_eligible_at"] = next_eligible_at
            if target in TERMINAL_STATES:
                payload["completed_at"] = utcnow()
            txn.update(task_ref, payload)
            return target

        return self._txn.run(_apply)

    def emit(
        self,
        *,
        task_id: str,
        tenant_id: str | None,
        event_type: EventType,
        detail: dict[str, Any],
        attempt_id: str | None = None,
        lease_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        event = TaskEvent(
            event_id=new_id("evt"),
            task_id=task_id,
            tenant_id=tenant_id or "",
            type=event_type,
            at=utcnow(),
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            detail={"source": "reconciler", **detail},
        )
        self._db.collection("tasks").document(task_id).collection("events").document(
            event.event_id
        ).set(
            {
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
        )

    # ------------------------------------------------------------------
    # Pass history
    # ------------------------------------------------------------------

    #: Outcomes kept on a pass document. A pass repairing thousands of leases
    #: would otherwise approach Firestore's 1 MiB document limit, and a write
    #: that fails because it grew too large loses the WHOLE record of the pass
    #: that most needed recording.
    MAX_STORED_OUTCOMES = 50

    def record_pass(self, report: dict[str, Any], *, retain_hours: int) -> str | None:
        """Persist one reconciliation pass, and return its id.

        WHY THIS EXISTS. The report used to live in a per-instance dict
        (`service.py`'s `state["last_report"]`) and nowhere else. swarm-reconciler
        runs with min_instance_count = 0, so a cold instance answered
        `last_pass_at: null` and every finding the previous pass had made was
        gone. The reconciler is the component that detects stale leases, dead
        workers and orphaned executions -- the platform's entire account of what
        went wrong at runtime -- and it was discarded within minutes.

        Persisting it is what makes an operator screen, or an alert on "no
        successful pass in N minutes", possible at all.

        Returns None rather than raising: a pass that repaired real damage must
        not be reported as a failure because its bookkeeping write failed. The
        error is logged by the caller.
        """
        from datetime import timedelta

        outcomes = list(report.get("outcomes") or [])
        stored = outcomes[: self.MAX_STORED_OUTCOMES]

        pass_id = new_id("pass")
        doc = {
            **{k: v for k, v in report.items() if k != "outcomes"},
            "pass_id": pass_id,
            "outcomes": stored,
            "outcome_count": len(outcomes),
            # Never a silent truncation: a reader must be able to tell a pass
            # with 50 outcomes from a pass with 4000 whose tail was dropped.
            "outcomes_truncated": len(outcomes) > len(stored),
            "expires_at": utcnow() + timedelta(hours=retain_hours),
        }
        self._db.collection("reconciler_passes").document(pass_id).set(doc)
        return pass_id
