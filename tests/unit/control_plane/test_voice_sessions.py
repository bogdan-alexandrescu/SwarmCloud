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
from datetime import datetime, timedelta, timezone
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
from swarm_api.voice import settings as vsettings
from swarm_api.voice import speech_catalogue as cat
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
    gpt_live_settings(db)
    return TestClient(app, raise_server_exceptions=False)


def gpt_live_settings(db) -> None:
    """Voice it › Settings name GPT-Live: these tests pin the Live path, and
    Start takes its engine from Settings alone (own speech by default)."""
    db.docs.setdefault(f"{vsettings.COLLECTION}/{vsettings.DOCUMENT}",
                       {"engine": cat.ENGINE_GPT_LIVE})


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


def _session(db, started) -> dict:
    return db.docs[f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"]


def test_a_closed_tabs_end_records_tab_closed(client, db, voice):
    # The holder's pagehide end (apps/swarm-ui/src/voice/session.tsx `endOnLeave`).
    started = _start(client, "alice").json()

    response = client.post(f"/v1/voice/sessions/{started['session_id']}/end",
                           json={"cause": "tab_closed"}, headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True
    assert vs.END_TAB_CLOSED == "tab_closed"
    session = _session(db, started)
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_TAB_CLOSED
    assert voice.ends == [(started["session_id"], vs.END_TAB_CLOSED)]
    ended = [row for row in _docs(db, vs.AUDIT + "/").values() if row["action"] == vs.AUDIT_ENDED]
    assert [row["detail"] for row in ended] == [{"cause": vs.END_TAB_CLOSED}]


def test_end_named_by_the_person_records_ended_by_person(client, db, voice):
    # End, Escape on Stop and the spoken stop name their cause, or send none.
    started = _start(client, "alice").json()

    response = client.post(f"/v1/voice/sessions/{started['session_id']}/end",
                           json={"cause": "ended_by_person"}, headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert _session(db, started)["end_cause"] == vs.END_BY_PERSON
    assert voice.ends == [(started["session_id"], vs.END_BY_PERSON)]


@pytest.mark.parametrize("cause", ["abandoned", "budget_cap", "idle", "paused_idle",
                                   "voice_unreachable", "TAB_CLOSED", "", "anything"])
def test_a_cause_the_person_may_not_name_is_422_and_ends_nothing(client, db, voice, cause):
    started = _start(client, "alice").json()

    response = client.post(f"/v1/voice/sessions/{started['session_id']}/end",
                           json={"cause": cause}, headers=auth_header("alice"))

    assert response.status_code == 422, response.text
    assert _session(db, started)["state"] == vs.LIVE
    assert voice.ends == []


def test_an_end_body_with_another_field_is_422(client, db, voice):
    started = _start(client, "alice").json()

    response = client.post(f"/v1/voice/sessions/{started['session_id']}/end",
                           json={"cause": "tab_closed", "person": "bob@example.com"},
                           headers=auth_header("alice"))

    assert response.status_code == 422, response.text
    assert _session(db, started)["state"] == vs.LIVE


def test_the_service_refuses_a_cause_the_person_may_not_name(client, db, voice):
    started = _start(client, "alice").json()
    service = vs.VoiceSessions(db, voice, now=lambda: datetime.now(timezone.utc))

    with pytest.raises(ValueError):
        service.end(tenant_id="eng", person="alice@example.com",
                    session_id=started["session_id"], cause=vs.END_ABANDONED)

    assert vs.PERSON_END_CAUSES == (vs.END_BY_PERSON, vs.END_TAB_CLOSED)
    assert _session(db, started)["state"] == vs.LIVE
    assert voice.ends == []


def test_another_persons_session_in_the_same_workspace_is_404_with_tab_closed(
        db, tokens, group_map, objects, voice):
    # root shares alice's workspace (eng); both are allowed.
    client = _client(db, tokens, group_map, objects, voice,
                     voice_url="http://voice.internal:8080",
                     voice_people=("alice@example.com", "root@example.com"))
    started = _start(client, "alice").json()

    response = client.post(f"/v1/voice/sessions/{started['session_id']}/end",
                           json={"cause": "tab_closed"},
                           headers={**auth_header("root"), "X-Swarm-Tenant": "eng"})

    assert response.status_code == 404, response.text
    assert _session(db, started)["state"] == vs.LIVE
    assert voice.ends == []


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
     409, "openai_key_in_use"),
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
    assert body["message"] == (vs.NO_API_KEY_SENTENCE if reason == "voice_pool_refused" and status == 409
                               else sentence)
    assert body["detail"]["reason"] == reason
    assert body["code"] == ("forbidden" if status == 403 else "conflict")
    (session,) = _sessions(db).values()
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_VOICE_POOL_REFUSED
    assert _docs(db, vs.LIVE_MARKERS) == {}
    voice.pool_refusal = None
    assert _start(client, "alice").status_code == 200


def test_a_start_with_no_api_key_in_the_pool_says_so_and_never_502s(client, db, voice):
    """swarm-voice's 409 `voice_pool_refused` is the broker finding no
    Live-capable account (an OpenAI API key) in the Voice it pool: the person
    is told where to add one, whatever older sentence swarm-voice sent."""
    voice.pool_refusal = vs.pool_refusal(
        409, b'{"code": "voice_pool_refused", "message": "Voice it has no ChatGPT plan account set up yet."}')

    response = _start(client, "alice")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["message"] == vs.NO_API_KEY_SENTENCE
    assert body["message"] == (
        "Voice it needs an OpenAI API key in the Voice it pool: add one on Admin \u203a Accounts")
    assert body["detail"]["reason"] == "voice_pool_refused"
    (session,) = _sessions(db).values()
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_VOICE_POOL_REFUSED


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
                      "mode": "text"} if body.get("mode") == "text"
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
     "openai_key_in_use"),
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
    if status == 409 and reason == "voice_pool_refused":
        sentence = vs.NO_API_KEY_SENTENCE
    assert raised.value.message == sentence
    assert raised.value.detail == {"reason": reason}
    assert "credential" not in seen[0]["body"]


@pytest.mark.parametrize("sent", [
    # An older swarm-voice sent the Live sentence for a text start too.
    {"code": "voice_pool_busy", "message":
     "Voice it's OpenAI API key is in use by another session; try again when it ends."},
    {"code": "voice_pool_busy", "message": "No Voice it plan account is free.",
     "reason": "no_voice_account_available", "capability": "text"},
], ids=["older-swarm-voice", "this-swarm-voice"])
def test_a_text_start_with_no_plan_free_says_so_and_never_blames_the_openai_key(swarm_voice, sent):
    url, _, state = swarm_voice
    state["status"], state["refusal"] = 409, sent
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    call = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="bob@example.com",
                        offer="", grant="g", mode=vs.MODE_TEXT)

    with pytest.raises(vs.VoicePoolBusy) as raised:
        client.start(call)
    assert raised.value.detail == {"reason": vs.NO_PLAN_FREE}
    assert raised.value.message == vs.NO_PLAN_SENTENCE
    assert "OpenAI" not in raised.value.message and "another session" not in raised.value.message


def test_only_a_live_start_whose_key_is_held_says_the_key_is_in_use(swarm_voice):
    url, _, state = swarm_voice
    state["status"], state["refusal"] = 409, {"code": "voice_pool_busy", "message": ""}
    client = vs.HttpVoiceClient(url, audience="swarm-voice", token=lambda a, t: "x")
    live = vs.StartCall(session_id="vs_1", project_id="vp_1", tenant_id="eng", person="bob@example.com",
                        offer=OFFER, grant="g")

    with pytest.raises(vs.VoicePoolBusy) as raised:
        client.start(live)
    assert raised.value.detail == {"reason": vs.OPENAI_KEY_IN_USE}
    assert raised.value.message == vs.KEY_IN_USE_SENTENCE


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


# --------------------------------------------------------------------------
# The session's models: Voice it › Settings, read at the start
# --------------------------------------------------------------------------

#: Terraform's `local.runner_models`, as `model_class_env` hands it to swarm-api.
CLASS_PINS = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1", "sonnet": "claude-sonnet-5-5",
              "haiku": "claude-haiku-5-5"}


def _voice_settings(db, **fields) -> None:
    from swarm_api.voice import settings as vsettings
    db.collection(vsettings.COLLECTION).document(vsettings.DOCUMENT).set(
        {"version": 1, "engine": cat.ENGINE_GPT_LIVE, **fields})


@pytest.fixture
def pinned(db, tokens, group_map, objects, voice) -> TestClient:
    """`client`, on a deployment whose settings pin the classes to CLASS_PINS."""
    return _client(db, tokens, group_map, objects, voice,
                   voice_url="http://voice.internal:8080",
                   voice_people=("alice@example.com", "bob@example.com"),
                   worker_class_models=dict(CLASS_PINS))


def test_the_start_sends_the_settings_planner_and_conversation_models(pinned, db, voice):
    """the owner, 6 November 2026 ('wire-in-follow-up', 'front-reads-it'): the
    planner class's pin and the conversation model's id, read at the start.
    A change reaches the next session's start, never the one already started."""
    client = pinned
    _voice_settings(db, planner_model="opus", conversation_model="claude-sonnet-5")
    first = _start(client, "alice")
    assert first.status_code == 200, first.text
    _voice_settings(db, planner_model="sonnet", conversation_model="claude-haiku-5-5")
    assert _end(client, "alice", first.json()["session_id"]).status_code == 200
    second = _start(client, "alice")
    assert second.status_code == 200, second.text

    assert [(c.planner_model, c.conversation_model) for c in voice.starts] == [
        ("claude-opus-5-5", "claude-sonnet-5"), ("claude-sonnet-5-5", "claude-haiku-5-5")]


def test_unset_or_unlisted_models_are_none_so_swarm_voice_keeps_its_pins(pinned, db, voice):
    client = pinned
    first = _start(client, "alice")  # no settings document at all
    assert first.status_code == 200, first.text
    assert _end(client, "alice", first.json()["session_id"]).status_code == 200
    _voice_settings(db, planner_model="mythos", conversation_model="gpt-4o")
    second = _start(client, "alice")
    assert second.status_code == 200, second.text

    assert [(c.planner_model, c.conversation_model) for c in voice.starts] == [(None, None), (None, None)]


def test_the_planner_pin_is_swarm_apis_own_class_models_not_the_environment(
        db, tokens, group_map, objects, voice, monkeypatch):
    """the owner, 6 November 2026: the voice route builds its sessions with
    `Settings.worker_class_models`, the map every other swarm-api caller
    reads, never a second read of WORKER_CLASS_MODELS. The environment here
    pins `opus` to one model and the settings to another: the settings' wins."""
    import json

    monkeypatch.setenv("WORKER_CLASS_MODELS", json.dumps(CLASS_PINS))
    settings_pins = {**CLASS_PINS, "opus": "claude-opus-5"}
    client = _client(db, tokens, group_map, objects, voice,
                     voice_url="http://voice.internal:8080",
                     voice_people=("alice@example.com",),
                     worker_class_models=settings_pins)
    _voice_settings(db, planner_model="opus")

    started = _start(client, "alice")

    assert started.status_code == 200, started.text
    assert [c.planner_model for c in voice.starts] == ["claude-opus-5"]


@pytest.mark.parametrize("mode", [vs.MODE_LIVE, vs.MODE_TEXT])
def test_the_http_client_sends_the_models_when_set_and_omits_them_when_not(swarm_voice, mode):
    from swarm_voice.main import StartRequest

    url, seen, _ = swarm_voice
    client = vs.HttpVoiceClient(url, token=lambda audience, timeout: secrets.token_hex(16))
    for models in ({"planner_model": "claude-opus-5-5", "conversation_model": "claude-sonnet-5"}, {}):
        call = vs.StartCall(session_id="vs_" + secrets.token_hex(16), project_id="vp_" + secrets.token_hex(16),
                            tenant_id="eng", person="alice@example.com",
                            offer=OFFER if mode == vs.MODE_LIVE else "", grant=secrets.token_urlsafe(32),
                            mode=mode, **models)
        client.start(call)

    sent, bare = (s["body"] for s in seen)
    assert sent["planner_model"] == "claude-opus-5-5"
    assert sent["conversation_model"] == "claude-sonnet-5"
    assert "planner_model" not in bare and "conversation_model" not in bare
    # Otherwise the body is today's, field for field.
    assert set(sent) - set(bare) == {"planner_model", "conversation_model"} and set(bare) <= set(sent)
    assert set(sent) <= set(StartRequest.model_fields)
