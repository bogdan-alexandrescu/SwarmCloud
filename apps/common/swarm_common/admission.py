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
from typing import Any, Protocol

from .models import Lease, SlotPool, Task, new_id, pool_names_for, utcnow
from .states import BlockedReason, TaskState, assert_transition


class AdmissionDenied(Exception):
    """Raised when a task cannot be admitted right now. Not an error."""

    def __init__(self, reasons: list[dict[str, Any]]) -> None:
        super().__init__(f"admission denied: {reasons}")
        self.reasons = reasons


@dataclass(frozen=True)
class AdmissionConfig:
    dispatch_timeout_seconds: int = 300
    lease_timeout_seconds: int = 120
    heartbeat_interval_seconds: int = 30


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
) -> list[dict[str, Any]]:
    """Pure capacity check. Returns the list of blockers; empty means admissible.

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
        if not pool.has_capacity(units):
            blockers.append(
                {"pool": name, "reason": _blocked_reason_for_pool(name).value,
                 "limit": pool.effective_limit, "active": pool.active}
            )
    return blockers


class TransactionLike(Protocol):
    def get(self, ref: Any) -> Any: ...
    def set(self, ref: Any, data: dict[str, Any]) -> None: ...
    def update(self, ref: Any, data: dict[str, Any]) -> None: ...


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
    snapshot = txn.get(task_ref)
    if not snapshot.exists:
        raise AdmissionDenied([{"reason": "task_missing"}])

    current = snapshot.to_dict()

    # Re-read rather than trusting the caller's copy: the task may have been
    # cancelled or admitted by another scheduler since it was queried.
    if current.get("state") != TaskState.READY.value:
        raise AdmissionDenied([{"reason": "not_ready", "state": current.get("state")}])
    if current.get("cancel_requested"):
        raise AdmissionDenied([{"reason": "cancel_requested"}])

    required = pool_names_for(
        tenant_id=task.tenant_id,
        provider=task.provider,
        resource_class=task.resource_class,
        runner_profile=task.runner_profile,
        backend=backend,
    )

    pool_refs = {name: db.collection("pools").document(name) for name in required}
    pools: dict[str, SlotPool] = {}
    for name, ref in pool_refs.items():
        snap = txn.get(ref)
        if snap.exists:
            d = snap.to_dict()
            pools[name] = SlotPool(
                name=name,
                hard_limit=d.get("hard_limit", 0),
                adaptive_target=d.get("adaptive_target"),
                quota_derived_limit=d.get("quota_derived_limit"),
                active=d.get("active", 0),
                enabled=d.get("enabled", True),
            )

    blockers = evaluate_capacity(pools, required, units)
    if blockers:
        raise AdmissionDenied(blockers)

    generation = int(current.get("current_generation", 0)) + 1
    attempt_id = new_id("att")
    lease = Lease(
        lease_id=new_id("lease"),
        task_id=task.id,
        attempt_id=attempt_id,
        tenant_id=task.tenant_id,
        generation=generation,
        pools=required,
        units=units,
        state=TaskState.LEASED,
        created_at=now,
        dispatch_deadline=now + timedelta(seconds=config.dispatch_timeout_seconds),
        expires_at=now + timedelta(seconds=config.lease_timeout_seconds),
    )

    # Reserve every pool. Because this runs inside the transaction, either all of
    # these increments land or none of them do.
    for name in required:
        pool = pools.get(name)
        if pool is None:
            continue
        txn.update(pool_refs[name], {"active": pool.active + units, "updated_at": now})

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
            "current_generation": generation,
            "attempt_count": int(current.get("attempt_count", 0)) + 1,
            "blocked_by": [],
            "updated_at": now,
        },
    )
    return lease


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
    snap = txn.get(lease_ref)
    if not snap.exists:
        return False
    d = snap.to_dict()
    if d.get("released_at") is not None:
        return False                      # already released; do not double-decrement

    units = int(d.get("units", 1))
    for name in d.get("pools", []):
        ref = db.collection("pools").document(name)
        psnap = txn.get(ref)
        if not psnap.exists:
            continue
        active = int(psnap.to_dict().get("active", 0))
        txn.update(ref, {"active": max(0, active - units), "updated_at": now})

    txn.update(lease_ref, {"released_at": now, "release_reason": reason})
    return True
