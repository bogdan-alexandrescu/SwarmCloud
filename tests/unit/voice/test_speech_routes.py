"""swarm-api's own-speech routes' admission (docs/design/voice-it-own-speech.md
§3.3 step 2, §4.5 stop 1; swarm_api/routes/voice_speech.py).

The sweep admits only an ID token for its audience from one of its callers,
and nobody when none is configured. The audio socket admits only a signed-in
person on the Voice it list, for a live speech session of theirs that holds
the machine, and only when the engine is deployed; every refusal is a close
code. Firestore is the in-memory fake, the verifier and the authenticator are
fakes: no network, no cloud credential.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from control_plane.fakes import FakeFirestore  # noqa: E402
from test_speech_machine import (  # noqa: E402
    ALICE,
    BOB,
    CHOSEN,
    CONFIG,
    Clock,
    FakeCompute,
    _lease,
    _machine,
    _mark_live,
)

from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.routes import voice_speech as rvs  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402

SCHEDULER = "swarm-tick@example-project.iam.gserviceaccount.com"
SWEEP = dict(sweep_audience="https://api.example.com/v1/internal/voice/speech/sweep",
             sweep_callers=(SCHEDULER,))


class FakeVerifier:
    """Admits one token, built at runtime, as the claims it was given."""

    def __init__(self, claims: dict[str, Any]) -> None:
        self.token = secrets.token_hex(16)
        self.claims = claims
        self.seen: list[str] = []

    def verify(self, token: str) -> dict[str, Any]:
        self.seen.append(token)
        if token != self.token:
            raise ValueError("refused")
        return self.claims


class FakeAuthenticator:
    def __init__(self, email: str | None, tenant_id: str = "eng") -> None:
        self.email, self.tenant_id = email, tenant_id

    def authenticate(self, presented: Any, iap: Any) -> Any:
        if self.email is None:
            raise PermissionError("not signed in")
        return SimpleNamespace(email=self.email, tenant_id=self.tenant_id,
                               tenant_choices=((self.tenant_id, "user"),))


class FakeVoiceClient:
    def __init__(self) -> None:
        self.ended: list[tuple[str, str]] = []

    def end(self, session_id: str, *, cause: str) -> None:
        self.ended.append((session_id, cause))


class Rig:
    def __init__(self, *, config: sm.SpeechConfig, email: str | None = ALICE.person,
                 people: tuple[str, ...] = (ALICE.person, BOB.person)) -> None:
        self.db = FakeFirestore()
        self.clock = Clock()
        self.compute = FakeCompute()
        self.voice = FakeVoiceClient()
        self.verifier = FakeVerifier({"email": SCHEDULER})
        app = FastAPI()
        app.include_router(rvs.router)

        @app.exception_handler(ApiError)
        async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
            return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

        app.state.ctx = SimpleNamespace(
            db=self.db, now=self.clock.now, authenticator=FakeAuthenticator(email),
            settings=SimpleNamespace(voice_people=people, child_key=""))
        app.state.speech_config = config
        app.state.speech_compute = self.compute
        app.state.speech_sweep_verifier = self.verifier
        app.state.voice_client = self.voice
        self.app = app
        self.client = TestClient(app)

    def session(self, ref: sm.SessionRef, *, mode: str = sm.MODE_SPEECH,
                state: str = vs.LIVE) -> None:
        self.db.docs[f"{vs.PROJECTS}/{ref.project_id}"] = {
            "person": ref.person, "tenant_id": ref.tenant_id}
        self.db.docs[f"{vs.PROJECTS}/{ref.project_id}/{vs.SESSIONS}/{ref.session_id}"] = {
            "person": ref.person, "tenant_id": ref.tenant_id, "state": state, "mode": mode}

    def take(self, ref: sm.SessionRef) -> sm.Started:
        _mark_live(self.db, ref)
        return _machine(self.db, self.compute, self.clock).start(ref, chosen=CHOSEN)

    def sweep(self, token: str | None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return self.client.post(rvs.SWEEP_ROUTE, headers=headers)

    def socket_close_code(self, session_id: str) -> int:
        url = rvs.AUDIO_ROUTE.format(session_id=session_id)
        with self.client.websocket_connect(url) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        return closed.value.code


# ---- the sweep (§4.5 stop 1) -------------------------------------------------


@pytest.mark.parametrize("missing", ["sweep_audience", "sweep_callers"])
def test_the_sweep_admits_nobody_without_an_audience_and_callers(missing):
    config = sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP, missing: SWEEP[missing][:0]})
    rig = Rig(config=config)
    reply = rig.sweep(rig.verifier.token)
    assert reply.status_code == 503
    assert rig.verifier.seen == [] and rig.compute.calls == []


def test_the_sweep_without_a_bearer_token_is_401():
    rig = Rig(config=sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP}))
    assert rig.sweep(None).status_code == 401
    assert rig.client.post(rvs.SWEEP_ROUTE, headers={"Authorization": "Basic x"}).status_code == 401
    assert rig.compute.calls == []


def test_a_refused_token_is_401_and_never_echoed():
    rig = Rig(config=sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP}))
    wrong = secrets.token_hex(16)
    reply = rig.sweep(wrong)
    assert reply.status_code == 401
    assert wrong not in reply.text
    assert rig.compute.calls == []


def test_an_account_outside_the_sweep_callers_is_403():
    rig = Rig(config=sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP}))
    rig.verifier.claims = {"email": "someone@example.com"}
    assert rig.sweep(rig.verifier.token).status_code == 403
    assert rig.compute.calls == []


def test_the_sweep_with_the_engine_off_touches_nothing():
    rig = Rig(config=sm.SpeechConfig(**SWEEP))
    reply = rig.sweep(rig.verifier.token)
    assert reply.status_code == 200
    assert reply.json() == {"enabled": False, "lease": None, "ended": None, "stopped": False,
                            "instance": None, "abandoned": 0}
    assert rig.compute.calls == []


def test_the_sweep_ends_a_lease_that_lost_its_heartbeat_and_stops_the_machine():
    rig = Rig(config=sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP}))
    rig.session(ALICE)
    rig.take(ALICE)
    assert rig.compute.status == sm.INSTANCE_RUNNING
    rig.clock.advance(sm.HEARTBEAT_STALE_SECONDS + 1)
    reply = rig.sweep(rig.verifier.token)
    assert reply.status_code == 200
    assert reply.json()["ended"] == sm.END_SWEPT and reply.json()["stopped"] is True
    assert rig.compute.status == sm.TERMINATED
    assert _lease(rig.db)["state"] == sm.STOPPED
    assert rig.voice.ended == [(ALICE.session_id, sm.END_SWEPT)]


def test_the_sweep_leaves_a_lease_with_a_fresh_heartbeat_running():
    rig = Rig(config=sm.SpeechConfig(**{**CONFIG.__dict__, **SWEEP}))
    rig.session(ALICE)
    rig.take(ALICE)
    rig.clock.advance(sm.HEARTBEAT_STALE_SECONDS - sm.HEARTBEAT_SECONDS)
    reply = rig.sweep(rig.verifier.token)
    assert reply.status_code == 200 and reply.json()["ended"] is None
    assert reply.json()["stopped"] is False
    assert rig.compute.status == sm.INSTANCE_RUNNING


# ---- the audio socket (§3.3 step 2) -----------------------------------------


def test_a_socket_not_signed_in_is_closed_4401():
    rig = Rig(config=CONFIG, email=None)
    rig.session(ALICE)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_UNAUTHENTICATED


def test_a_socket_from_someone_off_the_voice_list_is_closed_4403():
    rig = Rig(config=CONFIG, email="carol@example.com")
    rig.session(ALICE)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NOT_ALLOWED


def test_a_socket_for_another_persons_session_is_closed_4404():
    rig = Rig(config=CONFIG, email=BOB.person)
    rig.session(ALICE)
    rig.take(ALICE)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NOT_FOUND


def test_a_socket_for_a_session_in_another_tenant_is_closed_4404():
    rig = Rig(config=CONFIG)
    rig.app.state.ctx.authenticator.tenant_id = "research"
    rig.session(ALICE)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NOT_FOUND


def test_a_socket_for_a_malformed_session_id_is_closed_4404():
    rig = Rig(config=CONFIG)
    assert rig.socket_close_code("not-a-session") == rvs.CLOSE_NOT_FOUND


@pytest.mark.parametrize("mode,state", [(vs.MODE_TEXT, vs.LIVE), (sm.MODE_SPEECH, "ended")])
def test_a_socket_for_a_session_not_live_in_speech_is_closed_4409(mode, state):
    rig = Rig(config=CONFIG)
    rig.session(ALICE, mode=mode, state=state)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NOT_HELD


def test_a_socket_with_the_engine_not_deployed_is_closed_4503():
    rig = Rig(config=sm.SpeechConfig())
    rig.session(ALICE)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NO_ENGINE
    assert rig.compute.calls == []


def test_a_socket_for_a_session_that_does_not_hold_the_machine_is_closed_4409():
    rig = Rig(config=CONFIG)
    rig.session(ALICE)
    rig.session(BOB)
    rig.take(BOB)
    before = dict(_lease(rig.db))
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NOT_HELD
    assert _lease(rig.db) == before


def test_a_socket_for_a_session_whose_lease_was_swept_is_closed_4409():
    rig = Rig(config=CONFIG)
    rig.session(ALICE)
    started = rig.take(ALICE)
    _machine(rig.db, rig.compute, rig.clock).stop(started.holder, cause=sm.END_SWEPT)
    assert rig.socket_close_code(ALICE.session_id) == rvs.CLOSE_NOT_HELD
