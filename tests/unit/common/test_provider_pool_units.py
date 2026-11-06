"""A lease takes 1 unit in each `provider:*` pool, whatever its size (#2950).

A provider pool bounds concurrent LLM sessions against that provider's
accounts. A claude-code-large agent is one Claude session, like a standard one,
but admission reserved the resource class's units (large: 4) in every pool
`pool_names_for` lists, the provider pools included: ten large agents held 40
of Engineering's 50 Anthropic slots for ten sessions (measured 2026-11-01).

Accepted by the owner 2026-11-01 (contract request in
docs/contract-change-requests.md): a lease keeps its resource units in the
compute pools (global, tenant, resource, runner, backend) and takes 1 in each
provider pool. One function, `pool_units_for`, decides it; the lease records
what it took per pool (`pool_units`), and release returns exactly that. A lease
reserved before the change carries no `pool_units` and took its `units` in every
pool, so release returns `units` from every pool for it.

No emulator, no credentials: a dict stands in for Firestore.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from swarm_common.admission import (
    AdmissionConfig,
    acquire_lease_in_transaction,
    evaluate_capacity,
    release_lease_in_transaction,
)
from swarm_common.models import SlotPool, Task, pool_names_for
from swarm_common.states import TaskState

TENANT = "eng"
BACKEND = "GKE_AUTOPILOT"
NOW = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
PROVIDER_POOLS = ("provider:anthropic", f"provider:anthropic:tenant:{TENANT}")
COMPUTE_POOLS = (
    "global",
    f"tenant:{TENANT}",
    "resource:large",
    "runner:claude-code-large",
    f"backend:{BACKEND}",
)


class _Snap:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _Ref:
    def __init__(self, path: str) -> None:
        self.path = path


class _Collection:
    def __init__(self, name: str) -> None:
        self._name = name

    def document(self, doc_id: str) -> _Ref:
        return _Ref(f"{self._name}/{doc_id}")


class _Db:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs

    def collection(self, name: str) -> _Collection:
        return _Collection(name)


class _Txn:
    """Applies writes, and refuses a read after a write as Firestore does."""

    def __init__(self, db: _Db) -> None:
        self._db = db
        self._written = False

    def get(self, ref: _Ref) -> _Snap:
        assert not self._written, "a read after a write: Firestore raises ReadAfterWriteError"
        return _Snap(self._db.docs.get(ref.path))

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self._written = True
        self._db.docs[ref.path] = dict(data)

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self._written = True
        self._db.docs.setdefault(ref.path, {}).update(data)


def _task(task_id: str = "task_large") -> Task:
    return Task(
        id=task_id,
        tenant_id=TENANT,
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.READY,
        runner_profile="claude-code-large",
        resource_class="large",
        provider="anthropic",
        input={},
        submitted_by="alice@example.com",
    )


def _db(limit: int = 50, active: int = 0) -> _Db:
    task = _task()
    required = pool_names_for(
        tenant_id=TENANT,
        provider=task.provider,
        resource_class=task.resource_class,
        runner_profile=task.runner_profile,
        backend=BACKEND,
    )
    assert set(required) == set(PROVIDER_POOLS) | set(COMPUTE_POOLS)
    docs: dict[str, dict[str, Any]] = {
        f"pools/{name}": {"hard_limit": limit, "active": active, "enabled": True}
        for name in required
    }
    docs[f"tasks/{task.id}"] = {"state": TaskState.READY.value, "current_generation": 0}
    return _Db(docs)


def _active(db: _Db, name: str) -> int:
    return db.docs[f"pools/{name}"]["active"]


def _reserve(db: _Db, task: Task | None = None):
    return acquire_lease_in_transaction(
        _Txn(db),
        db=db,
        task=task or _task(),
        units=4,
        backend=BACKEND,
        config=AdmissionConfig(),
        now=NOW,
    )


def test_a_large_lease_takes_one_unit_in_each_provider_pool_and_four_in_each_compute_pool():
    db = _db()
    _reserve(db)

    assert {name: _active(db, name) for name in PROVIDER_POOLS} == {
        name: 1 for name in PROVIDER_POOLS
    }, "one Claude session is one provider slot, whatever the runner's size"
    assert {name: _active(db, name) for name in COMPUTE_POOLS} == {
        name: 4 for name in COMPUTE_POOLS
    }, "the compute pools still count the resource class's units"


def test_ten_large_agents_hold_ten_provider_slots_not_forty():
    """The measured case: ten large agents in one workspace."""
    db = _db(limit=50)
    for i in range(10):
        task_id = f"task_large_{i}"
        db.docs[f"tasks/{task_id}"] = {"state": TaskState.READY.value, "current_generation": 0}
        _reserve(db, _task(task_id))

    assert _active(db, f"provider:anthropic:tenant:{TENANT}") == 10
    assert _active(db, "resource:large") == 40


def test_the_lease_records_what_it_took_in_each_pool():
    db = _db()
    lease = _reserve(db)

    expected = {**{name: 1 for name in PROVIDER_POOLS}, **{name: 4 for name in COMPUTE_POOLS}}
    assert lease.pool_units == expected
    stored = db.docs[f"leases/{lease.lease_id}"]
    assert stored["pool_units"] == expected
    assert stored["units"] == 4, "`units` stays the resource class's units"


def test_release_returns_exactly_what_reserve_took():
    db = _db()
    lease = _reserve(db)

    assert release_lease_in_transaction(_Txn(db), db=db, lease_id=lease.lease_id, reason="done", now=NOW)
    for name in PROVIDER_POOLS + COMPUTE_POOLS:
        assert _active(db, name) == 0, name


def test_a_lease_reserved_under_the_old_rule_releases_every_pool_to_zero():
    """Reserved before the deploy: 4 in every pool, and no `pool_units` on the lease."""
    db = _db(active=4)
    pools = list(PROVIDER_POOLS + COMPUTE_POOLS)
    db.docs["leases/lease_old"] = {
        "lease_id": "lease_old",
        "task_id": "task_large",
        "tenant_id": TENANT,
        "generation": 1,
        "pools": pools,
        "units": 4,
        "state": TaskState.RUNNING.value,
        "released_at": None,
    }

    assert release_lease_in_transaction(_Txn(db), db=db, lease_id="lease_old", reason="done", now=NOW)
    for name in pools:
        assert _active(db, name) == 0, name


def test_old_and_new_leases_released_together_leave_every_pool_at_zero():
    """A pool holds both kinds across the deploy; each returns its own."""
    db = _db(active=4)
    pools = list(PROVIDER_POOLS + COMPUTE_POOLS)
    db.docs["leases/lease_old"] = {
        "lease_id": "lease_old", "pools": pools, "units": 4, "released_at": None,
    }
    new = _reserve(db)
    assert _active(db, f"provider:anthropic:tenant:{TENANT}") == 5
    assert _active(db, "resource:large") == 8

    for lease_id in ("lease_old", new.lease_id):
        assert release_lease_in_transaction(_Txn(db), db=db, lease_id=lease_id, reason="done", now=NOW)
    for name in pools:
        assert _active(db, name) == 0, name


def test_release_is_still_idempotent():
    db = _db()
    lease = _reserve(db)
    assert release_lease_in_transaction(_Txn(db), db=db, lease_id=lease.lease_id, reason="a", now=NOW)
    assert not release_lease_in_transaction(_Txn(db), db=db, lease_id=lease.lease_id, reason="b", now=NOW)
    assert _active(db, "provider:anthropic") == 0
    assert _active(db, "resource:large") == 0


def test_a_provider_pool_with_one_slot_left_admits_a_large_agent():
    db = _db(limit=50)
    for name in PROVIDER_POOLS:
        db.docs[f"pools/{name}"]["active"] = 49
    lease = _reserve(db)
    assert lease.pool_units[f"provider:anthropic:tenant:{TENANT}"] == 1
    assert _active(db, f"provider:anthropic:tenant:{TENANT}") == 50


def test_evaluate_capacity_charges_a_provider_pool_one_unit():
    required = list(PROVIDER_POOLS + COMPUTE_POOLS)
    pools = {name: SlotPool(name=name, hard_limit=50, active=0) for name in required}
    pools[f"provider:anthropic:tenant:{TENANT}"] = SlotPool(
        name=f"provider:anthropic:tenant:{TENANT}", hard_limit=50, active=49
    )
    assert evaluate_capacity(pools, required, 4) == []

    pools["resource:large"] = SlotPool(name="resource:large", hard_limit=40, active=37)
    blockers = evaluate_capacity(pools, required, 4)
    assert [b["pool"] for b in blockers] == ["resource:large"]


def test_one_function_decides_every_pools_units():
    from swarm_common.models import held_pool_units, pool_units_for, reserved_pool_units

    assert pool_units_for("provider:anthropic", 4) == 1
    assert pool_units_for(f"provider:anthropic:tenant:{TENANT}", 2) == 1
    for name in COMPUTE_POOLS:
        assert pool_units_for(name, 4) == 4
    assert pool_units_for("resource:standard", 1) == 1

    pools = list(COMPUTE_POOLS + PROVIDER_POOLS)
    new_rule = {**{name: 4 for name in COMPUTE_POOLS}, **{name: 1 for name in PROVIDER_POOLS}}
    assert reserved_pool_units(pools, 4) == new_rule
    # A lease that recorded what it took holds exactly that.
    assert held_pool_units(pools, 4, new_rule) == new_rule
    # A lease without a record took its units in every pool (the old rule).
    assert held_pool_units(pools, 4, None) == {name: 4 for name in pools}


def test_a_lease_says_what_it_holds_in_each_pool():
    from swarm_common.models import Lease

    pools = list(COMPUTE_POOLS + PROVIDER_POOLS)
    common = dict(
        lease_id="l", task_id="t", attempt_id="a", tenant_id=TENANT, generation=1,
        pools=pools, units=4, state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW, expires_at=NOW,
    )
    old = Lease(**common)
    assert old.pool_units is None
    assert old.held_units() == {name: 4 for name in pools}
    new = Lease(**common, pool_units={name: (1 if name in PROVIDER_POOLS else 4) for name in pools})
    assert new.held_units()[f"provider:anthropic:tenant:{TENANT}"] == 1
    assert new.held_units()["resource:large"] == 4
