"""Every pool that refuses a profile -- not the first one, and not the minimum.

`evaluate_capacity` has always returned a LIST. Two clients collapsed it to one
pool by keeping a running minimum, so when two ceilings bound at once the
screen named one, an operator raised it, and nothing moved. These tests hold
the server-side analyser to the list.

Nothing here needs credentials, an emulator or a network: the analyser is pure
and the route tests run against the in-memory Firestore in conftest.
"""

from __future__ import annotations

import pytest

from swarm_common.models import SlotPool
from swarm_common.states import BlockedReason, ParkReason

from swarm_api.headroom import (
    BASIS_MEASURED,
    BASIS_UNCAPPED,
    BASIS_UNKNOWN,
    GROUP_NEEDS_ACTION,
    GROUP_NO_ROOM,
    NEEDS_ACTION,
    NO_ROOM,
    analyse_profile,
    blocked_reason_groups,
    group_for,
)

from .conftest import auth_header, seed_pool, seed_tenant


def pool(name: str, limit: int, active: int, *, enabled: bool = True) -> SlotPool:
    return SlotPool(name=name, hard_limit=limit, active=active, enabled=enabled)


def by_name(*pools: SlotPool) -> dict[str, SlotPool]:
    return {p.name: p for p in pools}


# --------------------------------------------------------------------------
# The grouping, checked against the enum rather than against a list in a brief
# --------------------------------------------------------------------------

def test_the_two_groups_partition_blocked_reason_exactly():
    """Every member is in exactly one group, and no group invents a member.

    This is the test that makes the split maintainable: adding a member to
    `BlockedReason` fails here loudly instead of silently defaulting into
    "waiting is a valid answer", which is the group that tells an operator to
    do nothing.
    """
    assert NEEDS_ACTION | NO_ROOM == set(BlockedReason)
    assert NEEDS_ACTION & NO_ROOM == set()
    assert len(NEEDS_ACTION) + len(NO_ROOM) == len(list(BlockedReason)) == 12


def test_credential_missing_is_a_park_reason_and_not_a_blocked_reason():
    """The one correction to the brief's split, asserted so it stays corrected.

    `CREDENTIAL_MISSING` is a `ParkReason`, not a `BlockedReason`. A tenant
    with no key for the provider is PARKED -- durable, costing nothing -- not
    refused by a pool on a scheduler pass. Putting it in the blocker enum's
    grouping would have made the union above short by one and the four
    genuinely-blocking "needs action" reasons wrong.
    """
    assert "CREDENTIAL_MISSING" in {r.value for r in ParkReason}
    assert "CREDENTIAL_MISSING" not in {r.value for r in BlockedReason}
    assert group_for("CREDENTIAL_MISSING") is None


def test_a_reason_in_neither_group_is_reported_as_neither():
    """`admission.py` raises three bare strings before it reaches any pool."""
    for bare in ("task_missing", "not_ready", "cancel_requested"):
        assert group_for(bare) is None


def test_the_grouping_is_served_as_data():
    served = blocked_reason_groups()
    assert set(served) == {GROUP_NEEDS_ACTION, GROUP_NO_ROOM}
    assert served[GROUP_NEEDS_ACTION] == sorted(r.value for r in NEEDS_ACTION)
    flat = served[GROUP_NEEDS_ACTION] + served[GROUP_NO_ROOM]
    assert sorted(flat) == sorted(r.value for r in BlockedReason)


# --------------------------------------------------------------------------
# THE BUG: two pools at their ceiling, one name rendered
# --------------------------------------------------------------------------

def test_two_pools_at_their_ceiling_both_appear():
    required = ["global", "resource:large", "provider:anthropic"]
    pools = by_name(
        pool("global", 64, 0),
        pool("resource:large", 16, 16),      # full
        pool("provider:anthropic", 8, 8),    # full, at the same moment
    )

    got = analyse_profile(required=required, pools=pools, units=1)

    assert got["headroom"] == 0
    assert [b["pool"] for b in got["blockers"]] == [
        "resource:large",
        "provider:anthropic",
    ]
    # Each carries the numbers that made it fail, on the entry itself.
    assert got["blockers"][0]["limit"] == 16 and got["blockers"][0]["active"] == 16
    assert got["blockers"][1]["limit"] == 8 and got["blockers"][1]["active"] == 8
    assert got["blockers"][0]["reason"] == BlockedReason.RESOURCE_CLASS_LIMIT.value
    assert got["blockers"][1]["reason"] == BlockedReason.PROVIDER_CONCURRENCY_LIMIT.value
    assert {b["group"] for b in got["blockers"]} == {GROUP_NO_ROOM}


def test_blockers_are_in_the_profiles_own_pool_order():
    """Not sorted, not by severity: the order a task must clear them in.

    `pool_names_for` builds the list and admission walks it in that order, so
    reordering here would be a second, private opinion about a list the
    contract already ordered.
    """
    required = ["global", "tenant:acme", "resource:large", "runner:r", "backend:b"]
    pools = by_name(
        pool("global", 4, 4),
        pool("tenant:acme", 4, 4),
        pool("resource:large", 4, 4),
        pool("runner:r", 4, 4),
        pool("backend:b", 4, 4),
    )
    got = analyse_profile(required=required, pools=pools, units=1)
    assert [b["pool"] for b in got["blockers"]] == required


# --------------------------------------------------------------------------
# A pause is not a full pool
# --------------------------------------------------------------------------

def test_a_paused_pool_is_not_a_full_one():
    """Same headroom of 0, opposite remedies, so they must not be one fact."""
    paused = analyse_profile(
        required=["global", "tenant:acme"],
        pools=by_name(pool("global", 64, 0), pool("tenant:acme", 8, 0, enabled=False)),
        units=1,
    )
    full = analyse_profile(
        required=["global", "tenant:acme"],
        pools=by_name(pool("global", 64, 0), pool("tenant:acme", 8, 8)),
        units=1,
    )

    assert paused["headroom"] == full["headroom"] == 0

    (p,) = paused["blockers"]
    (f,) = full["blockers"]
    assert p["reason"] == BlockedReason.MANUAL_PAUSE.value
    assert p["group"] == GROUP_NEEDS_ACTION       # somebody must resume it
    assert f["reason"] == BlockedReason.TENANT_LIMIT.value
    assert f["group"] == GROUP_NO_ROOM            # waiting is a valid answer
    assert p["group"] != f["group"]

    # And a paused pool with room to spare still reads as paused, not as
    # "0 units in use of 8 so it is fine".
    assert p["active"] == 0 and p["limit"] == 8


def test_the_counterfactual_for_a_paused_pool_is_resume_not_raise():
    """Resuming it restores its OWN limit -- it does not make it unlimited."""
    got = analyse_profile(
        required=["global", "tenant:acme"],
        pools=by_name(pool("global", 64, 0), pool("tenant:acme", 3, 0, enabled=False)),
        units=1,
    )
    entry = next(c for c in got["counterfactual"] if c["pool"] == "tenant:acme")
    assert entry["action"] == "resume"
    # 3, not 64 and not unbounded: the pause is lifted, the ceiling is not.
    assert entry["headroom_after"] == 3
    assert entry["delta"] == 3

    other = next(c for c in got["counterfactual"] if c["pool"] == "global")
    assert other["action"] == "raise"
    # Raising global while tenant:acme is still PAUSED buys nothing at all.
    assert other["delta"] == 0
    assert other["next_binding"] == ["tenant:acme"]


# --------------------------------------------------------------------------
# A failed read is never a zero, and never a shorter list
# --------------------------------------------------------------------------

def test_an_unread_pool_makes_the_list_incomplete_without_shortening_it():
    required = ["global", "resource:large", "provider:anthropic"]
    pools = by_name(pool("global", 64, 0), pool("resource:large", 16, 16))

    got = analyse_profile(
        required=required,
        pools=pools,
        units=1,
        unread=["provider:anthropic"],
    )

    # Not 0. A number here would be a confident answer whose only support is
    # that the pool which might have contradicted it stayed silent.
    assert got["headroom"] is None
    assert got["basis"] == BASIS_UNKNOWN
    assert got["complete"] is False
    assert got["unread"] == ["provider:anthropic"]
    # The blocker that WAS measured is still reported. Silence about it would
    # tell an operator they had cleared everything when they had not.
    assert [b["pool"] for b in got["blockers"]] == ["resource:large"]
    # No counterfactual: a delta computed against an unknown baseline is a
    # prediction with nothing behind it.
    assert got["counterfactual"] == []


def test_an_absent_pool_that_was_readable_is_unlimited_not_unread():
    """Absent from a complete listing means never capped, which is a fact."""
    got = analyse_profile(
        required=["global", "runner:never-capped"],
        pools=by_name(pool("global", 64, 4)),
        units=1,
    )
    assert got["complete"] is True
    assert got["unread"] == []
    assert got["uncapped"] == ["runner:never-capped"]
    assert got["headroom"] == 60


def test_no_configured_pool_at_all_is_unbounded_and_not_zero():
    got = analyse_profile(required=["global", "runner:r"], pools={}, units=1)
    assert got["headroom"] is None
    assert got["basis"] == BASIS_UNCAPPED
    assert got["blockers"] == []
    assert got["counterfactual"] == []


# --------------------------------------------------------------------------
# The counterfactual, including the zeros
# --------------------------------------------------------------------------

def test_counterfactual_reports_zero_for_a_pool_that_is_not_binding():
    required = ["global", "resource:large", "tenant:acme"]
    pools = by_name(
        pool("global", 64, 0),          # miles of room
        pool("resource:large", 16, 0),  # 16 units -> 4 large agents: the binding one
        pool("tenant:acme", 40, 0),     # not binding
    )

    got = analyse_profile(required=required, pools=pools, units=4)
    assert got["headroom"] == 4
    assert got["binding"] == ["resource:large"]

    deltas = {c["pool"]: c["delta"] for c in got["counterfactual"]}
    assert deltas["resource:large"] > 0       # the only one worth raising
    assert deltas["tenant:acme"] == 0         # pointless
    assert deltas["global"] == 0              # pointless


def test_relaxing_the_binding_pool_reports_the_second_ceiling_not_infinity():
    """The case a naive counterfactual gets dangerously wrong.

    With `resource:large` lifted, `provider:anthropic` is the next ceiling --
    so the answer is 8 units / 4 = 2 agents, a delta of +1, and the pool that
    would bind next is named. An implementation that dropped the relaxed pool
    and then stopped checking would report an unbounded number and send the
    operator back for a second surprise.
    """
    required = ["global", "resource:large", "provider:anthropic"]
    pools = by_name(
        pool("global", 64, 0),
        pool("resource:large", 4, 0),        # 4 units -> 1 large agent: binds
        pool("provider:anthropic", 8, 0),    # 8 units -> 2 large agents: next
    )

    got = analyse_profile(required=required, pools=pools, units=4)
    assert got["headroom"] == 1
    assert got["binding"] == ["resource:large"]

    lifted = next(c for c in got["counterfactual"] if c["pool"] == "resource:large")
    assert lifted["headroom_after"] == 2           # NOT unbounded
    assert lifted["delta"] == 1
    assert lifted["basis_after"] == BASIS_MEASURED
    assert lifted["next_binding"] == ["provider:anthropic"]


def test_when_two_pools_bind_together_relaxing_either_alone_buys_nothing():
    """The delta that explains the original bug, in one assertion.

    Both at their ceiling: raising either one on its own moves the number by
    zero, because the other still refuses. A screen naming a single pool sends
    the operator to do exactly this.
    """
    required = ["resource:large", "provider:anthropic"]
    pools = by_name(pool("resource:large", 16, 16), pool("provider:anthropic", 8, 8))

    got = analyse_profile(required=required, pools=pools, units=4)
    assert got["headroom"] == 0
    assert len(got["blockers"]) == 2

    deltas = {c["pool"]: c["delta"] for c in got["counterfactual"]}
    assert deltas == {"resource:large": 0, "provider:anthropic": 0}
    for entry in got["counterfactual"]:
        # And each says which pool is still in the way.
        assert entry["next_binding"] == [
            p for p in required if p != entry["pool"]
        ]


def test_relaxing_the_only_configured_pool_is_reported_as_unbounded_not_a_number():
    got = analyse_profile(
        required=["global"], pools=by_name(pool("global", 4, 4)), units=1
    )
    (entry,) = got["counterfactual"]
    assert entry["headroom_after"] is None
    assert entry["basis_after"] == BASIS_UNCAPPED
    # NOT `inf - 0`. A delta against an unbounded number is not a delta.
    assert entry["delta"] is None
    assert entry["next_binding"] == []


def test_headroom_divides_by_weight_and_never_reports_a_partial_agent():
    """A large agent costs 4 units, so 6 free units admit one, not one and a half."""
    got = analyse_profile(
        required=["resource:large"],
        pools=by_name(pool("resource:large", 10, 4)),
        units=4,
    )
    assert got["headroom"] == 1
    assert got["binding"] == ["resource:large"]


def test_a_pool_held_above_its_ceiling_admits_nothing():
    """Drift, not capacity: `active` over `effective_limit` refuses everything."""
    got = analyse_profile(
        required=["global"], pools=by_name(pool("global", 4, 9)), units=1
    )
    assert got["headroom"] == 0
    assert [b["pool"] for b in got["blockers"]] == ["global"]


def test_an_unlimited_hard_limit_does_not_make_the_search_linear():
    """`store.UNLIMITED_HARD_LIMIT` is 1_000_000; a counting loop would hang.

    Binary search answers in ~20 probes. If this ever regresses to counting,
    this test will not fail -- it will stop finishing, which is louder.
    """
    got = analyse_profile(
        required=["global"], pools=by_name(pool("global", 1_000_000, 0)), units=1
    )
    assert got["headroom"] == 1_000_000


# --------------------------------------------------------------------------
# The route: what the browser actually receives
# --------------------------------------------------------------------------

@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng", max_active=40, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=64, active=0)
    seed_pool(db, "resource:standard", hard_limit=64, active=0)
    seed_pool(db, "resource:browser", hard_limit=4, active=4)       # full
    seed_pool(db, "runner:browser", hard_limit=64, active=0)
    seed_pool(db, "backend:GKE_AUTOPILOT", hard_limit=64, active=0)
    seed_pool(db, "backend:CLOUD_RUN_JOB", hard_limit=64, active=0)
    seed_pool(db, "provider:anthropic", hard_limit=8, active=8)     # full too
    seed_pool(db, "provider:anthropic:tenant:eng", hard_limit=16, active=0)
    return db


def test_capacity_route_names_both_blocking_pools(client, seeded):
    body = client.get("/v1/capacity", headers=auth_header("alice")).json()
    admission = body["runner_profiles"]["browser"]["admission"]

    assert admission["headroom"] == 0
    assert [b["pool"] for b in admission["blockers"]] == [
        "resource:browser",
        "provider:anthropic",
    ]
    assert all(b["group"] == GROUP_NO_ROOM for b in admission["blockers"])
    assert admission["complete"] is True
    assert body["pools_complete"] is True


def test_capacity_route_serves_the_grouping_so_no_client_restates_it(client, seeded):
    body = client.get("/v1/capacity", headers=auth_header("alice")).json()
    groups = body["blocked_reason_groups"]
    assert sorted(groups[GROUP_NEEDS_ACTION] + groups[GROUP_NO_ROOM]) == sorted(
        r.value for r in BlockedReason
    )


def test_capacity_route_counterfactual_marks_the_pointless_raise(client, seeded):
    body = client.get("/v1/capacity", headers=auth_header("alice")).json()
    cf = {
        c["pool"]: c
        for c in body["runner_profiles"]["browser"]["admission"]["counterfactual"]
    }
    # Both are at their ceiling, so raising either alone buys nothing and each
    # says which one is still in the way.
    assert cf["resource:browser"]["delta"] == 0
    assert cf["provider:anthropic"]["delta"] == 0
    assert "provider:anthropic" in cf["resource:browser"]["next_binding"]
    assert "resource:browser" in cf["provider:anthropic"]["next_binding"]
    # And a pool nowhere near its limit is reported as pointless too.
    assert cf["global"]["delta"] == 0


def test_capacity_route_reports_a_truncated_pool_listing_as_unread(
    client, seeded, monkeypatch, api_context
):
    """A pool listing that hit its page size cannot prove a pool is absent.

    `list_pools` takes `limit=500`. When it comes back full, a required pool
    missing from it may exist and be full; calling it "unconfigured, therefore
    unlimited" would print a headroom number over a pool nobody read.
    """
    store = api_context.submissions._store
    real = store.list_pools

    def truncated(limit: int = 500):
        rows = real(limit=limit)
        # Stand in for a listing that filled its page: return exactly `limit`
        # rows, with the provider pool sliced off the end.
        keep = [p for p in rows if p.name != "provider:anthropic"]
        return (keep * limit)[:limit]

    monkeypatch.setattr(store, "list_pools", truncated)

    body = client.get("/v1/capacity", headers=auth_header("alice")).json()
    assert body["pools_complete"] is False

    admission = body["runner_profiles"]["browser"]["admission"]
    assert admission["headroom"] is None
    assert admission["basis"] == BASIS_UNKNOWN
    assert admission["complete"] is False
    assert "provider:anthropic" in admission["unread"]
    # The blocker that was measured survives: an incomplete list presented as
    # complete is worse than the single-name bug it replaces.
    assert [b["pool"] for b in admission["blockers"]] == ["resource:browser"]
