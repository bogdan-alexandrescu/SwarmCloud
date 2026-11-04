"""Voice it session routes (docs/design/voice-it.md §10 phase 1).

Anyone not on the allowed list is 403, a second live session is 409, ending
marks the session ended and tells swarm-voice, and another tenant's session
is 404. The body and paths are the Talk pane's (apps/swarm-ui/src/voice/rtc.ts):
`offer_sdp` in, `answer_sdp` out, and ending by the session id alone.
swarm-voice is a fake on `app.state.voice_client`; no network.

The start call swarm-api sends carries no credential: swarm-voice leases the
ChatGPT plan account from the quota broker's Voice it pool itself (owner,
2026-11-02), so the body is {voice_session_id, person, offer, project_id,
grant}. swarm-voice's pool refusals (403, 409 `voice_pool_busy` /
`voice_pool_refused`) reach the browser with swarm-voice's own sentence.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.settings import ApiSettings
from swarm_api.voice import sessions as vs
from swarm_api.waker import NullWaker

from .conftest import (
    ENG_GROUP,
    RESEARCH_GROUP,
    NoForgeTokens,
    OfflineSecretMetadata,
    api_settings,
    auth_header,
)

# swarm-voice is not a member of the root uv workspace; its package is put on
# the path the way tests/unit/voice/test_live_session.py reaches it.
_VOICE = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _VOICE not in sys.path:
    sys.path.insert(0, _VOICE)


OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=-\r\n"
#: What the start call to swarm-voice carries, and nothing else.
START_FIELDS = {"voice_session_id", "project_id", "person", "offer", "grant"}


class FakeVoice:
    """swarm-voice's internal start and end calls, recorded."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        #: An exception the next starts raise (a pool refusal), when set.
        self.pool_refusal: Exception | None = None
        self.starts: list[vs.StartCall] = []
        self.ends: list[tuple[str, str]] = []

    def start(self, call: vs.StartCall) -> vs.StartAnswer:
        self.starts.append(call)
        if self.pool_refusal is not None:
            raise self.pool_refusal
        if self.refuse:
            raise vs.VoiceUnreachable("swarm-voice refused the session (HTTP 500); it was ended.")
        return vs.StartAnswer(answer=ANSWER, live_session_id="live_" + call.session_id[3:13])

    def end(self, session_id: str, *, cause: str) -> bool:
        self.ends.append((session_id, cause))
        return True


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


def _client(db, tokens, group_map, objects, voice, **settings) -> TestClient:
    ctx = build_context(
        settings=api_settings(**settings),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(ctx)
    app.state.secret_metadata = OfflineSecretMetadata()
    app.state.voice_client = voice
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    # alice is in eng, bob in research: both allowed, so tenancy is what
    # separates them. carol is not on the list.
    return _client(db, tokens, group_map, objects, voice,
                   voice_url="http://voice.internal:8080",
                   voice_people=("alice@example.com", "bob@example.com"))


def _docs(db, prefix: str) -> dict[str, dict]:
    return {path: data for path, data in db.docs.items() if path.startswith(prefix)}


def _sessions(db) -> dict[str, dict]:
    return {path: data for path, data in db.docs.items()
            if path.startswith(vs.PROJECTS + "/") and f"/{vs.SESSIONS}/" in path}


def _start(client: TestClient, user: str, **body):
    return client.post("/v1/voice/sessions", json={"offer_sdp": OFFER, **body},
                       headers=auth_header(user))


def _end(client: TestClient, user: str, session_id: str):
    return client.post(f"/v1/voice/sessions/{session_id}/end",
                       headers=auth_header(user))


def test_anyone_not_on_the_list_is_403_and_nothing_is_written(client, db, voice):
    response = _start(client, "carol")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert "Voice it" in response.json()["message"]
    assert _docs(db, vs.PROJECTS) == {}
    assert _docs(db, vs.LIVE_MARKERS) == {}
    assert voice.starts == []


def test_ending_is_403_for_anyone_not_on_the_list(client, db, voice):
    started = _start(client, "alice").json()

    response = _end(client, "carol", started["session_id"])

    assert response.status_code == 403
    assert voice.ends == []


def test_a_new_talk_gets_an_untitled_project_and_the_answer_comes_back(client, db, voice):
    response = _start(client, "alice")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer_sdp"] == ANSWER and "answer" not in body
    assert body["state"] == vs.LIVE and body["new_project"] is True
    project = db.docs[f"{vs.PROJECTS}/{body['project_id']}"]
    assert project["title"] == vs.UNTITLED
    assert project["tenant_id"] == "eng" and project["person"] == "alice@example.com"
    session = db.docs[f"{vs.PROJECTS}/{body['project_id']}/{vs.SESSIONS}/{body['session_id']}"]
    assert session["state"] == vs.LIVE and session["tenant_id"] == "eng"
    # The offer went to swarm-voice unchanged, with the grant whose hash
    # alone is stored; the grant is never returned to the browser.
    (call,) = voice.starts
    assert call.offer == OFFER and call.tenant_id == "eng" and call.project_id == body["project_id"]
    assert not hasattr(call, "credential")
    assert session["live_session_id"] == "live_" + body["session_id"][3:13]
    assert session["grant_hash"] == hashlib.sha256(call.grant.encode()).hexdigest()
    assert call.grant not in response.text
    assert call.grant not in repr(call)
    audit = list(_docs(db, vs.AUDIT + "/").values())
    assert [row["action"] for row in audit] == [vs.AUDIT_STARTED]
    assert audit[0]["tenant_id"] == "eng" and audit[0]["by"] == "alice@example.com"


def test_a_second_live_session_is_409(client, db, voice):
    first = _start(client, "alice").json()

    response = _start(client, "alice")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert "live Voice it session" in response.json()["message"]
    assert response.json()["detail"]["session_id"] == first["session_id"]
    assert len(_sessions(db)) == 1
    assert len(voice.starts) == 1


def test_a_second_session_on_the_same_project_is_409_too(client, db, voice):
    first = _start(client, "alice").json()

    response = _start(client, "alice", project_id=first["project_id"])

    assert response.status_code == 409


def test_ending_marks_it_ended_and_tells_swarm_voice(client, db, voice):
    started = _start(client, "alice").json()

    response = _end(client, "alice", started["session_id"])

    assert response.status_code == 200, response.text
    assert response.json()["state"] == vs.ENDED and response.json()["changed"] is True
    session = db.docs[f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"]
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_BY_PERSON
    assert voice.ends == [(started["session_id"], vs.END_BY_PERSON)]
    assert _docs(db, vs.LIVE_MARKERS) == {}
    actions = sorted(row["action"] for row in _docs(db, vs.AUDIT + "/").values())
    assert actions == sorted([vs.AUDIT_STARTED, vs.AUDIT_ENDED])


def test_ending_twice_changes_nothing_the_second_time(client, db, voice):
    started = _start(client, "alice").json()
    _end(client, "alice", started["session_id"])

    again = _end(client, "alice", started["session_id"])

    assert again.status_code == 200 and again.json()["changed"] is False
    assert len(voice.ends) == 1


def test_ending_finds_the_sessions_project_among_the_persons_others(client, db, voice):
    first = _start(client, "alice").json()
    _end(client, "alice", first["session_id"])
    second = _start(client, "alice").json()
    assert second["project_id"] != first["project_id"]

    response = _end(client, "alice", second["session_id"])

    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == second["project_id"]
    assert response.json()["changed"] is True
    session = db.docs[f"{vs.PROJECTS}/{second['project_id']}/{vs.SESSIONS}/{second['session_id']}"]
    assert session["state"] == vs.ENDED


def test_ending_a_session_nobody_started_is_404(client, db, voice):
    response = _end(client, "alice", "vs_" + secrets.token_hex(16))

    assert response.status_code == 404, response.text
    assert voice.ends == []


def test_the_old_project_scoped_end_path_is_gone(client, db, voice):
    started = _start(client, "alice").json()

    response = client.post(
        f"/v1/voice/projects/{started['project_id']}/sessions/{started['session_id']}/end",
        headers=auth_header("alice"))

    assert response.status_code == 404
    assert voice.ends == []


def test_the_old_offer_field_is_422(client, db, voice):
    response = client.post("/v1/voice/sessions", json={"offer": OFFER},
                           headers=auth_header("alice"))

    assert response.status_code == 422
    assert voice.starts == []


def test_after_ending_the_next_talk_resumes_the_project(client, db, voice):
    first = _start(client, "alice").json()
    _end(client, "alice", first["session_id"])

    second = _start(client, "alice", project_id=first["project_id"])

    assert second.status_code == 200, second.text
    assert second.json()["project_id"] == first["project_id"]
    assert second.json()["new_project"] is False
    assert len(_sessions(db)) == 2


def test_another_tenants_session_is_404(client, db, voice):
    started = _start(client, "alice").json()

    response = _end(client, "bob", started["session_id"])

    assert response.status_code == 404, response.text
    session = db.docs[f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"]
    assert session["state"] == vs.LIVE
    assert voice.ends == []


def test_another_tenants_project_is_404_and_looks_like_a_missing_one(client, db, voice):
    started = _start(client, "alice").json()

    theirs = _start(client, "bob", project_id=started["project_id"])
    missing = _start(client, "bob", project_id="vp_" + secrets.token_hex(16))

    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["code"] == missing.json()["code"]
    assert len(voice.starts) == 1


@pytest.fixture
def two_workspaces(db, tokens, group_map, objects, voice) -> TestClient:
    # alice is in eng AND research, so only the workspace the console selects
    # (X-Swarm-Tenant) separates their sessions: the person check cannot.
    group_map["alice@example.com"] = (ENG_GROUP, RESEARCH_GROUP)
    return _client(db, tokens, group_map, objects, voice,
                   voice_url="http://voice.internal:8080",
                   voice_people=("alice@example.com",))


def _in(tenant: str) -> dict[str, str]:
    return {**auth_header("alice"), "X-Swarm-Tenant": tenant}


def test_the_same_person_cannot_end_their_session_from_another_workspace(two_workspaces, db, voice):
    started = two_workspaces.post("/v1/voice/sessions", json={"offer_sdp": OFFER},
                                  headers=_in("eng"))
    assert started.status_code == 200, started.text
    started = started.json()
    path = f"/v1/voice/sessions/{started['session_id']}/end"

    response = two_workspaces.post(path, headers=_in("research"))

    assert response.status_code == 404, response.text
    session = db.docs[f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"]
    assert session["state"] == vs.LIVE and session["tenant_id"] == "eng"
    assert voice.ends == []
    assert two_workspaces.post(path, headers=_in("eng")).status_code == 200


def test_the_same_person_cannot_talk_on_their_project_from_another_workspace(
        two_workspaces, db, voice):
    started = two_workspaces.post("/v1/voice/sessions", json={"offer_sdp": OFFER},
                                  headers=_in("eng")).json()
    two_workspaces.post(
        f"/v1/voice/sessions/{started['session_id']}/end",
        headers=_in("eng"))

    response = two_workspaces.post(
        "/v1/voice/sessions", json={"offer_sdp": OFFER, "project_id": started["project_id"]},
        headers=_in("research"))

    assert response.status_code == 404, response.text
    assert len(voice.starts) == 1


def test_503_when_swarm_voice_is_not_deployed(db, tokens, group_map, objects):
    client = _client(db, tokens, group_map, objects, None,
                     voice_people=("alice@example.com",))

    response = _start(client, "alice")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "voice_unavailable"
    assert "SWARM_VOICE_URL" in response.json()["message"]
    assert _docs(db, vs.PROJECTS) == {}


def test_a_start_swarm_voice_refuses_ends_the_session_and_frees_the_person(
        db, tokens, group_map, objects):
    refusing = FakeVoice(refuse=True)
    client = _client(db, tokens, group_map, objects, refusing,
                     voice_url="http://voice.internal:8080",
                     voice_people=("alice@example.com",))

    response = _start(client, "alice")

    assert response.status_code == 502, response.text
    (session,) = _sessions(db).values()
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_VOICE_UNREACHABLE
    assert _docs(db, vs.LIVE_MARKERS) == {}
    refusing.refuse = False
    assert _start(client, "alice").status_code == 200


def test_a_session_left_starting_by_a_crash_stops_holding_the_person(client, db, voice):
    started = _start(client, "alice").json()
    path = f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"
    db.docs[path]["state"] = vs.STARTING
    db.docs[path]["created_at"] -= timedelta(seconds=vs.STARTING_STALE_SECONDS + 1)

    assert _start(client, "alice").status_code == 200


def test_the_start_reads_no_secret_and_sends_no_credential(client, db, voice):
    # No sign-in of anyone's is stored anywhere: the start still goes through,
    # because swarm-voice leases the Voice it pool's account itself.
    assert _start(client, "alice").status_code == 200
    (call,) = voice.starts
    assert call.person == "alice@example.com"
    assert "credential" not in vs.StartCall.__dataclass_fields__


def test_the_personal_chatgpt_sign_in_routes_are_gone(client):
    for method, path in (("GET", "/v1/voice/chatgpt"),
                         ("POST", "/v1/voice/chatgpt/authorize"),
                         ("POST", "/v1/voice/chatgpt/exchange")):
        response = client.request(method, path, headers=auth_header("alice"),
                                  json={"state": secrets.token_hex(8), "callback": "x" * 8})
        assert response.status_code == 404, (method, path, response.text)


@pytest.mark.parametrize(("refusal", "status", "reason"), [
    (lambda m: vs.pool_refusal(403, ('{"detail": "%s"}' % m).encode()), 403, "voice_pool_refused"),
    (lambda m: vs.pool_refusal(409, ('{"code": "voice_pool_busy", "message": "%s"}' % m).encode()),
     409, "voice_pool_busy"),
    (lambda m: vs.pool_refusal(409, ('{"detail": {"code": "voice_pool_refused", "message": "%s"}}'
                                     % m).encode()), 409, "voice_pool_refused"),
], ids=["403-not-in-pool", "409-busy", "409-refused"])
def test_a_pool_refusal_reaches_the_browser_with_its_sentence_and_frees_the_person(
        client, db, voice, refusal, status, reason):
    sentence = "The Voice it account is in use by another session " + secrets.token_hex(4) + "."
    voice.pool_refusal = refusal(sentence)

    response = _start(client, "alice")

    assert response.status_code == status, response.text
    body = response.json()
    assert body["message"] == sentence
    assert body["detail"]["reason"] == reason
    assert body["code"] == ("forbidden" if status == 403 else "conflict")
    (session,) = _sessions(db).values()
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_VOICE_POOL_REFUSED
    assert _docs(db, vs.LIVE_MARKERS) == {}
    voice.pool_refusal = None
    assert _start(client, "alice").status_code == 200


def test_a_malformed_offer_is_422(client, db, voice):
    response = client.post("/v1/voice/sessions", json={"offer_sdp": "hello"},
                           headers=auth_header("alice"))

    assert response.status_code == 422
    assert voice.starts == []


def test_allowed_people_default_to_the_platform_owner(monkeypatch):
    monkeypatch.delenv("VOICE_IT_PEOPLE", raising=False)
    monkeypatch.setenv("PLATFORM_OWNER", "Owner@Example.com")
    monkeypatch.setenv("SWARM_VOICE_URL", "http://voice.internal:8080")

    settings = _from_env(monkeypatch)

    assert settings.voice_people == ("owner@example.com",)
    assert settings.voice_url == "http://voice.internal:8080"


def test_allowed_people_come_from_voice_it_people(monkeypatch):
    monkeypatch.setenv("VOICE_IT_PEOPLE", "a@example.com, B@example.com")
    monkeypatch.setenv("PLATFORM_OWNER", "owner@example.com")

    assert _from_env(monkeypatch).voice_people == ("a@example.com", "b@example.com")


def test_nobody_is_allowed_with_neither_set():
    assert vs.is_allowed((), "alice@example.com") is False
    assert vs.is_allowed(("alice@example.com",), "") is False


def _from_env(monkeypatch) -> ApiSettings:
    from swarm_common.config import Settings

    from .conftest import core_settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: core_settings()))
    return ApiSettings.from_env()


# --------------------------------------------------------------------------
# HttpVoiceClient, against a loopback server standing in for swarm-voice
# --------------------------------------------------------------------------


@pytest.fixture
def swarm_voice():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict] = []
    state: dict = {"status": 201, "voice_session_id": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - the stdlib's name
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                         "body": json.loads(self.rfile.read(length))})
            body = seen[-1]["body"]
            # swarm-voice's start answer (swarm_voice/main.py, `start`); its end answers 200.
            payload = json.dumps(
                state["refusal"] if state.get("refusal") is not None and self.path == "/internal/sessions"
                else {"voice_session_id": state["voice_session_id"] or body.get("voice_session_id"),
                 "live_session_id": "live_0123456789", "answer": ANSWER}
                if self.path == "/internal/sessions" else {})
            self.send_response(state["status"] if self.path == "/internal/sessions"
                               else state.get("end_status", 200))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen, state
    server.shutdown()
    server.server_close()


def test_the_http_client_sends_the_offer_and_grant_with_an_id_token(swarm_voice):
    url, seen, _ = swarm_voice
    bearer = secrets.token_hex(16)
    audiences: list[str] = []

    def token(audience: str, timeout: float) -> str:
        audiences.append(audience)
        return bearer

    client = vs.HttpVoiceClient(url, token=token)
    grant = secrets.token_urlsafe(32)
    session_id = "vs_" + secrets.token_hex(16)
    call = vs.StartCall(session_id=session_id, project_id="vp_" + secrets.token_hex(16), tenant_id="eng",
                        person="alice@example.com", offer=OFFER, grant=grant)

    # swarm-voice answers 201 with the session's id, its live id and the SDP answer.
    assert client.start(call) == vs.StartAnswer(answer=ANSWER, live_session_id="live_0123456789")
    assert client.end(session_id, cause=vs.END_BY_PERSON) is True
    assert audiences == [url, url]
    start, end = seen
    assert start["path"] == "/internal/sessions"
    assert start["authorization"] == f"Bearer {bearer}"
    # No credential: swarm-voice leases the Voice it pool account itself.
    assert start["body"] == {"voice_session_id": session_id, "project_id": call.project_id,
                             "person": "alice@example.com", "offer": OFFER, "grant": grant}
    assert set(start["body"]) == START_FIELDS
    # Every field sent is one swarm-voice's StartRequest names.
    from swarm_voice.main import StartRequest
    assert set(start["body"]) <= set(StartRequest.model_fields)
    assert end["path"] == f"/internal/sessions/{session_id}/end"
    assert grant not in repr(call)


def test_the_http_client_reads_an_answer_for_another_session_as_unreachable(swarm_voice):
    url, _, state = swarm_voice
    state["voice_session_id"] = "vs_" + secrets.token_hex(16)
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_" + secrets.token_hex(16), project_id="vp_1", tenant_id="eng",
                        person="alice@example.com", offer=OFFER, grant=secrets.token_urlsafe(32))

    with pytest.raises(vs.VoiceUnreachable):
        client.start(call)


def test_the_http_client_reads_a_refusal_as_unreachable(swarm_voice):
    url, _, state = swarm_voice
    state["status"] = state["end_status"] = 500
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="alice@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(vs.VoiceUnreachable):
        client.start(call)
    assert client.end("vs_1", cause=vs.END_BY_PERSON) is False


@pytest.mark.parametrize(("status", "payload", "kind", "reason"), [
    (403, {"detail": "bob@example.com is not in the Voice it pool."}, vs.VoicePoolRefused,
     "voice_pool_refused"),
    (409, {"code": "voice_pool_busy", "message": "The Voice it account is busy."}, vs.VoicePoolBusy,
     "voice_pool_busy"),
    (409, {"detail": {"code": "voice_pool_refused", "message": "The broker refused the lease."}},
     vs.VoicePoolBusy, "voice_pool_refused"),
], ids=["403", "409-busy", "409-refused"])
def test_the_http_client_reads_a_pool_refusal_with_its_sentence(swarm_voice, status, payload, kind, reason):
    url, seen, state = swarm_voice
    state["status"], state["refusal"] = status, payload
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="bob@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(kind) as raised:
        client.start(call)
    sentence = payload.get("message") or payload["detail"]
    sentence = sentence["message"] if isinstance(sentence, dict) else sentence
    assert raised.value.message == sentence
    assert raised.value.detail == {"reason": reason}
    assert "credential" not in seen[0]["body"]


def test_a_409_that_names_no_pool_code_is_the_generic_refusal(swarm_voice):
    url, _, state = swarm_voice
    state["status"], state["refusal"] = 409, {"detail": "this voice session already has a live session"}
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="bob@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(vs.VoiceUnreachable):
        client.start(call)


def test_no_url_means_no_client():
    assert vs.client_for(api_settings()) is None
    assert isinstance(vs.client_for(api_settings(voice_url="http://v:8080")), vs.HttpVoiceClient)


def _openai_refused(status, code, kind) -> dict:
    """swarm-voice's own 502 body for a Live session OpenAI refused (swarm_voice/main.py)."""
    import json

    from swarm_voice.live import LiveRefused
    from swarm_voice.main import openai_refusal
    return json.loads(openai_refusal(LiveRefused("the Live session", status, code, kind)).body)


def test_the_http_client_says_what_openai_answered_when_it_refused_the_live_session(swarm_voice):
    """Measured 2026-11-04: OpenAI answered a Voice it pool account's create 403, and
    the person read only 'swarm-voice refused the session (HTTP 502)'."""
    url, _, state = swarm_voice
    state["status"], state["refusal"] = 502, _openai_refused(403, "unsupported_plan", "invalid_request_error")
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="alice@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(vs.VoiceUnreachable) as raised:
        client.start(call)
    assert raised.value.message == (
        "OpenAI refused the Live session for the Voice it account "
        "(403, unsupported_plan, invalid_request_error); the session was ended.")
    assert raised.value.detail == {"reason": "openai_refused", "openai_status": 403,
                                   "openai_code": "unsupported_plan", "openai_type": "invalid_request_error"}


@pytest.mark.parametrize(("refusal", "sentence"), [
    ({"code": "openai_refused", "openai_status": 403, "openai_code": "unreadable",
      "openai_type": "unreadable"}, "(403, unreadable)"),
    ({"code": "openai_refused", "openai_status": None, "openai_code": "plan_not_allowed",
      "openai_type": "plan_not_allowed"}, "(on the sideband, plan_not_allowed)"),
    ({"code": "openai_refused", "openai_status": "403", "openai_code": "Bearer QUOTED",
      "openai_type": "x" * 65}, "(on the sideband, unreadable)"),
], ids=["unreadable", "sideband", "held-to-the-pattern"])
def test_the_openai_refusal_sentence_holds_what_it_repeats_to_the_pattern(swarm_voice, refusal, sentence):
    url, _, state = swarm_voice
    state["status"], state["refusal"] = 502, refusal
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="alice@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(vs.VoiceUnreachable) as raised:
        client.start(call)
    assert raised.value.message == (
        f"OpenAI refused the Live session for the Voice it account {sentence}; the session was ended.")
    assert "QUOTED" not in raised.value.message and "QUOTED" not in str(raised.value.detail)


def test_a_502_that_is_not_openais_refusal_is_the_generic_one(swarm_voice):
    url, _, state = swarm_voice
    state["status"], state["refusal"] = 502, {"detail": "the Live sideband could not attach (OSError)"}
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="alice@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(vs.VoiceUnreachable) as raised:
        client.start(call)
    assert raised.value.message == "swarm-voice refused the session (HTTP 502); it was ended."


def test_the_browser_reads_openais_refusal_as_served(db, tokens, group_map, objects):
    refusing = FakeVoice()
    refusing.pool_refusal = vs.VoiceUnreachable(
        "OpenAI refused the Live session for the Voice it account (403, unsupported_plan); "
        "the session was ended.", detail={"reason": "openai_refused"})
    client = _client(db, tokens, group_map, objects, refusing,
                     voice_url="http://voice.internal:8080",
                     voice_people=("alice@example.com",))

    response = _start(client, "alice")

    assert response.status_code == 502, response.text
    assert response.json()["message"] == refusing.pool_refusal.message
    (session,) = _sessions(db).values()
    assert session["end_cause"] == vs.END_VOICE_UNREACHABLE
