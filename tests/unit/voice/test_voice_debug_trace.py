"""Voice it's debug mode, swarm-api's half (voice/settings.py `debug`,
voice/trace.py, routes/voice_trace.py; the owner, 10 November 2026: "Trace on
Telemetry").

THE DEFECT behind it (live use, 10 November 2026): the GPT voice engine told the
person "there is a card on screen" while the planner had drawn nothing, and
finding out why took reading the voice pod's logs by hand. This holds:

* the switch round-trips through Settings, default off, written by a platform
  admin only, refused 422 unless true or false, and audited like every field;
* the start body carries `debug: true` only while it is on;
* a turn swarm-voice sends through its grant is kept with every text masked,
  its refusals' reasons and its no-cards code, and goes to the card stream as
  one `trace.turn` event; a caller that is not a grant is 403;
* the reads follow Telemetry's rule: a Voice it person or an admin, the
  workspace's sessions only (another workspace's is 404), `scope=platform`
  for admins only;
* deleting the project deletes the trace.

Offline: the routers on their own app over the in-memory Firestore fake; every
fake secret is built at runtime with `secrets`.
"""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.fakes import FakeFirestore  # noqa: E402
from swarm_common.identity import Principal  # noqa: E402

from swarm_api.auth import AuthContext  # noqa: E402
from swarm_api.deps import current_auth, get_context, tenant_scope  # noqa: E402
from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.routes import voice_settings  # noqa: E402
from swarm_api.routes import voice_trace  # noqa: E402
from swarm_api.routes.accounts import account_pool  # noqa: E402
from swarm_api.voice import grant as vgrant  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import settings as vsettings  # noqa: E402
from swarm_api.voice import store as vstore  # noqa: E402
from swarm_api.voice import stream as vstream  # noqa: E402
from swarm_api.voice import trace as vtrace  # noqa: E402

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ALICE = "alice@example.com"   # a Voice it person in workspace eng, not an admin
BOB = "bob@example.com"       # a Voice it person in workspace ops
ROOT = "root@example.com"     # a platform admin
CAROL = "carol@example.com"   # neither
TENANTS = {ALICE: "eng", BOB: "ops", ROOT: "eng", CAROL: "eng"}


class FakePool:
    def voice_pool(self, person: str) -> dict[str, Any]:
        return {"account": None, "reason": vs.NO_LIVE_ACCOUNT}


def _app(db: FakeFirestore) -> TestClient:
    app = FastAPI()
    app.include_router(voice_settings.router)
    app.include_router(voice_trace.write_router)
    app.include_router(voice_trace.router)
    app.state.speech_zones = ("us-central1-a",)

    @app.exception_handler(ApiError)
    async def _error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status_code)

    ctx = SimpleNamespace(db=db, now=lambda: NOW,
                          settings=SimpleNamespace(voice_people=(ALICE, BOB)))

    def _auth(request: Request) -> AuthContext:
        email = request.headers.get("x-test-user", CAROL)
        grant = request.headers.get("x-test-grant", "")
        return AuthContext(principal=Principal(email=email, subject=email, domain="example.com"),
                           tenant_id=TENANTS[email], is_admin=email == ROOT,
                           via=vgrant.VIA if grant else "", voice_session=grant)

    def _tenant(request: Request) -> str:
        return TENANTS[request.headers.get("x-test-user", CAROL)]

    app.dependency_overrides[current_auth] = _auth
    app.dependency_overrides[get_context] = lambda: ctx
    app.dependency_overrides[account_pool] = lambda: FakePool()
    app.dependency_overrides[tenant_scope] = _tenant
    return TestClient(app)


def _as(user: str, grant: str = "") -> dict[str, str]:
    return {"x-test-user": user, **({"x-test-grant": grant} if grant else {})}


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def client(db) -> TestClient:
    return _app(db)


def _session(db: FakeFirestore, person: str = ALICE) -> tuple[str, str]:
    project = "vp_" + secrets.token_hex(16)
    session = "vs_" + secrets.token_hex(16)
    owner = {"tenant_id": TENANTS[person], "person": person}
    project_ref = db.collection(vs.PROJECTS).document(project)
    project_ref.set({**owner, "title": "Bakery", "status": "active"})
    project_ref.collection(vs.SESSIONS).document(session).set(
        {**owner, "state": vs.LIVE, "created_at": NOW - timedelta(minutes=4)})
    return project, session


def _turn(**over: Any) -> dict[str, Any]:
    turn = {"n": 1, "secs": 6.8,
            "lines": [{"who": "person", "text": "A small website for the bakery."}],
            "handoff": "Plan the bakery website.",
            "read": [{"tool": "list_agents", "target": None}],
            "drawn": [{"kind": "plan", "title": "Bakery website"},
                      {"kind": "mockup", "title": "Home page", "frames": ["Warm", "Photo first"]},
                      {"kind": "work", "title": "Publish the order form",
                       "refused": "The plan is not approved yet"}],
            "none": None, "told": "I drew a plan and a mock-up.",
            "events": {"pieces": 9, "finals": 1, "empty_finals": 0, "handoffs": 1, "barge_ins": 0}}
    return {**turn, **over}


def _body(turn: dict[str, Any]) -> dict[str, Any]:
    return {"engine": "gpt-live", "turn": turn,
            "session_events": {"pieces": 9, "finals": 1, "empty_finals": 0, "handoffs": 1,
                               "barge_ins": 0, "replies": 1}}


def _post(client: TestClient, session: str, turn: dict[str, Any], person: str = ALICE):
    return client.post(f"/v1/voice/sessions/{session}/trace", json=_body(turn),
                       headers=_as(person, grant=session))


# ---- the switch --------------------------------------------------------------


def test_the_switch_is_off_by_default_round_trips_and_is_audited(client, db):
    r = client.get("/v1/voice/settings", headers=_as(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["debug"] is False
    assert vsettings.VoiceSettings(db, now=lambda: NOW).session_debug() is False

    # Writing is a platform admin's, as for every Voice it setting.
    assert client.patch("/v1/voice/settings", json={"version": 0, "debug": True},
                        headers=_as(ALICE)).status_code == 403
    assert client.patch("/v1/voice/settings", json={"version": 0, "debug": "yes"},
                        headers=_as(ROOT)).status_code == 422

    r = client.patch("/v1/voice/settings", json={"version": 0, "debug": True}, headers=_as(ROOT))
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["debug"] is True
    assert r.json()["version"] == 1
    assert client.get("/v1/voice/settings", headers=_as(ALICE)).json()["settings"]["debug"] is True
    assert vsettings.VoiceSettings(db, now=lambda: NOW).session_debug() is True

    audits = [d for p, d in db.docs.items() if p.startswith(f"{vs.AUDIT}/")]
    assert [a["detail"]["changed"] for a in audits] == [{"debug": {"from": False, "to": True}}]
    assert audits[0]["by"] == ROOT and audits[0]["action"] == vsettings.AUDIT_CHANGED

    r = client.patch("/v1/voice/settings", json={"version": 1, "debug": False}, headers=_as(ROOT))
    assert r.json()["settings"]["debug"] is False


def test_the_start_body_carries_debug_only_while_it_is_on():
    sent: list[dict] = []

    class Client(vs.HttpVoiceClient):
        def _post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, bytes]:
            sent.append(body)
            return 503, b""

    voice = Client("https://swarm-voice.test", token=lambda _a, _t: "id-" + secrets.token_hex(4))
    grant = secrets.token_urlsafe(32)
    for debug in (False, True):
        call = vs.StartCall(session_id="vs_" + "1" * 32, project_id="vp_" + "2" * 32,
                            tenant_id="eng", person=ALICE, offer="", grant=grant,
                            mode=vs.MODE_SPEECH, debug=debug)
        with pytest.raises(ApiError):
            voice.start(call)
    # MUTATION: send `debug` whatever its value and the off body changes.
    assert "debug" not in sent[0]
    assert sent[1]["debug"] is True


# ---- the write ---------------------------------------------------------------


def test_a_turn_is_kept_masked_and_sent_to_the_stream(client, db):
    project, session = _session(db)
    key = "sk-" + secrets.token_hex(24)
    turn = _turn(lines=[{"who": "person", "text": f"my key is {key}"}], told=f"Stored {key}.")
    r = _post(client, session, turn)
    assert r.status_code == 200, r.text

    stored = [d for p, d in db.docs.items() if p.startswith(f"{vtrace.COLLECTION}/{session}/turns/")]
    assert len(stored) == 1
    # MUTATION: store the body's text as sent and the key is kept.
    assert key not in repr(stored[0])
    assert stored[0]["lines"][0]["text"].startswith("my key is sk-")
    assert stored[0]["drawn"][2] == {"kind": "work", "title": "Publish the order form",
                                     "refused": "The plan is not approved yet"}

    events = [d for p, d in db.docs.items()
              if p.startswith(f"{vs.PROJECTS}/{project}/{vs.SESSIONS}/{session}/events/")]
    assert [e["type"] for e in events] == [vtrace.TRACE_TURN]
    event = events[0]
    assert event["n"] == 1
    assert [d["title"] for d in event["drawn"]] == ["Bakery website", "Home page"]
    assert event["refused"] == [{"kind": "work", "title": "Publish the order form",
                                 "reason": "The plan is not approved yet"}]
    assert event["none"] is None
    framed = vstream.frame(event, session_id=session)
    assert framed is not None and "event: trace.turn" in framed
    assert ALICE not in framed


def test_an_empty_transcript_turn_records_its_code(client, db):
    _, session = _session(db)
    turn = _turn(lines=[], handoff=None, drawn=[], read=[], none="empty_transcript",
                 told="Sorry, I missed that.")
    assert _post(client, session, turn).status_code == 200
    answer = client.get(f"/v1/voice/trace/sessions/{session}", headers=_as(ALICE)).json()
    served = answer["turns"][0]
    assert served["none_code"] == "empty_transcript"
    assert served["none"] == vtrace.NONE_CODES["empty_transcript"]
    assert served["drawn"] == []


def test_only_a_grant_writes_and_only_to_its_own_session(client, db):
    _, session = _session(db)
    _, other = _session(db)
    r = client.post(f"/v1/voice/sessions/{session}/trace", json=_body(_turn()), headers=_as(ALICE))
    assert r.status_code == 403
    r = client.post(f"/v1/voice/sessions/{other}/trace", json=_body(_turn()),
                    headers=_as(ALICE, grant=session))
    assert r.status_code == 403
    assert _post(client, session, _turn(none="nothing_much")).status_code == 422
    assert not any(p.startswith(f"{vtrace.COLLECTION}/") for p in db.docs)


# ---- the reads ---------------------------------------------------------------


def test_the_reads_follow_the_telemetry_rule(client, db):
    _, mine = _session(db, ALICE)
    _, theirs = _session(db, BOB)
    assert _post(client, mine, _turn()).status_code == 200
    assert _post(client, theirs, _turn(), person=BOB).status_code == 200

    r = client.get(f"/v1/voice/trace/sessions/{mine}", headers=_as(ALICE))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "GPT-Live" and body["scope"] == "workspace"
    assert body["minutes"] == 4.0
    assert body["events"]["replies"] == 1
    turn = body["turns"][0]
    assert turn["lines"] == [{"who": "person", "text": ["A small website for the bakery."]}]
    assert turn["handoff"] == ["Plan the bakery website."]
    assert turn["read"] == ["list_agents"]
    assert turn["drawn"][1]["frames"] == ["Warm", "Photo first"]
    assert turn["told"] == ["I drew a plan and a mock-up."]

    listed = client.get("/v1/voice/trace/sessions", headers=_as(ALICE)).json()["sessions"]
    assert [(s["session_id"], s["turns"], s["cards"]) for s in listed] == [(mine, 1, 2)]

    # Another workspace's session is telemetry's 404; a non-Voice-it person is 403;
    # the platform view is an admin's.
    assert client.get(f"/v1/voice/trace/sessions/{theirs}", headers=_as(ALICE)).status_code == 404
    assert client.get(f"/v1/voice/trace/sessions/{mine}", headers=_as(CAROL)).status_code == 403
    assert client.get("/v1/voice/trace/sessions?scope=platform", headers=_as(ALICE)).status_code == 403
    every = client.get("/v1/voice/trace/sessions?scope=platform", headers=_as(ROOT)).json()
    assert {s["session_id"] for s in every["sessions"]} == {mine, theirs}
    assert client.get(f"/v1/voice/trace/sessions/{theirs}?scope=platform",
                      headers=_as(ROOT)).status_code == 200


def test_deleting_the_project_deletes_its_trace(client, db):
    project, session = _session(db)
    db.collection(vs.PROJECTS).document(project).collection(vs.SESSIONS).document(session).update(
        {"state": vs.ENDED})
    assert _post(client, session, _turn()).status_code == 200
    assert any(p.startswith(f"{vtrace.COLLECTION}/{session}") for p in db.docs)
    vstore.VoiceStore(db, now=lambda: NOW).delete_project(
        tenant_id="eng", person=ALICE, project_id=project, confirm="Bakery")
    # MUTATION: drop `refs_for` from the project delete and the trace outlives it.
    assert not any(p.startswith(f"{vtrace.COLLECTION}/") for p in db.docs)
