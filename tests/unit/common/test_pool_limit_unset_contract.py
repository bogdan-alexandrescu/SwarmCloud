"""Contract request 38: a pool with no `hard_limit` is UNKNOWN in the frozen contract (#374).

Before request 38 the frozen admission transaction read a pool document with
`d.get("hard_limit", 0)`, so a ceiling nobody set became a ceiling of 0 and was
refused as TENANT_LIMIT (or the pool's own reason) "at 0" -- which says an
operator set the pool to zero. Nobody did. The scheduler relabelled that
refusal after the frozen function had spoken (`Scheduler._name_unset_limits`)
and both codecs carried an `UnsetLimitPool` stand-in, because the contract had
no way to say it.

Accepted by the owner 2026-10-05: `BlockedReason.POOL_LIMIT_UNSET`, and
`SlotPool.hard_limit: int | None`, where None is "no limit set". Admission
still REFUSES through such a pool (never as 0, never as unlimited), and a
pause is still reported as the pause, because it refuses at any limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_common.admission import (
    AdmissionConfig,
    AdmissionDenied,
    acquire_lease_in_transaction,
    evaluate_capacity,
)
from swarm_common.models import SlotPool, Task, pool_names_for
from swarm_common.states import BlockedReason, TaskState

TENANT = "eng"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------


def test_blocked_reason_names_a_ceiling_nobody_set():
    assert BlockedReason.POOL_LIMIT_UNSET.value == "POOL_LIMIT_UNSET"


def test_the_unset_reason_is_in_the_needs_action_group():
    """Waiting never clears it: somebody has to set a limit."""
    from swarm_api.headroom import GROUP_NEEDS_ACTION, NEEDS_ACTION, group_for

    assert BlockedReason.POOL_LIMIT_UNSET in NEEDS_ACTION
    assert group_for("POOL_LIMIT_UNSET") == GROUP_NEEDS_ACTION


# --------------------------------------------------------------------------
# SlotPool
# --------------------------------------------------------------------------


def test_a_pool_with_no_hard_limit_has_no_effective_limit_and_no_capacity():
    pool = SlotPool(name=f"tenant:{TENANT}", hard_limit=None, adaptive_target=4, active=1)
    assert pool.effective_limit is None
    assert pool.available is None
    assert pool.has_capacity(1) is False
    # Not unlimited either: a huge request is refused just the same.
    assert pool.has_capacity(0) is False


def test_an_explicit_zero_is_still_zero():
    pool = SlotPool(name=f"tenant:{TENANT}", hard_limit=0)
    assert pool.effective_limit == 0
    assert pool.available == 0
    assert pool.has_capacity(1) is False


# --------------------------------------------------------------------------
# evaluate_capacity
# --------------------------------------------------------------------------


def test_admission_refuses_through_an_unset_pool_with_its_own_reason():
    name = f"tenant:{TENANT}"
    pools = {
        "global": SlotPool(name="global", hard_limit=10),
        name: SlotPool(name=name, hard_limit=None, active=2),
    }
    assert evaluate_capacity(pools, ["global", name], 1) == [
        {"pool": name, "reason": "POOL_LIMIT_UNSET", "limit": None, "active": 2}
    ]


def test_a_paused_unset_pool_is_reported_paused():
    name = f"tenant:{TENANT}"
    pools = {name: SlotPool(name=name, hard_limit=None, enabled=False)}
    [blocker] = evaluate_capacity(pools, [name], 1)
    assert blocker["reason"] == BlockedReason.MANUAL_PAUSE.value
    assert blocker["limit"] is None


def test_a_pool_set_to_zero_keeps_its_own_reason_at_zero():
    name = f"tenant:{TENANT}"
    pools = {name: SlotPool(name=name, hard_limit=0)}
    [blocker] = evaluate_capacity(pools, [name], 1)
    assert blocker["reason"] == BlockedReason.TENANT_LIMIT.value
    assert blocker["limit"] == 0


# --------------------------------------------------------------------------
# The transaction reads a missing or null hard_limit as unset
# --------------------------------------------------------------------------


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
    def __init__(self, db: _Db) -> None:
        self._db = db
        self.writes: list[str] = []

    def get(self, ref: _Ref) -> _Snap:
        return _Snap(self._db.docs.get(ref.path))

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.writes.append(ref.path)

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.writes.append(ref.path)


def _task() -> Task:
    return Task(
        id="task_u",
        tenant_id=TENANT,
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.READY,
        runner_profile="mock",
        resource_class="small",
        input={},
        submitted_by="alice@example.com",
    )


def _docs(tenant_pool: dict[str, Any]) -> dict[str, dict[str, Any]]:
    task = _task()
    required = pool_names_for(
        tenant_id=TENANT,
        provider=task.provider,
        resource_class=task.resource_class,
        runner_profile=task.runner_profile,
        backend="cloud_run",
    )
    docs: dict[str, dict[str, Any]] = {
        f"pools/{name}": {"hard_limit": 10, "active": 0, "enabled": True} for name in required
    }
    docs[f"pools/tenant:{TENANT}"] = tenant_pool
    docs["tasks/task_u"] = {"state": TaskState.READY.value, "current_generation": 0}
    return docs


@pytest.mark.parametrize(
    "tenant_pool",
    [
        pytest.param({"active": 0, "enabled": True}, id="missing"),
        pytest.param({"hard_limit": None, "active": 0, "enabled": True}, id="null"),
    ],
)
def test_the_transaction_refuses_an_unset_pool_as_unset_and_reserves_nothing(tenant_pool):
    db = _Db(_docs(tenant_pool))
    txn = _Txn(db)
    with pytest.raises(AdmissionDenied) as denied:
        acquire_lease_in_transaction(
            txn, db=db, task=_task(), units=1, backend="cloud_run", config=AdmissionConfig()
        )
    assert denied.value.reasons == [
        {"pool": f"tenant:{TENANT}", "reason": "POOL_LIMIT_UNSET", "limit": None, "active": 0}
    ]
    # All-or-nothing (invariant 2): no pool, lease or task was written.
    assert txn.writes == []


def test_the_transaction_still_reads_an_explicit_zero_as_zero():
    db = _Db(_docs({"hard_limit": 0, "active": 0, "enabled": True}))
    with pytest.raises(AdmissionDenied) as denied:
        acquire_lease_in_transaction(
            _Txn(db), db=db, task=_task(), units=1, backend="cloud_run", config=AdmissionConfig()
        )
    [blocker] = denied.value.reasons
    assert blocker["reason"] == "TENANT_LIMIT"
    assert blocker["limit"] == 0


def test_the_transaction_admits_through_a_configured_pool():
    """The control: the same fixture with a limit set admits, so the refusals above are the limit."""
    db = _Db(_docs({"hard_limit": 3, "active": 0, "enabled": True}))
    txn = _Txn(db)
    lease = acquire_lease_in_transaction(
        txn, db=db, task=_task(), units=1, backend="cloud_run", config=AdmissionConfig()
    )
    assert f"tenant:{TENANT}" in lease.pools
    assert "tasks/task_u" in txn.writes


# --------------------------------------------------------------------------
# The workarounds the contract could not avoid are gone
# --------------------------------------------------------------------------


def test_the_scheduler_no_longer_relabels_admissions_blockers():
    from scheduler import codec, loop, store

    assert not hasattr(loop.Scheduler, "_name_unset_limits")
    assert not hasattr(store, "_UnsetLimitReads")
    assert not hasattr(codec, "UnsetLimitPool")
    assert not hasattr(codec, "POOL_LIMIT_UNSET")


def test_both_codecs_read_an_unset_limit_into_the_contracts_none():
    from scheduler.codec import pool_from_dict as scheduler_pool_from_dict
    from swarm_api import codec as api_codec

    assert not hasattr(api_codec, "UnsetLimitPool")
    for read in (scheduler_pool_from_dict, api_codec.pool_from_dict):
        for doc in ({"active": 1}, {"hard_limit": None, "active": 1}):
            pool = read("tenant:eng", doc)
            assert type(pool) is SlotPool
            assert pool.hard_limit is None
            assert pool.effective_limit is None
        assert read("tenant:eng", {"hard_limit": 0}).hard_limit == 0
