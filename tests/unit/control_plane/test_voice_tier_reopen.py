"""A spent Live key reopens on the next tier (docs/design/llm-providers.md §5.3;
swarm_api/voice/sessions.py THE REOPEN, routes/voice.py `tier_reopen`).

swarm-voice says which tier it holds through the session grant; swarm-api
records the hold and streams `voice.tier.reopen` (the tier's name and nothing
else); the console's second start of the SAME session is accepted only while
a reopen is held, and only for that session's own person; anything else is
409 as a second start always was. swarm-voice is a fake; no network.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore
from swarm_api.voice import stream as vstream

from .conftest import auth_header
from .test_voice_grant import _build, _grant_headers, _start
from .test_voice_sessions import ANSWER, FakeVoice

OFFER_2 = "v=0\r\no=- 5 6 IN IP4 127.0.0.1\r\ns=-\r\n"


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice,
                  voice_people=("alice@example.com", "bob@example.com", "root@example.com"))


def _session(db, call: vs.StartCall) -> dict:
    return db.docs[f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}"]


def _events(db, call: vs.StartCall) -> list[dict]:
    prefix = f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}/{vstore.EVENTS}/"
    return [row for path, row in sorted(db.docs.items()) if path.startswith(prefix)]


def _tell(client: TestClient, call: vs.StartCall, tier: str = "voice_dedicated", **headers):
    return client.post(f"/v1/voice/sessions/{call.session_id}/reopen", json={"tier": tier},
                       headers={**_grant_headers(call), **headers})


def _again(client: TestClient, call: vs.StartCall, user: str = "alice"):
    return client.post("/v1/voice/sessions", json={"offer_sdp": OFFER_2, "session_id": call.session_id},
                       headers=auth_header(user))


# ---- swarm-voice tells, the stream carries ------------------------------------


def test_swarm_voices_held_tier_is_recorded_and_streamed_as_its_name_alone(client, db, voice):
    call = _start(client, voice)

    response = _tell(client, call)

    assert response.status_code == 200, response.text
    assert response.json() == {"session_id": call.session_id, "tier": "voice_dedicated"}
    assert _session(db, call)["reopen"]["tier"] == "voice_dedicated"
    (event,) = [e for e in _events(db, call) if e["type"] == vs.REOPEN_EVENT]
    text = vstream.frame(event, session_id=call.session_id)
    assert text is not None and text.startswith(f"id: {event['seq']}\nevent: voice.tier.reopen\n")
    assert '"tier":"voice_dedicated"' in text
    assert "alice@example.com" not in text and "eng" not in text.split("data:")[1]
    assert call.grant not in text and call.grant not in response.text


def test_the_stream_sends_a_reopen_as_its_tier_and_nothing_a_writer_added():
    stray = "sk-" + secrets.token_hex(12)
    event = {"type": vs.REOPEN_EVENT, "seq": 7, "tier": "api_key", "key": stray,
             "tenant_id": "eng", "person": "alice@example.com"}

    text = vstream.frame(event, session_id="vs_" + secrets.token_hex(16))

    assert vs.REOPEN_EVENT in vstream.EVENT_TYPES
    assert text is not None and stray not in text and "person" not in text and "tenant" not in text
    assert '"tier":"api_key"' in text
    assert vstream.frame({**event, "tier": stray}, session_id="vs_x" + "0" * 31) is None


def test_only_swarm_voices_grant_for_its_own_session_may_tell_a_tier(client, db, voice):
    call = _start(client, voice)

    as_person = client.post(f"/v1/voice/sessions/{call.session_id}/reopen", json={"tier": "plan"},
                            headers=auth_header("alice"))
    assert as_person.status_code == 403, as_person.text
    other = vs.StartCall(**{**call.__dict__, "session_id": "vs_" + secrets.token_hex(16)})
    elsewhere = client.post(f"/v1/voice/sessions/{other.session_id}/reopen", json={"tier": "plan"},
                            headers=_grant_headers(call))
    assert elsewhere.status_code == 403, elsewhere.text
    assert _tell(client, call, tier="sk-" + secrets.token_hex(8)).status_code == 422
    assert "reopen" not in _session(db, call)
    assert not [e for e in _events(db, call) if e["type"] == vs.REOPEN_EVENT]


# ---- the second start ------------------------------------------------------------


def test_a_second_start_while_a_reopen_is_held_forwards_the_fresh_offer_as_the_reopen(client, db, voice):
    call = _start(client, voice)
    assert _tell(client, call).status_code == 200

    response = _again(client, call)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == call.session_id and body["project_id"] == call.project_id
    assert body["answer_sdp"] == ANSWER and body["mode"] == vs.MODE_LIVE and body["tier"] == "voice_dedicated"
    reopen = voice.starts[-1]
    assert len(voice.starts) == 2 and reopen.reopen is True
    assert reopen.session_id == call.session_id and reopen.offer == OFFER_2 and reopen.grant == ""
    session = _session(db, call)
    assert session["state"] == vs.LIVE and session["reopen"] is None and session["tier"] == "voice_dedicated"
    # The hold is spent: the same start again is the second start it always was.
    again = _again(client, call)
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    assert len(voice.starts) == 2


def test_a_second_start_with_no_reopen_held_is_refused_as_today(client, db, voice):
    call = _start(client, voice)

    response = _again(client, call)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert len(voice.starts) == 1 and _session(db, call)["state"] == vs.LIVE


def test_a_held_reopen_older_than_the_hold_is_refused(client, db, voice):
    call = _start(client, voice)
    assert _tell(client, call).status_code == 200
    held = _session(db, call)["reopen"]
    held["at"] = held["at"] - timedelta(seconds=vs.REOPEN_HOLD_SECONDS + 1)

    assert _again(client, call).status_code == 409
    assert len(voice.starts) == 1


def test_a_held_reopen_is_its_own_persons_only(client, db, voice):
    call = _start(client, voice)
    assert _tell(client, call).status_code == 200

    for user in ("bob", "root"):
        response = _again(client, call, user=user)
        assert response.status_code == 404, (user, response.text)
    assert len(voice.starts) == 1
    assert _session(db, call)["reopen"]["tier"] == "voice_dedicated"


def test_a_reopen_swarm_voice_refuses_ends_the_session_and_tells_swarm_voice(client, db, voice):
    call = _start(client, voice)
    assert _tell(client, call).status_code == 200
    voice.refuse = True

    response = _again(client, call)

    assert response.status_code == 502, response.text
    session = _session(db, call)
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_REOPEN_FAILED
    assert session["grant_hash"] is None
    assert voice.ends == [(call.session_id, vs.END_REOPEN_FAILED)]


def test_a_reopen_names_its_session_and_no_project(client, db, voice):
    call = _start(client, voice)
    response = client.post("/v1/voice/sessions",
                           json={"offer_sdp": OFFER_2, "session_id": call.session_id,
                                 "project_id": call.project_id},
                           headers=auth_header("alice"))
    assert response.status_code == 422


def test_the_http_client_sends_a_reopen_as_the_offer_alone(monkeypatch):
    sent: list[tuple[str, dict]] = []
    client = vs.HttpVoiceClient("http://voice.internal:8080", token=lambda aud, t: secrets.token_hex(8))
    session_id = "vs_" + secrets.token_hex(16)

    def post(path: str, body: dict, timeout: float):
        sent.append((path, body))
        return 201, (b'{"voice_session_id": "%s", "live_session_id": "live_2", "answer": "%s"}'
                     % (session_id.encode(), ANSWER.replace("\r\n", "\\r\\n").encode()))

    monkeypatch.setattr(client, "_post", post)
    call = vs.StartCall(session_id=session_id, project_id="vp_" + secrets.token_hex(16), tenant_id="eng",
                        person="alice@example.com", offer=OFFER_2, grant="", reopen=True)

    assert client.start(call) == vs.StartAnswer(answer=ANSWER, live_session_id="live_2")
    ((path, body),) = sent
    assert path == "/internal/sessions"
    assert body == {"voice_session_id": session_id, "person": "alice@example.com", "offer": OFFER_2}
    from swarm_voice.main import StartRequest, session_grant
    # swarm-voice reads it as a Live start with no grant: its reopen keeps the spent session's.
    assert session_grant(StartRequest(**body)) is None
