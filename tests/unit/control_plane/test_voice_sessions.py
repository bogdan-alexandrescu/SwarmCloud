"""Voice it session routes (docs/design/voice-it.md §10 phase 1).

Anyone not on the allowed list is 403, a second live session is 409, ending
marks the session ended and tells swarm-voice, and another tenant's session
is 404. swarm-voice is a fake on `app.state.voice_client`; no network.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

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

from .conftest import NoForgeTokens, OfflineSecretMetadata, api_settings, auth_header

OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=-\r\n"


class FakeVoice:
    """swarm-voice's internal start and end calls, recorded."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.starts: list[vs.StartCall] = []
        self.ends: list[tuple[str, str]] = []

    def start(self, call: vs.StartCall) -> str:
        self.starts.append(call)
        if self.refuse:
            raise vs.VoiceUnreachable("swarm-voice refused the session (HTTP 500); it was ended.")
        return ANSWER

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
    return client.post("/v1/voice/sessions", json={"offer": OFFER, **body},
                       headers=auth_header(user))


def _end(client: TestClient, user: str, project_id: str, session_id: str):
    return client.post(f"/v1/voice/projects/{project_id}/sessions/{session_id}/end",
                       headers=auth_header(user))


def test_anyone_not_on_the_list_is_403_and_nothing_is_written(client, db, voice):
    response = _start(client, "carol")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "voice_not_allowed"
    assert _docs(db, vs.PROJECTS) == {}
    assert _docs(db, vs.LIVE_MARKERS) == {}
    assert voice.starts == []


def test_ending_is_403_for_anyone_not_on_the_list(client, db, voice):
    started = _start(client, "alice").json()

    response = _end(client, "carol", started["project_id"], started["session_id"])

    assert response.status_code == 403
    assert voice.ends == []


def test_a_new_talk_gets_an_untitled_project_and_the_answer_comes_back(client, db, voice):
    response = _start(client, "alice")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == ANSWER
    assert body["state"] == vs.LIVE and body["new_project"] is True
    project = db.docs[f"{vs.PROJECTS}/{body['project_id']}"]
    assert project["title"] == vs.UNTITLED
    assert project["tenant_id"] == "eng" and project["person"] == "alice@example.com"
    session = db.docs[f"{vs.PROJECTS}/{body['project_id']}/{vs.SESSIONS}/{body['session_id']}"]
    assert session["state"] == vs.LIVE and session["tenant_id"] == "eng"
    # The offer went to swarm-voice unchanged, with the grant whose hash
    # alone is stored; the grant is never returned to the browser.
    (call,) = voice.starts
    assert call.offer == OFFER and call.tenant_id == "eng"
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
    assert response.json()["code"] == "voice_session_live"
    assert response.json()["detail"]["session_id"] == first["session_id"]
    assert len(_sessions(db)) == 1
    assert len(voice.starts) == 1


def test_a_second_session_on_the_same_project_is_409_too(client, db, voice):
    first = _start(client, "alice").json()

    response = _start(client, "alice", project_id=first["project_id"])

    assert response.status_code == 409


def test_ending_marks_it_ended_and_tells_swarm_voice(client, db, voice):
    started = _start(client, "alice").json()

    response = _end(client, "alice", started["project_id"], started["session_id"])

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
    _end(client, "alice", started["project_id"], started["session_id"])

    again = _end(client, "alice", started["project_id"], started["session_id"])

    assert again.status_code == 200 and again.json()["changed"] is False
    assert len(voice.ends) == 1


def test_after_ending_the_next_talk_resumes_the_project(client, db, voice):
    first = _start(client, "alice").json()
    _end(client, "alice", first["project_id"], first["session_id"])

    second = _start(client, "alice", project_id=first["project_id"])

    assert second.status_code == 200, second.text
    assert second.json()["project_id"] == first["project_id"]
    assert second.json()["new_project"] is False
    assert len(_sessions(db)) == 2


def test_another_tenants_session_is_404(client, db, voice):
    started = _start(client, "alice").json()

    response = _end(client, "bob", started["project_id"], started["session_id"])

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


def test_a_malformed_offer_is_422(client, db, voice):
    response = client.post("/v1/voice/sessions", json={"offer": "hello"},
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
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - the stdlib's name
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                         "body": json.loads(self.rfile.read(length))})
            payload = json.dumps({"answer": ANSWER} if self.path == "/internal/sessions" else {})
            self.send_response(state["status"])
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
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng",
                        person="alice@example.com", offer=OFFER, grant=grant)

    assert client.start(call) == ANSWER
    assert client.end("vs_1", cause=vs.END_BY_PERSON) is True
    assert audiences == [url, url]
    start, end = seen
    assert start["path"] == "/internal/sessions"
    assert start["authorization"] == f"Bearer {bearer}"
    assert start["body"] == {"session_id": "vs_1", "project_id": "vp_1", "tenant_id": "eng",
                             "person": "alice@example.com", "offer": OFFER, "grant": grant}
    assert end["path"] == "/internal/sessions/vs_1/end"


def test_the_http_client_reads_a_refusal_as_unreachable(swarm_voice):
    url, _, state = swarm_voice
    state["status"] = 500
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng",
                        person="alice@example.com", offer=OFFER, grant="g")

    with pytest.raises(vs.VoiceUnreachable):
        client.start(call)
    assert client.end("vs_1", cause=vs.END_BY_PERSON) is False


def test_no_url_means_no_client():
    assert vs.client_for(api_settings()) is None
    assert isinstance(vs.client_for(api_settings(voice_url="http://v:8080")), vs.HttpVoiceClient)
