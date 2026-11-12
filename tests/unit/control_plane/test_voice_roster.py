"""Voice it's agent roster route: `GET /v1/voice/projects/{project_id}/roster`
(docs/design/voice-it.md §3.12.6; swarm_api/voice/roster.py).

* The person's own project: the agents, workflows and runs it started, by id,
  and the follow-ups tagged `voice:<project>`, each with its kind, title,
  state and progress; a started item another tenant holds is left out.
* Another person's project, another tenant's and a missing one are 404.
* The session grant does not reach it.
* A grant read marked quiet (the roster follower's) is audited and writes no
  `work.step`; only a GET marked `roster` is quiet.
* It reads: one starts query, one tag query, and batched reads, never a read
  per row.

Firestore is the in-memory fake; every token and grant is built at runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.voice import grant as vg
from swarm_api.voice import roster as vroster
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore
from swarm_api.voice import workfeed as vworkfeed

from .conftest import auth_header, seed_task
from .test_voice_grant import _build, _grant_headers, _start
from .test_voice_sessions import FakeVoice

ROSTER = "/v1/voice/projects/{project_id}/roster"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice,
                  voice_people=("alice@example.com", "bob@example.com", "root@example.com"))


def _task(db, task_id: str, *, tenant_id: str = "eng", state: str = "RUNNING",
          title: str | None = None, tag: str | None = None) -> None:
    seed_task(db, task_id=task_id, tenant_id=tenant_id, state=state)
    doc = db.docs[f"tasks/{task_id}"]
    doc["metadata"] = {**({"title": title} if title else {}),
                       **({"claude_session": tag} if tag else {})}


def _seed(db, call) -> None:
    store = vstore.VoiceStore(db, now=lambda: NOW)
    tag = vroster.tag_of(call.project_id)

    def start(kind: str, target_id: str) -> None:
        store.record_start(tenant_id="eng", person="alice@example.com", project_id=call.project_id,
                           session_id=call.session_id, kind=kind, target_id=target_id,
                           day="2026-11-12")

    _task(db, "t-started", state="PARKED", title="Fix the login page")
    start("agent", "t-started")
    _task(db, "t-step-1", state="SUCCEEDED", title="Plan the export")
    _task(db, "t-step-2", state="RUNNING")
    db.docs["workflows/wf-1"] = {"workflow_id": "wf-1", "tenant_id": "eng", "state": "RUNNING",
                                 "steps": [{"step_id": "a", "task_id": "t-step-1"},
                                           {"step_id": "b", "task_id": "t-step-2"},
                                           {"step_id": "c"}]}
    start("workflow", "wf-1")
    db.docs["issue_runs/run-1"] = {"tenant_id": "eng", "state": "PLANNED",
                                   "issue": {"owner": "example-org", "repo": "app", "number": 7},
                                   "planner_task_id": "t-planner"}
    start("run", "run-1")
    db.docs["issue_runs/run-other"] = {"tenant_id": "research", "state": "RUNNING"}
    start("run", "run-other")
    _task(db, "t-planner", state="SUCCEEDED", tag=tag)
    _task(db, "t-fix", state="FAILED", title="Fix round 1", tag=tag)
    _task(db, "t-elsewhere", tenant_id="research", state="RUNNING", tag=tag)
    _task(db, "t-untagged", state="RUNNING")


def test_the_roster_lists_what_the_project_started_and_its_follow_ups(client, db, voice):
    call = _start(client, voice)
    _seed(db, call)

    response = client.get(ROSTER.format(project_id=call.project_id), headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == call.project_id
    assert body["rows"] == [
        {"kind": "agent", "id": "t-started", "title": "Fix the login page", "state": "queued",
         "done": None, "total": None},
        {"kind": "workflow", "id": "wf-1", "title": "Plan the export", "state": "running",
         "done": 1, "total": 3},
        {"kind": "run", "id": "run-1", "title": "app#7", "state": "waiting",
         "done": None, "total": None},
        {"kind": "agent", "id": "t-fix", "title": "Fix round 1", "state": "failed",
         "done": None, "total": None},
    ]


def test_an_empty_project_has_an_empty_roster(client, db, voice):
    call = _start(client, voice)
    response = client.get(ROSTER.format(project_id=call.project_id), headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == []


def test_another_persons_or_another_tenants_project_is_404(client, db, voice):
    call = _start(client, voice)
    _seed(db, call)
    path = ROSTER.format(project_id=call.project_id)

    assert client.get(path, headers=auth_header("bob")).status_code == 404
    missing = ROSTER.format(project_id="vp_" + "0" * 32)
    assert client.get(missing, headers=auth_header("alice")).status_code == 404

    # The project moved to a workspace alice does not belong to.
    for doc_path, doc in list(db.docs.items()):
        if doc_path.startswith(vs.PROJECTS + "/") and doc.get("tenant_id") == "eng":
            db.docs[doc_path] = {**doc, "tenant_id": "research"}
    assert client.get(path, headers=auth_header("alice")).status_code == 404


def test_a_malformed_project_id_is_refused(client, voice):
    _start(client, voice)
    response = client.get(ROSTER.format(project_id="not-a-project"), headers=auth_header("alice"))
    assert response.status_code in (404, 422)


def test_the_session_grant_does_not_reach_the_roster(client, db, voice):
    call = _start(client, voice)
    response = client.get(ROSTER.format(project_id=call.project_id), headers=_grant_headers(call))
    assert response.status_code == 403, response.text


def _steps(db, call) -> list[dict]:
    prefix = f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}/{vstore.EVENTS}/"
    return [row for path, row in db.docs.items()
            if path.startswith(prefix) and row.get("type") == vworkfeed.WORK_STEP]


def test_a_quiet_grant_read_is_audited_and_writes_no_work_step(client, db, voice):
    call = _start(client, voice)
    quiet = {**_grant_headers(call), vg.QUIET_HEADER: vg.QUIET_ROSTER}
    assert client.get(f"/v1/tasks?claude_session=voice:{call.project_id}",
                      headers=quiet).status_code == 200
    assert _steps(db, call) == []
    audited = [row for path, row in db.docs.items()
               if path.startswith(vs.AUDIT + "/") and row.get("action") == vg.AUDIT_GRANT_USED]
    assert [row["quiet"] for row in audited] == [True]


def test_only_a_get_marked_roster_is_quiet(client, db, voice):
    call = _start(client, voice)
    headers = _grant_headers(call)
    assert client.get("/v1/tasks", headers=headers).status_code == 200
    assert client.get("/v1/tasks", headers={**headers, vg.QUIET_HEADER: "other"}).status_code == 200
    client.post(f"/v1/voice/sessions/{call.session_id}/cards",
                json={"card": {"kind": "work", "title": "Export"}},
                headers={**headers, vg.QUIET_HEADER: vg.QUIET_ROSTER})
    assert [s["class"] for s in _steps(db, call)] == ["read", "read", "write"]


def test_the_roster_batches_its_reads(db):
    store = vstore.VoiceStore(db, now=lambda: NOW)
    project_id = "vp_" + "2" * 32
    db.docs[f"{vs.PROJECTS}/{project_id}"] = {"tenant_id": "eng", "person": "alice@example.com"}
    calls: list[int] = []
    real = db.get_all

    def counted(refs, *args, **kwargs):
        refs = list(refs)
        calls.append(len(refs))
        return real(refs, *args, **kwargs)

    db.get_all = counted
    for n in range(12):
        _task(db, f"t-{n}")
        store.record_start(tenant_id="eng", person="alice@example.com", project_id=project_id,
                           session_id="vs_" + "1" * 32, kind="agent", target_id=f"t-{n}",
                           day="2026-11-12")
    rows = vroster.Roster(db, store).rows(tenant_id="eng", person="alice@example.com",
                                          project_id=project_id)
    assert len(rows) == 12
    assert calls == [12]
