"""`Workspaces.migrate`: a Terraform-era personal tenant's record made `ready`
and `migrated` (docs/workspaces.md §3.3; lane W9 of #847).

The case that shaped it: on 2026-10-09 the owner requested a workspace in the
console before the migration ran, so `workspaces/u-bogdan` was `requested`
with an id and a `workspace_ids/` entry. Writing a fresh id would have
orphaned both. So a `requested`, `failed` or `denied` record is completed in
place, a missing one is created with a fresh id, a migrated one is left alone,
and a dry run writes nothing.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import workspaces as ws
from swarm_api.errors import Conflict

PRINCIPAL = "bogdan@saga.xyz"
TENANT = ws.personal_tenant_id(PRINCIPAL)
KEPT_ID = "w-752763"
FRESH_ID = "w-0a1b2c"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _store(db) -> ws.Workspaces:
    return ws.Workspaces(db, now=lambda: NOW, gate=False, new_id=lambda: FRESH_ID)


def _tenant(db, **fields) -> None:
    doc = {"tenant_id": TENANT, "kind": "user", "principal": PRINCIPAL,
           "max_active": 80, "capacity_units": 80, "credentials": ["anthropic"],
           "enabled": True}
    doc.update(fields)
    db.docs[f"tenants/{TENANT}"] = doc


def _requested(db, **fields) -> dict:
    """The record the console's request wrote: today's shape, from request()."""
    status, record = ws.Workspaces(
        db, now=lambda: NOW - timedelta(hours=3), gate=False, new_id=lambda: KEPT_ID,
    ).request(tenant_id=TENANT, principal=PRINCIPAL, via="console")
    assert status == 202 and record["workspace_id"] == KEPT_ID
    if fields:
        db.docs[f"workspaces/{TENANT}"].update(fields)
    return db.docs[f"workspaces/{TENANT}"]


def _audits(db) -> list[dict]:
    return [v for k, v in db.docs.items() if k.startswith("admin_audit/")]


def _migrate(db, *, apply: bool = True, expect: str | None = None) -> dict:
    return _store(db).migrate(TENANT, quota_pods=100, quota_cpu=400, apply=apply, expect=expect)


def test_the_tenant_id_is_the_one_the_principal_derives_to() -> None:
    assert TENANT == "u-bogdan"


# --------------------------------------------------------------------------
# in place: the pending request's id is kept
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["requested", "failed", "denied"])
def test_a_pending_record_is_completed_in_place_and_keeps_its_workspace_id(db, state) -> None:
    _tenant(db)
    before = copy.deepcopy(_requested(db))
    if state != "requested":
        db.docs[f"workspaces/{TENANT}"].update({
            "state": state,
            "decision": {"by": "admin@saga.xyz", "at": NOW - timedelta(hours=1),
                         "verdict": "denied" if state == "denied" else "approved",
                         "reason": "no" if state == "denied" else None},
            "failure": {"step": "A3", "code": "X", "retryable": True} if state == "failed" else None,
            "steps": {"A1": {"state": "done"}},
        })

    result = _migrate(db)

    assert result["action"] == ws.MIGRATE_UPDATE
    assert result["workspace_id"] == KEPT_ID
    record = db.docs[f"workspaces/{TENANT}"]
    assert record["workspace_id"] == KEPT_ID
    assert record["request_id"] == before["request_id"]
    assert record["requested_at"] == before["requested_at"]
    assert record["requested_via"] == "console"
    assert record["state"] == "ready"
    assert record["migrated"] is True
    assert record["providers"] == ["anthropic"]
    assert record["limits"] == {"max_active": 80, "capacity_units": 80,
                                "quota_pods": 100, "quota_cpu": 400}
    assert record["decision"] == {"by": "migration", "at": NOW, "verdict": "approved",
                                  "reason": "Terraform-era tenant moved by W9"}
    assert record["failure"] is None and record["steps"] == {} and record["ready_at"] is None
    # The index still points at the same record, and no second id was drawn.
    assert db.docs[f"workspace_ids/{KEPT_ID}"] == {"tenant_id": TENANT}
    assert f"workspace_ids/{FRESH_ID}" not in db.docs
    # An earlier decision is kept where an admin can read it.
    if state == "requested":
        assert record["history"] == []
    else:
        assert record["history"][-1]["decision"]["by"] == "admin@saga.xyz"


def test_the_in_place_update_writes_one_admin_audit_entry(db) -> None:
    _tenant(db)
    record = _requested(db)

    _migrate(db)

    entries = _audits(db)
    assert entries == [{
        "action": "migrate", "target_workspace_id": KEPT_ID, "by": "migration", "at": NOW,
        "detail": {"from_state": "requested", "request_id": record["request_id"],
                   "reason": "Terraform-era tenant moved by W9"},
    }]


# --------------------------------------------------------------------------
# no record: created with a fresh id, as a request creates one
# --------------------------------------------------------------------------

def test_with_no_record_one_is_created_with_a_fresh_id_and_its_index(db) -> None:
    _tenant(db)

    result = _migrate(db)

    assert result["action"] == ws.MIGRATE_CREATE
    assert result["before"] is None
    assert result["workspace_id"] == FRESH_ID
    record = db.docs[f"workspaces/{TENANT}"]
    assert db.docs[f"workspace_ids/{FRESH_ID}"] == {"tenant_id": TENANT}
    assert record["tenant_id"] == TENANT and record["principal"] == PRINCIPAL
    assert record["state"] == "ready" and record["migrated"] is True
    assert record["requested_via"] == ws.MIGRATION_VIA
    assert record["limits"]["quota_pods"] == 100 and record["limits"]["max_active"] == 80
    # Every other field is the one a request's new record has.
    request_shape = ws.Workspaces._new_record(
        tenant_id=TENANT, workspace_id=FRESH_ID, principal=PRINCIPAL, now=NOW, via="api")
    assert set(request_shape) <= set(record)
    assert [e["detail"]["from_state"] for e in _audits(db)] == ["none"]


def test_a_drawn_id_that_is_taken_is_drawn_again(db) -> None:
    _tenant(db)
    db.docs[f"workspace_ids/{FRESH_ID}"] = {"tenant_id": "u-someone"}
    draws = iter([FRESH_ID, "w-ffffff"])
    store = ws.Workspaces(db, now=lambda: NOW, gate=False, new_id=lambda: next(draws))

    result = store.migrate(TENANT, quota_pods=100, quota_cpu=400, apply=True)

    assert result["workspace_id"] == "w-ffffff"
    assert db.docs[f"workspace_ids/{FRESH_ID}"] == {"tenant_id": "u-someone"}


# --------------------------------------------------------------------------
# idempotent: a migrated record is left alone
# --------------------------------------------------------------------------

def test_a_second_run_writes_nothing(db) -> None:
    _tenant(db)
    _requested(db)
    _migrate(db)
    after_first = copy.deepcopy(db.docs)

    result = _migrate(db)

    assert result["action"] == ws.MIGRATE_NOTHING
    assert result["workspace_id"] == KEPT_ID
    assert db.docs == after_first


def test_a_ready_migrated_record_is_left_alone_even_when_the_tenant_moved(db) -> None:
    _tenant(db)
    _requested(db)
    _migrate(db)
    _tenant(db, max_active=5)
    after_first = copy.deepcopy(db.docs)

    assert _migrate(db)["action"] == ws.MIGRATE_NOTHING
    assert db.docs == after_first


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

@pytest.mark.parametrize("existing", [True, False])
def test_a_dry_run_writes_nothing_and_says_what_it_would(db, existing) -> None:
    _tenant(db)
    if existing:
        _requested(db)
    before = copy.deepcopy(db.docs)

    result = _migrate(db, apply=False)

    assert db.docs == before
    assert result["action"] == (ws.MIGRATE_UPDATE if existing else ws.MIGRATE_CREATE)
    assert result["after"]["state"] == "ready" and result["after"]["migrated"] is True
    if existing:
        assert result["after"]["workspace_id"] == KEPT_ID


# --------------------------------------------------------------------------
# refusals: nothing written
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["approved", "applying", "needs_owner", "ready"])
def test_a_record_a_build_may_be_making_or_made_is_refused(db, state) -> None:
    _tenant(db)
    _requested(db, state=state)
    before = copy.deepcopy(db.docs)

    with pytest.raises(Conflict, match=state):
        _migrate(db)
    assert db.docs == before


def test_the_plan_is_refused_when_the_record_moved_since_the_dry_run(db) -> None:
    _tenant(db)
    plan = _migrate(db, apply=False)
    assert plan["action"] == ws.MIGRATE_CREATE
    _requested(db)
    before = copy.deepcopy(db.docs)

    with pytest.raises(Conflict, match="changed since the dry run"):
        _migrate(db, expect=plan["action"])
    assert db.docs == before


def test_no_tenant_document_is_refused(db) -> None:
    with pytest.raises(Conflict, match="does not exist"):
        _migrate(db)
    assert not db.docs


@pytest.mark.parametrize("fields", [
    {"kind": "group"},
    {"principal": "someone-else@saga.xyz"},
    {"max_active": None},
])
def test_a_tenant_that_is_not_this_persons_or_has_no_limits_is_refused(db, fields) -> None:
    _tenant(db, **fields)
    before = copy.deepcopy(db.docs)

    with pytest.raises(Conflict):
        _migrate(db)
    assert db.docs == before


def test_a_record_naming_another_principal_is_refused(db) -> None:
    _tenant(db)
    _requested(db, principal="someone-else@saga.xyz")
    before = copy.deepcopy(db.docs)

    with pytest.raises(Conflict, match="different principals"):
        _migrate(db)
    assert db.docs == before


@pytest.mark.parametrize("pods,cpu", [(0, 400), (100, -1), (True, 400), ("100", 400)])
def test_a_quota_that_is_not_a_whole_number_above_zero_is_refused(db, pods, cpu) -> None:
    _tenant(db)
    with pytest.raises(ValueError):
        _store(db).migrate(TENANT, quota_pods=pods, quota_cpu=cpu, apply=True)
    assert set(db.docs) == {f"tenants/{TENANT}"}
