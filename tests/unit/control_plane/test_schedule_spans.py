"""`GET /v1/schedules/{id}/spans`: one page of firings' work and attempt spans (#1109).

WHAT IS HELD HERE
-----------------
* ONE PAGE, NEVER EVERY FIRING (schedules.html detail A's risk note): the
  route serves the firing ids it was asked for, in that order, at most
  `SPAN_FIRINGS`; a firing it was not asked for is not read, and more ids
  than a page is a 422.
* TENANT-SCOPED (§5.1, invariant 9): another tenant's schedule is the same
  404 as a missing one; another schedule's firing id is `not_found`; a firing
  planted under this schedule's id but stored for another tenant is not
  served; another tenant's task named in a firing's work reads as not found.
* HONEST GAPS (redesign-v2.md): an item or a task whose attempts could not be
  read carries its reason, never an empty list that reads as "ran nothing".
* EACH ITEM'S COST AND WHAT IT IS (SCHED-HISTORY-FOLLOWUP, part of #1109):
  the list under the lanes names an issue run by its issue's number and
  title, and prints each item's cost summed from its attempts' recorded
  `cost_usd`, the way the tick sums the firing's. A cost never recorded is
  null with its reason, never 0.

No cloud and no emulator: the in-memory Firestore and the real app.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.routes import schedules as schedule_routes
from swarm_api.validation import IssueRef

from .conftest import auth_header, seed_task
from .test_schedule_routes import NOW, THEIR_REPO, built_types, client, create, ctx, executor  # noqa: F401

__all__ = ["built_types", "client", "ctx", "executor"]


def _firing(db, sid: str, n: int, work: list[dict[str, Any]], *, tenant: str = "eng") -> str:
    fid = f"{sid}:cron:{n}"
    at = NOW - timedelta(days=n)
    db.docs[f"schedule_firings/{fid}"] = {
        "firing_id": fid, "schedule_id": sid, "tenant_id": tenant, "type": "issue-sweep",
        "slot": at, "fired_at": at, "trigger": "cron", "state": "done", "work": work,
        "skip": None, "dry_run": None, "approval_id": None, "outcome": "succeeded",
        "cost": None, "ended_at": at + timedelta(hours=1),
    }
    return fid


def _attempt(db, task_id: str, generation: int, start: Any, minutes: int | None, *,
             exit_code: int | None = 0, tenant: str = "eng", cost: float | None = None) -> None:
    attempt_id = f"att_{task_id}_{generation}"
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant,
        "generation": generation, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "execution_name": None, "created_at": start, "started_at": start,
        "completed_at": None if minutes is None else start + timedelta(minutes=minutes),
        "exit_code": exit_code, "error": None, "peak_rss_bytes": None,
        "oom_near_miss": False, "checkpoints": [], "cost_usd": cost,
    }


def _spans(client, sid: str, ids: list[str], user: str = "alice"):
    return client.get(f"/v1/schedules/{sid}/spans", params=[("firing_id", i) for i in ids],
                      headers=auth_header(user))


def _five(db, sid: str) -> list[str]:
    """Five firings; firing n made one task with n attempts."""
    ids = []
    for n in range(1, 6):
        task_id = f"task_f{n}"
        fired = NOW - timedelta(days=n)
        seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED", created_at=fired)
        for g in range(1, n + 1):
            _attempt(db, task_id, g, fired + timedelta(minutes=10 * g), 5)
        ids.append(_firing(db, sid, n, [{"kind": "task", "id": task_id, "repo_id": "r1"}]))
    return ids


def test_it_serves_only_the_requested_pages_firings_with_each_items_attempts(client, db) -> None:
    sid = create(client)["schedule_id"]
    ids = _five(db, sid)
    response = _spans(client, sid, [ids[1], ids[0]])
    assert response.status_code == 200, response.text
    firings = response.json()["firings"]
    # In the order asked, and only those two: firings 3-5 are not in the answer.
    assert [f["firing_id"] for f in firings] == [ids[1], ids[0]]
    assert response.json()["not_found"] == []
    [item] = firings[0]["items"]
    assert item["kind"] == "task" and item["id"] == "task_f2" and item["unread"] is None
    [task] = item["tasks"]
    assert task["state"] == "SUCCEEDED" and task["attempts_unread"] is None
    # Oldest first, each with its span and generation, as the lanes draw them.
    assert [a["generation"] for a in task["attempts"]] == [1, 2]
    assert task["attempts"][0]["created_at"] < task["attempts"][0]["completed_at"]
    assert len(firings[1]["items"][0]["tasks"][0]["attempts"]) == 1


def test_all_five_firings_of_a_page_are_served_with_bars_matching_their_attempts(client, db) -> None:
    sid = create(client)["schedule_id"]
    ids = _five(db, sid)
    firings = _spans(client, sid, ids).json()["firings"]
    assert [len(f["items"][0]["tasks"][0]["attempts"]) for f in firings] == [1, 2, 3, 4, 5]


def test_more_firings_than_one_page_is_refused(client, db) -> None:
    sid = create(client)["schedule_id"]
    asked = [f"{sid}:cron:{n}" for n in range(schedule_routes.SPAN_FIRINGS + 1)]
    response = _spans(client, sid, asked)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["limit"] == schedule_routes.SPAN_FIRINGS


def test_another_workspaces_schedule_is_a_404_and_reads_nothing(client, db) -> None:
    sid = create(client)["schedule_id"]
    ids = _five(db, sid)
    response = _spans(client, sid, ids, user="bob")
    assert response.status_code == 404
    assert response.json()["message"] == f"schedule {sid!r} not found"


def test_another_schedules_or_tenants_firing_is_not_served_through_yours(client, db) -> None:
    theirs = create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]
    seed_task(db, task_id="their_task", tenant_id="research", state="SUCCEEDED", created_at=NOW)
    _attempt(db, "their_task", 1, NOW, 5, tenant="research")
    their_firing = _firing(db, theirs, 1, [{"kind": "task", "id": "their_task"}], tenant="research")
    mine = create(client)["schedule_id"]
    # Planted under my schedule's id, stored for their tenant.
    planted = _firing(db, mine, 2, [{"kind": "task", "id": "their_task"}], tenant="research")
    # Mine, but naming their task in its work.
    named = _firing(db, mine, 3, [{"kind": "task", "id": "their_task"}])
    body = _spans(client, mine, [their_firing, planted, named]).json()
    assert [f["firing_id"] for f in body["firings"]] == [named]
    assert body["not_found"] == [their_firing, planted]
    [task] = body["firings"][0]["items"][0]["tasks"]
    assert task["attempts"] is None and task["attempts_unread"] == "its task was not found"


def test_a_workflow_is_a_bar_per_step_and_an_api_action_a_reason(client, db, executor) -> None:
    executor.kind = "workflow"
    sid = create(client)["schedule_id"]
    fired = client.post(f"/v1/schedules/{sid}:run", json={}, headers=auth_header("alice")).json()["firing"]
    [work] = fired["work"]
    assert work["kind"] == "workflow"
    stored = db.docs[f"schedule_firings/{fired['firing_id']}"]
    stored["work"] = [*stored["work"], {"kind": "api_action", "id": "label:ready"}]
    body = _spans(client, sid, [fired["firing_id"]]).json()
    wf, action = body["firings"][0]["items"]
    assert wf["state"] is not None and wf["unread"] is None
    [step] = wf["tasks"]
    assert step["label"] == "only" and step["attempts"] == [] and step["attempts_unread"] is None
    assert action["tasks"] == [] and action["unread"] == schedule_routes._UNREAD_API_ACTION


def test_a_task_whose_attempts_could_not_be_read_says_so(client, ctx, db, monkeypatch) -> None:
    sid = create(client)["schedule_id"]
    ids = _five(db, sid)

    def broken(*args: Any, **kwargs: Any):
        raise RuntimeError("deadline exceeded")

    monkeypatch.setattr(ctx.store, "list_attempts", broken)
    response = _spans(client, sid, ids[:1])
    assert response.status_code == 200, response.text
    [task] = response.json()["firings"][0]["items"][0]["tasks"]
    assert task["attempts"] is None and task["attempts_unread"] == "its attempts could not be read"


def test_a_page_bigger_than_the_task_cap_serves_the_rest_as_unread(client, db, monkeypatch) -> None:
    monkeypatch.setattr(schedule_routes, "SPAN_TASKS", 2)
    sid = create(client)["schedule_id"]
    ids = _five(db, sid)
    firings = _spans(client, sid, ids).json()["firings"]
    assert [f["items"][0]["unread"] is None for f in firings] == [True, True, False, False, False]


def _issue_run(db, run_id: str, number: int, planner: str, *, title: str | None,
               state: RunState = RunState.DONE, tenant: str = "eng") -> None:
    run = IssueRun(
        id=run_id, tenant_id=tenant, created_by="alice", created_at=NOW, updated_at=NOW, state=state,
        issue=IssueRef(owner="example-org", repo="example-api", number=number), plan_approval="auto",
        auto_merge=False, fix_rounds=2, planner_task_id=planner,
        issue_read=None if title is None else {"title": title, "state": "open", "read_at": NOW},
    )
    IssueRuns(db, now=lambda: NOW).create(run)


def test_each_item_serves_its_cost_and_an_issue_run_its_issue(client, db) -> None:
    sid = create(client)["schedule_id"]
    # An issue run whose planner retried: both attempts recorded a cost.
    seed_task(db, task_id="task_plan", tenant_id="eng", state="SUCCEEDED", created_at=NOW)
    _attempt(db, "task_plan", 1, NOW, 5, cost=1.25)
    _attempt(db, "task_plan", 2, NOW + timedelta(minutes=10), 5, cost=0.5)
    _issue_run(db, "run_212", 212, "task_plan", title="Orders endpoint 500 on empty cart")
    # A task whose second attempt never reported a cost: the figure is a floor.
    seed_task(db, task_id="task_half", tenant_id="eng", state="SUCCEEDED", created_at=NOW)
    _attempt(db, "task_half", 1, NOW, 5, cost=2.0)
    _attempt(db, "task_half", 2, NOW + timedelta(minutes=10), 5)
    # A task none of whose attempts reported a cost: no figure, and why.
    seed_task(db, task_id="task_none", tenant_id="eng", state="SUCCEEDED", created_at=NOW)
    _attempt(db, "task_none", 1, NOW, 5)
    fid = _firing(db, sid, 1, [
        {"kind": "issue_run", "id": "run_212"}, {"kind": "task", "id": "task_half"},
        {"kind": "task", "id": "task_none"}, {"kind": "api_action", "id": "label:ready"},
    ])
    body = _spans(client, sid, [fid]).json()
    run, half, none, action = body["firings"][0]["items"]
    assert run["issue"] == {
        "number": 212, "ref": "example-org/example-api#212",
        "url": "https://github.com/example-org/example-api/issues/212",
        "title": "Orders endpoint 500 on empty cart",
    }
    assert run["cost"] == {"reported_usd": 1.75, "unreported_attempts": 0, "unread": None}
    assert half["cost"] == {"reported_usd": 2.0, "unreported_attempts": 1, "unread": None}
    assert half["issue"] is None
    # Never $0: a cost nobody recorded is null, with the reason the UI prints.
    assert none["cost"]["reported_usd"] is None
    assert none["cost"]["unreported_attempts"] == 1
    assert none["cost"]["unread"] == schedule_routes._COST_NONE_RECORDED
    assert action["cost"] == {"reported_usd": None, "unreported_attempts": 0,
                              "unread": schedule_routes._COST_API_ACTION}


def test_an_issue_run_whose_title_was_not_read_still_serves_its_number(client, db) -> None:
    sid = create(client)["schedule_id"]
    seed_task(db, task_id="task_plan", tenant_id="eng", state="RUNNING", created_at=NOW)
    _attempt(db, "task_plan", 1, NOW, None)
    _issue_run(db, "run_9", 9, "task_plan", title=None, state=RunState.PLANNING)
    fid = _firing(db, sid, 1, [{"kind": "issue_run", "id": "run_9"}])
    [item] = _spans(client, sid, [fid]).json()["firings"][0]["items"]
    assert item["issue"]["number"] == 9 and item["issue"]["title"] is None
    # Still running: its attempt has not reported, and the reason says so.
    assert item["cost"]["reported_usd"] is None
    assert item["cost"]["unread"] == schedule_routes._COST_STILL_RUNNING


def test_another_workspaces_issue_run_and_its_cost_are_not_served(client, db) -> None:
    sid = create(client)["schedule_id"]
    seed_task(db, task_id="their_plan", tenant_id="research", state="SUCCEEDED", created_at=NOW)
    _attempt(db, "their_plan", 1, NOW, 5, cost=9.0, tenant="research")
    _issue_run(db, "run_theirs", 7, "their_plan", title="Their secret plan", tenant="research")
    fid = _firing(db, sid, 1, [{"kind": "issue_run", "id": "run_theirs"}])
    response = _spans(client, sid, [fid])
    assert "Their secret plan" not in response.text
    [item] = response.json()["firings"][0]["items"]
    assert item["issue"] is None and item["tasks"] == []
    assert item["cost"]["reported_usd"] is None
    assert item["cost"]["unread"] == "its issue run was not found"
    # And their schedule itself is the same 404 as a missing one.
    assert _spans(client, sid, [fid], user="bob").status_code == 404


def test_the_cost_read_keeps_the_task_cap(client, db, monkeypatch) -> None:
    monkeypatch.setattr(schedule_routes, "SPAN_TASKS", 2)
    sid = create(client)["schedule_id"]
    ids = _five(db, sid)
    for n in range(1, 6):
        db.docs[f"attempts/att_task_f{n}_1"]["cost_usd"] = 1.0
    firings = _spans(client, sid, ids).json()["firings"]
    costs = [f["items"][0]["cost"] for f in firings]
    assert [c["reported_usd"] for c in costs] == [1.0, 1.0, None, None, None]
    assert costs[2]["unread"] == schedule_routes._UNREAD_CAP


# -- the docs guard: §7.1's route table lists `/spans` with what it reads -----

SCHEDULES_DOC = Path(__file__).resolve().parents[3] / "docs" / "schedules.md"


def _span_row() -> str:
    text = SCHEDULES_DOC.read_text(encoding="utf-8")
    start = text.index("### 7.1 Routes")
    table = text[start: text.index("\n### ", start + 1)]
    rows = [line for line in table.splitlines() if line.startswith("| `GET /v1/schedules/{id}/spans")]
    assert len(rows) == 1, "docs/schedules.md §7.1 lists no `/spans` row"
    return rows[0]


def test_the_route_table_lists_spans_with_what_it_reads_and_its_cap() -> None:
    row = _span_row()
    # Its caps, as the route holds them.
    assert f"at most {schedule_routes.SPAN_FIRINGS} firings" in row
    assert f"{schedule_routes.SPAN_TASKS} tasks" in row
    assert f"{schedule_routes.SPAN_ATTEMPTS} attempts" in row
    # What it reads and serves, and that it is the caller's tenant only.
    for words in ("firing_id", "issue run", "attempts", "`cost`", "`issue`", "title",
                  "`not_found`", "422", "404", "never $0"):
        assert words in row, f"the `/spans` row does not say {words!r}"
