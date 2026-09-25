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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

from swarm_common.admission import _snapshot, release_lease_in_transaction
from swarm_common.models import Attempt, ProviderState, TaskEvent, new_id, utcnow
from swarm_common.states import (
    CONCURRENCY_STATES,
    TERMINAL_STATES,
    EventType,
    ParkReason,
    TaskState,
    assert_transition,
)

from .errors import ControlPlaneError, FencedError, FencedWriteRefused, TenantMismatchError

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
    """Runs a function inside one Firestore transaction, retrying on contention."""

    def run(self, fn: Callable[[Any], Any]) -> Any: ...


class FirestoreTransactionRunner:
    def __init__(self, db: Any) -> None:
        self._db = db

    def run(self, fn: Callable[[Any], Any]) -> Any:
        from google.cloud import firestore  # lazy: unit tests never import grpc

        transaction = self._db.transaction()

        @firestore.transactional
        def _inner(txn: Any) -> Any:
            return fn(txn)

        return _inner(transaction)


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
        # `retry` and `timeout` for the control-plane calls a worker makes
        # before its runner exists: the generation check's reads, the state
        # read `advance_to_running` walks from, and the attempt's first write.
        # The lifecycle passes the same options to its own startup reads (the
        # checkpoint restore's task read and the quota preflight's poll)
        # through `startup_call_options`. The entrypoint supplies a bounded
        # budget (`__main__.firestore_startup_call_options`). Empty means the
        # library's defaults, which for a document read is up to 300 s of
        # silent retries. The mid-run control poll keeps its defaults:
        # changing how long a running agent tolerates a Firestore outage is a
        # separate decision.
        self._startup_call: dict[str, Any] = dict(startup_call_options or {})

    @property
    def startup_call_options(self) -> dict[str, Any]:
        return dict(self._startup_call)

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

    def fetch_task(self, *, call_options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        snap = self._task_ref().get(**dict(call_options or {}))
        if not snap.exists:
            raise FencedError(self.generation, -1, "task document no longer exists")
        return self._assert_tenant(
            snap.to_dict() or {}, kind="task", document_id=self.task_id
        )

    def fetch_lease(
        self, *, call_options: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        snap = self._lease_ref().get(**dict(call_options or {}))
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

        All three reads carry the startup budget (`startup_call_options`), so
        a Firestore that cannot be reached raises within it rather than after
        300 s of silent retries. The caller logs what it raised.
        """
        task = self.fetch_task(call_options=self._startup_call)
        attempt_snap = self._attempt_ref().get(**self._startup_call)
        if attempt_snap.exists:
            self._assert_tenant(
                attempt_snap.to_dict() or {}, kind="attempt", document_id=self.attempt_id
            )
        fence = _task_fence(task, generation=self.generation)
        if fence is not None:
            raise FencedError(self.generation, *fence)

        lease = self.fetch_lease(call_options=self._startup_call)
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
        task_snap = _snapshot(txn.get(self._task_ref()))
        if not task_snap.exists:
            raise FencedWriteRefused(
                self.generation, -1, "task document no longer exists", write=write
            )
        task = self._assert_tenant(
            task_snap.to_dict() or {}, kind="task", document_id=self.task_id
        )
        fence = _task_fence(task, generation=self.generation)
        if fence is None:
            lease_snap = _snapshot(txn.get(self._lease_ref()))
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
        self._txn.run(lambda txn: self._fenced_task(txn, write=write))

    # -- polling -----------------------------------------------------------
    def poll(
        self,
        provider: str | None = None,
        *,
        call_options: Mapping[str, Any] | None = None,
    ) -> ControlSignals:
        """One read of task + lease (+ quota), used by the supervision loop.

        `call_options` is for the one poll made before the runner exists, the
        quota preflight, which passes the startup budget. The supervision loop
        passes nothing and keeps the library's defaults.
        """
        options = dict(call_options or {})
        try:
            task = self.fetch_task(call_options=options)
        except FencedError:
            return ControlSignals(
                state=TaskState.CANCELLED,
                generation=-1,
                cancel_requested=False,
                lease_released=True,
                observed_at=utcnow(),
            )
        lease = self.fetch_lease(call_options=options) or {}
        provider_state: ProviderState | None = None
        provider_paused = False
        retry_after: int | None = None
        reset_at: datetime | None = None

        if provider:
            qsnap = self._quota_ref(provider).get(**options)
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
        ref.set(document)
        self._log.info("event", event_type=event_type.value, detail=document["detail"])

    # -- state transitions -------------------------------------------------
    def _current_state(self) -> TaskState:
        # Read only by `advance_to_running`, which runs before the runner
        # exists, so the read carries the startup budget.
        return _as_state(self.fetch_task(call_options=self._startup_call).get("state"))

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

        self._txn.run(_apply)
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
            # The attempt's first write, and the first write of all: under the
            # startup budget like the reads before it.
            **self._startup_call,
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

        self._txn.run(_point)
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

    def record_attempt_end(
        self,
        *,
        exit_code: int | None,
        error: str | None,
        call_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._attempt_ref().set(
            {
                "completed_at": utcnow(),
                "exit_code": exit_code,
                "error": error,
                "tenant_id": self.tenant_id,
            },
            merge=True,
            **dict(call_options or {}),
        )

    # -- heartbeat ---------------------------------------------------------
    def heartbeat(self) -> None:
        now = utcnow()
        self._lease_ref().update(
            {
                "heartbeat_at": now,
                "expires_at": now + timedelta(seconds=self._heartbeat_extension),
            }
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
        """
        now = utcnow()
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
        if state in (ProviderState.EXHAUSTED, ProviderState.THROTTLED):
            payload["last_429_at"] = now
        ref = self._quota_ref(provider)
        snap = ref.get()
        if snap.exists:
            existing = self._assert_tenant(
                snap.to_dict() or {},
                kind="quota",
                document_id=f"{provider}:{self.tenant_id}",
            )
            payload["rate_limit_count"] = int(existing.get("rate_limit_count", 0)) + (
                1 if state in (ProviderState.EXHAUSTED, ProviderState.THROTTLED) else 0
            )
            ref.update(payload)
        else:
            payload.setdefault("configured_hard_max", 50)
            payload.setdefault("rate_limit_count", 1)
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

        released = bool(self._txn.run(_release))
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
