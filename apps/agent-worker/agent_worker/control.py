"""The worker's window onto the control plane.

Everything the worker knows about the outside world -- whether it is still the
rightful owner of its task, whether someone cancelled it, whether its provider
has run out of quota -- arrives through this module, and every durable fact the
worker produces leaves through it.

Three rules shape the design.

**Fencing is checked against the task document, not against the environment.**
The worker is handed a generation in its environment, but the authority is
`tasks/{id}.current_generation`. If a reconciler decided this attempt was dead
and a newer attempt was admitted, the task document is where that fact lives, so
that is what `validate_generation` reads -- before any workspace exists, before
any credential is fetched, before the runner is started.

**Releasing capacity goes through the frozen admission module.** There is
exactly one function in the codebase that decrements a pool, and it lives in
`swarm_common.admission`. The worker calls it inside a transaction like everyone
else, and it is idempotent, so the worker, the reconciler and a cancellation can
all race to release the same lease without inflating capacity.

**A fenced worker does not release the lease.** Its lease belongs to a
superseded generation; the live lease is someone else's. Releasing is the
reconciler's job precisely because the reconciler can order invalidation before
release, and touching pools on the way out of a fence is the one way a worker
could hand a slot to nobody.

**Every write to the task re-checks the fence inside its own transaction.**
That covers each state transition and the checkpoint pointer. Checking once
at startup and on each control poll was not enough. A worker can reach the
end of an attempt between two polls: on SIGTERM, a crash, or a runner that
exits on its own. A check made before the write also loses to a fence that
lands between that check and the write. So `_fenced_task` reads the task and
the lease through the same transaction that writes. If the attempt is fenced
it raises `FencedWriteRefused` and nothing is committed. `validate_generation`
and `_fenced_task` share one predicate (`_task_fence`, `_lease_fence`), so the
gate and the writes agree on what "fenced" means. An event that ANNOUNCES one
of those writes (QUOTA_EXHAUSTED ahead of a park) is written in the same
transaction (`transition`'s `events`), so it commits with the write or not at
all.

**Every document is checked against this worker's tenant before it is used.**
Firestore has no document-level IAM: `roles/datastore.user` is granted per
DATABASE, so the tenant service account this worker runs as can physically read
and write any task, lease, attempt or quota document in the `swarm` database.
Invariant 9 therefore cannot rest on IAM alone on this path, and every read
here compares `tenant_id` against the tenant the attempt was admitted for. A
mismatch raises `TenantMismatchError` and the worker exits having written
nothing -- not a state change, not a lease release, not even an event, because
all three would be writes into another tenant's data.

**Before the runner exists, every Firestore call is bounded.** Inside
`ControlPlane.startup_budget()` each read, write and transaction RPC this
module makes carries `startup_call_options`: the reads and writes directly,
and a transaction's own begin, commit and rollback through
`FirestoreTransactionRunner`. Outside it, every call keeps the library's
defaults. The lifecycle holds the window open from the generation check
until the runner child is built, and nowhere else.
"""

from __future__ import annotations

import functools
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from swarm_common.admission import _snapshot, release_lease_in_transaction
from swarm_common.models import (
    Attempt,
    ProviderState,
    TaskEvent,
    new_id,
    retries_exhausted,
    utcnow,
)
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    EventType,
    ParkReason,
    TaskState,
    assert_transition,
)

from .errors import ControlPlaneError, FencedError, FencedWriteRefused, TenantMismatchError
from .startup import StartupInterrupted

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlSignals:
    """A point-in-time read of everything that can stop or divert this worker."""

    state: TaskState
    generation: int
    cancel_requested: bool
    lease_released: bool
    lease_expires_at: datetime | None = None
    provider_state: ProviderState | None = None
    provider_paused: bool = False
    retry_after_seconds: int | None = None
    quota_reset_at: datetime | None = None
    observed_at: datetime | None = None

    def is_fenced(self, generation: int) -> bool:
        """True when this worker no longer owns the task."""
        return (
            self.generation != generation
            or self.lease_released
            or self.state in TERMINAL_STATES
        )

    def wants_stop(self, generation: int) -> bool:
        return self.cancel_requested or self.is_fenced(generation)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Firestore's server timestamps expose .timestamp_pb() / .ToDatetime() shims.
    to_dt = getattr(value, "ToDatetime", None)
    if callable(to_dt):
        return to_dt()
    return None


def _as_state(value: Any) -> TaskState:
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(value)
    except (ValueError, TypeError) as exc:
        raise ControlPlaneError(f"task document holds an unknown state {value!r}") from exc


# ---------------------------------------------------------------------------
# The fence: ONE definition
# ---------------------------------------------------------------------------
#
# Read by `validate_generation` before the agent starts, and by `_fenced_task`
# inside every transaction that writes the task. Two copies of this rule would
# drift, and then the gate and the writes would disagree about whether an
# attempt still owns its task. That disagreement is the defect these exist to
# remove. Split in two only because the lease is read after the task, in the
# same order as before, so a task-level fence is reported ahead of a lease
# document that another tenant owns.


def _task_fence(task: dict[str, Any], *, generation: int) -> tuple[int, str] | None:
    """Why `task` no longer belongs to the attempt at `generation`, or None.

    Returns `(observed_generation, reason)`.
    """
    current = int(task.get("current_generation", 0))
    state = _as_state(task.get("state"))
    if current != generation:
        return current, "task has moved to a newer generation"
    if state in TERMINAL_STATES:
        return current, f"task is already terminal ({state.value})"
    if state not in CONCURRENCY_STATES:
        # READY or PARKED means the lease was reclaimed underneath us.
        return current, f"task no longer holds capacity (state={state.value})"
    return None


def _lease_fence(
    task: dict[str, Any],
    lease: dict[str, Any] | None,
    *,
    generation: int,
    task_id: str,
    lease_id: str,
) -> tuple[int, str] | None:
    """Why this attempt's lease no longer gives it the task, or None."""
    current = int(task.get("current_generation", 0))
    if lease is None:
        return current, "lease document is gone"
    if lease.get("released_at") is not None:
        return current, "lease has already been released"
    if int(lease.get("generation", -1)) != generation:
        return int(lease.get("generation", -1)), "lease belongs to a different generation"
    if lease.get("task_id") != task_id:
        return current, "lease belongs to a different task"
    if task.get("current_lease_id") not in (None, lease_id):
        return current, "task points at a different lease"
    return None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TransactionRunner(Protocol):
    """Runs a function inside one Firestore transaction, retrying on contention.

    `call_options` is the `retry` and `timeout` for the transaction's own
    RPCs. `ControlPlane` passes it only when it is not empty, so a runner that
    takes the body alone still fits everywhere outside the startup window.
    """

    def run(
        self, fn: Callable[[Any], Any], *, call_options: Mapping[str, Any] | None = None
    ) -> Any: ...


class FirestoreTransactionRunner:
    def __init__(self, db: Any) -> None:
        self._db = db

    def run(
        self, fn: Callable[[Any], Any], *, call_options: Mapping[str, Any] | None = None
    ) -> Any:
        from google.cloud import firestore  # lazy: unit tests never import grpc

        transaction = self._db.transaction()
        if call_options:
            # The same kind of transaction the client makes, with its begin,
            # commit and rollback under the budget. See `_with_budget`.
            transaction = _with_budget(type(transaction))(self._db, budget=call_options)

        @firestore.transactional
        def _inner(txn: Any) -> Any:
            return fn(txn)

        return _inner(transaction)


def _commit_retry(retry: Any) -> Any:
    """`retry` for a transaction's commit: the same deadline, a narrower predicate.

    A commit is retried only on errors that mean it was not applied:
    UNAVAILABLE and RESOURCE_EXHAUSTED. DEADLINE_EXCEEDED and INTERNAL can
    arrive after the commit landed, and a second commit of the same
    transaction id is then refused. That would report a failed transition that
    had in fact been made. The library's own default for `commit` draws the
    same line. A read has no such hazard, which is why the read budget retries
    DEADLINE_EXCEEDED and INTERNAL.
    """
    with_predicate = getattr(retry, "with_predicate", None)
    if with_predicate is None:
        return retry
    from google.api_core import exceptions as core_exceptions  # lazy: grpc
    from google.api_core.retry import if_exception_type

    return with_predicate(
        if_exception_type(core_exceptions.ServiceUnavailable, core_exceptions.ResourceExhausted)
    )


@functools.lru_cache(maxsize=None)
def _with_budget(base: type) -> type:
    """`base`, a Firestore `Transaction` class, with its three RPCs under a budget.

    WHY A SUBCLASS. `Transaction._begin`, `_commit` and `_rollback` call the
    Firestore API with no `retry` or `timeout`, so each gets the library's
    default: 60 s of retries per call (google-cloud-firestore 2.30.0,
    services/firestore/transports/base.py). No public argument reaches them.
    These three overrides make the same requests the library makes, with the
    budget added. They read the same private attributes the library's own
    methods read (`_client._firestore_api`, `_id`, `_write_pbs`), and uv.lock
    pins the library version they were read from.
    `tests/unit/worker/test_startup_window_under_real_transactions.py` runs
    them inside the library's own `firestore.transactional`.

    TWO CHANGES TO ROLLBACK, besides the budget. `firestore.transactional`
    calls `_rollback` from `except BaseException`, and whatever `_rollback`
    raises replaces the exception that was unwinding:

      * A transaction whose `begin_transaction` never returned has no id. The
        library raises `ValueError("... cannot be rolled back.")` for that,
        and a SIGTERM's `StartupInterrupted`, or the `RetryError` that ended
        the begin, left the transaction as that ValueError. Here there is
        nothing to roll back, so nothing is raised and the original goes on.
      * A rollback RPC that fails is added to the unwinding exception as a
        note and not raised. Firestore expires an abandoned transaction by
        itself. What unwound is the error the caller has to act on.

    Under a SIGTERM the rollback is one try, with the per-call timeout and no
    retries. The process has been asked to leave, and the rollback only frees
    locks that Firestore frees on its own when the transaction expires.
    """
    from google.cloud.firestore_v1.base_transaction import (  # lazy: grpc
        _CANT_BEGIN,
        _CANT_COMMIT,
        _CANT_ROLLBACK,
    )

    class BudgetedTransaction(base):  # type: ignore[misc, valid-type]
        def __init__(self, client: Any, *, budget: Mapping[str, Any]) -> None:
            super().__init__(client)
            self._budget = dict(budget)

        def _begin(self, retry_id: bytes | None = None) -> None:
            if self.in_progress:
                raise ValueError(_CANT_BEGIN.format(self._id))
            response = self._client._firestore_api.begin_transaction(
                request={
                    "database": self._client._database_string,
                    "options": self._options_protobuf(retry_id),
                },
                metadata=self._client._rpc_metadata,
                **self._budget,
            )
            self._id = response.transaction

        def _commit(self) -> list:
            if not self.in_progress:
                raise ValueError(_CANT_COMMIT)
            options = dict(self._budget)
            if "retry" in options:
                options["retry"] = _commit_retry(options["retry"])
            response = self._client._firestore_api.commit(
                request={
                    "database": self._client._database_string,
                    "writes": self._write_pbs,
                    "transaction": self._id,
                },
                metadata=self._client._rpc_metadata,
                **options,
            )
            self._clean_up()
            self.write_results = list(response.write_results)
            self.commit_time = response.commit_time
            return self.write_results

        def _rollback(self) -> None:
            unwinding = sys.exc_info()[1]
            if not self.in_progress:
                if unwinding is None:
                    raise ValueError(_CANT_ROLLBACK)
                return
            options = dict(self._budget)
            if isinstance(unwinding, StartupInterrupted):
                options["retry"] = None
            try:
                self._client._firestore_api.rollback(
                    request={
                        "database": self._client._database_string,
                        "transaction": self._id,
                    },
                    metadata=self._client._rpc_metadata,
                    **options,
                )
            except Exception as exc:
                if unwinding is None:
                    raise
                unwinding.add_note(
                    "the transaction's rollback failed as well, and was not "
                    f"raised: {type(exc).__name__}: {exc}"
                )
            finally:
                self._clean_up()

    BudgetedTransaction.__name__ = f"Budgeted{base.__name__}"
    BudgetedTransaction.__qualname__ = BudgetedTransaction.__name__
    return BudgetedTransaction


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


class ControlPlane:
    """Firestore-backed control-plane operations for exactly one attempt."""

    def __init__(
        self,
        db: Any,
        *,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        tenant_id: str,
        generation: int,
        logger: Any,
        txn_runner: TransactionRunner | None = None,
        heartbeat_extension_seconds: int = 120,
        startup_call_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._db = db
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.lease_id = lease_id
        self.tenant_id = tenant_id
        self.generation = generation
        self._log = logger
        self._txn = txn_runner or FirestoreTransactionRunner(db)
        self._heartbeat_extension = heartbeat_extension_seconds
        self._lease_released = False
        # `retry` and `timeout` for every Firestore call a worker makes before
        # its runner exists, applied inside `startup_budget()`. The entrypoint
        # supplies a bounded budget (`__main__.firestore_startup_call_options`).
        # Empty means the library's defaults, which for a document read is up
        # to 300 s of silent retries, and 60 s for each of a transaction's
        # begin, commit and rollback. Calls made while the agent runs keep the
        # defaults: changing how long a running agent tolerates a Firestore
        # outage is a separate decision.
        self._startup_call: dict[str, Any] = dict(startup_call_options or {})
        # True inside `startup_budget()`. Read by `call_options`, which every
        # call this class can make before the runner goes through. The writes
        # made only while an agent runs or after it (`record_checkpoint`,
        # `record_resource_usage`, `record_spend`, `update_quota_state`) do
        # not ask, and keep the library's defaults.
        self._budgeted = False

    @property
    def startup_call_options(self) -> dict[str, Any]:
        return dict(self._startup_call)

    @contextmanager
    def startup_budget(self) -> Iterator[None]:
        """Every Firestore call made inside carries `startup_call_options`.

        Reads, writes, events, the heartbeat, and a transaction's own begin,
        commit and rollback. The lifecycle opens it for the generation check,
        again from `record_attempt_start` until the runner child is built, and
        for the attempt's own record on the two exits taken from that window.
        Nowhere else: the same calls made while an agent runs keep the
        library's defaults.

        ONE SWITCH, rather than a budget argument on each call. Before this,
        the budget was passed call by call, and the calls nobody passed it to
        (the three transactions in `advance_to_running`, the STARTING and
        RUNNING events, the first heartbeat) kept 60 s and 300 s defaults.
        """
        previous = self._budgeted
        self._budgeted = True
        try:
            yield
        finally:
            self._budgeted = previous

    def call_options(self) -> dict[str, Any]:
        """`retry` and `timeout` for a Firestore call made now.

        The startup budget inside `startup_budget()`, else empty, which means
        the library's defaults. Public because the lifecycle's own startup
        reads (`secrets.load_tenant`, `inputs.stage_inputs`) take the same
        options.
        """
        return dict(self._startup_call) if self._budgeted else {}

    def _run_transaction(self, fn: Callable[[Any], Any]) -> Any:
        options = self.call_options()
        if options:
            return self._txn.run(fn, call_options=options)
        # Called exactly as before when there is no budget, so a runner written
        # to the one-argument form still works outside the window.
        return self._txn.run(fn)

    # -- raw reads ---------------------------------------------------------
    def _task_ref(self) -> Any:
        return self._db.collection("tasks").document(self.task_id)

    def _lease_ref(self) -> Any:
        return self._db.collection("leases").document(self.lease_id)

    def _attempt_ref(self) -> Any:
        return self._db.collection("attempts").document(self.attempt_id)

    def _quota_ref(self, provider: str) -> Any:
        return self._db.collection("quota").document(f"{provider}:{self.tenant_id}")

    # -- tenant scoping ----------------------------------------------------
    def _assert_tenant(self, data: dict[str, Any], *, kind: str, document_id: str) -> dict[str, Any]:
        """Refuse a document that belongs to a different tenant.

        An absent `tenant_id` is refused too. Treating "missing" as "mine" is
        how an unscoped read becomes a cross-tenant read the first time a
        document is written by something that forgot the field.
        """
        actual = data.get("tenant_id")
        if actual != self.tenant_id:
            raise TenantMismatchError(
                kind=kind,
                document_id=document_id,
                expected=self.tenant_id,
                actual=actual if isinstance(actual, str) else None,
            )
        return data

    def fetch_task(self) -> dict[str, Any]:
        snap = self._task_ref().get(**self.call_options())
        if not snap.exists:
            raise FencedError(self.generation, -1, "task document no longer exists")
        return self._assert_tenant(
            snap.to_dict() or {}, kind="task", document_id=self.task_id
        )

    def fetch_lease(self) -> dict[str, Any] | None:
        snap = self._lease_ref().get(**self.call_options())
        if not snap.exists:
            return None
        return self._assert_tenant(
            snap.to_dict() or {}, kind="lease", document_id=self.lease_id
        )

    # -- fencing -----------------------------------------------------------
    def validate_generation(self) -> ControlSignals:
        """THE safety gate. Raises FencedError if this worker is superseded.

        Called before the workspace is created, before credentials are read and
        before the runner is started. Everything below the raise is what does
        NOT happen when an attempt has been superseded.

        It is also the tenant gate: `fetch_task` and `fetch_lease` both refuse a
        document carrying another tenant's id, and the attempt document is
        checked here as well, so an attempt cannot be pointed at a task, a lease
        and an attempt that do not all belong to the same tenant.

        Called only before the runner, inside `startup_budget()`, so all three
        reads carry the startup budget, and a Firestore that cannot be reached
        raises within it rather than after 300 s of silent retries. The caller
        logs what it raised.
        """
        task = self.fetch_task()
        attempt_snap = self._attempt_ref().get(**self.call_options())
        if attempt_snap.exists:
            self._assert_tenant(
                attempt_snap.to_dict() or {}, kind="attempt", document_id=self.attempt_id
            )
        fence = _task_fence(task, generation=self.generation)
        if fence is not None:
            raise FencedError(self.generation, *fence)

        lease = self.fetch_lease()
        fence = _lease_fence(
            task,
            lease,
            generation=self.generation,
            task_id=self.task_id,
            lease_id=self.lease_id,
        )
        if fence is not None:
            raise FencedError(self.generation, *fence)
        assert lease is not None  # `_lease_fence` refuses a missing lease

        return ControlSignals(
            state=_as_state(task.get("state")),
            generation=int(task.get("current_generation", 0)),
            cancel_requested=bool(task.get("cancel_requested")),
            lease_released=False,
            lease_expires_at=_as_datetime(lease.get("expires_at")),
            observed_at=utcnow(),
        )

    def _fenced_task(self, txn: Any, *, write: str) -> dict[str, Any]:
        """Read the task and the lease THROUGH `txn`; refuse if this attempt is fenced.

        Returns the task as `txn` read it. The caller writes only after this
        returns, because Firestore forbids a read after a write inside a
        transaction. If the fence lands after these reads, the commit is
        refused and the body is run again against fresh reads, so the write is
        never made on a stale answer.

        Raises `FencedWriteRefused` with nothing written, and
        `TenantMismatchError` for a document that is not this tenant's.
        """
        # Through the transaction, and still under the budget inside the
        # startup window: `Transaction.get` is a `batch_get_documents` call,
        # with its 300 s default. `firestore.transactional` retries only an
        # ABORTED commit, never a read.
        options = self.call_options()
        task_snap = _snapshot(txn.get(self._task_ref(), **options))
        if not task_snap.exists:
            raise FencedWriteRefused(
                self.generation, -1, "task document no longer exists", write=write
            )
        task = self._assert_tenant(
            task_snap.to_dict() or {}, kind="task", document_id=self.task_id
        )
        fence = _task_fence(task, generation=self.generation)
        if fence is None:
            lease_snap = _snapshot(txn.get(self._lease_ref(), **options))
            lease = (
                self._assert_tenant(
                    lease_snap.to_dict() or {}, kind="lease", document_id=self.lease_id
                )
                if lease_snap.exists
                else None
            )
            fence = _lease_fence(
                task,
                lease,
                generation=self.generation,
                task_id=self.task_id,
                lease_id=self.lease_id,
            )
        if fence is not None:
            raise FencedWriteRefused(self.generation, *fence, write=write)
        return task

    def ensure_owner(self, *, write: str) -> None:
        """Refuse, with `FencedWriteRefused`, work this attempt no longer owns.

        It writes nothing. It is for work that must be refused BEFORE its first
        side effect. A checkpoint uploads its archive long before it writes the
        pointer to it, and a stale archive left in the task's prefix is one
        that `CheckpointManager.find_latest` can later choose. It uses the same
        predicate as every guarded write, so it cannot pass where a write would
        be refused. It does not replace the check in the write's own
        transaction, because the fence can still land in between.

        IT IS NOT A READ-ONLY TRANSACTION. `self._txn.run` in production is
        `FirestoreTransactionRunner.run`, which opens `db.transaction()`, and
        `read_only` defaults to False there. So this is a read-write
        transaction that reads the task and the lease and commits no writes.
        The cost is that Firestore holds its locks on those two documents for
        the two reads and the empty commit. A reconciler fencing this task at
        that instant waits for them, or one of the two transactions is aborted
        and retried by `firestore.transactional`. Nothing is written either
        way. A read-only transaction would take no locks. It was not used
        because `TransactionRunner` has one entry point, and no test in this
        repository runs the worker against a real Firestore transaction.
        """
        self._run_transaction(lambda txn: self._fenced_task(txn, write=write))

    # -- polling -----------------------------------------------------------
    def poll(self, provider: str | None = None) -> ControlSignals:
        """One read of task + lease (+ quota), used by the supervision loop.

        The quota preflight makes the one poll that comes before the runner,
        inside `startup_budget()`, and so under the budget. The supervision
        loop's polls keep the library's defaults.
        """
        try:
            task = self.fetch_task()
        except FencedError:
            return ControlSignals(
                state=TaskState.CANCELLED,
                generation=-1,
                cancel_requested=False,
                lease_released=True,
                observed_at=utcnow(),
            )
        lease = self.fetch_lease() or {}
        provider_state: ProviderState | None = None
        provider_paused = False
        retry_after: int | None = None
        reset_at: datetime | None = None

        if provider:
            qsnap = self._quota_ref(provider).get(**self.call_options())
            if qsnap.exists:
                q = qsnap.to_dict() or {}
                try:
                    provider_state = ProviderState(q.get("state", ProviderState.UNKNOWN.value))
                except ValueError:
                    provider_state = ProviderState.UNKNOWN
                provider_paused = provider_state in (
                    ProviderState.EXHAUSTED,
                    ProviderState.COOLDOWN,
                    ProviderState.DISABLED,
                )
                retry_after = q.get("retry_after_seconds")
                reset_at = _as_datetime(q.get("reset_at")) or _as_datetime(q.get("cooldown_until"))

        return ControlSignals(
            state=_as_state(task.get("state")),
            generation=int(task.get("current_generation", -1)),
            cancel_requested=bool(task.get("cancel_requested")),
            lease_released=lease.get("released_at") is not None or not lease,
            lease_expires_at=_as_datetime(lease.get("expires_at")),
            provider_state=provider_state,
            provider_paused=provider_paused,
            retry_after_seconds=int(retry_after) if retry_after is not None else None,
            quota_reset_at=reset_at,
            observed_at=utcnow(),
        )

    # -- events ------------------------------------------------------------
    def _event_write(
        self,
        event_type: EventType,
        detail: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """The event document, and the reference it is written to.

        One shape for both ways an event is written: on its own (`emit`) and
        inside a fenced transaction (`transition`'s `events`).
        """
        event = TaskEvent(
            event_id=new_id("evt"),
            task_id=self.task_id,
            tenant_id=self.tenant_id,
            type=event_type,
            at=at or utcnow(),
            attempt_id=self.attempt_id,
            lease_id=self.lease_id,
            generation=self.generation,
            detail=detail or {},
        )
        ref = self._task_ref().collection("events").document(event.event_id)
        return ref, {
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

    def emit(
        self,
        event_type: EventType,
        detail: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
    ) -> None:
        ref, document = self._event_write(event_type, detail, at=at)
        # A retry after a DEADLINE_EXCEEDED that had landed rewrites the same
        # event id with the same bytes, so the read budget's predicate is safe.
        ref.set(document, **self.call_options())
        self._log.info("event", event_type=event_type.value, detail=document["detail"])

    # -- state transitions -------------------------------------------------
    def _current_state(self) -> TaskState:
        return _as_state(self.fetch_task().get("state"))

    def transition(
        self,
        to_state: TaskState,
        *,
        fields: dict[str, Any] | None = None,
        events: Sequence[tuple[EventType, dict[str, Any]]] = (),
    ) -> None:
        """Move the task to `to_state`, ONLY while this attempt still owns it.

        The state is read inside the same transaction as the fence, and the
        transition is checked against that read. The earlier version read the
        task and then updated it blind. On the way out of a fenced attempt,
        that parked a task the reconciler had fenced. It also wrote PARKED or
        FAILED over a newer attempt the scheduler had already admitted.

        `events` are written in the same transaction, ahead of the state
        change, so they commit with it or not at all. They are for an event
        that ANNOUNCES this write. Emitted on its own before the write, the
        announcement lands even when the write it announces is refused, in a
        stream that by then belongs to a newer generation.

        Raises `FencedWriteRefused` with nothing written, events included.
        """
        write = f"transition to {to_state.value}"
        # Built once, outside the body. A contended transaction runs the body
        # again, and each run must write the same event ids, so the retry
        # cannot leave the announcement in the stream twice.
        announced = [self._event_write(kind, detail) for kind, detail in events]

        def _apply(txn: Any) -> None:
            task = self._fenced_task(txn, write=write)
            current = _as_state(task.get("state"))
            if current is not to_state:
                assert_transition(current, to_state)
            for ref, document in announced:
                txn.set(ref, document)
            if current is to_state:
                if fields:
                    txn.update(self._task_ref(), {**fields, "updated_at": utcnow()})
                return
            payload: dict[str, Any] = {"state": to_state.value, "updated_at": utcnow()}
            payload.update(fields or {})
            txn.update(self._task_ref(), payload)

        self._run_transaction(_apply)
        for _, document in announced:
            self._log.info("event", event_type=document["type"], detail=document["detail"])

    def advance_to_running(self) -> None:
        """Walk LEASED -> DISPATCHED -> STARTING -> RUNNING legally.

        The dispatcher normally sets DISPATCHED, but a worker that starts faster
        than that write lands here at LEASED, and skipping a hop would trip the
        frozen state machine rather than silently corrupting it.
        """
        state = self._current_state()
        if state is TaskState.LEASED:
            self.transition(TaskState.DISPATCHED)
            state = TaskState.DISPATCHED
        if state is TaskState.DISPATCHED:
            self.transition(TaskState.STARTING, fields={"started_at": utcnow()})
            self.emit(EventType.STARTING)
            state = TaskState.STARTING
        if state is TaskState.STARTING:
            self.transition(TaskState.RUNNING)
            self.emit(EventType.RUNNING)
            return
        if state is TaskState.RUNNING:
            return
        raise ControlPlaneError(f"cannot start from state {state.value}")

    # -- attempt bookkeeping ----------------------------------------------
    def record_attempt_start(self, *, backend: str, execution_name: str | None) -> None:
        attempt = Attempt(
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            tenant_id=self.tenant_id,
            generation=self.generation,
            lease_id=self.lease_id,
            backend=backend,
            created_at=utcnow(),
            execution_name=execution_name,
            started_at=utcnow(),
        )
        self._attempt_ref().set(
            {
                "attempt_id": attempt.attempt_id,
                "task_id": attempt.task_id,
                "tenant_id": attempt.tenant_id,
                "generation": attempt.generation,
                "lease_id": attempt.lease_id,
                "backend": attempt.backend,
                "created_at": attempt.created_at,
                "execution_name": attempt.execution_name,
                "started_at": attempt.started_at,
                "completed_at": None,
                "exit_code": None,
                "error": None,
                "peak_rss_bytes": None,
                "peak_disk_bytes": None,
                "oom_near_miss": False,
                "checkpoints": [],
            },
            # The attempt's first write, and the first write of all. Made
            # inside `startup_budget()`, like the reads before it.
            **self.call_options(),
        )

    def record_checkpoint(self, *, checkpoint_id: str, uri: str, size_bytes: int, seq: int) -> None:
        # Read-modify-write rather than ArrayUnion: exactly one worker owns an
        # attempt document, so there is no contention to serialise, and this
        # keeps the Firestore sentinel types out of the worker's hot path.
        snap = self._attempt_ref().get()
        existing: list[str] = []
        if snap.exists:
            data = self._assert_tenant(
                snap.to_dict() or {}, kind="attempt", document_id=self.attempt_id
            )
            existing = list(data.get("checkpoints", []))
        if checkpoint_id not in existing:
            existing.append(checkpoint_id)
        # merge-set, not update: an attempt cancelled before it started has no
        # attempt document yet, and losing the record would be worse than
        # creating it late. `tenant_id` goes in every merge so a document this
        # path creates is never one the tenant check would later refuse.
        self._attempt_ref().set(
            {"checkpoints": existing, "tenant_id": self.tenant_id}, merge=True
        )

        # FENCED LIKE A TRANSITION. `latest_checkpoint` is what the next
        # attempt restores from. A stale worker repointing it would hand the
        # stale attempt's work to whatever runs after a newer attempt.
        def _point(txn: Any) -> None:
            self._fenced_task(txn, write="checkpoint pointer")
            txn.update(self._task_ref(), {"latest_checkpoint": uri, "updated_at": utcnow()})

        self._run_transaction(_point)
        self.emit(
            EventType.CHECKPOINT_COMPLETED,
            {"checkpoint_id": checkpoint_id, "uri": uri, "size_bytes": size_bytes, "seq": seq},
        )

    def record_resource_usage(
        self, *, peak_rss_bytes: int, peak_disk_bytes: int, oom_near_miss: bool
    ) -> None:
        self._attempt_ref().set(
            {
                "peak_rss_bytes": int(peak_rss_bytes),
                "peak_disk_bytes": int(peak_disk_bytes),
                "oom_near_miss": bool(oom_near_miss),
                "tenant_id": self.tenant_id,
            },
            merge=True,
        )

    def record_cpu_usage(self, fields: dict[str, float]) -> None:
        """The attempt's CPU, onto the attempt, as typed fields (request #15).

        `fields` is `metrics.attempt_cpu_fields`: the attempt's own figures,
        every runner combined, with anything not measured already left out.
        Only the four keys the frozen `Attempt` declares are written, and only
        as numbers -- `bool` excluded, for the reason `record_spend` gives. A
        key that is absent is left as it is on the document: the write is a
        merge, so a null would erase a figure an earlier write recorded.

        The attempt document is this attempt's own. A superseded attempt still
        writes it, as it writes its memory peak -- the fence guards the task,
        its lease and its event stream, which this does not touch.
        """
        from .metrics import ATTEMPT_CPU_FIELDS

        doc: dict[str, Any] = {
            key: float(fields[key])
            for key in ATTEMPT_CPU_FIELDS
            if isinstance(fields.get(key), (int, float)) and not isinstance(fields.get(key), bool)
        }
        if not doc:
            return
        doc["tenant_id"] = self.tenant_id
        self._attempt_ref().set(doc, merge=True)

    def record_spend(self, usage: dict[str, Any]) -> None:
        """Token counts and cost, onto the attempt, as typed fields.

        `usage` is what `lifecycle._usage_summary` pulled out of the CLI result
        BEFORE truncation. Only the keys the frozen `Attempt` declares are
        written: the summary also carries num_turns, durations and a model list,
        which belong in the runner summary rather than as columns on an attempt.

        A key the runner did not report is OMITTED, not written as zero. None
        means "not reported" and zero means "cost nothing", and a mock task is
        genuinely the second while a result that failed to parse is the first.
        """
        if not usage:
            return
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        # `not isinstance(v, bool)` on every one of them: bool subclasses int in
        # Python, so `input_tokens: True` would land as a token count of 1. A
        # silently wrong number is the failure mode this whole field set exists
        # to avoid, and it is cheaper to exclude the type than to explain the
        # number later.
        doc: dict[str, Any] = {
            key: int(usage[key])
            for key in fields
            if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
        }
        cost = usage.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            doc["cost_usd"] = float(cost)
        if not doc:
            return
        doc["tenant_id"] = self.tenant_id
        self._attempt_ref().set(doc, merge=True)

    def record_attempt_end(self, *, exit_code: int | None, error: str | None) -> None:
        self._attempt_ref().set(
            {
                "completed_at": utcnow(),
                "exit_code": exit_code,
                "error": error,
                "tenant_id": self.tenant_id,
            },
            merge=True,
            **self.call_options(),
        )

    # -- heartbeat ---------------------------------------------------------
    def heartbeat(self) -> None:
        now = utcnow()
        self._lease_ref().update(
            {
                "heartbeat_at": now,
                "expires_at": now + timedelta(seconds=self._heartbeat_extension),
            },
            # The startup heartbeats, inside `startup_budget()`. The payload is
            # built before the call, so a retry writes the same values.
            **self.call_options(),
        )

    # -- quota -------------------------------------------------------------
    def update_quota_state(
        self,
        *,
        provider: str,
        state: ProviderState,
        retry_after_seconds: int | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        """Publish what this worker learned about the provider.

        The quota broker owns the adaptive limits; the worker only reports the
        ground truth it just observed (a 429, a retry-after header) so the next
        admission decision is made with it.

        `rate_limit_count` IS THE COUNT OF THE CURRENT RATE-LIMIT RUN (CP-10,
        #85; owner decision 2026-09-25). A run starts at a 429 and ends when a
        clean run reports the provider AVAILABLE -- here, rule (c) -- or when
        the broker's `aimd.refresh` retires the cooldown and the reset window,
        which writes 0 ("The rate-limit run is over"). Provider quota labels the
        column `429s (this run)`, and three rules make the label true:

          (a) a NEW document counts 1 only when its first report is itself a
              429; it used to be seeded with 1 whatever the report was, so a
              document created by a clean run claimed a 429 nobody saw;
          (b) a stored count with no `last_429_at` behind it is read as 0
              before anything is added, so a document seeded that way corrects
              itself on its next report -- no one-off Firestore write;
          (c) a report of AVAILABLE writes 0, because `refresh` never resets a
              document that is already AVAILABLE, so without this the count
              would carry 429s from runs that are over.

        The broker's exhaustion threshold (`AimdConfig.exhaustion_threshold`)
        therefore counts 429s since the last clean run or retired cooldown. A
        document no worker reports on again keeps its old count until one does.
        `last_429_at` is never cleared: `Last 429` keeps its time when the count
        returns to 0.
        """
        now = utcnow()
        rate_limited = state in (ProviderState.EXHAUSTED, ProviderState.THROTTLED)
        payload: dict[str, Any] = {
            "provider": provider,
            "tenant_id": self.tenant_id,
            "state": state.value,
            "updated_at": now,
        }
        if retry_after_seconds is not None:
            payload["retry_after_seconds"] = int(retry_after_seconds)
        if reset_at is not None:
            payload["reset_at"] = reset_at
        if rate_limited:
            payload["last_429_at"] = now
        ref = self._quota_ref(provider)
        snap = ref.get()
        if snap.exists:
            existing = self._assert_tenant(
                snap.to_dict() or {},
                kind="quota",
                document_id=f"{provider}:{self.tenant_id}",
            )
            if state == ProviderState.AVAILABLE:
                # (c) A clean run ends the run.
                payload["rate_limit_count"] = 0
            else:
                # (b) A count with no 429 behind it is no count at all.
                stored = (
                    int(existing.get("rate_limit_count", 0))
                    if existing.get("last_429_at") is not None
                    else 0
                )
                payload["rate_limit_count"] = stored + (1 if rate_limited else 0)
            ref.update(payload)
        else:
            payload.setdefault("configured_hard_max", 50)
            # (a) One only when the first report is itself a 429.
            payload["rate_limit_count"] = 1 if rate_limited else 0
            payload.setdefault("success_count", 0)
            ref.set(payload)

    # -- lease release -----------------------------------------------------
    def release_lease(self, reason: str) -> bool:
        """Return capacity. Idempotent, and never called on the fenced path."""
        if self._lease_released:
            return False

        def _release(txn: Any) -> bool:
            return release_lease_in_transaction(
                txn, db=self._db, lease_id=self.lease_id, reason=reason
            )

        released = bool(self._run_transaction(_release))
        self._lease_released = True
        if released:
            self.emit(EventType.LEASE_RELEASED, {"reason": reason})
        return released

    # -- terminal outcomes -------------------------------------------------
    def park(
        self,
        *,
        reason: ParkReason,
        next_eligible_at: datetime,
        detail: dict[str, Any] | None = None,
        announce: Sequence[tuple[EventType, dict[str, Any]]] = (),
    ) -> None:
        """Quota path: checkpointed already, now give the slot back and exit.

        Order matters. The task leaves the concurrency states first, so that the
        instant the pools are decremented there is no document claiming this
        task is still running.

        The transition is fenced (see `transition`). A superseded attempt gets
        `FencedWriteRefused` here, before the event and before the release, so
        it writes nothing.

        `announce` is for an event that says this park is happening, such as
        QUOTA_EXHAUSTED. It is written in the park's own transaction, ahead of
        the state change. A caller that emitted it before calling here would
        leave it in the task's stream when the park is refused: a fence that
        lands during the park's uploads is met here, after the announcement.
        """
        self.transition(
            TaskState.PARKED,
            fields={
                "park_reason": reason.value,
                "next_eligible_at": next_eligible_at,
                "current_lease_id": None,
                "blocked_by": [{"reason": reason.value, **(detail or {})}],
            },
            events=announce,
        )
        self.emit(
            EventType.PARKED,
            {"reason": reason.value, "next_eligible_at": next_eligible_at, **(detail or {})},
        )
        self.release_lease(f"parked:{reason.value}")

    def finish(
        self,
        *,
        state: TaskState,
        exit_code: int | None,
        error: str | None = None,
        result_summary: dict[str, Any] | None = None,
    ) -> None:
        """Persist a terminal state, record the attempt's end, release the lease.

        The transition is fenced (see `transition`), and it comes first. A
        superseded attempt gets `FencedWriteRefused` before anything else is
        written: no attempt end, no event, no release.
        """
        if state not in TERMINAL_STATES:
            raise ControlPlaneError(f"{state.value} is not terminal")
        fields: dict[str, Any] = {
            "completed_at": utcnow(),
            "current_lease_id": None,
            "last_error": error,
        }
        if result_summary is not None:
            fields["result_summary"] = result_summary
        self.transition(state, fields=fields)
        self.record_attempt_end(exit_code=exit_code, error=error)
        event = {
            TaskState.SUCCEEDED: EventType.SUCCEEDED,
            TaskState.FAILED: EventType.FAILED,
            TaskState.CANCELLED: EventType.CANCELLED,
            TaskState.DEAD_LETTERED: EventType.DEAD_LETTERED,
        }[state]
        self.emit(event, {"exit_code": exit_code, "error": error})
        self.release_lease(f"terminal:{state.value}")

    def fail_retryably(
        self,
        *,
        exit_code: int | None,
        error: str,
        cause: str,
        result_summary: dict[str, Any] | None = None,
        retry_delay_seconds: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> TaskState:
        """End this ATTEMPT as failed, and send the task back to READY if it has
        attempts left. Returns the state the task was left in.

        `finish(FAILED)` ends the task for good. This ends only the attempt.
        One fenced transaction reads the task and picks the target:

          * CANCELLED when a cancel was requested. The user's decision survives
            a retry, as it does in the reconciler's `repair_task_state`.
          * FAILED once `attempt_count` has reached `max_attempts`, decided by
            `swarm_common.models.retries_exhausted`. That is the predicate the
            scheduler and the reconciler apply, not a second copy of it.
          * READY otherwise, with `next_eligible_at`. The scheduler admits it
            on a later drain as a new attempt, which acquires a new lease with
            a new generation. RUNNING -> READY is legal in the frozen state
            machine.

        The count is read from the DOCUMENT inside the transaction, never from
        the task this worker fetched when it started. The scheduler's
        dispatch-failure path decided from its own snapshot and was one attempt
        behind (`task_b568a623be8645eb87c6`, 2026-09-24).

        Then the attempt's end, one event and the lease release, in the order
        `finish` uses. The task leaves the concurrency states before the pools
        are decremented. The event for READY is RETRYING with `cause`, because
        the task is not terminal and an event type that says it is would be
        read that way (see `EventType.CANCEL_REQUESTED`).

        Raises `FencedWriteRefused` with nothing written.
        """
        write = "retryable failure"
        now = utcnow()

        def _apply(txn: Any) -> tuple[TaskState, int, int]:
            task = self._fenced_task(txn, write=write)
            current = _as_state(task.get("state"))
            attempt_count = int(task.get("attempt_count", 0))
            max_attempts = int(task.get("max_attempts", 3))
            if task.get("cancel_requested"):
                target = TaskState.CANCELLED
            elif retries_exhausted(attempt_count, max_attempts):
                target = TaskState.FAILED
            else:
                target = TaskState.READY
            assert_transition(current, target)
            payload: dict[str, Any] = {
                "state": target.value,
                "updated_at": now,
                "current_lease_id": None,
                "last_error": (
                    f"cancelled on request; {error}" if target is TaskState.CANCELLED else error
                ),
            }
            if result_summary is not None:
                payload["result_summary"] = result_summary
            if target is TaskState.READY:
                payload["next_eligible_at"] = now + timedelta(
                    seconds=max(0, retry_delay_seconds)
                )
            else:
                # Terminal. CLEARED, not omitted: a retry time on a task that
                # will never run again is a promise nothing keeps.
                payload["completed_at"] = now
                payload["next_eligible_at"] = None
            txn.update(self._task_ref(), payload)
            return target, attempt_count, max_attempts

        target, attempt_count, max_attempts = self._run_transaction(_apply)
        self.record_attempt_end(exit_code=exit_code, error=error)
        event = {
            TaskState.READY: EventType.RETRYING,
            TaskState.FAILED: EventType.FAILED,
            TaskState.CANCELLED: EventType.CANCELLED,
        }[target]
        self.emit(
            event,
            {
                "exit_code": exit_code,
                "error": error,
                "cause": cause,
                **(detail or {}),
                "to_state": target.value,
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "retries_exhausted": target is TaskState.FAILED,
            },
        )
        self.release_lease(
            f"retry:{cause}" if target is TaskState.READY else f"terminal:{target.value}"
        )
        return target
