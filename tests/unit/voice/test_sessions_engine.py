"""Start picks the engine from Settings (docs/design/voice-it-own-speech.md
§4.1, §10, §14 row L6).

A session's Start uses exactly the engine Voice it › Settings name, and takes
no engine from the caller: our own speech by default (`mode: "speech"`, the
speech machine and the fast Claude front), GPT-Live when Settings say so
(`mode: "live"`), and text mode as the fallback when the speech engine cannot
start, with the fallback's reason as a fixed code. GPT-Live with no OpenAI
API key in the Voice it pool is refused with a fixed code: no other account
is tried. `VoiceSessions.delegation` is the client of swarm-voice's
`GET /delegations/{id}`.

Firestore is the in-memory fake, the speech machine runs on a fake Compute
and a fake clock, swarm-voice is a fake client (or `urlopen` a fake for the
HTTP client): no network, no cloud credential.
"""

from __future__ import annotations

import io
import json
import secrets
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from control_plane.fakes import FakeFirestore  # noqa: E402
from test_speech_machine import CONFIG, Clock, FakeCompute, _lease, _machine  # noqa: E402

from swarm_api.deps import get_context, tenant_scope  # noqa: E402
from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.routes import accounts as raccounts  # noqa: E402
from swarm_api.routes import voice as rv  # noqa: E402
from swarm_api.routes import voice_speech as rvs  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import settings as vsettings  # noqa: E402
from swarm_api.voice import speech_catalogue as cat  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402

ALICE = "alice@example.com"
TENANT = "eng"
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=-\r\n"
CHOSEN = dict(cat.defaults((CONFIG.zone,)))


class FakeVoice:
    """swarm-voice's start, turn, end and delegation, recorded. A speech start
    is refused when `front_refused` is set; nothing here is textchat."""

    def __init__(self) -> None:
        self.starts: list[vs.StartCall] = []
        self.ends: list[tuple[str, str]] = []
        self.turns: list[tuple[str, str]] = []
        self.front_refused = False
        self.asked: list[tuple[str, str, float]] = []

    def start(self, call: vs.StartCall) -> vs.StartAnswer:
        self.starts.append(call)
        if call.mode == vs.MODE_SPEECH:
            if self.front_refused:
                raise vs.SpeechFrontRefused("no Claude account for the front")
            return vs.StartAnswer(answer="", live_session_id="", mode=vs.MODE_SPEECH)
        if call.mode == vs.MODE_TEXT:
            return vs.StartAnswer(answer="", live_session_id="", mode=vs.MODE_TEXT)
        return vs.StartAnswer(answer=ANSWER, live_session_id="live_" + call.session_id[3:13])

    def end(self, session_id: str, *, cause: str) -> bool:
        self.ends.append((session_id, cause))
        return True

    def turn(self, session_id: str, text: str) -> vs.TurnAnswer:
        self.turns.append((session_id, text))
        return vs.TurnAnswer(turn_id="vf_1", reply="Looking at that now.", cards=[],
                             delegation_id="vd_1")

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        self.asked.append((session_id, delegation_id, timeout))
        return "Three steps are on screen."


class FakePool:
    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def voice_pool(self, person: str) -> dict:
        account = None if self.reason else {"label": "key", "provider": "openai-api"}
        return {"account": account, "reason": self.reason}


class Rig:
    def __init__(self, *, stockout: bool = False, running_after: int = 1) -> None:
        self.db = FakeFirestore()
        self.clock = Clock()
        self.compute = FakeCompute(stockout=stockout, running_after=running_after)
        self.voice = FakeVoice()
        self.machine = _machine(self.db, self.compute, self.clock)

    def sessions(self, *, machine: Any = "default") -> vs.VoiceSessions:
        return vs.VoiceSessions(self.db, self.voice, now=self.clock.now, class_models={},
                                machine=self.machine if machine == "default" else machine)

    def start(self, mode: str, *, fallback: str | None = None, **kwargs: Any) -> dict:
        return self.sessions(**kwargs).start(tenant_id=TENANT, person=ALICE, offer="", project_id=None,
                                             mode=mode, fallback=fallback, chosen=CHOSEN)

    def session(self, answer: dict) -> dict:
        return self.db.docs[f"{vs.PROJECTS}/{answer['project_id']}/{vs.SESSIONS}/{answer['session_id']}"]


def _settings(db: FakeFirestore, **fields: Any) -> None:
    db.docs[f"{vsettings.COLLECTION}/{vsettings.DOCUMENT}"] = dict(fields)


# ---- pick_engine: Settings' engine, no choice at Start --------------------------


def test_the_engine_is_settings_own_speech_by_default():
    pick = vs.pick_engine(CHOSEN, FakePool(""), ALICE, speech_deployed=True)
    assert CHOSEN["engine"] == cat.ENGINE_OWN
    assert (pick.engine, pick.mode, pick.fallback) == (cat.ENGINE_OWN, vs.MODE_SPEECH, None)


def test_gpt_live_when_settings_say_so():
    pick = vs.pick_engine({**CHOSEN, "engine": cat.ENGINE_GPT_LIVE}, FakePool(""), ALICE,
                          speech_deployed=True)
    assert (pick.engine, pick.mode, pick.fallback) == (cat.ENGINE_GPT_LIVE, vs.MODE_LIVE, None)


def test_own_speech_with_no_machine_deployed_is_text_with_its_reason():
    pick = vs.pick_engine(CHOSEN, FakePool(""), ALICE, speech_deployed=False)
    assert (pick.mode, pick.fallback) == (vs.MODE_TEXT, vs.FALLBACK_NOT_DEPLOYED)


def test_gpt_live_with_no_key_falls_back_to_text_with_its_reason():
    pick = vs.pick_engine({**CHOSEN, "engine": cat.ENGINE_GPT_LIVE}, FakePool(vs.NO_LIVE_ACCOUNT), ALICE,
                          speech_deployed=True)
    assert (pick.engine, pick.mode, pick.fallback) == (
        cat.ENGINE_GPT_LIVE, vs.MODE_TEXT, vs.FALLBACK_NO_OPENAI_KEY)
    assert vs.FALLBACK_FROM[pick.fallback] == cat.ENGINE_GPT_LIVE


def test_own_speech_never_asks_the_openai_pool():
    class Untouchable:
        def voice_pool(self, person: str) -> dict:
            raise AssertionError("own speech reads no OpenAI key")

    assert vs.pick_engine(CHOSEN, Untouchable(), ALICE, speech_deployed=True).mode == vs.MODE_SPEECH


# ---- the speech start -----------------------------------------------------------


def test_a_speech_start_starts_the_front_then_the_machine_and_is_live():
    rig = Rig()
    answer = rig.start(vs.MODE_SPEECH)

    assert answer["mode"] == vs.MODE_SPEECH and answer["state"] == vs.LIVE
    assert "answer_sdp" not in answer and "fallback" not in answer
    (call,) = rig.voice.starts
    assert call.mode == vs.MODE_SPEECH and call.offer == ""
    session = rig.session(answer)
    assert session["mode"] == vs.MODE_SPEECH and session["state"] == vs.LIVE
    lease = _lease(rig.db)
    assert lease["session_id"] == answer["session_id"] and lease["state"] in sm.HOLDING
    assert rig.compute.status == sm.INSTANCE_RUNNING
    assert rig.voice.ends == []


def test_a_stockout_falls_back_to_text_with_its_reason_and_tries_no_other_zone():
    rig = Rig(stockout=True)
    answer = rig.start(vs.MODE_SPEECH)

    assert answer["mode"] == vs.MODE_TEXT and answer["state"] == vs.LIVE
    assert answer["fallback"] == {"from": cat.ENGINE_OWN, "reason": sm.FALLBACK_NO_L4}
    session = rig.session(answer)
    assert session["mode"] == vs.MODE_TEXT and session["fallback"]["reason"] == "no_l4"
    # The front was told to end, then the same session started as text.
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_SPEECH, vs.MODE_TEXT]
    assert rig.voice.ends == [(answer["session_id"], sm.FALLBACK_NO_L4)]
    assert [c for c, _ in rig.compute.calls].count("start") == 1
    assert _lease(rig.db)["state"] == sm.STOPPED


def test_a_start_past_the_four_minute_wait_falls_back_to_text():
    rig = Rig(running_after=10_000)
    answer = rig.start(vs.MODE_SPEECH)

    assert answer["mode"] == vs.MODE_TEXT
    assert answer["fallback"] == {"from": cat.ENGINE_OWN, "reason": sm.FALLBACK_NOT_READY}
    assert rig.voice.ends == [(answer["session_id"], sm.FALLBACK_NOT_READY)]
    assert rig.compute.status == sm.TERMINATED


def test_a_front_refusal_falls_back_to_text_and_never_starts_the_machine():
    rig = Rig()
    rig.voice.front_refused = True
    answer = rig.start(vs.MODE_SPEECH)

    assert answer["mode"] == vs.MODE_TEXT
    assert answer["fallback"] == {"from": cat.ENGINE_OWN, "reason": vs.FALLBACK_FRONT_REFUSED}
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_SPEECH, vs.MODE_TEXT]
    assert rig.compute.calls == []


def test_a_machine_in_use_is_refused_and_frees_the_person():
    rig = Rig()
    rig.db.docs[f"{sm.COLLECTION}/{sm.LEASE}"] = {
        "session_id": "vs_" + "9" * 32, "state": sm.RUNNING, "generation": 3,
        "heartbeat_at": rig.clock.now()}
    with pytest.raises(sm.MachineInUse):
        rig.start(vs.MODE_SPEECH)
    (session,) = [d for p, d in rig.db.docs.items() if f"/{vs.SESSIONS}/" in p]
    assert session["state"] == vs.ENDED
    assert [p for p in rig.db.docs if p.startswith(vs.LIVE_MARKERS + "/")] == []
    assert len(rig.voice.ends) == 1


def test_no_machine_wired_falls_back_rather_than_failing():
    rig = Rig()
    answer = rig.start(vs.MODE_SPEECH, machine=None)
    assert answer["mode"] == vs.MODE_TEXT and answer["fallback"]["reason"] == vs.FALLBACK_NOT_DEPLOYED


def test_a_text_start_carries_the_fallback_it_was_picked_with():
    rig = Rig()
    answer = rig.start(vs.MODE_TEXT, fallback=vs.FALLBACK_NOT_DEPLOYED)
    assert answer["fallback"] == {"from": cat.ENGINE_OWN, "reason": vs.FALLBACK_NOT_DEPLOYED}
    assert rig.session(answer)["fallback"]["reason"] == vs.FALLBACK_NOT_DEPLOYED
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_TEXT]


def test_a_speech_session_stays_held_while_its_machine_starts():
    rig = Rig()
    service = rig.sessions()
    answer = rig.start(vs.MODE_SPEECH)
    path = f"{vs.PROJECTS}/{answer['project_id']}/{vs.SESSIONS}/{answer['session_id']}"
    rig.db.docs[path] = {**rig.db.docs[path], "state": vs.STARTING, "created_at": rig.clock.now()}
    rig.clock.advance(vs.STARTING_STALE_SECONDS + 30)
    with pytest.raises(vs.VoiceSessionLive):
        service.start(tenant_id=TENANT, person=ALICE, offer="", project_id=None, mode=vs.MODE_SPEECH,
                      chosen=CHOSEN)


def test_ending_a_speech_session_stops_the_machine():
    rig = Rig()
    service = rig.sessions()
    answer = rig.start(vs.MODE_SPEECH)
    ended = service.end(tenant_id=TENANT, person=ALICE, session_id=answer["session_id"])
    assert ended["changed"] is True
    assert rig.compute.status == sm.TERMINATED
    assert _lease(rig.db)["state"] == sm.STOPPED and _lease(rig.db)["cause"] == vs.END_BY_PERSON


# ---- turns and delegations ------------------------------------------------------


def test_a_speech_session_takes_turns_and_carries_the_delegation():
    rig = Rig()
    service = rig.sessions()
    answer = rig.start(vs.MODE_SPEECH)
    said = service.turn(tenant_id=TENANT, person=ALICE, session_id=answer["session_id"], text="plan it")
    assert said["reply"] == "Looking at that now." and said["delegation_id"] == "vd_1"
    assert service.delegation(answer["session_id"], "vd_1", timeout=5.0) == "Three steps are on screen."
    assert rig.voice.asked == [(answer["session_id"], "vd_1", 5.0)]


class _Response(io.BytesIO):
    def __init__(self, status: int, body: dict) -> None:
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _http(monkeypatch, answer: dict, seen: list) -> vs.HttpVoiceClient:
    def urlopen(request, timeout):  # noqa: ANN001 -- urllib's signature
        seen.append((request.get_method(), request.full_url, timeout))
        return _Response(200, answer)

    monkeypatch.setattr(vs.urllib.request, "urlopen", urlopen)
    return vs.HttpVoiceClient("http://voice.internal:8080", token=lambda _aud, _t: secrets.token_hex(8))


def test_the_delegation_client_reads_swarm_voices_delegations_route(monkeypatch):
    seen: list = []
    session_id = "vs_" + "1" * 32
    client = _http(monkeypatch, {"delegation_id": "vd_7", "voice_session_id": session_id,
                                 "state": "done", "spoken": "It is on screen.", "cards": []}, seen)
    service = vs.VoiceSessions(FakeFirestore(), client, now=Clock().now)

    assert service.delegation(session_id, "vd_7", timeout=30.0) == "It is on screen."
    ((method, url, _timeout),) = seen
    parsed = urllib.parse.urlsplit(url)
    assert method == "GET" and parsed.path == "/delegations/vd_7"
    assert urllib.parse.parse_qs(parsed.query) == {"wait": ["30.0"]}


def test_the_delegation_client_reads_nothing_for_another_session_or_an_unfinished_one(monkeypatch):
    seen: list = []
    other = _http(monkeypatch, {"delegation_id": "vd_7", "voice_session_id": "vs_" + "2" * 32,
                                "state": "done", "spoken": "Not yours.", "cards": []}, seen)
    assert other.delegation("vs_" + "1" * 32, "vd_7", timeout=1.0) is None
    running = _http(monkeypatch, {"delegation_id": "vd_7", "voice_session_id": "vs_" + "1" * 32,
                                  "state": "running", "spoken": "", "cards": []}, seen)
    assert running.delegation("vs_" + "1" * 32, "vd_7", timeout=1.0) is None


def test_the_http_client_reads_a_front_refusal_as_the_text_fallback(monkeypatch):
    def urlopen(request, timeout):  # noqa: ANN001
        body = json.loads(request.data)
        assert body["mode"] == vs.MODE_SPEECH and "offer" not in body
        raise vs.urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(json.dumps(
            {"code": vs.FRONT_REFUSED_CODE, "message": "no Claude account"}).encode()))

    monkeypatch.setattr(vs.urllib.request, "urlopen", urlopen)
    client = vs.HttpVoiceClient("http://voice.internal:8080", token=lambda _aud, _t: secrets.token_hex(8))
    call = vs.StartCall(session_id="vs_" + "1" * 32, project_id="vp_" + "1" * 32, tenant_id=TENANT,
                        person=ALICE, offer="", grant=secrets.token_urlsafe(32), mode=vs.MODE_SPEECH)
    with pytest.raises(vs.SpeechFrontRefused):
        client.start(call)


def test_the_http_client_reads_a_speech_start_and_a_turns_delegation(monkeypatch):
    session_id = "vs_" + "1" * 32
    answers = iter([(201, {"voice_session_id": session_id, "mode": "speech"}),
                    (200, {"turn_id": "vf_1", "reply": "On it.", "cards": [], "delegation_id": "vd_2"})])

    def urlopen(request, timeout):  # noqa: ANN001
        return _Response(*next(answers))

    monkeypatch.setattr(vs.urllib.request, "urlopen", urlopen)
    client = vs.HttpVoiceClient("http://voice.internal:8080", token=lambda _aud, _t: secrets.token_hex(8))
    call = vs.StartCall(session_id=session_id, project_id="vp_" + "1" * 32, tenant_id=TENANT,
                        person=ALICE, offer="", grant=secrets.token_urlsafe(32), mode=vs.MODE_SPEECH)
    assert client.start(call).mode == vs.MODE_SPEECH
    assert client.turn(session_id, "plan it").delegation_id == "vd_2"


# ---- the route: no engine at Start ----------------------------------------------


def _route_app(rig: Rig, pool: FakePool, *, speech: sm.SpeechConfig) -> TestClient:
    app = FastAPI()
    app.include_router(rv.router)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    ctx = SimpleNamespace(db=rig.db, now=rig.clock.now, settings=SimpleNamespace(
        voice_people=(ALICE,), child_key="", worker_class_models={}, voice_url="http://voice.internal"))
    app.state.ctx = ctx
    app.state.voice_client = rig.voice
    app.state.speech_config = speech
    app.state.speech_machine = rig.machine
    app.dependency_overrides[rv.allowed_person] = lambda: ALICE
    app.dependency_overrides[tenant_scope] = lambda: TENANT
    app.dependency_overrides[get_context] = lambda: ctx
    app.dependency_overrides[raccounts.account_pool] = lambda: pool
    return TestClient(app)


def test_the_route_starts_settings_own_speech_and_takes_no_engine():
    rig = Rig()
    client = _route_app(rig, FakePool(""), speech=CONFIG)
    refused = client.post("/v1/voice/sessions", json={"engine": "gpt-live"})
    assert refused.status_code == 422

    started = client.post("/v1/voice/sessions", json={})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == vs.MODE_SPEECH
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_SPEECH]


def test_the_route_starts_gpt_live_when_settings_say_so():
    rig = Rig()
    _settings(rig.db, engine=cat.ENGINE_GPT_LIVE)
    client = _route_app(rig, FakePool(""), speech=CONFIG)
    started = client.post("/v1/voice/sessions", json={"offer_sdp": OFFER})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == vs.MODE_LIVE and started.json()["answer_sdp"] == ANSWER
    assert rig.compute.calls == []


def test_the_route_falls_back_to_text_for_gpt_live_with_no_key():
    rig = Rig()
    _settings(rig.db, engine=cat.ENGINE_GPT_LIVE)
    client = _route_app(rig, FakePool(vs.NO_LIVE_ACCOUNT), speech=CONFIG)
    started = client.post("/v1/voice/sessions", json={})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == vs.MODE_TEXT and "answer_sdp" not in started.json()
    assert started.json()["fallback"] == {"from": cat.ENGINE_GPT_LIVE, "reason": vs.FALLBACK_NO_OPENAI_KEY}
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_TEXT]
    assert rig.compute.calls == []  # the speech machine is never started


def test_the_route_falls_back_to_text_when_no_speech_machine_is_deployed():
    rig = Rig()
    client = _route_app(rig, FakePool(""), speech=sm.SpeechConfig())
    started = client.post("/v1/voice/sessions", json={})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == vs.MODE_TEXT
    assert started.json()["fallback"] == {"from": cat.ENGINE_OWN, "reason": vs.FALLBACK_NOT_DEPLOYED}
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_TEXT]


def test_a_deployment_that_holds_the_template_back_runs_no_own_speech():
    # voice_speech.template = false: the release renders every SWARM_SPEECH_*
    # but an empty SWARM_SPEECH_TEMPLATE. Own speech fails closed: the start
    # is text with speech_not_deployed ("own speech is not set up on this
    # deployment"), and no Compute call is made, a create least of all.
    config = sm.SpeechConfig.from_env({
        "SWARM_SPEECH_PROJECT": "proj", "SWARM_SPEECH_ZONES": ",".join(CONFIG.zones),
        "SWARM_SPEECH_INSTANCE": "swarm-speech-", "SWARM_SPEECH_ADDR": "10.40.0.9",
        "SWARM_SPEECH_TEMPLATE": ""})
    assert config.address and config.zones and config.instance and config.project
    assert not config.enabled
    rig = Rig()
    client = _route_app(rig, FakePool(""), speech=config)
    started = client.post("/v1/voice/sessions", json={})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == vs.MODE_TEXT
    assert started.json()["fallback"] == {"from": cat.ENGINE_OWN, "reason": vs.FALLBACK_NOT_DEPLOYED}
    assert [c.mode for c in rig.voice.starts] == [vs.MODE_TEXT]
    assert rig.compute.calls == []


def test_the_voice_router_mounts_the_speech_routes():
    paths = {getattr(route, "path", None) for route in rv.router.routes}
    assert {rvs.AUDIO_ROUTE, rvs.SWEEP_ROUTE} <= paths
    assert "/v1/voice/sessions" in paths
