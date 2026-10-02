"""#179: each lease-holding row of `GET /v1/tasks` carries its worker's heartbeat.

The worker beats on its LEASE, never its task, so the Agents list could not
draw a silent worker from the rows it already reads. Every row in LEASED,
DISPATCHED, STARTING or RUNNING now carries `heartbeat_at` (the current lease's
last beat) and `heartbeat_grace_seconds` (the reconciler's threshold, from the
same helper `GET /v1/admin/leases` uses), with `heartbeat` saying what the
reading is.

WHAT THIS HOLDS:

  * a lease that never beat serves `heartbeat_at: null`, NOT its `created_at`;
  * a 200-row page costs a bounded number of lease reads (batched `get_all`),
    never one per row;
  * a failed lease read is served as `heartbeat: "not read"`, never as a null
    that reads like silence;
  * a tenant member who is not an admin receives it, on the list and on
    `GET /v1/tasks/{id}`;
  * the grace is the admin route's grace -- one function, not a copy.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_api import heartbeats
from swarm_api.routes import admin as admin_routes

from .conftest import auth_header, seed_task, seed_tenant
from .fakes import FakeDocumentRef


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease(db, lease_id: str, *, task_id: str, tenant: str = "eng",
           beat: datetime | None, created: datetime) -> None:
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id,
        "task_id": task_id,
        "attempt_id": f"att_{lease_id}",
        "tenant_id": tenant,
        "generation": 1,
        "pools": ["global", f"tenant:{tenant}"],
        "units": 1,
        "state": "DISPATCHED",
        "created_at": created,
        "dispatch_deadline": created + timedelta(minutes=8),
        "expires_at": created + timedelta(minutes=30),
        "heartbeat_at": beat,
        "released_at": None,
        "release_reason": None,
    }


def _holding(db, task_id: str, lease_id: str, *, state: str = "RUNNING",
             beat: datetime | None, created: datetime | None = None,
             task_created: datetime | None = None) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state=state, created_at=task_created)
    doc["current_lease_id"] = lease_id
    doc["current_generation"] = 1
    _lease(db, lease_id, task_id=task_id, beat=beat,
           created=created or _now() - timedelta(minutes=15))


def _rows(client, user: str = "alice", **params: Any) -> dict[str, dict[str, Any]]:
    response = client.get("/v1/tasks", headers=auth_header(user), params=params)
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["tasks"]}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_a_member_who_is_not_an_admin_receives_the_heartbeat_on_the_list(client, db, api_context):
    seed_tenant(db, "eng")
    beat = _now() - timedelta(seconds=40)
    _holding(db, "task_run", "lease_run", beat=beat)
    seed_task(db, task_id="task_ready", tenant_id="eng", state="READY")

    assert client.get("/v1/admin/leases", headers=auth_header("alice")).status_code == 403, (
        "the premise: alice is a member, not an admin"
    )
    rows = _rows(client)
    running = rows["task_run"]
    assert running["heartbeat"] == "read"
    assert abs((_parse(running["heartbeat_at"]) - beat).total_seconds()) < 1
    assert running["heartbeat_grace_seconds"] == heartbeats.heartbeat_grace_seconds(
        api_context.settings.core
    )
    # A task that holds no lease has no heartbeat to read.
    ready = rows["task_ready"]
    assert ready["heartbeat"] is None
    assert ready["heartbeat_at"] is None
    assert ready["heartbeat_grace_seconds"] is None


def test_every_lease_holding_state_carries_it(client, db):
    seed_tenant(db, "eng")
    states = ("LEASED", "DISPATCHED", "STARTING", "RUNNING")
    for state in states:
        _holding(db, f"task_{state}", f"lease_{state}", state=state,
                 beat=_now() - timedelta(seconds=5))
    rows = _rows(client)
    for state in states:
        assert rows[f"task_{state}"]["heartbeat"] == "read", state
        assert rows[f"task_{state}"]["heartbeat_at"] is not None, state


def test_a_lease_that_never_beat_is_null_not_its_created_at(client, db):
    seed_tenant(db, "eng")
    created = _now() - timedelta(minutes=20)
    _holding(db, "task_boot", "lease_boot", state="DISPATCHED", beat=None, created=created)

    row = _rows(client)["task_boot"]
    assert row["heartbeat"] == "read", "the lease WAS read; it has simply never beaten"
    assert row["heartbeat_at"] is None, (
        f"a lease that never beat served {row['heartbeat_at']!r}; "
        "the lease's created_at is when it was admitted, not a beat"
    )


def test_the_single_task_read_carries_it_too(client, db):
    seed_tenant(db, "eng")
    beat = _now() - timedelta(seconds=12)
    _holding(db, "task_one", "lease_one", beat=beat)
    response = client.get("/v1/tasks/task_one", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["heartbeat"] == "read"
    assert abs((_parse(task["heartbeat_at"]) - beat).total_seconds()) < 1
    assert isinstance(task["heartbeat_grace_seconds"], int)


def test_a_page_of_200_costs_a_bounded_number_of_lease_reads(client, db, monkeypatch):
    seed_tenant(db, "eng")
    base = _now() - timedelta(hours=1)
    for i in range(200):
        _holding(db, f"task_{i:03d}", f"lease_{i:03d}", beat=_now(),
                 task_created=base + timedelta(seconds=i))

    batches: list[int] = []
    single_lease_reads: list[str] = []
    in_batch = {"depth": 0}
    original_get_all = db.get_all
    original_get = FakeDocumentRef.get

    def counting_get_all(references, *args, **kwargs):
        refs = list(references)
        lease_refs = [r for r in refs if r.path.startswith("leases/")]
        if lease_refs:
            batches.append(len(lease_refs))
        in_batch["depth"] += 1
        try:
            yield from original_get_all(refs, *args, **kwargs)
        finally:
            in_batch["depth"] -= 1

    def counting_get(self, *args, **kwargs):
        if self.path.startswith("leases/") and in_batch["depth"] == 0:
            single_lease_reads.append(self.path)
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", counting_get_all)
    monkeypatch.setattr(FakeDocumentRef, "get", counting_get)

    rows = _rows(client, limit=200)
    assert len(rows) == 200
    assert all(row["heartbeat"] == "read" for row in rows.values())
    assert not single_lease_reads, (
        f"{len(single_lease_reads)} leases were read one document at a time"
    )
    assert batches and len(batches) <= 2, (
        f"a 200-row page read its leases in {len(batches)} batches: {batches}"
    )
    assert sum(batches) == 200


def test_a_failed_lease_read_is_not_read_never_silence(client, db, monkeypatch):
    seed_tenant(db, "eng")
    _holding(db, "task_run", "lease_run", beat=_now())
    seed_task(db, task_id="task_ready", tenant_id="eng", state="READY")
    original_get_all = db.get_all

    def failing_get_all(references, *args, **kwargs):
        refs = list(references)
        if any(r.path.startswith("leases/") for r in refs):
            raise RuntimeError("lease read refused")
        return original_get_all(refs, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", failing_get_all)

    rows = _rows(client)
    row = rows["task_run"]
    assert row["heartbeat"] == "not read", (
        f"a failed read was served as {row['heartbeat']!r}; a null here would be "
        "drawn as a silent worker"
    )
    assert row["heartbeat_at"] is None
    assert row["heartbeat_grace_seconds"] is not None
    # The listing itself still answers.
    assert rows["task_ready"]["state"] == "READY"


def test_a_lease_of_another_tenant_is_never_served(client, db):
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_x", tenant_id="eng", state="RUNNING")
    doc["current_lease_id"] = "lease_other"
    _lease(db, "lease_other", task_id="task_x", tenant="research", beat=_now(),
           created=_now() - timedelta(minutes=3))
    row = _rows(client)["task_x"]
    assert row["heartbeat"] == "no lease"
    assert row["heartbeat_at"] is None


def test_the_grace_is_one_function_shared_with_the_admin_route():
    assert admin_routes._heartbeat_grace_seconds is heartbeats.heartbeat_grace_seconds
