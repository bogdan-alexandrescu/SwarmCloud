"""Control-plane reads and writes for the reconciler.

Two responsibilities, kept apart on purpose:

* **Reading** builds a `ControlSnapshot` from one bounded set of queries. The
  reconciler only ever looks at tasks in the four concurrency states and leases
  that have not been released, because those are the only documents that can
  hold capacity -- a backlog of a hundred thousand QUEUED tasks costs this
  service nothing, exactly as invariant 1 requires.

* **Writing** exposes precisely four mutations: invalidate a generation, release
  a lease, repair a task's state, record an event -- and `fence_release_repair`,
  which commits the first three in ONE transaction, so a repair never leaves a
  fenced generation behind a held lease (#560). Every one of them runs in a
  transaction and re-reads what it is about to change, so a reconciler racing a
  live worker loses rather than corrupts.

Capacity is never decremented here. `release_lease` calls the frozen
`release_lease_in_transaction`, which is the only function in the platform
allowed to touch `pool.active`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from swarm_common.admission import _snapshot
from swarm_common.admission import release_lease_in_transaction
from swarm_common.models import EndCause, TaskEvent, new_id, retries_exhausted, utcnow
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    EventType,
    TaskState,
    can_transition,
)

from .model import (
    STARTUP_REFUNDS_KEY,
    AttemptView,
    ControlSnapshot,
    LeaseView,
    TaskView,
    cancel_end_cause,
    count_startup_end,
    startup_refunds_used,
)
from .model import as_datetime as _as_datetime


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


#: What a malformed TASK document raises out of `TaskView.from_doc` -- a bad
#: type, a value `int()`/`TaskState()` refuses, or (before #290's fix to
#: `_startup_refunds_int`) an `OverflowError` from `int(float("inf"))`.
#: Mirrors `quota_broker.accountstore._MALFORMED`: these four-plus-one are
#: what a bad DOCUMENT produces, not a bare `except Exception`, which would
#: also swallow a Firestore outage or a decoder bug and turn "the store is
#: down" into "every task is malformed".
_MALFORMED_TASK_DOC = (KeyError, ValueError, TypeError, AttributeError, OverflowError)


def _named_task(data: Any) -> str:
    """The task id a raw document names, or "" when it names none readably."""
    task_id = data.get("task_id") if isinstance(data, dict) else None
    return task_id if isinstance(task_id, str) else ""


def _withhold(snapshot: ControlSnapshot, task_ids: set[str]) -> None:
    """Take each task out of `snapshot.tasks` and record it as unreadable.

    A task whose lease or attempt could not be decoded is judged on nothing
    this pass. Left in `tasks`, it would be a task in a concurrency state with
    no lease beside it -- which `detect_leaseless_tasks` requeues -- on the
    strength of a lease that exists and was merely unreadable. Recorded in
    `unreadable_tasks`, the set the orphan-execution and orphan-lease rules
    already treat as "conclude nothing" (the same set a malformed TASK
    document lands in).
    """
    for task_id in task_ids:
        if not task_id:
            continue
        snapshot.tasks.pop(task_id, None)
        snapshot.unreadable_tasks.add(task_id)


@dataclass(frozen=True)
class RepairPlan:
    """What `fence_release_repair` moves the task to, by `repair_task_state`'s rules."""

    to_state: TaskState
    expected_lease_id: str | None
    error: str | None = None
    next_eligible_at: datetime | None = None
    failed_cause: EndCause = EndCause.LOST_WORKER
    #: The ended-at-startup refund (#67), judged against this transaction's
    #: own fence. None: no refund.
    startup_refund_limit: int | None = None


@dataclass(frozen=True)
class OneRepair:
    """What one `fence_release_repair` transaction committed."""

    #: The generation this transaction fenced the task to; None if it did not.
    new_generation: int | None
    released: bool
    repaired_to: TaskState | None
    #: The release (and with it the repair) was refused because the task
    #: still holds the lease at its generation.
    release_refused: bool = False


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
            try:
                snapshot.tasks[doc.id] = TaskView.from_doc(doc.to_dict() or {}, doc.id)
            except _MALFORMED_TASK_DOC as exc:
                # One malformed task must not abort the pass for every other
                # task of every other tenant (security review, PR #290) --
                # the same reasoning as `quota_broker.accountstore.list`
                # skipping a malformed account (#180). Recorded in
                # `unreadable_tasks` too: an execution that names this task id
                # must not be read as an orphan just because the document
                # could not be decoded this pass (see `detect.py`).
                self._log.warning(
                    "a task document is malformed and cannot be read; skipping it",
                    task_id=doc.id,
                    error=type(exc).__name__,
                )
                snapshot.unreadable_tasks.add(doc.id)

        leases_query = self._db.collection("leases").where(
            filter=self._filter("released_at", "==", None)
        )
        cutoff = utcnow() - timedelta(hours=lease_lookback_hours)
        # Task ids whose lease or attempt could not be decoded this pass. Each
        # is withheld from `snapshot.tasks` below, so that no rule judges it on
        # half the evidence -- see `_withhold`.
        withheld: set[str] = set()
        for doc in leases_query.stream():
            data = doc.to_dict() or {}
            try:
                lease = LeaseView.from_doc(data, doc.id)
            except _MALFORMED_TASK_DOC as exc:
                # The same rule as the task loop above, for the same reason: one
                # document with `generation` or `units` of NaN or Infinity
                # (`int()` raises on both) must not stop the pass for every
                # tenant (#294).
                self._log.warning(
                    "a lease document is malformed and cannot be read; skipping it",
                    lease_id=doc.id,
                    task_id=_named_task(data),
                    error=type(exc).__name__,
                )
                withheld.add(_named_task(data))
                continue
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
                try:
                    snapshot.attempts[lease.attempt_id] = AttemptView.from_doc(
                        doc.to_dict() or {}, lease.attempt_id
                    )
                except _MALFORMED_TASK_DOC as exc:
                    # An attempt document is the worker's, and a tenant's
                    # agents can reach their tenant's documents
                    # (docs/multi-tenancy.md); the same rule as a lease (#294).
                    self._log.warning(
                        "an attempt document is malformed and cannot be read; skipping it",
                        attempt_id=lease.attempt_id,
                        task_id=lease.task_id,
                        error=type(exc).__name__,
                    )
                    withheld.add(lease.task_id)
        _withhold(snapshot, withheld)
        return snapshot

    def task_and_attempts(self, task_id: str) -> tuple[TaskView | None, list[AttemptView]]:
        """Everything checkpoint retention needs about one task.

        Not part of `snapshot()`, and deliberately so: the snapshot reads only
        tasks in the four concurrency states, because those are the only ones
        that can hold capacity. Retention asks the opposite question -- which
        tasks are finished -- so it reads by id, one task at a time, and a
        backlog of a hundred thousand QUEUED tasks still costs this pass
        nothing.

        A task document that does not exist returns None, which `classify`
        hands to the age backstop. A READ THAT FAILS raises, and the caller
        keeps the objects untouched: "the task is gone" and "I could not look"
        must never produce the same deletion.
        """
        snap = self._db.collection("tasks").document(task_id).get()
        task = TaskView.from_doc(snap.to_dict() or {}, task_id) if snap.exists else None

        attempts: list[AttemptView] = []
        query = self._db.collection("attempts").where(
            filter=self._filter("task_id", "==", task_id)
        )
        for doc in query.stream():
            attempts.append(AttemptView.from_doc(doc.to_dict() or {}, doc.id))
        return task, attempts

    def finished_attempts(
        self, *, since: datetime, until: datetime
    ) -> list[tuple[TaskView, AttemptView]]:
        """Each task that finished in [since, until] with its current, unended attempt (#380).

        For `detect_lost_after_finish`. `snapshot()` reads attempts only through
        unreleased leases, and the attempt this is about may have had its lease
        released by the orphan-lease rule, or by `finish` itself, long before.

        Two single-field reads, which Firestore's automatic indexes serve: a
        range on the task's `completed_at`, then each such task's attempts by
        `task_id`, filtered here -- the read `task_and_attempts` makes. Only an
        attempt at the task's generation with no `completed_at` is returned.
        `until` is the grace, applied to the query so a task still inside it
        costs no attempt read. A malformed document is skipped: it yields no
        finding, which is the safe side. A READ THAT FAILS raises, and the
        caller concludes nothing.
        """
        query = (
            self._db.collection("tasks")
            .where(filter=self._filter("completed_at", ">=", since))
            .where(filter=self._filter("completed_at", "<=", until))
        )
        found: list[tuple[TaskView, AttemptView]] = []
        for doc in query.stream():
            try:
                task = TaskView.from_doc(doc.to_dict() or {}, doc.id)
            except _MALFORMED_TASK_DOC as exc:
                self._log.warning(
                    "a finished task document is malformed and cannot be read; skipping it",
                    task_id=doc.id,
                    error=type(exc).__name__,
                )
                continue
            if not task.is_terminal:
                continue
            attempts = self._db.collection("attempts").where(
                filter=self._filter("task_id", "==", task.task_id)
            )
            for attempt_doc in attempts.stream():
                try:
                    attempt = AttemptView.from_doc(attempt_doc.to_dict() or {}, attempt_doc.id)
                except _MALFORMED_TASK_DOC as exc:
                    self._log.warning(
                        "an attempt document is malformed and cannot be read; skipping it",
                        attempt_id=attempt_doc.id,
                        task_id=task.task_id,
                        error=type(exc).__name__,
                    )
                    continue
                if attempt.generation == task.generation and attempt.completed_at is None:
                    found.append((task, attempt))
        return found

    def record_lost_attempt_end(
        self,
        task_id: str,
        attempt_id: str,
        *,
        expected_generation: int,
        error: str,
    ) -> bool:
        """Record the end of an attempt its worker never ended (#380). True if written.

        The fence's transaction rules, for a task nothing fences: the task and
        the attempt are re-read here, every read before the write, and the end
        is written only while the task is still terminal AT THE ATTEMPT'S
        GENERATION and the attempt is still unended. A task fenced past it
        since the pass read it is a newer generation's, and nothing of it is
        touched (invariant 5); an attempt whose worker recorded its own end
        after all keeps the worker's account of it. The task document and
        every lease are never written.

        The fields are exactly those `ControlPlane.record_attempt_end` writes:
        no exit code is known, and `error` carries the cause.
        """
        task_ref = self._db.collection("tasks").document(task_id)
        attempt_ref = self._db.collection("attempts").document(attempt_id)

        def _apply(txn: Any) -> bool:
            task_snap = _snapshot(txn.get(task_ref))
            attempt_snap = _snapshot(txn.get(attempt_ref))
            if not task_snap.exists or not attempt_snap.exists:
                return False
            task = task_snap.to_dict() or {}
            attempt = attempt_snap.to_dict() or {}
            try:
                state = TaskState(task.get("state"))
                current = int(task.get("current_generation", -1))
                generation = int(attempt.get("generation", -1))
            except (ValueError, TypeError, OverflowError):
                return False
            if (
                state not in TERMINAL_STATES
                or current != expected_generation
                or generation != expected_generation
                or attempt.get("task_id") != task_id
                or attempt.get("completed_at") is not None
            ):
                self._log.warning(
                    "refusing to record a lost attempt's end: it moved since the pass read it",
                    task_id=task_id,
                    attempt_id=attempt_id,
                    state=state.value,
                    current_generation=current,
                    attempt_generation=generation,
                    expected_generation=expected_generation,
                )
                return False
            txn.update(
                attempt_ref,
                {
                    "completed_at": utcnow(),
                    "exit_code": None,
                    "error": error,
                    "tenant_id": attempt.get("tenant_id") or task.get("tenant_id"),
                },
            )
            return True

        return bool(self._txn.run(_apply))

    def task_by_id(self, task_id: str) -> TaskView | None:
        """One task, whatever its state, or None if the document does not exist.

        For an active execution naming a task that `snapshot()` did not return
        -- which is every task outside the four concurrency states, finished
        ones included. A READ THAT FAILS raises: "this task does not exist" and
        "I could not look" must never lead to the same termination.
        """
        snap = self._db.collection("tasks").document(task_id).get()
        return TaskView.from_doc(snap.to_dict() or {}, task_id) if snap.exists else None

    def task_for_lease(self, task_id: str) -> TaskView | None:
        """The task an unreleased lease names, whatever its state (#332).

        The same document read as `task_by_id`, kept a separate method because
        it is a separate input. `task_by_id` feeds the eviction rules and is
        switched off with them (`RECONCILER_ENABLE_GKE_EVICTION=false`); this
        feeds the orphan-lease rule, which runs whatever that switch says, and
        without it a lease admitted between the snapshot's two queries is
        released as naming "a task that no longer exists". A READ THAT FAILS
        raises, as `task_by_id` does.
        """
        snap = self._db.collection("tasks").document(task_id).get()
        return TaskView.from_doc(snap.to_dict() or {}, task_id) if snap.exists else None

    def task_and_attempt_docs(
        self, task_id: str, attempt_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The raw task and attempt documents a stop request names (#627).

        None for a document that does not exist. A READ THAT FAILS raises, as
        `task_by_id` does: "there is no such attempt" must never be what a
        failed read looks like to the caller deciding whether to stop it.
        """
        task = self._db.collection("tasks").document(task_id).get()
        attempt = self._db.collection("attempts").document(attempt_id).get()
        return (
            (task.to_dict() or {}) if task.exists else None,
            (attempt.to_dict() or {}) if attempt.exists else None,
        )

    def attempt_events(self, task_id: str, attempt_id: str) -> list[dict[str, Any]]:
        """Every event one attempt has written to its task's stream.

        A single-field equality on `attempt_id`, which Firestore's automatic
        index serves without a composite one (`terraform/modules/firestore`
        declares none for it). Ordering is done by the caller, in memory: an
        attempt writes a heartbeat event every 150s and two checkpoint events
        every 120s, so even a 90-minute browser attempt is under 150 documents,
        and it is read only when the stuck rule could act on it.
        """
        query = (
            self._db.collection("tasks")
            .document(task_id)
            .collection("events")
            .where(filter=self._filter("attempt_id", "==", attempt_id))
        )
        return [doc.to_dict() or {} for doc in query.stream()]

    #: Where the checkpoint sweep records that it ran. A single document, read
    #: and written inside one transaction, because swarm-reconciler scales to
    #: zero and an in-process timer would reset on every cold start -- the same
    #: mistake `record_pass` exists to undo.
    SWEEP_STATE_PATH = ("reconciler_state", "checkpoint_gc")

    def claim_checkpoint_sweep(self, *, min_interval_seconds: int, now: datetime) -> bool:
        """True at most once per `min_interval_seconds`, across all instances.

        The claim is written BEFORE the sweep runs, not after. A sweep that
        crashes half way therefore waits a full interval before trying again,
        which is the safe direction: the alternative is a failing sweep relisting
        the entire bucket on every one-minute tick for as long as it keeps
        failing.
        """
        collection, document = self.SWEEP_STATE_PATH
        ref = self._db.collection(collection).document(document)

        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            if snap.exists:
                last = (snap.to_dict() or {}).get("last_started_at")
                last_dt = _as_datetime(last)
                if last_dt is not None and (now - last_dt).total_seconds() < min_interval_seconds:
                    return False
                txn.update(ref, {"last_started_at": now})
            else:
                txn.set(ref, {"last_started_at": now})
            return True

        return bool(self._txn.run(_apply))

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
        return set(self.tenant_namespaces())

    def tenant_namespaces(self) -> dict[str, str | None]:
        """Every registered tenant, and the Kubernetes namespace it RECORDS.

        None where the document records no namespace. The value is carried
        rather than re-derived because the dispatcher reads it first
        (`GkeJobDispatcher.namespace_for` prefers `tenant.namespace` over its own
        template), so it is where that tenant's Jobs actually are.
        `GkeBackend.namespace_for` applies the same precedence to this pair.

        This is how the reconciler learns which namespaces to read without a
        cluster-scope list: it asks the control plane which tenants exist rather
        than asking the cluster which namespaces do.
        """
        tenants: dict[str, str | None] = {}
        for doc in self._db.collection("tenants").stream():
            data = doc.to_dict() or {}
            tenant_id = data.get("tenant_id") or doc.id
            if tenant_id:
                recorded = data.get("namespace")
                tenants[str(tenant_id)] = str(recorded) if recorded else None
        return tenants

    # -- writes ----------------------------------------------------------
    def invalidate_generation(
        self,
        task_id: str,
        expected_generation: int,
        *,
        only_from: tuple[TaskState, ...] | None = None,
        unless_holding_lease: str | None = None,
    ) -> int | None:
        """Bump `current_generation`, fencing any worker still running.

        This is the step that makes termination safe to be best-effort on the
        slow path: a worker whose generation no longer matches exits without
        finishing, and it checks on every control poll.

        Returns the new generation, or None if the task had already moved on --
        in which case somebody else has already fenced it and there is nothing
        to do.

        `only_from`, when given, is the states the finding was about, re-read
        here: a task that has left them since the snapshot is not fenced. The
        ended-at-startup rule passes DISPATCHED and STARTING (#198).

        `unless_holding_lease`, when given, is the lease an ORPHAN-LEASE finding
        is about, and the fence is refused while the task re-read here still
        holds it: in a concurrency state, with `current_lease_id` naming it.
        Such a task is not an orphan's task, it is the lease's rightful owner
        (#332). The generation needs no second check: `expected_generation` is
        the lease's, and a task fenced past it is already refused below.
        """
        task_ref = self._db.collection("tasks").document(task_id)

        def _apply(txn: Any) -> int | None:
            snap = _snapshot(txn.get(task_ref))
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            new_generation = self._fence_decision(
                data,
                task_id,
                expected_generation,
                only_from=only_from,
                unless_holding_lease=unless_holding_lease,
            )
            if new_generation is None:
                return None
            txn.update(
                task_ref,
                {
                    "current_generation": new_generation,
                    "updated_at": utcnow(),
                },
            )
            return new_generation

        return self._txn.run(_apply)

    def _fence_decision(
        self,
        data: dict[str, Any],
        task_id: str,
        expected_generation: int,
        *,
        only_from: tuple[TaskState, ...] | None,
        unless_holding_lease: str | None,
    ) -> int | None:
        """The generation a fence would write onto this task document, or None.

        The whole of `invalidate_generation`'s judgement, on a document read in
        the caller's transaction, so `fence_release_repair` applies exactly
        the same rule without a second copy of it.
        """
        try:
            state = TaskState(data.get("state"))
        except (ValueError, TypeError):
            return None
        if state in TERMINAL_STATES:
            return None          # nothing left to fence
        if only_from is not None and state not in only_from:
            return None          # moved on since the snapshot
        if (
            unless_holding_lease is not None
            and state in CONCURRENCY_STATES
            and (data.get("current_lease_id") or None) == unless_holding_lease
        ):
            self._log.warning(
                "refusing to fence a task that still holds the lease the finding "
                "called orphaned",
                task_id=task_id,
                lease_id=unless_holding_lease,
                state=state.value,
            )
            return None
        current = int(data.get("current_generation", 0))
        if current != expected_generation:
            return None
        return current + 1

    def release_lease(
        self,
        lease_id: str,
        reason: str,
        *,
        refuse_while_task_holds_it: bool = False,
    ) -> bool:
        """Return the lease's capacity through the frozen release. Idempotent.

        `refuse_while_task_holds_it` is for an ORPHAN-LEASE finding (#332). The
        lease and its task are re-read here, before the frozen function's own
        reads (every read precedes every write, as Firestore requires), and the
        release is refused while the task is in a concurrency state, names this
        lease in `current_lease_id` and is at the lease's generation. That is a
        task holding its lease, however the snapshot came to miss it.

        The generation is part of the test on purpose. A task fenced past the
        lease's generation by an interrupted earlier repair still names the
        lease, and holding THAT lease would leak its slots for ever
        (test_orphan_lease_after_partial_repair.py).

        A concurrency state, not merely a non-terminal one: a QUEUED, READY or
        PARKED task holds no capacity (invariant 1), so a lease still naming
        one is the orphan `detect_orphan_leases` reports as "should hold no
        capacity", and refusing it would leak its slots.
        """

        def _apply(txn: Any) -> bool:
            if refuse_while_task_holds_it and self._task_holds_lease(txn, lease_id):
                return False
            return release_lease_in_transaction(
                txn, db=self._db, lease_id=lease_id, reason=reason
            )

        return bool(self._txn.run(_apply))

    def _task_holds_lease(self, txn: Any, lease_id: str) -> bool:
        lease_snap = _snapshot(txn.get(self._db.collection("leases").document(lease_id)))
        if not lease_snap.exists:
            return False
        lease = lease_snap.to_dict() or {}
        task_id = lease.get("task_id")
        if not task_id:
            return False
        task_snap = _snapshot(txn.get(self._db.collection("tasks").document(task_id)))
        if not task_snap.exists:
            return False
        return self._holds(task_snap.to_dict() or {}, lease, lease_id, task_id)

    def _holds(
        self, task: dict[str, Any], lease: dict[str, Any], lease_id: str, task_id: str
    ) -> bool:
        """The task document holds this lease: a concurrency state, naming it,
        at its generation. See `release_lease` for why each part is needed."""
        try:
            state = TaskState(task.get("state"))
        except (ValueError, TypeError):
            return False
        holds = (
            state in CONCURRENCY_STATES
            and (task.get("current_lease_id") or None) == lease_id
            and int(task.get("current_generation", 0)) == int(lease.get("generation", 0))
        )
        if holds:
            self._log.warning(
                "refusing to release a lease its task still holds",
                lease_id=lease_id,
                task_id=task_id,
                state=state.value,
            )
        return holds

    def repair_task_state(
        self,
        task_id: str,
        *,
        to_state: TaskState,
        expected_lease_id: str | None = None,
        error: str | None = None,
        next_eligible_at: datetime | None = None,
        only_from: tuple[TaskState, ...] | None = None,
        failed_cause: EndCause = EndCause.LOST_WORKER,
        startup_refund_limit: int | None = None,
        fenced_generation: int | None = None,
    ) -> TaskState | None:
        """Move a task out of a concurrency state it can no longer justify.

        THE END CAUSE IS DECIDED HERE, INSIDE THE TRANSACTION, because the
        terminal state is (contract request 23). A CANCELLED end is
        CANCEL_REQUESTED, or CHILD_CASCADE for a child its parent's cascade
        flagged (contract request 41): only the flag, re-read below, picks it,
        and the re-read metadata picks which. A FAILED end
        is `failed_cause` -- LOST_WORKER for a requeue this downgraded on spent
        attempts, which is every caller but the one that fails a worker that
        could not start (CANNOT_START).

        Re-reads inside the transaction and refuses an illegal transition rather
        than forcing one: if a worker wrote SUCCEEDED between the snapshot and
        now, the correct repair is no repair.

        `expected_lease_id` is the lease the FINDING was about, and this refuses
        when the task has since moved to a different one -- the same "re-read and
        refuse on mismatch" discipline `invalidate_generation` applies to the
        generation. Without it, a finding about an old attempt reset a task whose
        NEW attempt was running: `invalidate_generation` and `release_lease` both
        correctly no-op on a superseded finding, and then this forced RUNNING ->
        READY and cleared `current_lease_id` out from under a live, heartbeating
        lease. The next drain admitted a second worker on the same repository --
        invariant 5 broken through the machinery that enforces it -- and the
        unhooked lease held capacity nothing would return.

        None means "no expectation", for findings that carry no lease (an orphan
        execution whose lease was released long ago). A task holding no lease has
        nothing to strand, so those still repair.

        `only_from`, when given, refuses a task no longer in one of those
        states, as `invalidate_generation` does. A worker that parked its task
        between the snapshot and now has decided how it resumes, and READY
        would overrule it (#198).

        `startup_refund_limit`, when given, is the ended-at-startup rule's
        requeue (#67): an attempt that ended before its runner started is
        counted by `count_startup_end`, IN THIS TRANSACTION and BEFORE the
        spent-attempts check, so a refunded attempt requeues rather than
        fails. The refund is written only when the task is still at
        `fenced_generation`, the generation this pass's own fence wrote: a
        task fenced again since (or never fenced by this pass) belongs to
        somebody else's decision, and its attempt is counted as before
        (invariant 5). `error` then gets the counting's tail appended, so
        `last_error` says what this transaction decided, not the snapshot.
        """
        task_ref = self._db.collection("tasks").document(task_id)

        def _apply(txn: Any) -> TaskState | None:
            snap = _snapshot(txn.get(task_ref))
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            decided = self._repair_decision(
                data,
                task_id,
                to_state=to_state,
                expected_lease_id=expected_lease_id,
                error=error,
                next_eligible_at=next_eligible_at,
                only_from=only_from,
                failed_cause=failed_cause,
                startup_refund_limit=startup_refund_limit,
                fenced_generation=fenced_generation,
            )
            if decided is None:
                return None
            target, payload = decided
            txn.update(task_ref, payload)
            return target

        return self._txn.run(_apply)

    def _repair_decision(
        self,
        data: dict[str, Any],
        task_id: str,
        *,
        to_state: TaskState,
        expected_lease_id: str | None,
        error: str | None,
        next_eligible_at: datetime | None,
        only_from: tuple[TaskState, ...] | None,
        failed_cause: EndCause,
        startup_refund_limit: int | None,
        fenced_generation: int | None,
    ) -> tuple[TaskState, dict[str, Any]] | None:
        """`repair_task_state`'s judgement on a document read in the caller's
        transaction: the state it moves to and the payload, or None to refuse.
        Shared with `fence_release_repair`, so there is one copy of the rule."""
        try:
            current = TaskState(data.get("state"))
        except (ValueError, TypeError):
            return None
        if current in TERMINAL_STATES:
            return None
        if only_from is not None and current not in only_from:
            self._log.info(
                "refusing to repair a task that has left the states the finding was about",
                task_id=task_id,
                state=current.value,
                expected=[s.value for s in only_from],
            )
            return None
        held = data.get("current_lease_id") or None
        if held is not None and held != expected_lease_id:
            self._log.info(
                "refusing to repair a task that has moved to another lease",
                task_id=task_id,
                holds_lease=held,
                finding_lease=expected_lease_id,
                state=current.value,
            )
            return None
        target = to_state
        if target in (TaskState.READY, TaskState.FAILED) and data.get("cancel_requested"):
            # Re-read here, not taken from the snapshot: the API sets the
            # flag with a plain update, so a cancel pressed after this pass
            # read the task still lands on the right terminal state. READY
            # would be a second hop -- the scheduler's drain cancels a READY
            # task with the flag set -- and a hop that depends on another
            # service being healthy is how a requested cancel sat ignored for
            # hours on 2026-09-24. FAILED, from a worker that could not
            # start, would record a task somebody stopped as having failed.
            target = TaskState.CANCELLED
        counting: dict[str, Any] = {}
        message = error
        if target is TaskState.READY and startup_refund_limit is not None:
            # The ended-at-startup requeue: refund first, then decide
            # whether the attempts are spent, from the same re-read (#67).
            metadata = data.get("metadata")
            fenced_here = fenced_generation is not None and int(
                data.get("current_generation", 0)
            ) == int(fenced_generation)
            counted = count_startup_end(
                int(data.get("attempt_count", 0)),
                int(data.get("max_attempts", 3)),
                startup_refunds_used(metadata),
                max(0, int(startup_refund_limit)) if fenced_here else 0,
            )
            if counted.refunded:
                counting["attempt_count"] = counted.attempt_count
                counting["metadata"] = {
                    **(metadata if isinstance(metadata, dict) else {}),
                    STARTUP_REFUNDS_KEY: counted.refunds_used,
                }
            if counted.exhausted:
                target = TaskState.FAILED
            message = f"{error}; {counted.tail}" if error else counted.tail
        elif target is TaskState.READY:
            # ONLY a READY target is ever downgraded. A CANCELLED target is
            # the user's decision and survives exhausted attempts: recording
            # a task someone stopped as FAILED would misreport why it ended,
            # and a workflow's derived state settles on its WORST terminal
            # step (swarm_api.rollup), so the whole run would read FAILED.
            #
            # The shared predicate, not a second copy of the comparison.
            # This path had the rule and the scheduler's dispatch-failure
            # path did not, which is how a task reached 83 attempts against
            # a cap of 3 on 2026-09-23.
            if retries_exhausted(
                int(data.get("attempt_count", 0)),
                int(data.get("max_attempts", 3)),
            ):
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
            **counting,
        }
        if message:
            payload["last_error"] = message[:2000]
        if next_eligible_at is not None and target is not TaskState.CANCELLED:
            # A retry time means nothing on a task that will never run again.
            payload["next_eligible_at"] = next_eligible_at
        if target in TERMINAL_STATES:
            payload["completed_at"] = utcnow()
            payload["end_cause"] = (
                cancel_end_cause(data.get("metadata"))
                if target is TaskState.CANCELLED
                else failed_cause
            ).value
        return target, payload

    def fence_release_repair(
        self,
        task_id: str | None,
        *,
        expected_generation: int | None,
        lease_id: str | None,
        release_reason: str,
        repair: RepairPlan | None = None,
        only_from: tuple[TaskState, ...] | None = None,
        unless_holding_lease: str | None = None,
        refuse_while_task_holds_it: bool = False,
    ) -> OneRepair:
        """Fence, release and repair in ONE transaction (#560).

        These were three transactions, and the reconciler committed the first
        before it knew whether it could do the other two. When the kill between
        them was not confirmed it returned with the fence written and the lease
        held, and from then on every rule saw a superseded lease whose
        execution belonged to some other rule: four leases held for ten hours
        on 2026-10-04, with one cancel pressed on them and never honoured. Now
        the caller does whatever must precede the release -- the kill -- first,
        and only then are all three written together, or none of them.

        Each step keeps exactly the rule it had on its own -- these call the
        same decisions `invalidate_generation`, `release_lease` and
        `repair_task_state` make -- and each is still optional and refusable on
        its own: a fence somebody else already wrote is not written again, and
        the release and the repair still go ahead behind it. What is new:

        * every read precedes every write, as Firestore requires, so the
          release and the repair judge the task as the fence leaves it -- what
          they saw when the fence had committed in a transaction before theirs;
        * a release refused because the task still holds the lease (at its
          generation, in a concurrency state) also refuses the repair: moving a
          task off a lease that stays held is how capacity was leaked. With
          `refuse_while_task_holds_it` that is how a repair that has no proof
          its execution stopped is made safe -- it releases only once THIS
          transaction's fence (or an earlier one) has superseded the lease.

        The ended-at-startup refund (#67) is decided against this
        transaction's own fence, as it was against the fence the pass had just
        written.
        """
        task_ref = self._db.collection("tasks").document(task_id) if task_id else None
        lease_ref = self._db.collection("leases").document(lease_id) if lease_id else None

        def _apply(txn: Any) -> OneRepair:
            # ---- every read, before any write -------------------------
            task: dict[str, Any] | None = None
            if task_ref is not None:
                snap = _snapshot(txn.get(task_ref))
                task = (snap.to_dict() or {}) if snap.exists else None
            lease: dict[str, Any] | None = None
            if lease_ref is not None:
                snap = _snapshot(txn.get(lease_ref))
                lease = (snap.to_dict() or {}) if snap.exists else None
            # The task the LEASE names, which the release guard judges. The
            # finding's own task in every case this was written for; read
            # separately should the two ever differ.
            lease_task: dict[str, Any] | None = None
            lease_task_id = (lease or {}).get("task_id") or None
            if refuse_while_task_holds_it and lease is not None and lease_task_id:
                if lease_task_id != task_id:
                    snap = _snapshot(
                        txn.get(self._db.collection("tasks").document(lease_task_id))
                    )
                    lease_task = (snap.to_dict() or {}) if snap.exists else None

            # ---- decisions ---------------------------------------------
            new_generation: int | None = None
            if task is not None and task_id and expected_generation is not None:
                new_generation = self._fence_decision(
                    task,
                    task_id,
                    expected_generation,
                    only_from=only_from,
                    unless_holding_lease=unless_holding_lease,
                )
            after = (
                {**task, "current_generation": new_generation}
                if task is not None and new_generation is not None
                else task
            )
            refused = False
            if refuse_while_task_holds_it and lease is not None and lease_id and lease_task_id:
                judged = after if lease_task_id == task_id else lease_task
                refused = judged is not None and self._holds(
                    judged, lease, lease_id, lease_task_id
                )
            decided: tuple[TaskState, dict[str, Any]] | None = None
            if repair is not None and after is not None and task_id and not refused:
                decided = self._repair_decision(
                    after,
                    task_id,
                    to_state=repair.to_state,
                    expected_lease_id=repair.expected_lease_id,
                    error=repair.error,
                    next_eligible_at=repair.next_eligible_at,
                    only_from=only_from,
                    failed_cause=repair.failed_cause,
                    startup_refund_limit=repair.startup_refund_limit,
                    fenced_generation=(
                        new_generation if repair.startup_refund_limit is not None else None
                    ),
                )

            # ---- writes --------------------------------------------------
            # The frozen release reads the lease and its pools before it
            # writes anything, so it goes first: nothing of ours has been
            # written yet when it reads.
            released = False
            if lease_id and lease is not None and not refused:
                released = bool(
                    release_lease_in_transaction(
                        txn, db=self._db, lease_id=lease_id, reason=release_reason
                    )
                )
            update: dict[str, Any] = {}
            if new_generation is not None:
                update.update(current_generation=new_generation, updated_at=utcnow())
            if decided is not None:
                update.update(decided[1])
            if update and task_ref is not None:
                txn.update(task_ref, update)
            return OneRepair(
                new_generation=new_generation,
                released=released,
                repaired_to=decided[0] if decided is not None else None,
                release_refused=refused,
            )

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
