"""#362: a READY task held behind a full pool says which pool refuses it.

`swarm_api.waiting.waiting_for` is a READ of the task's own required pools,
answered by `headroom.analyse_profile` -- `evaluate_capacity` underneath -- so
nothing here restates the admission rule. These tests hold:

  * every pool state and the lead order (paused > unknown > zero/below_units >
    full; within a tier, `pool_names_for` order);
  * that "unknown" is never a 0 and never "full";
  * parity with `evaluate_capacity` over generated pools;
  * the routes: served for READY only, one `get_all` per page, no writes on a
    GET, and only the caller's own tenant's pools read.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_common.admission import evaluate_capacity
from swarm_common.models import SlotPool, Task, pool_names_for
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import TaskState

from swarm_api.waiting import required_pools, waiting_for

from .conftest import auth_header, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def task(
    *,
    tenant_id: str = "eng",
    runner_profile: str = "mock",
    resource_class: str = "standard",
    provider: str | None = None,
    state: TaskState = TaskState.READY,
) -> Task:
    return Task(
        id="task_w",
        tenant_id=tenant_id,
        created_at=NOW,
        updated_at=NOW,
        state=state,
        runner_profile=runner_profile,
        resource_class=resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=provider,
    )


def doc(limit: int | None, active: int = 0, *, enabled: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"active": active, "enabled": enabled}
    if limit is not None:
        out["hard_limit"] = limit
    return out


def required_for(t: Task) -> list[str]:
    profile = RUNNER_PROFILES[t.runner_profile]
    return pool_names_for(
        tenant_id=t.tenant_id,
        provider=t.provider,
        resource_class=t.resource_class,
        runner_profile=t.runner_profile,
        backend=resolve_backend(profile).value,
    )


# --------------------------------------------------------------------------
# What is required, built from the frozen function
# --------------------------------------------------------------------------

def test_required_pools_are_pool_names_for_and_units_are_the_tasks_class():
    t = task(runner_profile="browser", resource_class="browser", provider="anthropic")
    required, units = required_pools(t)
    assert required == required_for(t)
    assert units == RESOURCE_CLASSES["browser"].units == 2


def test_units_follow_the_tasks_resource_class_as_admission_does():
    """The scheduler weighs `RESOURCE_CLASSES[task.resource_class]`, not the profile's."""
    t = task(runner_profile="mock", resource_class="large")
    required, units = required_pools(t)
    assert units == RESOURCE_CLASSES["large"].units
    assert "resource:large" in required


# --------------------------------------------------------------------------
# Every state
# --------------------------------------------------------------------------

def test_full_tenant_pool_is_the_lead_with_its_numbers():
    out = waiting_for(
        task(), {"tenant:eng": doc(20, 20), "global": doc(100, 3)}, listing_complete=True, as_of=NOW
    )
    assert out["admissible_now"] is False
    assert out["holds_capacity"] is False
    assert out["complete"] is True
    assert out["as_of"] == NOW
    assert out["lead"] == {
        "pool": "tenant:eng", "state": "full", "active": 20, "limit": 20, "units": 1,
        "reason": "TENANT_LIMIT",
    }
    states = {p["pool"]: p["state"] for p in out["pools"]}
    assert states == {"tenant:eng": "full", "global": "open"}


def test_paused_is_checked_before_anything_else_on_the_same_pool():
    """A paused pool that is also full, or has no limit at all, is paused."""
    out = waiting_for(
        task(), {"tenant:eng": doc(20, 20, enabled=False)}, listing_complete=True, as_of=NOW
    )
    assert out["lead"]["state"] == "paused"
    assert out["lead"]["reason"] == "MANUAL_PAUSE"

    out = waiting_for(
        task(), {"tenant:eng": doc(None, 0, enabled=False)}, listing_complete=True, as_of=NOW
    )
    assert out["lead"]["state"] == "paused"
    assert out["lead"]["limit"] is None


def test_zero_and_below_units():
    t = task(resource_class="large")  # 4 units
    out = waiting_for(
        t,
        {"tenant:eng": doc(0), "resource:large": doc(3)},
        listing_complete=True,
        as_of=NOW,
    )
    states = {p["pool"]: p["state"] for p in out["pools"]}
    assert states["tenant:eng"] == "zero"
    assert states["resource:large"] == "below_units"
    assert out["lead"]["pool"] == "tenant:eng"  # same tier, pool_names_for order
    assert out["lead"]["units"] == 4


def test_a_pool_with_no_hard_limit_key_is_unknown_never_zero_or_full():
    out = waiting_for(task(), {"tenant:eng": doc(None, 5)}, listing_complete=True, as_of=NOW)
    lead = out["lead"]
    assert lead["state"] == "unknown"
    assert lead["limit"] is None
    assert lead["state"] not in ("zero", "full")
    assert out["complete"] is False
    assert out["admissible_now"] is None


def test_an_unread_pool_is_unknown_and_a_missing_doc_is_uncapped():
    t = task()
    # listing incomplete, tenant:eng not in the map -> unread -> unknown
    out = waiting_for(t, {"global": doc(100, 0)}, listing_complete=False, as_of=NOW)
    unknown = [p["pool"] for p in out["pools"] if p["state"] == "unknown"]
    assert "tenant:eng" in unknown
    assert all(p["limit"] is None for p in out["pools"] if p["state"] == "unknown")
    assert out["complete"] is False

    # listing complete: every absent pool has no doc, is uncapped and never listed
    out = waiting_for(t, {"global": doc(100, 0), "tenant:eng": None}, listing_complete=True, as_of=NOW)
    assert [p["pool"] for p in out["pools"]] == ["global"]
    assert out["lead"] is None
    assert out["admissible_now"] is True
    assert out["complete"] is True


def test_lead_order_across_every_tier():
    t = task(resource_class="large")
    required = required_for(t)
    # global full, tenant zero, resource unknown, runner paused, backend below units
    raw = {
        "global": doc(10, 10),
        "tenant:eng": doc(0),
        "resource:large": doc(None),
        f"runner:{t.runner_profile}": doc(10, 0, enabled=False),
        required[4]: doc(2),
    }
    out = waiting_for(t, raw, listing_complete=True, as_of=NOW)
    assert [(p["pool"], p["state"]) for p in out["pools"]] == [
        (f"runner:{t.runner_profile}", "paused"),
        ("resource:large", "unknown"),
        ("tenant:eng", "zero"),
        (required[4], "below_units"),
        ("global", "full"),
    ]
    assert out["lead"]["pool"] == f"runner:{t.runner_profile}"


def test_a_measured_refusal_beside_an_unknown_pool_is_still_refused():
    out = waiting_for(
        task(), {"tenant:eng": doc(20, 20), "global": doc(None)}, listing_complete=True, as_of=NOW
    )
    assert out["admissible_now"] is False
    assert out["complete"] is False
    assert out["lead"]["pool"] == "global"  # unknown outranks full
    assert {p["pool"]: p["state"] for p in out["pools"]}["tenant:eng"] == "full"


def test_not_ready_gets_null():
    for state in TaskState:
        if state is TaskState.READY:
            continue
        assert waiting_for(task(state=state), {"tenant:eng": doc(1, 1)}, listing_complete=True) is None


# --------------------------------------------------------------------------
# Parity with evaluate_capacity
# --------------------------------------------------------------------------

def test_parity_with_evaluate_capacity_on_generated_pools():
    rng = random.Random(362)
    profiles = [
        ("mock", "standard", None),
        ("claude-code", "standard", "anthropic"),
        ("browser", "browser", "anthropic"),
        ("mock", "large", None),
    ]
    checked = 0
    for _ in range(600):
        name, rc, provider = rng.choice(profiles)
        t = task(runner_profile=name, resource_class=rc, provider=provider)
        required, units = required_pools(t)
        raw: dict[str, Any] = {}
        pools: dict[str, SlotPool] = {}
        for pool_name in required:
            if rng.random() < 0.25:
                raw[pool_name] = None
                continue
            limit = rng.randint(0, 8)
            active = rng.randint(0, 9)
            enabled = rng.random() > 0.15
            adaptive = rng.choice([None, rng.randint(0, 8)])
            quota = rng.choice([None, rng.randint(0, 8)])
            raw[pool_name] = {
                "hard_limit": limit, "active": active, "enabled": enabled,
                "adaptive_target": adaptive, "quota_derived_limit": quota,
            }
            pools[pool_name] = SlotPool(
                name=pool_name, hard_limit=limit, active=active, enabled=enabled,
                adaptive_target=adaptive, quota_derived_limit=quota,
            )
        out = waiting_for(t, raw, listing_complete=True, as_of=NOW)
        blockers = evaluate_capacity(pools, required, units)
        refused = [p["pool"] for p in out["pools"] if p["state"] != "open"]
        assert sorted(refused) == sorted(b["pool"] for b in blockers)
        assert out["admissible_now"] is (not blockers)
        by_pool = {p["pool"]: p for p in out["pools"]}
        for b in blockers:
            assert by_pool[b["pool"]]["reason"] == b["reason"]
            assert by_pool[b["pool"]]["limit"] == b["limit"]
            assert by_pool[b["pool"]]["active"] == b["active"]
        checked += 1
    assert checked == 600


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

class CountingGetAll:
    """Wraps the fake's `get_all` and records every batch of pool refs asked for."""

    def __init__(self, db) -> None:
        self.db = db
        self.pool_batches: list[list[str]] = []
        self._inner = db.get_all

    def __call__(self, references, *args, **kwargs):
        refs = list(references)
        paths = [r.path for r in refs]
        if any(p.startswith("pools/") for p in paths):
            self.pool_batches.append(paths)
        return self._inner(refs, *args, **kwargs)


@pytest.fixture
def counting(db, monkeypatch):
    counter = CountingGetAll(db)
    monkeypatch.setattr(db, "get_all", counter)
    return counter


def seed_eng(db):
    seed_tenant(db, "eng", max_active=20)
    seed_pool(db, "tenant:eng", hard_limit=20, active=20)
    seed_pool(db, "global", hard_limit=100, active=20)


def test_get_task_serves_waiting_for_on_ready(client, db, counting):
    seed_eng(db)
    seed_task(db, task_id="task_ready", tenant_id="eng")
    res = client.get("/v1/tasks/task_ready", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    waiting = res.json()["task"]["waiting_for"]
    assert waiting["lead"]["pool"] == "tenant:eng"
    assert waiting["lead"]["state"] == "full"
    assert waiting["lead"]["active"] == 20 and waiting["lead"]["limit"] == 20
    assert waiting["holds_capacity"] is False
    assert len(counting.pool_batches) == 1


def test_get_task_serves_null_for_every_other_state(client, db, counting):
    seed_eng(db)
    for state in ("QUEUED", "PARKED", "LEASED", "RUNNING", "SUCCEEDED"):
        seed_task(db, task_id=f"task_{state.lower()}", tenant_id="eng", state=state)
        res = client.get(f"/v1/tasks/task_{state.lower()}", headers=auth_header("alice"))
        assert res.status_code == 200, res.text
        body = res.json()["task"]
        assert "waiting_for" in body
        assert body["waiting_for"] is None
    assert counting.pool_batches == [], "no pool is read for a task that is not READY"


def test_list_tasks_reads_pools_once_per_page_de_duplicated(client, db, counting):
    seed_eng(db)
    for i in range(5):
        seed_task(db, task_id=f"task_r{i}", tenant_id="eng")
    seed_task(db, task_id="task_big", tenant_id="eng", resource_class="large")
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    res = client.get("/v1/tasks", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    rows = {t["id"]: t for t in res.json()["tasks"]}
    assert rows["task_done"]["waiting_for"] is None
    for i in range(5):
        assert rows[f"task_r{i}"]["waiting_for"]["lead"]["pool"] == "tenant:eng"
    assert rows["task_big"]["waiting_for"]["lead"]["units"] == 4
    assert len(counting.pool_batches) == 1
    batch = counting.pool_batches[0]
    assert len(batch) == len(set(batch)), "every pool is asked for once"
    assert "pools/resource:large" in batch and "pools/resource:standard" in batch


def test_list_with_no_ready_task_reads_no_pool(client, db, counting):
    seed_eng(db)
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    res = client.get("/v1/tasks", headers=auth_header("alice"))
    assert res.status_code == 200
    assert counting.pool_batches == []


def test_a_get_writes_nothing(client, db):
    seed_eng(db)
    seed_task(db, task_id="task_ready", tenant_id="eng")
    before = db.dump()
    begun = db.transactions_begun
    assert client.get("/v1/tasks/task_ready", headers=auth_header("alice")).status_code == 200
    assert client.get("/v1/tasks", headers=auth_header("alice")).status_code == 200
    assert db.transactions_begun == begun, "no transaction: this is a read"
    assert db.dump() == before, "no write: a READY task holds no capacity (invariant 1)"


def test_only_the_tasks_own_tenants_pools_are_read(client, db, counting):
    seed_eng(db)
    seed_tenant(db, "research", max_active=5)
    seed_pool(db, "tenant:research", hard_limit=5, active=5)
    seed_pool(db, "provider:anthropic:tenant:research", hard_limit=1, active=1)
    seed_task(db, task_id="task_cc", tenant_id="eng", runner_profile="claude-code",
              provider="anthropic")
    res = client.get("/v1/tasks", headers=auth_header("alice"))
    assert res.status_code == 200
    batch = counting.pool_batches[0]
    tenant_scoped = [p for p in batch if "tenant:" in p]
    assert tenant_scoped and all(p.endswith("tenant:eng") for p in tenant_scoped)
    waiting = res.json()["tasks"][0]["waiting_for"]
    assert all("research" not in p["pool"] for p in waiting["pools"])


def test_a_failed_pool_read_is_unknown_not_an_error(client, db, monkeypatch):
    seed_eng(db)
    seed_task(db, task_id="task_ready", tenant_id="eng")
    inner = db.get_all

    def failing(references, *args, **kwargs):
        refs = list(references)
        if any(r.path.startswith("pools/") for r in refs):
            raise RuntimeError("firestore unavailable")
        return inner(refs, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", failing)
    res = client.get("/v1/tasks/task_ready", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    waiting = res.json()["task"]["waiting_for"]
    assert waiting["complete"] is False
    assert waiting["admissible_now"] is None
    assert waiting["lead"]["state"] == "unknown"
    assert all(p["limit"] is None for p in waiting["pools"])
