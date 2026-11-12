"""The card write path: the one write a Voice it session grant reaches
(docs/design/voice-it.md §3.5, §3.6, §4, §10 phase 2; routes/voice.py,
swarm_api/voice/grant.py, swarm_api/voice/store.py).

swarm-voice's planner draws a card through `POST /v1/voice/sessions/{id}/cards`
as the session's person, with the tenant from the session; the request is
audited `via: voice-it` before the route runs. The grant's write list is that
one route, apart from the read list; a grant writes only to its own session.
`VoiceStore.put_card` validates against the catalogue itself, so an unknown
kind is refused even by a caller that skips `cards.write_card`. Firestore is
the in-memory fake; tokens are built at runtime; no network.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.voice import grant as vg
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header
from .test_voice_cards import _card, _cards
from .test_voice_grant import (  # noqa: F401 - fixtures
    _audit,
    _authenticated,
    _grant_headers,
    _start,
    client,
    voice,
)

ROUTE = "/v1/voice/sessions/{session_id}/cards"


def _path(session_id: str) -> str:
    return ROUTE.format(session_id=session_id)


# ---- the grant's write list -------------------------------------------------


def test_the_write_list_is_the_card_route_alone_and_apart_from_the_reads(client):  # noqa: F811
    # voice-it-own-speech.md §7.3 and §8.3 add the read-back answer and the
    # visual and note writes, voice-it.md §3.9 the answer to an agent's
    # question; each is classed as heard (voice/readback.py).
    assert vg.WRITE_ROUTES == frozenset({
        ("POST", ROUTE),
        ("POST", "/v1/voice/sessions/{session_id}/readbacks/answer"),
        ("POST", "/v1/voice/sessions/{session_id}/visuals"),
        ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"),
        # voice-it.md §3.9: the planner's answer act tool (swarm_voice/tools_act.py).
        ("POST", "/v1/tasks/{task_id}/answers"),
        # Debug mode's trace of a planner turn (routes/voice_trace.py).
        ("POST", "/v1/voice/sessions/{session_id}/trace"),
        # A spent Live key's next tier, held for the reopen (routes/voice.py).
        ("POST", "/v1/voice/sessions/{session_id}/reopen"),
        # The work feed: a delegation's lifecycle and the roster (voice/workfeed.py).
        ("POST", "/v1/voice/sessions/{session_id}/work"),
    })
    assert not vg.WRITE_ROUTES & vg.ALLOWED_ROUTES
    assert all(method == "GET" for method, _ in vg.ALLOWED_ROUTES)
    table = {(m, p) for m, p, _r in _authenticated(client.app)}
    assert ("POST", ROUTE) in table, "the card write route is not in swarm-api's route table"


def test_a_grant_draws_a_card_in_its_session_as_its_person_and_is_audited(client, db, voice):  # noqa: F811
    call = _start(client, voice)

    response = client.post(_path(call.session_id), json={"card": _card()},
                           headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    card = response.json()["card"]
    assert card["kind"] == "proposal" and card["version"] == 1
    assert card["project_id"] == call.project_id and card["session_id"] == call.session_id
    stored = list(_cards(db).values())
    assert len(stored) == 1
    assert stored[0]["tenant_id"] == "eng" and stored[0]["person"] == "alice@example.com"
    events = vstore.VoiceStore(db, now=lambda: datetime.now(timezone.utc)).events_after(
        tenant_id="eng", person="alice@example.com", project_id=call.project_id,
        session_id=call.session_id)
    # The grant's masked work step (voice/workfeed.py), then the card.
    assert [e["type"] for e in events] == ["work.step", "card.upsert"]
    rows = [r for r in _audit(db) if r["route"] == ROUTE]
    assert len(rows) == 1
    assert rows[0]["via"] == vg.VIA and rows[0]["method"] == "POST"
    assert rows[0]["session_id"] == call.session_id and rows[0]["tenant_id"] == "eng"


def test_a_grant_writes_to_no_other_session(client, db, voice):  # noqa: F811
    call = _start(client, voice)
    other = "vs_" + secrets.token_hex(16)

    response = client.post(_path(other), json={"card": _card()}, headers=_grant_headers(call))

    assert response.status_code == 403
    assert _cards(db) == {}


def test_a_grant_reaches_no_other_write(client, db, voice):  # noqa: F811
    call = _start(client, voice)
    headers = _grant_headers(call)
    for method, path in (("POST", f"/v1/voice/sessions/{call.session_id}/end"),
                         ("PATCH", f"/v1/voice/projects/{call.project_id}"),
                         ("PATCH", "/v1/voice/preferences"),
                         ("POST", "/v1/voice/sessions")):
        assert client.request(method, path, json={}, headers=headers).status_code == 403, path


def test_a_grant_card_of_an_unknown_kind_is_422_and_nothing_is_stored(client, db, voice):  # noqa: F811
    call = _start(client, voice)

    response = client.post(_path(call.session_id), json={"card": _card(kind="banner")},
                           headers=_grant_headers(call))

    assert response.status_code == 422
    assert _cards(db) == {}


def test_a_credential_shaped_card_is_refused_and_not_echoed(client, db, voice):  # noqa: F811
    call = _start(client, voice)
    fake = "sk-" + secrets.token_hex(24)
    card = _card()
    card["body"] = {**card["body"], "question": f"use {fake}"}

    response = client.post(_path(call.session_id), json={"card": card},
                           headers=_grant_headers(call))

    assert response.status_code == 422
    assert fake not in response.text
    assert _cards(db) == {}


# ---- the person's own path --------------------------------------------------


def test_the_person_draws_in_their_own_live_session(client, db, voice):  # noqa: F811
    call = _start(client, voice)

    response = client.post(_path(call.session_id), json={"card": _card()},
                           headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["card"]["session_id"] == call.session_id


def test_another_persons_session_is_404_and_a_stranger_is_403(client, db, voice):  # noqa: F811
    call = _start(client, voice)

    assert client.post(_path(call.session_id), json={"card": _card()},
                       headers=auth_header("root")).status_code == 404
    assert client.post(_path(call.session_id), json={"card": _card()},
                       headers=auth_header("carol")).status_code == 403
    assert _cards(db) == {}


def test_an_ended_session_takes_no_card(client, db, voice):  # noqa: F811
    call = _start(client, voice)
    assert client.post(f"/v1/voice/sessions/{call.session_id}/end",
                       headers=auth_header("alice")).status_code == 200

    response = client.post(_path(call.session_id), json={"card": _card()},
                           headers=auth_header("alice"))

    assert response.status_code == 409
    assert _cards(db) == {}


# ---- the store validates ----------------------------------------------------


def _put(db, project: str, **over):
    store = vstore.VoiceStore(db, now=lambda: datetime.now(timezone.utc))
    db.collection(vs.PROJECTS).document(project).set(
        {"tenant_id": "eng", "person": "alice@example.com", "title": "T"})
    card = _card(**over)
    return store.put_card(
        tenant_id="eng", person="alice@example.com", project_id=project,
        card_id="vc_" + secrets.token_hex(16), kind=card.pop("kind"), body=card)


def test_the_store_refuses_a_card_of_an_unknown_kind_itself(db):
    project = "vp_" + secrets.token_hex(16)

    with pytest.raises(ValidationFailed) as refused:
        _put(db, project, kind="banner")

    assert refused.value.detail == {"field": "kind"}
    assert _cards(db) == {}


def test_the_store_refuses_a_field_the_kind_does_not_carry(db):
    project = "vp_" + secrets.token_hex(16)

    with pytest.raises(ValidationFailed):
        _put(db, project, body={**_card()["body"], "script": "alert(1)"})

    assert _cards(db) == {}


def test_the_store_keeps_a_valid_card(db):
    stored = _put(db, "vp_" + secrets.token_hex(16))

    assert stored["kind"] == "proposal" and stored["version"] == 1
    assert stored["body"]["body"]["options"][0]["label"] == "CSV"
