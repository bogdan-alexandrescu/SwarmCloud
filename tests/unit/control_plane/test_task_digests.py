"""GET /v1/tasks/digests: what a page draws for MANY tasks, in one read.

THE DEFECT (owner, 2026-11-02, measured in the owner's browser on a 40-step
workflow whose steps all run claude-code-browser). The workflow page made 61
requests on load: 24 per-step `GET /v1/tasks/{id}/events` pages (the slowest
16.7 s, 31.6 s in all) to time each waiting step from its entry event, and 12
`GET /v1/tasks/{id}/attempts` reads to sum each step's tokens and cost. The
Agents list fired one `GET /v1/tasks/{id}/live/session` per browser row on
every refresh -- a transaction each -- for a device outline.

WHAT IS PINNED

  * one read serves, per task: `entered_at` (when a PARKED, LEASED or
    DISPATCHED task entered that state, by the rule the console's
    `stateEnteredAt` applies to the newest events page), `usage` (the console's
    `StepUsage`: attempts, how many carried cost and tokens, checkpoints, and
    each figure summed, null when no attempt reported it), and, with
    `include=live`, the current attempt's live session state and device;
  * tenant scope: another tenant's task id, a missing one and one a
    continuation-scoped caller did not submit are all simply absent, in the
    same way -- never a 404 that confirms an id;
  * the batch is bounded (100 ids) and a larger one is a 422.

And the events page itself reads its task once, not twice, and does not mask
the task's input and metadata, which it never serves.

Offline: the real routes over FakeFirestore. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_common.states import EventType

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)


def _event(db, task_id: str, event_id: str, kind: EventType, at: datetime, tenant: str = "eng",
           detail: dict[str, Any] | None = None) -> None:
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "type": kind.value,
        "at": at,
        "attempt_id": None,
        "lease_id": None,
        "generation": None,
        "detail": detail or {},
    }


def _attempt(db, attempt_id: str, task_id: str, *, tenant: str = "eng", generation: int = 1,
             cost_usd: float | None = None, input_tokens: int | None = None,
             output_tokens: int | None = None, checkpoints: tuple[str, ...] = ()) -> None:
    doc: dict[str, Any] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": None,
        "created_at": T0 + timedelta(minutes=generation),
        "started_at": T0 + timedelta(minutes=generation),
        "completed_at": None,
        "exit_code": None,
        "error": None,
        "peak_rss_bytes": None,
        "oom_near_miss": False,
        "checkpoints": list(checkpoints),
    }
    # Absent, never zero, as `control.record_spend` writes it.
    if cost_usd is not None:
        doc["cost_usd"] = cost_usd
    if input_tokens is not None:
        doc["input_tokens"] = input_tokens
    if output_tokens is not None:
        doc["output_tokens"] = output_tokens
    db.docs[f"attempts/{attempt_id}"] = doc


def _digests(client, ids: list[str], user: str = "alice", **params: Any):
    return client.get(
        "/v1/tasks/digests",
        params={"ids": ",".join(ids), **params},
        headers=auth_header(user),
    )


def _ok(client, ids: list[str], **params: Any) -> dict[str, dict[str, Any]]:
    response = _digests(client, ids, **params)
    assert response.status_code == 200, response.text
    body = response.json()
    return {row["task_id"]: row for row in body["digests"]}


def test_one_read_serves_each_waiting_tasks_entry_time(client, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t_parked", tenant_id="eng", state="PARKED")
    _event(db, "t_parked", "e1", EventType.SUBMITTED, T0)
    _event(db, "t_parked", "e2", EventType.QUEUED, T0 + timedelta(seconds=1))
    # A re-park while parked keeps the FIRST park's time.
    _event(db, "t_parked", "e3", EventType.PARKED, T0 + timedelta(minutes=2))
    _event(db, "t_parked", "e4", EventType.HEARTBEAT, T0 + timedelta(minutes=3))
    _event(db, "t_parked", "e5", EventType.PARKED, T0 + timedelta(minutes=4))
    seed_task(db, task_id="t_leased", tenant_id="eng", state="LEASED")
    _event(db, "t_leased", "e1", EventType.READY, T0)
    _event(db, "t_leased", "e2", EventType.LEASE_ACQUIRED, T0 + timedelta(minutes=7))
    # The record says DISPATCHED; the newest transition the log holds is not
    # its entry, so no time is served rather than an earlier one.
    seed_task(db, task_id="t_stale", tenant_id="eng", state="DISPATCHED")
    _event(db, "t_stale", "e1", EventType.LEASE_ACQUIRED, T0)
    seed_task(db, task_id="t_running", tenant_id="eng", state="RUNNING")
    _event(db, "t_running", "e1", EventType.RUNNING, T0)

    rows = _ok(client, ["t_parked", "t_leased", "t_stale", "t_running"])

    assert set(rows) == {"t_parked", "t_leased", "t_stale", "t_running"}
    assert rows["t_parked"]["state"] == "PARKED"
    assert _at(rows["t_parked"]["entered_at"]) == T0 + timedelta(minutes=2)
    assert _at(rows["t_leased"]["entered_at"]) == T0 + timedelta(minutes=7)
    assert rows["t_stale"]["entered_at"] is None
    # RUNNING is not a clocked wait: no event read, no time.
    assert rows["t_running"]["entered_at"] is None


def test_one_read_serves_each_tasks_attempt_usage_with_null_never_zero(client, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t_two", tenant_id="eng", state="SUCCEEDED")
    _attempt(db, "a1", "t_two", generation=1, input_tokens=100, checkpoints=("c1",))
    _attempt(db, "a2", "t_two", generation=2, cost_usd=0.25, input_tokens=50, output_tokens=7,
             checkpoints=("c2", "c3"))
    seed_task(db, task_id="t_none", tenant_id="eng", state="READY")

    rows = _ok(client, ["t_two", "t_none"])

    assert rows["t_two"]["usage_read"] == "ok"
    assert rows["t_two"]["usage"] == {
        "attempts": 2,
        "attempts_with_cost": 1,
        "attempts_with_tokens": 2,
        "checkpoints": 3,
        "cost_usd": 0.25,
        "input_tokens": 150,
        "output_tokens": 7,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }
    assert rows["t_none"]["usage"]["attempts"] == 0
    assert rows["t_none"]["usage"]["cost_usd"] is None, "no attempt ran: unmeasured, not $0"


def test_another_tenants_and_missing_ids_are_absent_alike(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "other")
    seed_task(db, task_id="t_mine", tenant_id="eng", state="PARKED")
    _event(db, "t_mine", "e1", EventType.PARKED, T0)
    seed_task(db, task_id="t_theirs", tenant_id="other", state="PARKED")
    _event(db, "t_theirs", "e1", EventType.PARKED, T0, tenant="other")
    _attempt(db, "a_theirs", "t_theirs", tenant="other", cost_usd=9.0)

    response = _digests(client, ["t_mine", "t_theirs", "t_nowhere"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["task_id"] for row in body["digests"]] == ["t_mine"]
    assert "t_theirs" not in response.text, "another tenant's id must not be echoed or described"
    assert "9.0" not in response.text


def test_a_continuation_scoped_caller_gets_only_its_own_tasks(client, db, monkeypatch):
    from swarm_api import deps
    from swarm_api.main import create_app  # noqa: F401 -- the app the client wraps

    seed_tenant(db, "eng")
    seed_task(db, task_id="t_own", tenant_id="eng", state="RUNNING")
    seed_task(db, task_id="t_peer", tenant_id="eng", state="RUNNING")
    db.docs["tasks/t_peer"]["submitted_by"] = "someone@eng"
    client.app.dependency_overrides[deps.submission_scope] = lambda: "seed@eng"
    try:
        rows = _ok(client, ["t_own", "t_peer"])
    finally:
        client.app.dependency_overrides.pop(deps.submission_scope, None)

    assert set(rows) == {"t_own"}


def test_the_batch_is_bounded(client, db):
    seed_tenant(db, "eng")
    response = _digests(client, [f"t_{i}" for i in range(101)])
    assert response.status_code == 422, response.text


def test_live_is_served_only_when_asked_and_carries_the_device(client, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t_browser", tenant_id="eng", state="RUNNING",
              runner_profile="claude-code-browser")
    db.docs["tasks/t_browser"]["current_lease_id"] = "lease_b"
    db.docs["leases/lease_b"] = {"lease_id": "lease_b", "task_id": "t_browser",
                                 "tenant_id": "eng", "attempt_id": "att_b"}
    db.docs["live_sessions/live_att_b"] = {
        "id": "live_att_b",
        "tenant_id": "eng",
        "task_id": "t_browser",
        "attempt_id": "att_b",
        "state": "open",
        "device": {"preset": "iphone-15", "orientation": "portrait", "touch": True},
    }
    seed_task(db, task_id="t_plain", tenant_id="eng", state="RUNNING")

    without = _ok(client, ["t_browser"])
    assert "live" not in without["t_browser"], "the live read is opt-in: a workflow poll does not pay for it"

    response = _digests(client, ["t_browser", "t_plain"], include="live")
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["task_id"]: row for row in body["digests"]}
    assert rows["t_browser"]["live"] == {
        "live_browser": True,
        "session_state": "open",
        "device": {"preset": "iphone-15", "orientation": "portrait", "touch": True},
    }
    assert rows["t_plain"]["live"] == {"live_browser": False, "session_state": None, "device": None}
    assert {p["name"] for p in body["presets"]} >= {"desktop", "iphone-15"}


def test_the_events_page_reads_its_task_once_and_masks_no_input(client, db, monkeypatch):
    """The events route read the task, then `Store.list_events` read it again
    for its own tenant check; and it warmed the task's masker, masking the
    whole input and metadata the page never serves."""
    from swarm_api import store as store_module
    from swarm_api import task_input

    seed_tenant(db, "eng")
    seed_task(db, task_id="t_long", tenant_id="eng", state="RUNNING")
    db.docs["tasks/t_long"]["input"] = {"prompt": "x " * 2000}
    for i in range(5):
        _event(db, "t_long", f"e{i}", EventType.HEARTBEAT, T0 + timedelta(minutes=i))
    reads: list[str] = []
    original = store_module.Store.get_task

    def counting(self, tenant_id, task_id, *, submitted_by):
        reads.append(task_id)
        return original(self, tenant_id, task_id, submitted_by=submitted_by)

    monkeypatch.setattr(store_module.Store, "get_task", counting)
    task_input.MASKING_CACHE.clear()
    warmed: list[str] = []
    original_input_value = task_input.TaskMasking.input_value

    def watching(self, *args, **kwargs):
        warmed.append("input")
        return original_input_value(self, *args, **kwargs)

    monkeypatch.setattr(task_input.TaskMasking, "input_value", watching)

    response = client.get("/v1/tasks/t_long/events?order=desc", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert len(response.json()["events"]) == 5
    assert reads == ["t_long"], f"the task was read {len(reads)} times for one events page"
    assert warmed == [], "the events page masked the task's input, which it does not serve"


def test_the_events_page_still_refuses_another_tenants_task(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "other")
    seed_task(db, task_id="t_theirs", tenant_id="other", state="RUNNING")
    _event(db, "t_theirs", "e1", EventType.HEARTBEAT, T0, tenant="other")

    response = client.get("/v1/tasks/t_theirs/events", headers=auth_header("alice"))

    assert response.status_code == 404


def _at(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
