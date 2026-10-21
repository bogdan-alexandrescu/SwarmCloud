"""Who disabled a tenant, and the tenant-disabled park that names them (WAIT-PAUSER; part of #1782).

The owner's 2026-10-21 ~06:00Z decision: record who disabled a tenant and copy
that into the scheduler's "tenant is missing or disabled" park as `paused_by`,
the field `swarm_api.waiting_detail._pause` reads.

THE RECORD. `PUT /v1/admin/tenants/{id}/limits` is the only API path that
writes a tenant's `enabled` flag. A change of it appends one `admin_audit` row
(`tenant_disabled` / `tenant_enabled`, `target_tenant_id`, `by`, `at`) in the
same transaction as the flag: the repository's existing record of admin
actions, not a new field on the frozen `Tenant`'s document.

THE PARK. The scheduler reads the tenant's newest such row. Only a
`tenant_disabled` row, written since the tenant document was created, names a
person; anything else -- no row, a newer `tenant_enabled`, a row older than a
re-registered tenant, a missing tenant, a read that fails -- parks with no
`paused_by` key, and the reader serves null with its reason. The catalogue
park never names one: no person paused it.

Offline: in-memory Firestore. Each assertion names the mutation it catches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_common.states import ParkReason

from scheduler.loop import DrainReport

from .conftest import auth_header, seed_task, seed_tenant

TENANT = "eng"
ROOT = "root@example.com"
ERROR = "tenant is missing or disabled"


def _audit_rows(db, tenant_id: str = TENANT) -> list[dict]:
    return [doc for path, doc in sorted(db.docs.items())
            if path.startswith("admin_audit/") and doc.get("target_tenant_id") == tenant_id]


def _parked_events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for path, doc in sorted(db.docs.items())
            if path.startswith(prefix) and doc.get("type") == "parked"]


def _disable(client, tenant_id: str = TENANT) -> None:
    response = client.put(f"/v1/admin/tenants/{tenant_id}/limits",
                          headers=auth_header("root"), json={"enabled": False})
    assert response.status_code == 200, response.text


def _enable(client, tenant_id: str = TENANT) -> None:
    response = client.put(f"/v1/admin/tenants/{tenant_id}/limits",
                          headers=auth_header("root"), json={"enabled": True})
    assert response.status_code == 200, response.text


def _admit(make_scheduler, task_id: str) -> None:
    scheduler = make_scheduler()
    task = scheduler.store.get_task(task_id)
    assert scheduler._admit_one(task, DrainReport()) is False


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------

def test_disabling_a_tenant_through_the_api_records_the_callers_principal(client, db):
    seed_tenant(db, TENANT)
    before = set(db.docs[f"tenants/{TENANT}"])

    _disable(client)

    # MUTATION: `set_tenant_limits` writes the flag and no audit row -> fails.
    (row,) = _audit_rows(db)
    # MUTATION: the action is not named, or named as an enable -> fails.
    assert row["action"] == "tenant_disabled"
    # MUTATION: `by` is the tenant's principal, or a constant, not the caller -> fails.
    assert row["by"] == ROOT
    # MUTATION: no time recorded -> fails.
    assert isinstance(row["at"], datetime)
    # MUTATION: the change is not recorded as from -> to -> fails.
    assert row["detail"] == {"enabled": {"from": True, "to": False}}
    # MUTATION: the record is a new field on the tenant document instead
    # (the frozen `Tenant` would drop it) -> fails.
    assert set(db.docs[f"tenants/{TENANT}"]) == before
    assert db.docs[f"tenants/{TENANT}"]["enabled"] is False


def test_enabling_it_again_records_that_too(client, db):
    seed_tenant(db, TENANT)
    _disable(client)
    _enable(client)

    rows = sorted(_audit_rows(db), key=lambda r: r["at"])
    # MUTATION: only disables are recorded -> one row -> fails.
    assert [r["action"] for r in rows] == ["tenant_disabled", "tenant_enabled"]
    assert rows[1]["detail"] == {"enabled": {"from": False, "to": True}}


def test_a_write_that_does_not_change_the_flag_records_nothing(client, db):
    seed_tenant(db, TENANT)
    response = client.put(f"/v1/admin/tenants/{TENANT}/limits",
                          headers=auth_header("root"), json={"max_active": 3})
    assert response.status_code == 200
    _disable(client)
    _disable(client)

    # MUTATION: every limits write is recorded as a disable or enable -> fails.
    # MUTATION: a re-affirming `enabled: false` is recorded as a second disable -> fails.
    assert [r["action"] for r in _audit_rows(db)] == ["tenant_disabled"]


# --------------------------------------------------------------------------
# the park
# --------------------------------------------------------------------------

def test_the_tenant_disabled_park_records_who_disabled_it(client, db, make_scheduler):
    seed_tenant(db, TENANT)
    _disable(client)
    (row,) = _audit_rows(db)
    seed_task(db, task_id="task_d", tenant_id=TENANT)

    _admit(make_scheduler, "task_d")

    assert db.docs["tasks/task_d"]["park_reason"] == ParkReason.MANUAL_PAUSE.value
    (event,) = _parked_events(db, "task_d")
    # MUTATION: the park reads no record -> no paused_by -> fails.
    assert event["detail"]["paused_by"] == ROOT
    # MUTATION: paused_at is the park instant or absent -> fails.
    assert event["detail"]["paused_at"] == row["at"].isoformat()
    # MUTATION: the error the park has always carried is dropped -> fails.
    assert event["detail"]["error"] == ERROR


def test_a_tenant_disabled_with_no_recorded_principal_parks_with_no_paused_by(
    db, make_scheduler
):
    """Disabled by a direct Firestore write (the runbook's fallback), or
    before this lane: there is no person, and none is invented."""
    seed_tenant(db, TENANT, enabled=False)
    seed_task(db, task_id="task_n", tenant_id=TENANT)

    _admit(make_scheduler, "task_n")

    (event,) = _parked_events(db, "task_n")
    # MUTATION: a placeholder principal ("unknown", "terraform", "") is written -> fails.
    assert event["detail"] == {"reason": ParkReason.MANUAL_PAUSE.value, "error": ERROR}


def test_a_disable_undone_by_an_enable_names_nobody(client, db, make_scheduler):
    """Disabled through the API, enabled through the API, then disabled by a
    direct write: the newest record is the enable, so the person who disabled
    it the first time is not credited with the second."""
    seed_tenant(db, TENANT)
    _disable(client)
    _enable(client)
    db.docs[f"tenants/{TENANT}"]["enabled"] = False
    seed_task(db, task_id="task_e", tenant_id=TENANT)

    _admit(make_scheduler, "task_e")

    (event,) = _parked_events(db, "task_e")
    # MUTATION: the newest `tenant_disabled` row is read whatever came after it -> fails.
    assert "paused_by" not in event["detail"]
    assert "paused_at" not in event["detail"]


def test_a_record_older_than_the_tenant_names_nobody(client, db, make_scheduler):
    """Offboarded and registered again under the same id: the old document's
    disabler did not disable this one."""
    seed_tenant(db, TENANT)
    _disable(client)
    db.docs[f"tenants/{TENANT}"]["created_at"] = datetime.now(timezone.utc) + timedelta(minutes=5)
    seed_task(db, task_id="task_r", tenant_id=TENANT)

    _admit(make_scheduler, "task_r")

    (event,) = _parked_events(db, "task_r")
    # MUTATION: rows are not bounded by the tenant's created_at -> fails.
    assert "paused_by" not in event["detail"]


def test_a_missing_tenant_names_nobody(client, db, make_scheduler):
    seed_tenant(db, TENANT)
    _disable(client)
    del db.docs[f"tenants/{TENANT}"]
    seed_task(db, task_id="task_m", tenant_id=TENANT)

    _admit(make_scheduler, "task_m")

    (event,) = _parked_events(db, "task_m")
    # MUTATION: a missing tenant reads the old document's audit -> fails.
    assert event["detail"] == {"reason": ParkReason.MANUAL_PAUSE.value, "error": ERROR}


def test_a_failed_audit_read_still_parks_with_no_paused_by(
    client, db, make_scheduler, monkeypatch
):
    seed_tenant(db, TENANT)
    _disable(client)
    seed_task(db, task_id="task_f", tenant_id=TENANT)
    real = db.collection

    def collection(path):
        if path == "admin_audit":
            raise RuntimeError("firestore unavailable")
        return real(path)

    monkeypatch.setattr(db, "collection", collection)

    _admit(make_scheduler, "task_f")

    # MUTATION: the read's error propagates -> the task is never parked -> fails.
    assert db.docs["tasks/task_f"]["park_reason"] == ParkReason.MANUAL_PAUSE.value
    (event,) = _parked_events(db, "task_f")
    assert event["detail"] == {"reason": ParkReason.MANUAL_PAUSE.value, "error": ERROR}


def test_the_catalogue_park_records_no_paused_by(client, db, make_scheduler):
    """No person paused a profile out of the catalogue -- even in a tenant a
    person did disable, the catalogue park comes first and names nobody."""
    seed_tenant(db, TENANT)
    _disable(client)
    seed_task(db, task_id="task_c", tenant_id=TENANT, runner_profile="not-a-profile")

    _admit(make_scheduler, "task_c")

    (event,) = _parked_events(db, "task_c")
    # MUTATION: the catalogue park copies the tenant's disabler -> fails.
    assert event["detail"] == {
        "reason": ParkReason.MANUAL_PAUSE.value,
        "error": "runner_profile 'not-a-profile' is not in the catalogue",
    }


def test_the_scheduler_reads_the_names_swarm_api_writes():
    """The scheduler image does not carry swarm-api, so it restates the names."""
    from scheduler import loop
    from swarm_api import store

    # MUTATION: either side renames the collection or an action -> the park
    # reads nothing and names nobody, silently -> fails.
    assert (loop.ADMIN_AUDIT, loop.AUDIT_TENANT_DISABLED, loop.AUDIT_TENANT_ENABLED) == (
        store.ADMIN_AUDIT, store.AUDIT_TENANT_DISABLED, store.AUDIT_TENANT_ENABLED)
