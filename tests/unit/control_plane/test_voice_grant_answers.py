"""A Voice it session grant reaches exactly one new write: the answer to an
agent's question, `POST /v1/tasks/{task_id}/answers` (docs/design/voice-it.md
§3.9; the owner's pick of 6 November 2026; swarm_api/voice/grant.py `WRITE_ROUTES`).

The planner's `answer_question` act tool (swarm_voice/tools_act.py) sends it
with `via: "voice-it"`. Who may answer is the route's rule, unchanged: the
task's submitter answers, another member does not, and a viewer's grant is
refused by the route (`role_member_required`) with nothing stored.

Offline: the real app, a session started through the real route with a fake
swarm-voice, tokens built at runtime.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest

from swarm_api import refusals, roles
from swarm_api.voice import grant as vg
from swarm_api.voice import readback as rb

from .conftest import ENG_GROUP, seed_task, seed_tenant
from .test_roles_store import grant as grant_role
from .test_voice_grant import (  # noqa: F401 -- fixtures used by name
    _audit,
    _authenticated,
    _build,
    _grant_headers,
    _start,
    client,
    voice,
)

ANSWERS = ("POST", "/v1/tasks/{task_id}/answers")
TASK = "task_voice_q"
OWNER = "alice@example.com"
VIC = "vic@example.com"
DAN = "dan@example.com"

#: The write list as it stood before this lane: cards, read-back answers,
#: visuals and notes.
BEFORE = frozenset({
    ("POST", "/v1/voice/sessions/{session_id}/cards"),
    ("POST", "/v1/voice/sessions/{session_id}/readbacks/answer"),
    ("POST", "/v1/voice/sessions/{session_id}/visuals"),
    ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"),
})


@pytest.fixture
def group_map(group_map):
    return {**group_map, VIC: (ENG_GROUP,), DAN: (ENG_GROUP,)}


@pytest.fixture
def people_client(db, tokens, group_map, objects, voice):  # noqa: F811
    return _build(db, tokens, group_map, objects, voice,
                  voice_people=(OWNER, VIC, DAN, "root@example.com"),
                  settings_key=secrets.token_hex(32))


def _parked(db, *, submitted_by: str = OWNER) -> None:
    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=now)
    doc["submitted_by"] = submitted_by
    doc["updated_at"] = now
    question = {"question": "Keep the old route?",
                "options": [{"label": "keep", "description": "a release"},
                            {"label": "drop", "description": "one"}],
                "recommended": "keep", "context": None, "blocking": False}
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait", "question_wait": {
        "questions": [question], "blocking": [0], "attempt_id": "att_1", "asked_at": now.isoformat(),
    }}]


def _answer(client, call, **body):
    payload = {"question_index": 0, "choice": "drop", "via": "voice-it",
               "session": call.session_id, **body}
    return client.post(f"/v1/tasks/{TASK}/answers?reply=short", json=payload,
                       headers=_grant_headers(call))


def _stored(db) -> list[dict]:
    return list((db.docs[f"tasks/{TASK}"].get("metadata") or {}).get("owner_answers") or [])


def test_the_write_list_gains_the_answers_route_and_nothing_else(client):  # noqa: F811
    # MUTATION: drop the route from WRITE_ROUTES and the planner's answer is 403.
    # (Debug mode's trace write joined the list later: routes/voice_trace.py;
    # so did a spent Live key's reopen and the work feed's write: routes/voice.py.)
    LATER = {
        ("POST", "/v1/voice/sessions/{session_id}/trace"),
        ("POST", "/v1/voice/sessions/{session_id}/reopen"),
        ("POST", "/v1/voice/sessions/{session_id}/work"),
    }
    assert vg.WRITE_ROUTES - BEFORE - LATER == {ANSWERS}
    assert BEFORE <= vg.WRITE_ROUTES
    assert ANSWERS not in vg.ALLOWED_ROUTES
    table = {(m, p) for m, p, _r in _authenticated(client.app)}
    assert ANSWERS in table, "the answers route is not in swarm-api's route table"
    # It runs as heard: answering is not one of the read-back's five verbs.
    assert rb.classify(*ANSWERS) == rb.AS_HEARD


def test_a_grant_posts_its_persons_answer_via_voice_it_and_is_audited(people_client, db, voice):
    _parked(db)
    call = _start(people_client, voice, "alice")

    response = _answer(people_client, call)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "recorded"
    assert response.json()["by"] == OWNER
    [stored] = _stored(db)
    assert (stored["choice"], stored["via"], stored["session"]) == ("drop", "voice-it", call.session_id)
    assert any(row["route"] == ANSWERS[1] and row["via"] == vg.VIA for row in _audit(db))


def test_the_grant_reaches_no_other_task_write(people_client, db, voice):
    _parked(db)
    call = _start(people_client, voice, "alice")
    headers = _grant_headers(call)

    for method, path in (("POST", "/v1/tasks"), ("POST", f"/v1/tasks/{TASK}/cancel"),
                         ("POST", f"/v1/tasks/{TASK}/retry")):
        response = people_client.request(method, path, headers=headers, json={})
        assert response.status_code in (403, 404, 405), f"{method} {path} -> {response.status_code}"
        assert response.status_code != 200
    assert _stored(db) == []


def test_another_members_grant_may_not_answer_the_submitters_task(people_client, db, voice):
    _parked(db)
    call = _start(people_client, voice, "dan")

    response = _answer(people_client, call)

    assert response.status_code in (403, 404), response.text
    assert _stored(db) == []


def test_a_viewers_grant_is_refused_by_the_route(people_client, db, voice, monkeypatch):
    monkeypatch.setenv(refusals.env_name("role_member_required"), "on")
    _parked(db, submitted_by=VIC)
    call = _start(people_client, voice, "vic")
    # Narrowed to viewer while the session is live: the route reads the role fresh.
    grant_role(people_client.app.state.ctx.roles, VIC, roles.VIEWER)

    response = _answer(people_client, call)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_member_required"
    assert _stored(db) == []
