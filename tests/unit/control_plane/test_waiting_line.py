"""A READY task's place in line behind its binding pool (#1782, WAIT-LINE).

`GET /v1/tasks/{id}` serves `waiting.detail.place_in_line`: 1 + the READY
tasks the scheduler's READY query (`SchedulerStore.ready_tasks`: priority
DESC, created_at ASC) puts before it among the tasks that need the binding
pool -- ONE count aggregation, on the single-task GET only. A tenant pool
counts the task's own tenant; a shared pool counts across tenants and serves
the number only (invariant 9). A failed count is null with a reason, never a
failed GET.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from google.api_core import exceptions as gexc

from scheduler.store import SchedulerStore
from swarm_api.waiting_detail import build_waiting, place_in_line

from .conftest import auth_header, seed_pool, seed_task, seed_tenant
from .fakes import FakeFirestore, FakeQuery

BASE = datetime.now(timezone.utc) - timedelta(hours=1)


def _get_detail(client, task_id: str) -> dict[str, Any]:
    res = client.get(f"/v1/tasks/{task_id}", headers=auth_header("alice"))
    # MUTATION: letting the count's failure fail the GET turns this red.
    assert res.status_code == 200, res.text
    return res.json()["task"]["waiting"]["detail"]


def _seed_tenant_line(db: FakeFirestore) -> None:
    """eng's own pool is full; three of eng's READY tasks are ahead of task_mine."""
    seed_tenant(db, "eng", max_active=2)
    seed_pool(db, "tenant:eng", hard_limit=2, active=2)
    # Ahead: a higher priority, however new; then the older tasks of its priority.
    seed_task(db, task_id="task_hi", tenant_id="eng", priority=5, created_at=BASE + timedelta(minutes=30))
    seed_task(db, task_id="task_old1", tenant_id="eng", created_at=BASE)
    seed_task(db, task_id="task_old2", tenant_id="eng", created_at=BASE + timedelta(minutes=5))
    seed_task(db, task_id="task_mine", tenant_id="eng", created_at=BASE + timedelta(minutes=10))
    # Behind or not in line: newer at its priority, older at a lower one,
    # running, and another tenant's.
    seed_task(db, task_id="task_newer", tenant_id="eng", created_at=BASE + timedelta(minutes=20))
    seed_task(db, task_id="task_low", tenant_id="eng", priority=-1, created_at=BASE - timedelta(hours=1))
    seed_task(db, task_id="task_running", tenant_id="eng", state="RUNNING", priority=9,
              created_at=BASE - timedelta(hours=2))
    seed_task(db, task_id="task_theirs", tenant_id="research", priority=9,
              created_at=BASE - timedelta(hours=2))


def test_three_ready_tasks_ahead_in_the_drain_order_put_it_fourth(client, db):
    _seed_tenant_line(db)
    d = _get_detail(client, "task_mine")
    # MUTATION: serving null (the pre-WAIT-LINE answer), counting the newer or
    # lower-priority task, the RUNNING one or another tenant's turns this red.
    assert d["place_in_line"] == 4
    # MUTATION: keeping a reason beside a served number turns this red.
    assert d["place_in_line_unavailable"] is None
    basis = d["place_in_line_basis"]
    # MUTATION: a basis naming a pool other than waiting_for's lead turns this red.
    assert basis["pool"] == "tenant:eng" == d["pool"]
    # MUTATION: counting a tenant pool across tenants turns this red.
    assert basis["scope"] == "tenant"
    # MUTATION: a basis that does not name the order it counted in turns this red.
    assert "priority" in basis["order"] and "created_at" in basis["order"]


def test_the_count_follows_the_schedulers_own_ready_query_order(db):
    """Parity: each READY task's place is its position in `SchedulerStore.ready_tasks`."""
    for n, (tenant, priority, minutes) in enumerate([
        ("eng", 0, 0), ("research", 3, 50), ("eng", 3, 10), ("eng", -2, -90),
        ("research", 0, 5), ("eng", 0, 40), ("research", 7, 70), ("eng", 1, 25),
    ]):
        seed_task(db, task_id=f"task_{n}", tenant_id=tenant, priority=priority,
                  created_at=BASE + timedelta(minutes=minutes))
    line = SchedulerStore(db).ready_tasks(100)
    wf = {"lead": {"pool": "global", "state": "full", "active": 1, "limit": 1, "units": 1}}
    places = [place_in_line(db, t, wf)["place_in_line"] for t in line]
    # MUTATION: ordering by anything but priority DESC then created_at ASC
    # (created_at DESC, priority ASC, or no priority at all) turns this red.
    assert places == list(range(1, len(line) + 1))


def test_a_shared_pool_counts_across_tenants_and_serves_only_the_number(client, db):
    seed_tenant(db, "eng", max_active=20)
    seed_pool(db, "runner:mock", hard_limit=1, active=1)
    seed_task(db, task_id="task_r1", tenant_id="research", created_at=BASE)
    seed_task(db, task_id="task_r2", tenant_id="research", priority=4, created_at=BASE + timedelta(minutes=30))
    seed_task(db, task_id="task_e1", tenant_id="eng", created_at=BASE + timedelta(minutes=1))
    seed_task(db, task_id="task_codex", tenant_id="research", runner_profile="codex", created_at=BASE)
    seed_task(db, task_id="task_mine", tenant_id="eng", created_at=BASE + timedelta(minutes=10))
    res = client.get("/v1/tasks/task_mine", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    w = res.json()["task"]["waiting"]
    d = w["detail"]
    # MUTATION: counting only eng's tasks for a shared pool (2), or counting a
    # task that does not need runner:mock (task_codex, 5), turns this red.
    assert d["place_in_line"] == 4
    # MUTATION: a shared pool served with tenant scope turns this red.
    assert d["place_in_line_basis"]["pool"] == "runner:mock"
    assert d["place_in_line_basis"]["scope"] == "shared"
    served = json.dumps(w)
    # MUTATION: serving which tasks or tenants are ahead (invariant 9) turns this red.
    for other in ("research", "task_r1", "task_r2", "task_e1", "task_codex"):
        assert other not in served, other


def test_a_backend_pool_counts_every_profile_that_runs_on_that_backend(client, db):
    seed_tenant(db, "eng", max_active=20)
    seed_pool(db, "backend:CLOUD_RUN_JOB", hard_limit=1, active=1)
    seed_task(db, task_id="task_pv", tenant_id="research", runner_profile="post-verdict", created_at=BASE)
    seed_task(db, task_id="task_gke", tenant_id="research", runner_profile="codex", created_at=BASE)
    seed_task(db, task_id="task_mine", tenant_id="eng", created_at=BASE + timedelta(minutes=10))
    d = _get_detail(client, "task_mine")
    # MUTATION: counting only the task's own profile (1), or a GKE profile (3), turns this red.
    assert d["place_in_line"] == 2
    assert d["place_in_line_basis"] == {**d["place_in_line_basis"], "pool": "backend:CLOUD_RUN_JOB",
                                        "scope": "shared"}


def test_an_index_still_building_serves_null_with_why_and_the_get_answers(client, db, monkeypatch):
    _seed_tenant_line(db)

    def building(self, alias=None):
        raise gexc.FailedPrecondition("The query requires an index. That index is currently building")

    monkeypatch.setattr(FakeQuery, "count", building)
    d = _get_detail(client, "task_mine")
    # MUTATION: serving a guessed number, or 0, when the count failed turns this red.
    assert d["place_in_line"] is None
    # MUTATION: a null without the reason, or a reason that does not name the index, turns this red.
    assert "index" in d["place_in_line_unavailable"]
    # MUTATION: dropping the rest of the detail on a failed count turns this red.
    assert d["pool"] == "tenant:eng"


def test_any_failed_count_serves_null_with_why_and_the_get_answers(client, db, monkeypatch):
    _seed_tenant_line(db)
    marker = "count-boom-" + "x" * 8

    def broken(self, alias=None):
        raise RuntimeError(marker)

    monkeypatch.setattr(FakeQuery, "count", broken)
    d = _get_detail(client, "task_mine")
    # MUTATION: re-raising (a 500) or serving a number turns this red.
    assert d["place_in_line"] is None
    # MUTATION: a failure reason that does not name what failed turns this red.
    assert "RuntimeError" in d["place_in_line_unavailable"]
    # MUTATION: copying the exception's text into the response turns this red.
    assert marker not in json.dumps(d)


def test_the_listings_make_no_count_query(client, db, monkeypatch):
    _seed_tenant_line(db)
    counted: list[str] = []
    real_end_before = FakeQuery.end_before

    def spy(self, document_fields):
        counted.append(self._path)
        return real_end_before(self, document_fields)

    monkeypatch.setattr(FakeQuery, "end_before", spy)
    for view in ("", "?view=summary", "?state=READY"):
        res = client.get(f"/v1/tasks{view}", headers=auth_header("alice"))
        assert res.status_code == 200, res.text
    # MUTATION: counting the line for a listing row turns this red.
    assert counted == []
    # The control: the same spy sees the GET's one count, so an empty list
    # above is the listing not counting, not the spy not seeing.
    _get_detail(client, "task_mine")
    # MUTATION: more than one count query per GET turns this red.
    assert counted == ["tasks"]


def test_a_limit_below_the_tasks_units_serves_no_line_and_reads_nothing():
    class Untouchable:
        def collection(self, name):
            raise AssertionError("no count for a pool that can never admit the task")

    from .test_waiting_detail import task
    from swarm_common.states import TaskState

    t = task(state=TaskState.READY)
    wf = {"lead": {"pool": "runner:mock", "state": "zero", "active": 0, "limit": 0, "units": 1}}
    line = place_in_line(Untouchable(), t, wf)
    # MUTATION: counting a line no admission ever moves turns this red (the
    # count's failure reason would replace this one).
    assert line["place_in_line"] is None
    assert "limit" in line["place_in_line_unavailable"]


def test_the_builder_serves_the_line_it_was_given_and_null_when_none_was_read():
    from .test_waiting_detail import READ_AT, task, waiting_for_full
    from swarm_common.states import TaskState

    t = task(state=TaskState.READY)
    given = {"place_in_line": 7, "place_in_line_unavailable": None,
             "place_in_line_basis": {"pool": "runner:merge", "scope": "shared", "order": "o"}}
    d = build_waiting(t, events=[], waiting_for=waiting_for_full(), line=given, as_of=READ_AT)["detail"]
    # MUTATION: the builder ignoring the route's count turns this red.
    assert d["place_in_line"] == 7
    assert d["place_in_line_basis"]["pool"] == "runner:merge"
    unread = build_waiting(t, events=None, waiting_for=waiting_for_full(), as_of=READ_AT)["detail"]
    # MUTATION: a number, or a null without why, where nothing was counted turns this red.
    assert unread["place_in_line"] is None
    assert unread["place_in_line_unavailable"]
    assert unread["place_in_line_basis"] is None
