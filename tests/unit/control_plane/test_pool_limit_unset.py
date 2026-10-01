"""A pool document with no `hard_limit` is a ceiling nobody set, not a ceiling of 0 (#374, S0).

THE DEFECT. Both `pool_from_dict`s -- swarm-api's and the scheduler's -- read
`data.get("hard_limit", 0)`. A pool document without the key (one written by
hand, or by a writer that set only `enabled`) therefore became limit 0, and
every reader downstream said what a 0 says: admission refused with
TENANT_LIMIT / RESOURCE_CLASS_LIMIT "at 0", the API served `hard_limit: 0`,
and the console told the reader an operator had set the pool to zero. Nobody
had. The ceiling was never written, so it was never read.

WHAT IS STILL TRUE, AND WHY ADMISSION STILL REFUSES. The frozen admission
transaction (`acquire_lease_in_transaction`) reads the same document with the
same default, so a task through such a pool is still refused -- refusing is the
safe answer to "how much may run here" when nobody has said. What changes is
that the refusal is named for what it is (`POOL_LIMIT_UNSET`, limit null), and
the API serves the limit as unknown (null), never as a 0 somebody chose.

A pool set to 0 on purpose is unaffected: it is still a 0, with the pool's own
reason.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_api.codec import pool_from_dict as api_pool_from_dict
from swarm_api.codec import pool_to_api

from scheduler.codec import POOL_LIMIT_UNSET, hard_limit_known
from scheduler.codec import pool_from_dict as scheduler_pool_from_dict

from .conftest import auth_header, seed_pool, seed_task, seed_tenant

TENANT = "eng"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def unset(name: str, **fields) -> dict:
    """A pool document as a hand-written one looks: no `hard_limit` key at all."""
    return {"name": name, "active": 0, "enabled": True, **fields}


# --------------------------------------------------------------------------
# The codecs
# --------------------------------------------------------------------------


def test_the_scheduler_codec_reads_a_missing_hard_limit_as_unknown():
    pool = scheduler_pool_from_dict("tenant:eng", unset("tenant:eng"))
    assert hard_limit_known(pool) is False


def test_the_scheduler_codec_reads_a_null_hard_limit_as_unknown():
    pool = scheduler_pool_from_dict("tenant:eng", unset("tenant:eng", hard_limit=None))
    assert hard_limit_known(pool) is False


def test_the_scheduler_codec_still_reads_an_explicit_zero_as_zero():
    pool = scheduler_pool_from_dict("tenant:eng", unset("tenant:eng", hard_limit=0))
    assert hard_limit_known(pool) is True
    assert pool.hard_limit == 0
    assert pool.effective_limit == 0


def test_the_api_serves_a_missing_hard_limit_as_null_not_zero():
    served = pool_to_api(api_pool_from_dict("resource:browser", unset("resource:browser", active=2)))
    assert served["hard_limit"] is None
    assert served["effective_limit"] is None
    assert served["available"] is None
    # What was read is still served as read.
    assert served["active"] == 2
    assert served["enabled"] is True


def test_the_api_still_serves_an_explicit_zero_as_zero():
    served = pool_to_api(api_pool_from_dict("resource:browser", unset("resource:browser", hard_limit=0)))
    assert served["hard_limit"] == 0
    assert served["effective_limit"] == 0
    assert served["available"] == 0


def test_the_api_serves_a_configured_pool_unchanged():
    served = pool_to_api(
        api_pool_from_dict("provider:anthropic", unset("provider:anthropic", hard_limit=8, active=3, quota_derived_limit=5))
    )
    assert served["hard_limit"] == 8
    assert served["effective_limit"] == 5
    assert served["available"] == 2


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_admission_through_an_unset_pool_names_the_unset_ceiling(db, make_scheduler, dispatcher):
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT)
    db.docs[f"pools/tenant:{TENANT}"] = unset(f"tenant:{TENANT}")
    seed_task(db, task_id="task_a", tenant_id=TENANT)

    report = make_scheduler(now=lambda: NOW).drain()

    doc = db.docs["tasks/task_a"]
    assert doc["state"] == "READY"
    assert dispatcher.dispatched == []
    assert [path for path in db.docs if path.startswith("leases/")] == []
    blocker = next(b for b in doc["blocked_by"] if b["pool"] == f"tenant:{TENANT}")
    assert blocker["reason"] == POOL_LIMIT_UNSET
    assert blocker["limit"] is None
    assert "TENANT_LIMIT" not in report.blockers
    assert report.blockers.get(POOL_LIMIT_UNSET) == 1


def test_a_pool_set_to_zero_on_purpose_keeps_its_own_reason(db, make_scheduler):
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT)
    seed_pool(db, f"tenant:{TENANT}", hard_limit=0)
    seed_task(db, task_id="task_z", tenant_id=TENANT)

    make_scheduler(now=lambda: NOW).drain()

    blocker = next(
        b for b in db.docs["tasks/task_z"]["blocked_by"] if b["pool"] == f"tenant:{TENANT}"
    )
    assert blocker["reason"] == "TENANT_LIMIT"
    assert blocker["limit"] == 0


def test_a_paused_pool_with_no_limit_is_still_reported_paused(db, make_scheduler):
    """Paused refuses at any ceiling, so the pause is the true reason, not the unset limit."""
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT)
    db.docs[f"pools/tenant:{TENANT}"] = unset(f"tenant:{TENANT}", enabled=False)
    seed_task(db, task_id="task_p", tenant_id=TENANT)

    make_scheduler(now=lambda: NOW).drain()

    blocker = next(
        b for b in db.docs["tasks/task_p"]["blocked_by"] if b["pool"] == f"tenant:{TENANT}"
    )
    assert blocker["reason"] == "MANUAL_PAUSE"


# --------------------------------------------------------------------------
# /v1/capacity: profile headroom through an unset pool
# --------------------------------------------------------------------------


def _capacity_profiles(client) -> dict:
    response = client.get("/v1/capacity", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()


def test_capacity_serves_an_unset_pool_as_null_and_its_profiles_headroom_as_unknown(client, db):
    """Not a measured 0 and not TENANT_LIMIT at 0: the ceiling was never read."""
    seed_tenant(db, TENANT, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=64)
    db.docs[f"pools/tenant:{TENANT}"] = unset(f"tenant:{TENANT}")

    served = _capacity_profiles(client)

    pool = next(p for p in served["pools"] if p["name"] == f"tenant:{TENANT}")
    assert pool["hard_limit"] is None
    assert pool["effective_limit"] is None
    # Vacuity guard: at least one profile goes through the tenant pool.
    through = {
        name: p for name, p in served["runner_profiles"].items()
        if f"tenant:{TENANT}" in p["pools"]
    }
    assert through
    for name, profile in through.items():
        admission = profile["admission"]
        assert admission["headroom"] is None, name
        assert admission["basis"] == "unknown", name
        assert f"tenant:{TENANT}" in admission["unread"], name
        assert all(b["pool"] != f"tenant:{TENANT}" for b in admission["blockers"]), name


def test_capacity_still_measures_a_pool_set_to_zero_on_purpose(client, db):
    seed_tenant(db, TENANT, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=64)
    seed_pool(db, f"tenant:{TENANT}", hard_limit=0)

    served = _capacity_profiles(client)

    profile = next(
        p for p in served["runner_profiles"].values() if f"tenant:{TENANT}" in p["pools"]
    )
    assert profile["admission"]["headroom"] == 0
    assert profile["admission"]["basis"] == "measured"
