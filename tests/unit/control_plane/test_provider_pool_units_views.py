"""swarm-api's capacity views count a provider pool at 1 per agent (#2950).

Contract request 129: a lease takes `pool_units_for(pool, units)` in each pool
-- 1 in every `provider:*` pool, its resource units elsewhere. Every view that
says how many agents fit, why one is waiting, or what the leases hold must use
the same rule, or a screen would call a provider pool with room for ten large
agents "room for two".

  * `headroom.analyse_profile`: how many fit, and what binds next;
  * `waiting.waiting_for`: each row's `units` and the below-units verdict;
  * `routes.admin.units_held_by_pool` and the lease row's `pool_units`: the
    accounting-drift check's input, per pool, for new and old leases alike.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_common.models import Lease, SlotPool, Task
from swarm_common.states import TaskState

from swarm_api.codec import lease_from_dict, lease_to_api
from swarm_api.headroom import analyse_profile
from swarm_api.routes.admin import units_held_by_pool
from swarm_api.waiting import waiting_for

NOW = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
TENANT = "eng"
PROVIDER = f"provider:anthropic:tenant:{TENANT}"
REQUIRED = ["global", "resource:large", "provider:anthropic", PROVIDER]


def _pools(provider_limit: int, provider_active: int = 0) -> dict[str, SlotPool]:
    return {
        "global": SlotPool(name="global", hard_limit=1000),
        "resource:large": SlotPool(name="resource:large", hard_limit=40),
        "provider:anthropic": SlotPool(name="provider:anthropic", hard_limit=1000),
        PROVIDER: SlotPool(name=PROVIDER, hard_limit=provider_limit, active=provider_active),
    }


def test_ten_large_agents_fit_a_provider_pool_of_ten():
    out = analyse_profile(required=REQUIRED, pools=_pools(10), units=4)
    assert out["headroom"] == 10, "resource:large's 40 and the provider's 10 both admit ten"
    assert sorted(out["binding"]) == sorted(["resource:large", PROVIDER])


def test_a_provider_pool_with_three_left_admits_three_large_agents():
    """The measured screen: 47/50, three slots, three more large agents."""
    out = analyse_profile(required=REQUIRED, pools=_pools(50, provider_active=47), units=4)
    assert out["headroom"] == 3
    assert out["binding"] == [PROVIDER]


def test_waiting_rows_name_what_the_task_takes_in_each_pool():
    task = Task(
        id="task_w",
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
    raw = {
        "global": {"hard_limit": 1000, "active": 0},
        "resource:large": {"hard_limit": 40, "active": 0},
        "provider:anthropic": {"hard_limit": 1000, "active": 0},
        # Full: the large agent is refused here, as FULL, not as below units --
        # a limit of 2 admits a large agent's 1 unit.
        PROVIDER: {"hard_limit": 2, "active": 2},
    }
    out = waiting_for(task, raw, listing_complete=True, as_of=NOW)
    rows = {r["pool"]: r for r in out["pools"]}
    assert rows[PROVIDER]["units"] == 1
    assert rows[PROVIDER]["state"] == "full"
    assert rows["provider:anthropic"]["units"] == 1
    assert rows["resource:large"]["units"] == 4
    assert out["lead"]["pool"] == PROVIDER


def _lease(lease_id: str, pool_units: dict[str, int] | None) -> Lease:
    return Lease(
        lease_id=lease_id, task_id="t_" + lease_id, attempt_id="a", tenant_id=TENANT,
        generation=1, pools=list(REQUIRED), units=4, state=TaskState.LEASED,
        created_at=NOW, dispatch_deadline=NOW, expires_at=NOW, pool_units=pool_units,
    )


def test_units_held_by_pool_reads_each_lease_by_what_it_took():
    new = _lease("new", {"global": 4, "resource:large": 4, "provider:anthropic": 1, PROVIDER: 1})
    old = _lease("old", None)
    assert units_held_by_pool([new, old]) == {
        "global": 8, "resource:large": 8, "provider:anthropic": 5, PROVIDER: 5,
    }


def test_the_lease_codec_round_trips_pool_units_and_serves_what_is_held():
    taken = {"global": 4, "resource:large": 4, "provider:anthropic": 1, PROVIDER: 1}
    stored = {
        "lease_id": "l1", "task_id": "t1", "attempt_id": "a1", "tenant_id": TENANT,
        "generation": 1, "pools": list(REQUIRED), "units": 4, "pool_units": taken,
        "state": "LEASED", "created_at": NOW, "dispatch_deadline": NOW, "expires_at": NOW,
    }
    lease = lease_from_dict(stored)
    assert lease.pool_units == taken
    assert lease_to_api(lease, now=NOW)["pool_units"] == taken

    old = lease_from_dict({k: v for k, v in stored.items() if k != "pool_units"})
    assert old.pool_units is None
    assert lease_to_api(old, now=NOW)["pool_units"] == {name: 4 for name in REQUIRED}
