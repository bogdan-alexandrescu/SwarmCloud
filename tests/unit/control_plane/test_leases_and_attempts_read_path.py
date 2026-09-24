"""The read paths for `leases` and `attempts`, which did not exist.

Both collections were written from the first day, both had decoders in
`codec`, and both had composite indexes declared in terraform. Neither had a
`Store` method or a route, so "which agents are holding capacity right now"
and "what happened on attempt 1 of 3" were unanswerable through the API.

These tests run the REAL store against the in-memory Firestore in `fakes.py`,
so the queries under test are the queries that will run in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api.codec import attempt_to_api, lease_to_api
from swarm_api.store import Store


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _store(db) -> Store:
    return Store(db)


def _lease(db, lease_id, tenant, *, units=1, released=False, minutes_ago=0, state="LEASED"):
    created = NOW - timedelta(minutes=minutes_ago)
    db.collection("leases").document(lease_id).set({
        "lease_id": lease_id,
        "task_id": f"task_{lease_id}",
        "attempt_id": f"att_{lease_id}",
        "tenant_id": tenant,
        "generation": 1,
        "pools": ["global", f"tenant:{tenant}"],
        "units": units,
        "state": state,
        "created_at": created,
        "dispatch_deadline": created + timedelta(minutes=5),
        "expires_at": created + timedelta(minutes=30),
        "heartbeat_at": created + timedelta(seconds=30),
        "released_at": created + timedelta(minutes=1) if released else None,
        "release_reason": "terminal:SUCCEEDED" if released else None,
    })


def _attempt(db, attempt_id, tenant, task_id, *, generation=1, minutes_ago=0, exit_code=None):
    created = NOW - timedelta(minutes=minutes_ago)
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": f"swarm-job-{tenant}-mock-{attempt_id}",
        "created_at": created,
        "started_at": created + timedelta(seconds=10),
        "completed_at": created + timedelta(minutes=2) if exit_code is not None else None,
        "exit_code": exit_code,
        "error": "boom" if exit_code else None,
        "peak_rss_bytes": 1234,
        "oom_near_miss": False,
        "checkpoints": [],
    })


# -- leases ---------------------------------------------------------------

def test_active_only_hides_released_leases_and_default_is_active(db):
    _lease(db, "l_live", "eng")
    _lease(db, "l_done", "eng", released=True)

    ids = [x.lease_id for x in _store(db).list_leases("eng")]
    assert ids == ["l_live"], "a released lease still holds no capacity"

    all_ids = {x.lease_id for x in _store(db).list_leases("eng", active_only=False)}
    assert all_ids == {"l_live", "l_done"}


def test_no_tenant_means_every_tenant(db):
    _lease(db, "l_a", "eng")
    _lease(db, "l_b", "u-bogdan")
    assert {x.lease_id for x in _store(db).list_leases()} == {"l_a", "l_b"}


def test_a_tenant_filter_does_not_leak_another_tenants_lease(db):
    _lease(db, "l_a", "eng")
    _lease(db, "l_b", "u-bogdan")
    assert [x.lease_id for x in _store(db).list_leases("eng")] == ["l_a"]


def test_newest_first(db):
    _lease(db, "l_old", "eng", minutes_ago=30)
    _lease(db, "l_new", "eng", minutes_ago=1)
    assert [x.lease_id for x in _store(db).list_leases("eng")] == ["l_new", "l_old"]


def test_the_api_shape_calls_it_dispatch_state_not_state(db):
    """Nothing ever writes STARTING or RUNNING to a lease.

    The worker advances the TASK through those and touches the lease only to
    heartbeat. A column labelled "state" would therefore show DISPATCHED for
    an agent that has been running for an hour.
    """
    _lease(db, "l1", "eng", state="DISPATCHED")
    body = lease_to_api(_store(db).list_leases("eng")[0])
    assert body["dispatch_state"] == "DISPATCHED"
    assert "state" not in body


def test_the_api_shape_computes_the_predicates_server_side(db):
    _lease(db, "l1", "eng")
    body = lease_to_api(_store(db).list_leases("eng")[0])
    for key in ("released", "expired", "dispatch_overdue"):
        assert key in body, f"{key} must not be left for each caller to recompute"
    assert body["released"] is False


def test_units_are_not_agents(db):
    """A browser agent takes 2 units and a large one 4."""
    _lease(db, "l1", "eng", units=2)
    _lease(db, "l2", "eng", units=4)
    leases = _store(db).list_leases("eng")
    assert len(leases) == 2
    assert sum(x.units for x in leases) == 6


# -- attempts -------------------------------------------------------------

def test_every_attempt_of_a_retried_task_is_readable(db):
    """The point of this endpoint.

    result_summary is written once, at terminal state, so a task that failed
    twice and succeeded on the third carries only attempt three's numbers.
    """
    for i, code in enumerate([1, 1, 0], start=1):
        _attempt(db, f"a{i}", "eng", "task_x", generation=i, minutes_ago=30 - i, exit_code=code)

    attempts = _store(db).list_attempts("eng", "task_x")
    assert [a.generation for a in attempts] == [3, 2, 1], "newest first"
    assert [a.exit_code for a in attempts] == [0, 1, 1]


def test_attempts_are_tenant_scoped(db):
    _attempt(db, "a1", "eng", "task_x")
    _attempt(db, "a2", "u-bogdan", "task_y")
    assert [a.attempt_id for a in _store(db).list_attempts("eng")] == ["a1"]


def test_absent_usage_is_null_not_zero(db):
    """A run with no measurement must never render as a free one."""
    _attempt(db, "a1", "eng", "task_x")
    body = attempt_to_api(_store(db).list_attempts("eng", "task_x")[0])
    for key in ("input_tokens", "output_tokens", "cost_usd"):
        assert body[key] is None, f"{key} defaulted to a number it never measured"


def test_the_api_shape_carries_what_result_summary_cannot(db):
    _attempt(db, "a1", "eng", "task_x", exit_code=1)
    body = attempt_to_api(_store(db).list_attempts("eng", "task_x")[0])
    for key in ("exit_code", "error", "peak_rss_bytes", "execution_name", "backend"):
        assert key in body
    assert body["exit_code"] == 1


# -- the thresholds must not become a third copy of the number -------------

def test_the_api_resolves_the_grace_exactly_as_the_reconciler_does(monkeypatch):
    """90 and 120 live in the reconciler. The API must not restate them.

    `scripts/lib/check-contract-parity.sh` exists because every shell and jq
    restatement of the frozen contract in this repository has drifted at
    least once. A `const GRACE = 90` in the front end would be the same bug
    one layer further out, which is why the threshold ships with the data --
    and why this test compares the two resolutions directly rather than
    asserting 90.
    """
    from reconciler.config import ReconcilerConfig
    from swarm_api.routes.admin import _heartbeat_grace_seconds
    from swarm_common.config import Settings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.delenv("HEARTBEAT_GRACE_SECONDS", raising=False)

    # The default derivation, max(90, interval * 3).
    core = Settings.from_env()
    assert _heartbeat_grace_seconds(core) == ReconcilerConfig.from_env(core).heartbeat_grace_seconds

    # And when an operator overrides it, both must move together.
    monkeypatch.setenv("HEARTBEAT_GRACE_SECONDS", "301")
    core2 = Settings.from_env()
    assert _heartbeat_grace_seconds(core2) == 301
    assert ReconcilerConfig.from_env(core2).heartbeat_grace_seconds == 301


def test_a_long_heartbeat_interval_raises_the_grace_in_both(monkeypatch):
    """The derivation is max(90, interval*3), not a constant 90."""
    from reconciler.config import ReconcilerConfig
    from swarm_api.routes.admin import _heartbeat_grace_seconds
    from swarm_common.config import Settings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.delenv("HEARTBEAT_GRACE_SECONDS", raising=False)
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "45")

    core = Settings.from_env()
    assert _heartbeat_grace_seconds(core) == 135
    assert ReconcilerConfig.from_env(core).heartbeat_grace_seconds == 135


# -- the two limit routes that did not exist -------------------------------

def test_backend_auto_is_refused_as_a_limit_target():
    """AUTO is a routing instruction, not a backend anything runs on.

    A pool named backend:AUTO would be taken by no lease, so setting it would
    be a no-op that looked exactly like a change -- which is the failure this
    whole codebase is organised against.
    """
    from swarm_common.profiles import Backend

    real = {b.value for b in Backend} - {Backend.AUTO.value}
    assert real == {"CLOUD_RUN_JOB", "GKE_AUTOPILOT"}
    assert "AUTO" not in real


def test_the_per_tenant_provider_pool_has_its_own_name():
    """Raising provider:anthropic does not raise provider:anthropic:tenant:X.

    A lease takes BOTH, so capacity is the lower. This is the arithmetic that
    made five the real ceiling on 2026-09-20 while every other pool said 40.
    """
    from swarm_common.models import pool_names_for

    names = pool_names_for(
        tenant_id="u-bogdan",
        provider="anthropic",
        runner_profile="claude-code",
        resource_class="standard",
        backend="CLOUD_RUN_JOB",
    )
    assert "provider:anthropic" in names
    assert "provider:anthropic:tenant:u-bogdan" in names, (
        "the per-tenant slice must be a distinct pool, or raising the "
        "provider-wide one would be enough and it is not"
    )
    assert "backend:CLOUD_RUN_JOB" in names


# -- dispatch_overdue, after contract change request 9 ----------------------
#
# These exist because the suite was green while the flag was permanently False.
# The only test that touched `dispatch_overdue` asserted the KEY was present
# and never its VALUE:
#
#     for key in ("released", "expired", "dispatch_overdue"):
#         assert key in body
#
# So the operator query `overdue_only=1` could return an empty list at exactly
# the moment an incident made someone ask it, and nothing went red for weeks.
# A predicate is not covered by a test that checks it was spelled correctly.
#
# THESE ANCHOR ON THE REAL CLOCK, NOT ON THE MODULE'S `NOW`. `lease_to_api`
# calls `lease.dispatch_overdue()` with no argument, so the predicate resolves
# against `utcnow()`. A fixture dated 2026-09-20 is already days past any
# deadline, which makes "overdue" assertions pass no matter what the code does
# -- the first draft of these tests did exactly that and only the negative case
# exposed it.


def _lease_at(db, lease_id, tenant, *, deadline_delta, state="DISPATCHED"):
    """A lease whose dispatch deadline sits `deadline_delta` from real now."""
    real_now = datetime.now(timezone.utc)
    created = real_now - timedelta(minutes=1)
    db.collection("leases").document(lease_id).set({
        "lease_id": lease_id,
        "task_id": f"task_{lease_id}",
        "attempt_id": f"att_{lease_id}",
        "tenant_id": tenant,
        "generation": 1,
        "pools": ["global", f"tenant:{tenant}"],
        "units": 1,
        "state": state,
        "created_at": created,
        "dispatch_deadline": real_now + deadline_delta,
        "expires_at": real_now + timedelta(minutes=30),
        "heartbeat_at": None,
        "released_at": None,
        "release_reason": None,
    })


def test_a_dispatched_lease_past_its_deadline_is_overdue(db):
    """THE ONE THAT WOULD HAVE CAUGHT IT.

    `mark_dispatched` writes DISPATCHED the moment the backend ACCEPTS the
    create call, long before a container runs. The old guard
    `state is TaskState.LEASED` therefore excluded essentially every lease
    `dispatch_overdue` names. Measured live on 2026-09-22: still False 301s
    after admission.
    """
    _lease_at(db, "l1", "eng", deadline_delta=-timedelta(minutes=5), state="DISPATCHED")
    body = lease_to_api(_store(db).list_leases("eng")[0])
    assert body["dispatch_overdue"] is True, (
        "a DISPATCHED lease five minutes past its dispatch deadline is "
        "overdue; a guard on state is LEASED hides exactly this case"
    )


def test_a_lease_inside_its_deadline_is_not_overdue(db):
    """The other half. Without it, returning a constant True also passes --
    and it is what exposed the first draft anchoring on the wrong clock."""
    _lease_at(db, "l1", "eng", deadline_delta=timedelta(minutes=5), state="DISPATCHED")
    body = lease_to_api(_store(db).list_leases("eng")[0])
    assert body["dispatch_overdue"] is False


def test_a_still_LEASED_lease_past_its_deadline_is_overdue(db):
    """The case the old guard DID cover, kept so removing it cannot regress."""
    _lease_at(db, "l1", "eng", deadline_delta=-timedelta(minutes=5), state="LEASED")
    body = lease_to_api(_store(db).list_leases("eng")[0])
    assert body["dispatch_overdue"] is True
