"""Voice it's project states (docs/design/voice-it.md §3.13.2;
swarm_api/voice/projectstate.py, routes/voice_projects.py).

* Old rows read without a migration: `active` is `in_progress`, `shipped` is
  `completed`, a missing status `in_progress`.
* The planner cannot move a cancelled or archived project, nor complete one
  with a queued or running start or an open brief item.
* A person's Cancel moves the project to `cancelled`, then cancels each queued
  or running start through its kind's platform cancel (a fake records the
  calls); one whose cancel fails is listed as still running, with the failure
  in the audit row. Through the route, the platform's own task cancel runs.
* Unarchive restores the state the project had.
* The last merge completes a project only when nothing runs, nothing is open
  and no card waits on the owner; two readers settle it once; a Reopen is not
  undone by the next read.
* Every reader §3.13.2 names settles it: Projects' list and get, a session
  start on the project and the follower's `{roster}` report; a check that
  fails there does not refuse the start.
* Another person's or another tenant's project is 404; a session grant is 403.

Firestore is the in-memory fake; every token and grant is built at runtime.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.errors import NotFound, ValidationFailed
from swarm_api.voice import projectstate as vstate
from swarm_api.voice import roster as vroster
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header, seed_task
from .test_voice_grant import OFFER, _build, _grant_headers, _start
from .test_voice_sessions import FakeVoice

STATE = "/v1/voice/projects/{project_id}/state"
PROJECT = "/v1/voice/projects/{project_id}"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
ALICE = "alice@example.com"


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice,
                  voice_people=(ALICE, "bob@example.com", "root@example.com"))


def _store(db) -> vstore.VoiceStore:
    return vstore.VoiceStore(db, now=lambda: NOW)


def _states(db) -> vstate.ProjectStates:
    return vstate.ProjectStates(db, _store(db), now=lambda: NOW)


def _project(db, *, status: str | None = "active", **fields) -> str:
    project_id = "vp_" + secrets.token_hex(16)
    doc = {"tenant_id": "eng", "person": ALICE, "title": "Bakery website",
           "created_at": NOW, "updated_at": NOW, **fields}
    if status is not None:
        doc["status"] = status
    db.docs[f"{vs.PROJECTS}/{project_id}"] = doc
    return project_id


def _task(db, task_id: str, state: str, *, tenant_id: str = "eng") -> None:
    seed_task(db, task_id=task_id, tenant_id=tenant_id, state=state)
    db.docs[f"tasks/{task_id}"]["metadata"] = {"title": f"Task {task_id}"}


def _start_row(db, project_id: str, kind: str, target_id: str) -> None:
    _store(db).record_start(tenant_id="eng", person=ALICE, project_id=project_id,
                            session_id="vs_" + "1" * 32, kind=kind, target_id=target_id,
                            day="2026-11-12")


def _card(db, project_id: str, kind: str, state: str) -> None:
    card_id = "vc_" + secrets.token_hex(16)
    db.docs[f"{vs.PROJECTS}/{project_id}/{vstore.CARDS}/{card_id}"] = {
        "tenant_id": "eng", "person": ALICE, "project_id": project_id, "kind": kind,
        "body": {"state": state, "title": f"A {kind}"}, "version": 1}


def _doc(db, project_id: str) -> dict:
    return db.docs[f"{vs.PROJECTS}/{project_id}"]


def _audits(db) -> list[dict]:
    return [row for path, row in db.docs.items()
            if path.startswith(vs.AUDIT + "/") and row.get("action") == vstate.AUDIT_STATE]


# --------------------------------------------------------------------------
# Old rows, no migration
# --------------------------------------------------------------------------


def test_every_old_name_reads_as_one_of_the_five_states():
    assert vstore.STATUSES == ("in_progress", "waiting", "completed", "cancelled", "archived")
    assert vstore.read_state("active") == "in_progress"
    assert vstore.read_state("shipped") == "completed"
    assert vstore.read_state("waiting") == "waiting"
    assert vstore.read_state("archived") == "archived"
    assert vstore.read_state(None) == "in_progress"
    assert all(new in vstore.STATUSES for new in vstore.OLD_STATUSES.values())
    assert set(vstore.OLD_STATUSES) == {"active", "shipped"}


def test_a_stored_project_is_served_in_the_new_names_and_its_counts(db):
    active = _project(db, status="active")
    shipped = _project(db, status="shipped")
    missing = _project(db, status=None)
    _card(db, active, "review", "waiting")
    _card(db, active, "work", "acting")
    _card(db, active, "work", "done")
    db.docs[f"{vs.PROJECTS}/{active}/{vs.SESSIONS}/vs_{'2' * 32}"] = {
        "tenant_id": "eng", "person": ALICE, "created_at": NOW}

    store = _store(db)
    view = store.get_project(tenant_id="eng", person=ALICE, project_id=active)
    assert view["state"] == "in_progress"
    assert view["open_cards"] == 2
    assert [w["kind"] for w in view["waiting_on_owner"]] == ["review"]
    assert view["last_session_at"] == NOW.isoformat()
    assert view["state_history"] == []
    assert store.get_project(tenant_id="eng", person=ALICE, project_id=shipped)["state"] == "completed"
    assert store.get_project(tenant_id="eng", person=ALICE, project_id=missing)["state"] == "in_progress"
    # Read, not rewritten: the stored value waits for the project's next move.
    assert _doc(db, active)["status"] == "active"


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["cancelled", "archived"])
def test_the_planner_cannot_move_a_cancelled_or_archived_project(db, status):
    project_id = _project(db, status=status)
    for state in vstate.PLANNER_STATES:
        with pytest.raises(vstate.StateRefused):
            _states(db).planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                     state=state, why="done")
    assert _doc(db, project_id)["status"] == status


def test_the_planner_cannot_cancel_or_archive(db):
    project_id = _project(db)
    for state in ("cancelled", "archived"):
        with pytest.raises(ValidationFailed):
            _states(db).planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                     state=state)


def test_the_planner_cannot_complete_a_project_with_a_running_start(db):
    project_id = _project(db)
    _task(db, "t-running", "RUNNING")
    _start_row(db, project_id, "agent", "t-running")

    with pytest.raises(vstate.StateRefused) as refused:
        _states(db).planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                 state="completed")
    assert refused.value.status_code == 409
    assert _doc(db, project_id)["status"] == "active"


def test_the_planner_cannot_complete_a_project_with_a_queued_follow_up(db):
    project_id = _project(db)
    _task(db, "t-follow", "QUEUED")
    db.docs["tasks/t-follow"]["metadata"]["claude_session"] = vroster.tag_of(project_id)

    with pytest.raises(vstate.StateRefused):
        _states(db).planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                 state="completed")


def test_the_planner_cannot_complete_a_project_whose_brief_has_an_open_item(db):
    project_id = _project(db, brief={"open": ["which payment provider"]})
    with pytest.raises(vstate.StateRefused):
        _states(db).planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                 state="completed")


def test_the_planner_moves_between_its_three_states_and_records_each(db):
    project_id = _project(db)
    _task(db, "t-done", "SUCCEEDED")
    _start_row(db, project_id, "agent", "t-done")
    states = _states(db)

    view = states.planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                               state="waiting", why="needs a decision")
    assert view["state"] == "waiting"
    view = states.planner_move(tenant_id="eng", person=ALICE, project_id=project_id,
                               state="completed", why="all shipped")
    assert view["state"] == "completed"
    history = _doc(db, project_id)["state_history"]
    assert [(h["from"], h["to"], h["by"]) for h in history] == [
        ("in_progress", "waiting", "planner"), ("waiting", "completed", "planner")]
    # The planner's moves write no audit row; a person's do.
    assert _audits(db) == []


# --------------------------------------------------------------------------
# A person's Cancel
# --------------------------------------------------------------------------


class FakePlatform:
    """The platform's cancel routes: records each call; fails the ids named."""

    def __init__(self, db, failing: tuple[str, ...] = ()) -> None:
        self.db = db
        self.calls: list[tuple[str, str]] = []
        self.failing = failing

    def cancel(self, kind: str, target_id: str) -> None:
        self.calls.append((kind, target_id))
        if target_id in self.failing:
            raise RuntimeError("the platform refused the cancel")
        collection = "tasks" if kind == "agent" else "workflows"
        self.db.docs[f"{collection}/{target_id}"]["state"] = "CANCELLED"
        # A run whose agent is cancelled ends cancelled (runs.py).
        for path, doc in self.db.docs.items():
            if path.startswith("issue_runs/") and doc.get("planner_task_id") == target_id:
                doc["state"] = "CANCELLED"


def _cancel_seed(db) -> str:
    project_id = _project(db)
    _task(db, "t-running", "RUNNING")
    _start_row(db, project_id, "agent", "t-running")
    _task(db, "t-queued", "QUEUED")
    _start_row(db, project_id, "agent", "t-queued")
    _task(db, "t-done", "SUCCEEDED")
    _start_row(db, project_id, "agent", "t-done")
    db.docs["workflows/wf-1"] = {"workflow_id": "wf-1", "tenant_id": "eng", "state": "RUNNING",
                                 "steps": []}
    _start_row(db, project_id, "workflow", "wf-1")
    db.docs["issue_runs/run-1"] = {"tenant_id": "eng", "state": "PLANNING",
                                   "issue": {"owner": "example-org", "repo": "app", "number": 7},
                                   "planner_task_id": "t-planner"}
    _task(db, "t-planner", "RUNNING")
    _start_row(db, project_id, "run", "run-1")
    return project_id


def test_a_persons_cancel_cancels_each_queued_or_running_start_by_its_kind(db):
    project_id = _cancel_seed(db)
    platform = FakePlatform(db)

    answer = _states(db).person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                     action="cancel", cancel=platform.cancel)

    assert sorted(platform.calls) == sorted([
        ("agent", "t-running"), ("agent", "t-queued"), ("workflow", "wf-1"),
        ("agent", "t-planner")])
    assert answer["project"]["state"] == "cancelled"
    assert answer["still_running"] == []
    assert {c["id"] for c in answer["cancelled"]} == {"t-running", "t-queued", "wf-1", "run-1"}
    assert _doc(db, project_id)["status"] == "cancelled"
    assert _doc(db, project_id)["state_history"][-1]["by"] == "person"
    (audit,) = _audits(db)
    assert audit["by"] == ALICE and audit["detail"]["to"] == "cancelled"
    assert audit["detail"]["failed"] == []


def test_a_start_whose_cancel_fails_stays_listed_as_running_with_its_failure(db):
    project_id = _cancel_seed(db)
    platform = FakePlatform(db, failing=("wf-1",))

    answer = _states(db).person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                     action="cancel", cancel=platform.cancel)

    assert answer["project"]["state"] == "cancelled"
    (still,) = answer["still_running"]
    assert still["kind"] == "workflow" and still["id"] == "wf-1" and still["state"] == "running"
    assert "refused" in still["reason"]
    assert _doc(db, project_id)["still_running"] == answer["still_running"]
    (audit,) = _audits(db)
    assert [f["id"] for f in audit["detail"]["failed"]] == ["wf-1"]
    rows = _states(db).live(tenant_id="eng", person=ALICE, project_id=project_id)
    assert [(r["kind"], r["id"], r["state"]) for r in rows] == [("workflow", "wf-1", "running")]


def test_a_start_that_ended_before_its_cancel_is_skipped(db):
    project_id = _cancel_seed(db)
    platform = FakePlatform(db)
    states = _states(db)
    real = states._cancel_target

    def ended_first(tenant_id, row):
        if row["id"] == "t-queued":
            db.docs["tasks/t-queued"]["state"] = "SUCCEEDED"
        return real(tenant_id, row)

    states._cancel_target = ended_first
    answer = states.person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                action="cancel", cancel=platform.cancel)
    assert ("agent", "t-queued") not in platform.calls
    assert [s["id"] for s in answer["skipped"]] == ["t-queued"]


def test_a_cancelled_project_cannot_be_cancelled_again(db):
    project_id = _project(db, status="cancelled")
    with pytest.raises(vstate.StateRefused):
        _states(db).person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                action="cancel", cancel=FakePlatform(db).cancel)


def test_cancel_through_the_route_runs_the_platforms_own_task_cancel(client, db, voice):
    call = _start(client, voice)
    _task(db, "t-running", "QUEUED")
    _start_row(db, call.project_id, "agent", "t-running")

    response = client.post(STATE.format(project_id=call.project_id),
                           json={"action": "cancel"}, headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"]["state"] == "cancelled"
    assert [c["id"] for c in body["cancelled"]] == ["t-running"]
    assert db.docs["tasks/t-running"]["state"] == "CANCELLED"


# --------------------------------------------------------------------------
# Archive, Unarchive, Reopen, Mark completed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["waiting", "completed", "cancelled", "active"])
def test_unarchive_restores_the_state_the_project_had(db, status):
    project_id = _project(db, status=status)
    states = _states(db)
    archived = states.person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                  action="archive")
    assert archived["project"]["state"] == "archived"
    restored = states.person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                                  action="unarchive")
    assert restored["project"]["state"] == vstore.read_state(status)
    assert len(_audits(db)) == 2


def test_a_person_moves_through_the_route_and_a_move_not_allowed_is_409(client, db, voice):
    call = _start(client, voice)
    path = STATE.format(project_id=call.project_id)

    done = client.post(path, json={"action": "complete"}, headers=auth_header("alice"))
    assert done.status_code == 200, done.text
    assert done.json()["project"]["state"] == "completed"
    assert client.post(path, json={"action": "unarchive"},
                       headers=auth_header("alice")).status_code == 409
    reopened = client.post(path, json={"action": "reopen"}, headers=auth_header("alice"))
    assert reopened.json()["project"]["state"] == "in_progress"
    assert client.post(path, json={"action": "explode"},
                       headers=auth_header("alice")).status_code == 422


# --------------------------------------------------------------------------
# A merge, settled on read
# --------------------------------------------------------------------------


def _merged(db, **fields) -> str:
    project_id = _project(db, **fields)
    _task(db, "t-merged", "SUCCEEDED")
    _start_row(db, project_id, "agent", "t-merged")
    return project_id


def _read(db, project_id: str) -> dict:
    return _store(db).get_project(tenant_id="eng", person=ALICE, project_id=project_id)


def test_the_last_merge_completes_a_project_on_read(db):
    project_id = _merged(db)
    view = _read(db, project_id)
    assert view["state"] == "completed"
    assert view["state_history"][-1]["by"] == "merge"
    assert _doc(db, project_id)["status"] == "completed"


def test_a_merge_does_not_complete_a_project_while_something_runs(db):
    project_id = _merged(db)
    _task(db, "t-still", "RUNNING")
    _start_row(db, project_id, "agent", "t-still")
    assert _read(db, project_id)["state"] == "in_progress"


def test_a_merge_does_not_complete_a_project_with_an_open_brief_item(db):
    project_id = _merged(db, brief={"open": ["the colour of the header"]})
    assert _read(db, project_id)["state"] == "in_progress"


def test_a_merge_does_not_complete_a_project_with_a_card_waiting_on_the_owner(db):
    project_id = _merged(db, status="waiting")
    _card(db, project_id, "proposal", "proposed")
    assert _read(db, project_id)["state"] == "waiting"


def test_a_project_with_no_work_is_not_completed_by_a_read(db):
    project_id = _project(db)
    assert _read(db, project_id)["state"] == "in_progress"


def test_two_readers_settle_a_merge_once(db):
    project_id = _merged(db)
    data = dict(_doc(db, project_id))
    counts = {"waiting_on_owner": []}
    first = _states(db).settle(tenant_id="eng", person=ALICE, project_id=project_id,
                               data=data, counts=counts)
    second = _states(db).settle(tenant_id="eng", person=ALICE, project_id=project_id,
                                data=data, counts=counts)
    assert first["status"] == second["status"] == "completed"
    history = _doc(db, project_id)["state_history"]
    assert [(h["from"], h["to"], h["by"]) for h in history] == [
        ("in_progress", "completed", "merge")]


def test_a_reopen_is_not_undone_by_the_next_read(db):
    project_id = _merged(db)
    assert _read(db, project_id)["state"] == "completed"
    _states(db).person_move(tenant_id="eng", person=ALICE, project_id=project_id,
                            action="reopen")
    assert _read(db, project_id)["state"] == "in_progress"
    # New work that merges completes it again.
    _task(db, "t-next", "SUCCEEDED")
    _start_row(db, project_id, "agent", "t-next")
    assert _read(db, project_id)["state"] == "completed"


def _history(db, project_id: str) -> list[tuple]:
    return [(h["from"], h["to"], h["by"])
            for h in _doc(db, project_id).get("state_history") or []]


def test_projects_list_settles_every_project_it_reads(client, db, voice):
    project_id = _merged(db)
    response = client.get("/v1/voice/projects", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert _history(db, project_id) == [("in_progress", "completed", "merge")]


def test_a_session_start_on_a_project_settles_its_merge(client, db, voice):
    project_id = _merged(db)

    response = client.post("/v1/voice/sessions",
                           json={"offer_sdp": OFFER, "project_id": project_id},
                           headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert _doc(db, project_id)["status"] == "completed"
    assert _history(db, project_id) == [("in_progress", "completed", "merge")]


def test_a_failed_merge_check_does_not_refuse_a_session_start(client, db, voice, monkeypatch):
    project_id = _merged(db)

    def _broken(self, **_kw):
        raise RuntimeError("the roster read failed")

    monkeypatch.setattr(vstate.ProjectStates, "settle", _broken)
    response = client.post("/v1/voice/sessions",
                           json={"offer_sdp": OFFER, "project_id": project_id},
                           headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert _doc(db, project_id)["status"] == "active"


def test_the_followers_roster_report_settles_the_sessions_project(client, db, voice):
    call = _start(client, voice)
    _task(db, "t-merged", "SUCCEEDED")
    _start_row(db, call.project_id, "agent", "t-merged")
    assert _doc(db, call.project_id)["status"] == "active"

    response = client.post(f"/v1/voice/sessions/{call.session_id}/work",
                           json={"roster": [{"kind": "agent", "id": "t-merged", "state": "done"}]},
                           headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    assert response.json()["type"] == "roster.update"
    assert _history(db, call.project_id) == [("in_progress", "completed", "merge")]


def test_a_delegation_report_is_not_a_read_of_the_project(client, db, voice):
    call = _start(client, voice)
    _task(db, "t-merged", "SUCCEEDED")
    _start_row(db, call.project_id, "agent", "t-merged")

    response = client.post(f"/v1/voice/sessions/{call.session_id}/work",
                           json={"delegation": {"delegation_id": "call_1", "state": "started"}},
                           headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    assert _doc(db, call.project_id)["status"] == "active"


# --------------------------------------------------------------------------
# Whose project
# --------------------------------------------------------------------------


def test_another_persons_or_another_tenants_project_is_404(client, db, voice):
    call = _start(client, voice)
    path = STATE.format(project_id=call.project_id)

    assert client.post(path, json={"action": "archive"},
                       headers=auth_header("bob")).status_code == 404
    missing = STATE.format(project_id="vp_" + "0" * 32)
    assert client.post(missing, json={"action": "archive"},
                       headers=auth_header("alice")).status_code == 404

    doc_path = f"{vs.PROJECTS}/{call.project_id}"
    db.docs[doc_path] = {**db.docs[doc_path], "tenant_id": "research"}
    assert client.post(path, json={"action": "archive"},
                       headers=auth_header("alice")).status_code == 404
    with pytest.raises(NotFound):
        _states(db).planner_move(tenant_id="eng", person=ALICE, project_id=call.project_id,
                                 state="waiting")
    assert db.docs[doc_path].get("status") == "active"


def test_the_session_grant_cannot_move_a_projects_state(client, db, voice):
    call = _start(client, voice)
    response = client.post(STATE.format(project_id=call.project_id),
                           json={"action": "cancel"}, headers=_grant_headers(call))
    assert response.status_code == 403, response.text
