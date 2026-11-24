"""A Voice it project's status view and the planner's brief
(swarm_api/voice/projectbrief.py, routes/voice_status.py).

* `GET /v1/voice/projects/{project_id}/status` answers the project's cards
  and roster, as the person's project page shows them; another person's
  project, another tenant's and a missing one are 404, as for the roster.
* A session grant reads its own session's project and nothing else: any
  other project, the person's own included, is 403; another workspace is 403.
* `POST /v1/voice/sessions/{session_id}/brief` is swarm-voice's alone, for its
  own session; it is bounded, holds no credential, and at a spend cap it is
  refused with nothing written.

Firestore is the in-memory fake; every token and grant is built at runtime.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.voice import grant as vg
from swarm_api.voice import ledger as vl
from swarm_api.voice import projectbrief as vbrief
from swarm_api.voice import readback as vreadback
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore
from swarm_api.voice import workfeed as vworkfeed

from .conftest import auth_header
from .test_voice_grant import _build, _grant_headers, _start
from .test_voice_roster import _seed
from .test_voice_sessions import FakeVoice

STATUS = "/v1/voice/projects/{project_id}/status"
BRIEF = "/v1/voice/sessions/{session_id}/brief"
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
#: test_voice_budget.py's rate: talk seconds that cost one dollar.
PER_DOLLAR = 1200


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice,
                  voice_people=("alice@example.com", "bob@example.com", "root@example.com"))


def _card(db, call, card_id: str, *, kind: str = "plan", state: str = "proposed",
          title: str = "Export plan", summary: str = "Three lanes") -> None:
    db.docs[f"{vs.PROJECTS}/{call.project_id}/{vstore.CARDS}/{card_id}"] = {
        "tenant_id": "eng", "person": "alice@example.com", "project_id": call.project_id,
        "kind": kind, "updated_at": NOW,
        "body": {"state": state, "title": title, "summary": summary}}


def _project(db, call) -> dict:
    return db.docs[f"{vs.PROJECTS}/{call.project_id}"]


# ---- the status view -------------------------------------------------------------------


def test_the_person_reads_the_projects_cards_and_roster(client, db, voice):
    call = _start(client, voice)
    _seed(db, call)
    _card(db, call, "vc_" + "1" * 32)

    response = client.get(STATUS.format(project_id=call.project_id), headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == call.project_id
    assert [c["title"] for c in body["cards"]] == ["Export plan"]
    assert body["cards"][0]["state"] == "proposed" and body["cards_total"] == 1
    assert body["waiting_on_owner"] == [{"card_id": "vc_" + "1" * 32, "kind": "plan",
                                         "title": "Export plan"}]
    # The roster route's own rows, unchanged.
    roster = client.get(f"/v1/voice/projects/{call.project_id}/roster",
                        headers=auth_header("alice")).json()["rows"]
    assert body["roster"] == roster and len(roster) == 4
    assert body["brief"] is None


def test_the_grant_reads_its_own_sessions_project(client, db, voice):
    call = _start(client, voice)
    _seed(db, call)

    response = client.get(STATUS.format(project_id=call.project_id), headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    assert {r["id"] for r in response.json()["roster"]} == {"t-started", "wf-1", "run-1", "t-fix"}
    assert ("GET", STATUS) in vg.ALLOWED_ROUTES
    assert vworkfeed.phrase_for("GET", STATUS) != vworkfeed.FALLBACK


def test_another_persons_or_another_tenants_project_is_404_to_the_person(client, db, voice):
    call = _start(client, voice)
    _seed(db, call)
    path = STATUS.format(project_id=call.project_id)

    assert client.get(path, headers=auth_header("bob")).status_code == 404
    missing = STATUS.format(project_id="vp_" + "0" * 32)
    assert client.get(missing, headers=auth_header("alice")).status_code == 404
    for doc_path, doc in list(db.docs.items()):
        if doc_path.startswith(vs.PROJECTS + "/") and doc.get("tenant_id") == "eng":
            db.docs[doc_path] = {**doc, "tenant_id": "research"}
    assert client.get(path, headers=auth_header("alice")).status_code == 404


def test_a_grant_reads_no_other_project_not_even_the_persons_own(client, db, voice):
    alice = _start(client, voice, "alice")
    bob = _start(client, voice, "bob")
    # Another project of alice's own, from an earlier session.
    earlier = "vp_" + secrets.token_hex(16)
    db.docs[f"{vs.PROJECTS}/{earlier}"] = {"tenant_id": "eng", "person": "alice@example.com",
                                           "title": "Earlier", "status": "in_progress",
                                           "created_at": NOW, "updated_at": NOW}
    assert client.get(STATUS.format(project_id=earlier),
                      headers=auth_header("alice")).status_code == 200

    for project_id in (bob.project_id, earlier, "vp_" + "0" * 32):
        response = client.get(STATUS.format(project_id=project_id), headers=_grant_headers(alice))
        assert response.status_code == 403, (project_id, response.text)


def test_a_grant_reads_its_project_only_in_its_sessions_workspace(client, db, voice):
    call = _start(client, voice)

    response = client.get(STATUS.format(project_id=call.project_id),
                          headers={**_grant_headers(call), "X-Swarm-Tenant": "research"})

    assert response.status_code == 403


def test_the_status_view_writes_nothing(client, db, voice):
    call = _start(client, voice)
    _seed(db, call)
    before = {k: v for k, v in db.docs.items() if not k.startswith(vs.AUDIT + "/")}

    assert client.get(STATUS.format(project_id=call.project_id),
                      headers=auth_header("alice")).status_code == 200

    after = {k: v for k, v in db.docs.items() if not k.startswith(vs.AUDIT + "/")}
    assert after == before


# ---- the brief -------------------------------------------------------------------------


def test_the_grant_writes_the_brief_and_the_next_status_read_has_it(client, db, voice):
    call = _start(client, voice)
    text = "Export plan proposed; waiting on the owner. Login fix queued."

    response = client.post(BRIEF.format(session_id=call.session_id), json={"text": text},
                           headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    assert _project(db, call)[vbrief.BRIEF_FIELD]["text"] == text
    status = client.get(STATUS.format(project_id=call.project_id), headers=_grant_headers(call))
    assert status.json()["brief"]["text"] == text
    assert status.json()["brief"]["session_id"] == call.session_id
    # The `brief` object (notes_to_work's open list) is left alone.
    assert "brief" not in _project(db, call) or _project(db, call)["brief"] == {}
    assert vreadback.classify("POST", BRIEF) == vreadback.AS_HEARD


def test_a_brief_over_the_bound_is_refused_and_nothing_stored(client, db, voice):
    call = _start(client, voice)
    long = "é" * (vbrief.BRIEF_TEXT_MAX // 2 + 1)

    response = client.post(BRIEF.format(session_id=call.session_id), json={"text": long},
                           headers=_grant_headers(call))

    assert response.status_code == 422, response.text
    assert vbrief.BRIEF_FIELD not in _project(db, call)


def test_a_credential_shaped_brief_is_refused_and_not_echoed(client, db, voice):
    call = _start(client, voice)
    fake = "sk-" + secrets.token_hex(24)

    response = client.post(BRIEF.format(session_id=call.session_id),
                           json={"text": f"use {fake} for the deploy"},
                           headers=_grant_headers(call))

    assert response.status_code == 422
    assert fake not in response.text
    assert vbrief.BRIEF_FIELD not in _project(db, call)


def test_only_swarm_voice_writes_the_brief_and_only_for_its_session(client, db, voice):
    call = _start(client, voice)
    other = _start(client, voice, "bob")

    person = client.post(BRIEF.format(session_id=call.session_id), json={"text": "x"},
                         headers=auth_header("alice"))
    assert person.status_code == 403
    elsewhere = client.post(BRIEF.format(session_id=other.session_id), json={"text": "x"},
                            headers=_grant_headers(call))
    assert elsewhere.status_code == 403
    assert vbrief.BRIEF_FIELD not in _project(db, call)
    assert vbrief.BRIEF_FIELD not in _project(db, other)


def test_a_session_at_its_cap_writes_no_brief(client, db, voice):
    call = _start(client, voice)
    (db.collection(vs.PROJECTS).document(call.project_id)
     .collection(vs.SESSIONS).document(call.session_id)
     .update({"talk_seconds": 100 * PER_DOLLAR}))

    response = client.post(BRIEF.format(session_id=call.session_id),
                           json={"text": "Export plan proposed."}, headers=_grant_headers(call))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == vl.SESSION_CAPPED
    assert vbrief.BRIEF_FIELD not in _project(db, call)


def test_the_stored_brief_is_bounded_on_read():
    held = {vbrief.BRIEF_FIELD: {"text": "a" * (vbrief.BRIEF_TEXT_MAX * 2), "at": NOW}}
    assert len(vbrief.read_brief(held)["text"].encode("utf-8")) == vbrief.BRIEF_TEXT_MAX
    assert vbrief.read_brief({}) is None
    assert vbrief.read_brief({vbrief.BRIEF_FIELD: {"text": "  "}}) is None
