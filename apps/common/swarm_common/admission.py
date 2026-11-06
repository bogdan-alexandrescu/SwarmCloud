"""Atomic admission: the only place capacity is ever reserved.

The guarantee this module exists to provide:

    If two schedulers race for the last free slot, exactly one wins, and the
    configured limit is never exceeded -- not even transiently.

That is achieved by doing ALL of the following inside a single Firestore
transaction, which Firestore aborts and retries if any document it read has
changed:

    1. re-read the task and confirm it is still READY
    2. read every applicable pool
    3. confirm every pool has capacity
    4. increment `active` on every pool
    5. write the lease
    6. move the task READY -> LEASED

All six succeed or none do. Partial reservation is the one failure mode that
would leak capacity permanently, because nothing would ever release the orphaned
half, so there is deliberately no code path that can produce it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, Sequence

from .models import (
    Lease,
    SlotPool,
    Task,
    held_pool_units,
    new_id,
    pool_names_for,
    pool_units_for,
    reserved_pool_units,
    utcnow,
)
from .states import BlockedReason, TaskState, assert_transition


class AdmissionDenied(Exception):
    """Raised when a task cannot be admitted right now. Not an error."""

    def __init__(self, reasons: list[dict[str, Any]]) -> None:
        super().__init__(f"admission denied: {reasons}")
        self.reasons = reasons


@dataclass(frozen=True)
class AdmissionConfig:
    # Settings.dispatch_timeout_seconds, which says why it is 480 (contract
    # request 37). The scheduler passes its setting; this default must match.
    dispatch_timeout_seconds: int = 480
    lease_timeout_seconds: int = 120
    heartbeat_interval_seconds: int = 30


def _snapshot(result: Any) -> Any:
    """One DocumentSnapshot from a Firestore transactional get.

    `Transaction.get()` in google-cloud-firestore returns a GENERATOR, because
    the same method accepts a Query as well as a DocumentReference. Treating the
    result as a snapshot raises

        AttributeError: 'generator' object has no attribute 'exists'

    at the first `.exists`, which is exactly what happened on the live scheduler:
    every Pub/Sub wake returned 503 and no task was ever admitted. In-memory test
    doubles return a snapshot directly, so no unit test could catch it -- hence
    this helper accepts both shapes.
    """
    if hasattr(result, "exists"):
        return result
    try:
        return next(iter(result))
    except StopIteration:  # pragma: no cover - a get always yields one result
        raise RuntimeError("transactional get returned no snapshot") from None


def _blocked_reason_for_pool(pool_name: str) -> BlockedReason:
    if pool_name == "global":
        return BlockedReason.GLOBAL_CONCURRENCY_LIMIT
    if pool_name.startswith("tenant:"):
        return BlockedReason.TENANT_LIMIT
    if pool_name.startswith("provider:"):
        return BlockedReason.PROVIDER_CONCURRENCY_LIMIT
    if pool_name.startswith("resource:"):
        return BlockedReason.RESOURCE_CLASS_LIMIT
    if pool_name.startswith("runner:"):
        return BlockedReason.RUNNER_LIMIT
    if pool_name.startswith("backend:"):
        return BlockedReason.BACKEND_LIMIT
    return BlockedReason.GLOBAL_CONCURRENCY_LIMIT


def evaluate_capacity(
    pools: dict[str, SlotPool],
    required: list[str],
    units: int,
    tasks: int = 1,
) -> list[dict[str, Any]]:
    """Pure capacity check. Returns the list of blockers; empty means admissible.

    `units` is the task's resource units; each pool is checked for what the
    task would take in it, `pool_units_for(name, units)` -- 1 in a provider
    pool (contract request 129, #2950). `tasks` asks the same question for
    that many such tasks at once, each pool needing `tasks` times its share:
    a headroom search asks it, and must not multiply `units` itself, which
    would charge a provider pool n x units instead of n.

    Kept free of any Firestore dependency so the concurrency invariant can be
    unit-tested exhaustively without an emulator.
    """
    blockers: list[dict[str, Any]] = []
    for name in required:
        pool = pools.get(name)
        if pool is None:
            # An unconfigured pool is unlimited by construction; the global pool
            # and the tenant pool are always created at provisioning time, so a
            # missing pool here is a narrow named one that was never capped.
            continue
        if not pool.enabled:
            blockers.append(
                {"pool": name, "reason": BlockedReason.MANUAL_PAUSE.value,
                 "limit": pool.effective_limit, "active": pool.active}
            )
            continue
        # Checked after the pause, which refuses at any limit and so stays the
        # reason. A ceiling nobody set refuses too, but as itself: never as a
        # limit of 0, never as unlimited (contract request 38, #374).
        if pool.hard_limit is None:
            blockers.append(
                {"pool": name, "reason": BlockedReason.POOL_LIMIT_UNSET.value,
                 "limit": None, "active": pool.active}
            )
            continue
        if not pool.has_capacity(tasks * pool_units_for(name, units)):
            blockers.append(
                {"pool": name, "reason": _blocked_reason_for_pool(name).value,
                 "limit": pool.effective_limit, "active": pool.active}
            )
    return blockers


class TransactionLike(Protocol):
    def get(self, ref: Any) -> Any: ...
    def set(self, ref: Any, data: dict[str, Any]) -> None: ...
    def update(self, ref: Any, data: dict[str, Any]) -> None: ...


def _read_pools(txn: TransactionLike, pool_refs: dict[str, Any]) -> dict[str, SlotPool]:
    """Read each pool once. A pool with no document is absent: unlimited, and
    never written."""
    pools: dict[str, SlotPool] = {}
    for name, ref in pool_refs.items():
        snap = _snapshot(txn.get(ref))
        if snap.exists:
            d = snap.to_dict() or {}
            # A missing or null `hard_limit` is None: no ceiling was set, and
            # `evaluate_capacity` refuses with POOL_LIMIT_UNSET (request 38).
            pools[name] = SlotPool(
                name=name,
                hard_limit=d.get("hard_limit"),
                adaptive_target=d.get("adaptive_target"),
                quota_derived_limit=d.get("quota_derived_limit"),
                active=d.get("active", 0),
                enabled=d.get("enabled", True),
            )
    return pools


def _readiness_denial(current: dict[str, Any] | None) -> AdmissionDenied | None:
    """Why a re-read task cannot be admitted, or None when it can.

    Re-read rather than trusting the caller's copy: the task may have been
    cancelled or admitted by another scheduler since it was queried.
    """
    if current is None:
        return AdmissionDenied([{"reason": "task_missing"}])
    if current.get("state") != TaskState.READY.value:
        return AdmissionDenied([{"reason": "not_ready", "state": current.get("state")}])
    if current.get("cancel_requested"):
        return AdmissionDenied([{"reason": "cancel_requested"}])
    return None


def _required_pools(task: Task, backend: str) -> list[str]:
    return pool_names_for(
        tenant_id=task.tenant_id,
        provider=task.provider,
        resource_class=task.resource_class,
        runner_profile=task.runner_profile,
        backend=backend,
    )


def _mint_lease(
    task: Task,
    current: dict[str, Any],
    *,
    required: list[str],
    units: int,
    taken: dict[str, int],
    config: AdmissionConfig,
    now: datetime,
) -> Lease:
    generation = int(current.get("current_generation", 0)) + 1
    attempt_id = new_id("att")
    return Lease(
        lease_id=new_id("lease"),
        task_id=task.id,
        attempt_id=attempt_id,
        tenant_id=task.tenant_id,
        generation=generation,
        pools=required,
        units=units,
        pool_units=taken,
        state=TaskState.LEASED,
        created_at=now,
        dispatch_deadline=now + timedelta(seconds=config.dispatch_timeout_seconds),
        expires_at=now + timedelta(seconds=config.lease_timeout_seconds),
    )


def _write_lease_and_task(
    txn: TransactionLike,
    *,
    db: Any,
    task_ref: Any,
    lease: Lease,
    current: dict[str, Any],
    now: datetime,
) -> None:
    """The lease document and the task's READY -> LEASED write."""
    txn.set(
        db.collection("leases").document(lease.lease_id),
        {
            "lease_id": lease.lease_id,
            "task_id": lease.task_id,
            "attempt_id": lease.attempt_id,
            "tenant_id": lease.tenant_id,
            "generation": lease.generation,
            "pools": lease.pools,
            "units": lease.units,
            "pool_units": lease.pool_units,
            "state": lease.state.value,
            "created_at": lease.created_at,
            "dispatch_deadline": lease.dispatch_deadline,
            "expires_at": lease.expires_at,
            "heartbeat_at": None,
            "released_at": None,
        },
    )

    assert_transition(TaskState.READY, TaskState.LEASED)
    txn.update(
        task_ref,
        {
            "state": TaskState.LEASED.value,
            "current_lease_id": lease.lease_id,
            "current_generation": lease.generation,
            "attempt_count": int(current.get("attempt_count", 0)) + 1,
            "blocked_by": [],
            "updated_at": now,
        },
    )


def acquire_lease_in_transaction(
    txn: TransactionLike,
    *,
    db: Any,
    task: Task,
    units: int,
    backend: str,
    config: AdmissionConfig,
    now: datetime | None = None,
) -> Lease:
    """Reserve every required pool and mint a lease, atomically.

    Raises AdmissionDenied if the task is no longer READY or any pool is full.
    The caller runs this inside `firestore.transactional`, so a concurrent
    winner causes Firestore to abort and re-run this function against fresh
    reads -- which is what makes the last-slot race resolve to exactly one
    winner rather than two.
    """
    now = now or utcnow()

    task_ref = db.collection("tasks").document(task.id)
    snapshot = _snapshot(txn.get(task_ref))
    current = (snapshot.to_dict() or {}) if snapshot.exists else None
    denial = _readiness_denial(current)
    if denial is not None:
        raise denial
    assert current is not None

    required = _required_pools(task, backend)
    taken = reserved_pool_units(required, units)
    pool_refs = {name: db.collection("pools").document(name) for name in required}
    pools = _read_pools(txn, pool_refs)

    blockers = evaluate_capacity(pools, required, units)
    if blockers:
        raise AdmissionDenied(blockers)

    lease = _mint_lease(
        task, current, required=required, units=units, taken=taken, config=config, now=now
    )

    # Reserve every pool. Because this runs inside the transaction, either all of
    # these increments land or none of them do. Each takes what `taken` says,
    # and the lease records it, so release gives back exactly this.
    for name in required:
        pool = pools.get(name)
        if pool is None:
            continue
        txn.update(pool_refs[name], {"active": pool.active + taken[name], "updated_at": now})

    _write_lease_and_task(txn, db=db, task_ref=task_ref, lease=lease, current=current, now=now)
    return lease


@dataclass(frozen=True)
class AdmissionRequest:
    """One task of a batched admission (`acquire_leases_in_transaction`): the
    units and backend `acquire_lease_in_transaction` takes as arguments."""

    task: Task
    units: int
    backend: str


def acquire_leases_in_transaction(
    txn: TransactionLike,
    *,
    db: Any,
    requests: Sequence[AdmissionRequest],
    config: AdmissionConfig,
    now: datetime | None = None,
    before_writes: Callable[[list[Lease | AdmissionDenied]], None] | None = None,
) -> list[Lease | AdmissionDenied]:
    """Admit a batch of tasks in ONE transaction. One outcome per request, in order.

    Contract request 139. `acquire_lease_in_transaction` reads, then writes,
    so it cannot run twice in one transaction (Firestore refuses a read after
    a write). This is the same admission for many tasks, with every pool
    document the batch needs READ ONCE and WRITTEN ONCE, with the sum of what
    the admitted tasks take: the per-task transaction wrote `pools/global`
    once per task, against Firestore's sustained ceiling of about one write
    per second per document.

    EACH TASK IS STILL ALL-OR-NOTHING (invariant 2). The requests are judged
    in the order given, each against the pools as the tasks before it in this
    batch left them, by `evaluate_capacity`. A task that fits takes its share
    in EVERY pool it needs; one that does not takes nothing, stays READY and
    gets its `AdmissionDenied` (returned, not raised); the rest of the batch
    still admits. Firestore commits the batch whole or not at all, so no task
    is ever half-reserved.

    The per-task rules are the single function's, through the same helpers:
    the task is re-read and must be READY with no cancel requested; the pools
    are `pool_names_for`; the shares are `reserved_pool_units`; a missing pool
    is unlimited and not written; the fencing generation is
    `current_generation + 1` (invariant 5); the lease document and the task's
    write are the same code. A request repeated in one batch is admitted
    once; its repeat is denied `not_ready`, as the next transaction would.

    `before_writes`, when given, is called with the outcomes after every read
    and decision and before the first write, so a caller can make its own
    reads in this transaction (a workflow's derived state) and write after
    this returns.
    """
    now = now or utcnow()
    if not requests:
        return []

    # EVERY READ BEFORE ANY WRITE: the tasks, then each pool once.
    task_refs = [db.collection("tasks").document(r.task.id) for r in requests]
    stored: list[dict[str, Any] | None] = []
    for ref in task_refs:
        snap = _snapshot(txn.get(ref))
        stored.append((snap.to_dict() or {}) if snap.exists else None)
    required = [_required_pools(r.task, r.backend) for r in requests]
    pool_refs = {
        name: db.collection("pools").document(name) for names in required for name in names
    }
    pools = _read_pools(txn, pool_refs)
    stored_active = {name: pool.active for name, pool in pools.items()}

    outcomes: list[Lease | AdmissionDenied] = []
    admitted: list[tuple[int, Lease]] = []
    seen: set[str] = set()
    for i, request in enumerate(requests):
        task, current = request.task, stored[i]
        if current is not None and task.id in seen:
            outcomes.append(
                AdmissionDenied([{"reason": "not_ready", "state": TaskState.LEASED.value}])
            )
            continue
        denial = _readiness_denial(current)
        if denial is not None:
            outcomes.append(denial)
            continue
        assert current is not None
        # Judged against the pools AS THIS BATCH HAS LEFT THEM so far.
        blockers = evaluate_capacity(pools, required[i], request.units)
        if blockers:
            outcomes.append(AdmissionDenied(blockers))
            continue
        taken = reserved_pool_units(required[i], request.units)
        for name in required[i]:
            pool = pools.get(name)
            if pool is not None:
                pool.active += taken[name]
        seen.add(task.id)
        lease = _mint_lease(
            task, current, required=required[i], units=request.units, taken=taken,
            config=config, now=now,
        )
        outcomes.append(lease)
        admitted.append((i, lease))

    if before_writes is not None:
        before_writes(list(outcomes))

    # The writes. Each pool once, with the batch's sum.
    for name, pool in pools.items():
        if pool.active != stored_active[name]:
            txn.update(pool_refs[name], {"active": pool.active, "updated_at": now})
    for i, lease in admitted:
        _write_lease_and_task(
            txn, db=db, task_ref=task_refs[i], lease=lease, current=stored[i] or {}, now=now
        )
    return outcomes


def release_lease_in_transaction(
    txn: TransactionLike,
    *,
    db: Any,
    lease_id: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Return capacity to every pool the lease held. Idempotent.

    Idempotency matters more than it looks: the worker, the reconciler and the
    cancellation path can all race to release the same lease, and double-release
    would decrement pools below the true active count, silently inflating
    capacity until the next reconciler sweep.
    """
    now = now or utcnow()
    lease_ref = db.collection("leases").document(lease_id)
    snap = _snapshot(txn.get(lease_ref))
    if not snap.exists:
        return False
    d = snap.to_dict()
    if d.get("released_at") is not None:
        return False                      # already released; do not double-decrement

    # What the lease took in each pool: its record, or -- for a lease reserved
    # before contract request 129, which has none -- `units` in every pool.
    held = held_pool_units(list(d.get("pools", [])), int(d.get("units", 1)), d.get("pool_units"))

    # EVERY READ BEFORE ANY WRITE. Firestore forbids a read after a write inside
    # a transaction and raises ReadAfterWriteError. Reading each pool and
    # updating it in the same loop meant the second pool's read came after the
    # first pool's write, so releasing a lease that held more than one pool
    # always failed -- and a lease that cannot be released holds its slots for
    # ever. `acquire_lease_in_transaction` already reads all pools first, which
    # is why admission worked and only release was broken.
    pool_refs = {name: db.collection("pools").document(name) for name in d.get("pools", [])}
    actives: dict[str, int] = {}
    for name, ref in pool_refs.items():
        psnap = _snapshot(txn.get(ref))
        if psnap.exists:
            actives[name] = int((psnap.to_dict() or {}).get("active", 0))

    for name, active in actives.items():
        txn.update(pool_refs[name], {"active": max(0, active - held[name]), "updated_at": now})

    txn.update(lease_ref, {"released_at": now, "release_reason": reason})
    return True
