"""Turn latency, stored and served (docs/design/voice-it-own-speech.md §12.6;
swarm_api/voice/speech_telemetry.py, routes/voice_telemetry.py,
routes/voice_trace.py).

* an own-speech turn record goes through the relay's existing sink into the
  session's samples and document, fenced by the lease's generation as every
  sample is;
* a hosted-voice record arrives on the session grant's existing trace write
  and is kept beside them, named by its engine;
* a record carries durations, the engine, the turn id and the session id and
  nothing else: any other key refuses it;
* `GET /v1/voice/telemetry/latency` serves, per engine and for the page's
  range, the count, p50 and p90 of the span and of each part, and how many
  turns were not measured; hosted sessions stay out of the own-speech
  sessions and days.

FakeFirestore and a fake wall clock: no cloud credentials, no emulator.
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
from swarm_api.routes import voice_telemetry as routes  # noqa: E402
from swarm_api.routes import voice_trace  # noqa: E402
from swarm_api.voice import grant as vgrant  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402
from swarm_api.voice import speech_telemetry as tel  # noqa: E402

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
ALICE = "alice@example.com"
BOB = "bob@example.com"
ROOT = "root@example.com"
TENANTS = {ALICE: "eng", BOB: "ops", ROOT: "eng"}
OWN = sm.SessionRef(tenant_id="eng", person=ALICE, project_id="vp_1", session_id="vs_own1")
SPAN = {"from": "2026-11-05", "to": "2026-11-06", "tz": "UTC", "scope": "platform"}

#: Everything a stored turn may carry (beside the sample's envelope).
TURN_KEYS = {"engine", "turn_id", "session_id", "span_ms", "recognition_ms",
             "front_first_token_ms", "front_ms", "synthesis_ms"}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _lease(db: FakeFirestore, owner: sm.SessionRef, generation: int) -> None:
    db.collection(sm.COLLECTION).document(sm.LEASE).set({
        "session_id": owner.session_id, "tenant_id": owner.tenant_id,
        "generation": generation, "state": sm.RUNNING})


def _own(span: float | None, recognition: float | None = 300.0, front: float | None = 700.0,
         synthesis: float | None = 200.0, session_id: str = OWN.session_id) -> dict[str, Any]:
    return {"t": "timing", "kind": "turn", "engine": "own", "turn_id": "turn_" + secrets.token_hex(6),
            "session_id": session_id, "span_ms": span, "recognition_ms": recognition,
            "front_first_token_ms": None, "front_ms": front, "synthesis_ms": synthesis}


def _hosted(session_id: str, span: float | None) -> dict[str, Any]:
    return {"engine": "gpt-live", "turn_id": "turn_" + secrets.token_hex(6),
            "session_id": session_id, "span_ms": span}


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def store(db, clock) -> tel.Telemetry:
    return tel.Telemetry(db, now=clock)


def _samples(db: FakeFirestore, session_id: str) -> list[dict[str, Any]]:
    prefix = f"{tel.SESSIONS}/{session_id}/{tel.SAMPLES}/"
    return [doc for path, doc in db.docs.items() if path.startswith(prefix)]


# ---- the record --------------------------------------------------------------


def test_an_own_speech_turn_is_stored_with_durations_engine_turn_and_session_only(db, store):
    _lease(db, OWN, 4)
    assert store.record(OWN, 4, _own(1200.0)) is not None
    [sample] = _samples(db, OWN.session_id)
    assert sample["kind"] == tel.KIND_TURN
    assert set(sample["data"]) == TURN_KEYS
    assert set(sample) == {"kind", "at", "generation", "session_id", "tenant_id", "expire_at", "data"}


@pytest.mark.parametrize("extra", ["text", "transcript", "reply", "audio", "words", "person"])
def test_a_turn_carrying_anything_but_durations_is_refused(db, store, extra):
    _lease(db, OWN, 4)
    with pytest.raises(tel.SampleRefused) as refused:
        store.record(OWN, 4, {**_own(900.0), extra: "what is running"})
    assert "what is running" not in str(refused.value)
    assert _samples(db, OWN.session_id) == []


def test_a_duration_that_is_text_is_refused(db, store):
    _lease(db, OWN, 4)
    with pytest.raises(tel.SampleRefused):
        store.record(OWN, 4, {**_own(900.0), "span_ms": "nine hundred"})


def test_a_stale_generation_writes_no_turn(db, store):
    _lease(db, OWN, 5)
    assert store.record(OWN, 4, _own(900.0)) is None
    assert _samples(db, OWN.session_id) == []


def test_a_hosted_turn_names_its_engine_and_is_refused_as_another_session(db, store):
    owner = sm.SessionRef(tenant_id="eng", person=ALICE, project_id="vp_2", session_id="vs_live1")
    store.record_hosted(owner, _hosted("vs_live1", 640.0))
    [sample] = _samples(db, "vs_live1")
    assert sample["data"]["engine"] == "gpt-live" and sample["generation"] == 0
    with pytest.raises(tel.SampleRefused):
        store.record_hosted(owner, _hosted("vs_other", 640.0))
    with pytest.raises(tel.SampleRefused):
        store.record_hosted(owner, {**_hosted("vs_live1", 640.0), "engine": "own"})
    with pytest.raises(tel.SampleRefused):
        store.record_hosted(owner, {**_hosted("vs_live1", 640.0), "transcript": "hello"})


# ---- the summary: count, p50, p90, not measured ---------------------------------


def test_latency_is_served_per_engine_with_percentiles_and_not_measured(db, store, clock):
    _lease(db, OWN, 1)
    for span in (1000.0, 1200.0, 1400.0, 1600.0, 1800.0, 2000.0, 2200.0, 2400.0, 2600.0, 2800.0):
        store.record(OWN, 1, _own(span))
    store.record(OWN, 1, _own(None, synthesis=None))
    store.record(OWN, 1, _own(None, front=None, synthesis=None))
    live = sm.SessionRef(tenant_id="eng", person=ALICE, project_id="vp_2", session_id="vs_live1")
    clock.at = NOW + timedelta(minutes=10)
    for span in (500.0, 700.0, None):
        store.record_hosted(live, _hosted("vs_live1", span))

    answer = store.latency(tel.parse_span("2026-11-05", "2026-11-06", "UTC"), tenant_id=None)
    own = answer["own"]
    assert own["turns"] == 12
    assert own["span_ms"] == {"count": 10, "p50": 1800.0, "p90": 2600.0, "not_measured": 2}
    assert own["parts"]["recognition_ms"] == {"count": 12, "p50": 300.0, "p90": 300.0, "not_measured": 0}
    assert own["parts"]["front_ms"]["not_measured"] == 1
    assert own["parts"]["synthesis_ms"]["not_measured"] == 2
    first = own["parts"]["front_first_token_ms"]
    assert first == {"count": 0, "p50": None, "p90": None, "not_measured": 12}
    hosted = answer["gpt-live"]
    assert hosted["turns"] == 3
    assert hosted["span_ms"] == {"count": 2, "p50": 500.0, "p90": 700.0, "not_measured": 1}
    assert hosted["parts"] == {}


def test_nothing_measured_is_zero_turns_and_no_percentile(store):
    answer = store.latency(tel.parse_span("2026-11-05", "2026-11-06", "UTC"), tenant_id=None)
    for engine in ("own", "gpt-live"):
        assert answer[engine]["turns"] == 0
        assert answer[engine]["span_ms"] == {"count": 0, "p50": None, "p90": None, "not_measured": 0}


def test_hosted_sessions_stay_out_of_the_own_speech_sessions_and_days(db, store):
    live = sm.SessionRef(tenant_id="eng", person=ALICE, project_id="vp_2", session_id="vs_live1")
    store.record_hosted(live, _hosted("vs_live1", 500.0))
    span = tel.parse_span("2026-11-05", "2026-11-06", "UTC")
    assert store.sessions(span, tenant_id=None) == []
    assert all(day["sessions"] == 0 for day in store.days(span, tenant_id=None))


def test_a_workspace_reads_only_its_own_turns(db, store):
    _lease(db, OWN, 1)
    store.record(OWN, 1, _own(900.0))
    ops = sm.SessionRef(tenant_id="ops", person=BOB, project_id="vp_3", session_id="vs_live2")
    store.record_hosted(ops, _hosted("vs_live2", 500.0))
    span = tel.parse_span("2026-11-05", "2026-11-06", "UTC")
    eng = store.latency(span, tenant_id="eng")
    assert eng["own"]["turns"] == 1 and eng["gpt-live"]["turns"] == 0


# ---- the routes --------------------------------------------------------------


def _client(db: FakeFirestore, clock: Clock) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(voice_trace.write_router)

    @app.exception_handler(ApiError)
    async def _error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status_code)

    ctx = SimpleNamespace(db=db, now=clock, settings=SimpleNamespace(voice_people=(ALICE, BOB)))

    def _auth(request: Request) -> AuthContext:
        email = request.headers.get("x-test-user", ALICE)
        grant = request.headers.get("x-test-grant", "")
        return AuthContext(principal=Principal(email=email, subject=email, domain="example.com"),
                           tenant_id=TENANTS[email], is_admin=email == ROOT,
                           via=vgrant.VIA if grant else "", voice_session=grant)

    app.dependency_overrides[current_auth] = _auth
    app.dependency_overrides[tenant_scope] = lambda request: TENANTS[request.headers.get("x-test-user", ALICE)]
    app.dependency_overrides[get_context] = lambda: ctx
    return TestClient(app)


def _voice_session(db: FakeFirestore, person: str = ALICE) -> str:
    project = "vp_" + secrets.token_hex(16)
    session = "vs_" + secrets.token_hex(16)
    owner = {"tenant_id": TENANTS[person], "person": person}
    ref = db.collection(vs.PROJECTS).document(project)
    ref.set({**owner, "title": "Bakery", "status": "active"})
    ref.collection(vs.SESSIONS).document(session).set({**owner, "state": vs.LIVE, "created_at": NOW})
    return session


def test_the_grants_trace_write_keeps_a_hosted_turn(db, clock):
    client = _client(db, clock)
    session = _voice_session(db)
    r = client.post(f"/v1/voice/sessions/{session}/trace", json={"latency": _hosted(session, 640.0)},
                    headers={"x-test-user": ALICE, "x-test-grant": session})
    assert r.status_code == 200, r.text
    [sample] = _samples(db, session)
    assert sample["data"]["span_ms"] == 640.0 and sample["tenant_id"] == "eng"
    # Not through a grant, or for another session: refused, nothing kept.
    other = _voice_session(db)
    assert client.post(f"/v1/voice/sessions/{other}/trace", json={"latency": _hosted(other, 1.0)},
                       headers={"x-test-user": ALICE}).status_code == 403
    assert client.post(f"/v1/voice/sessions/{other}/trace", json={"latency": _hosted(other, 1.0)},
                       headers={"x-test-user": ALICE, "x-test-grant": session}).status_code == 403
    assert _samples(db, other) == []


def test_a_hosted_turn_with_content_is_a_422_and_keeps_nothing(db, clock):
    client = _client(db, clock)
    session = _voice_session(db)
    r = client.post(f"/v1/voice/sessions/{session}/trace",
                    json={"latency": {**_hosted(session, 640.0), "transcript": "hello there"}},
                    headers={"x-test-user": ALICE, "x-test-grant": session})
    assert r.status_code == 422
    assert "hello there" not in r.text
    assert _samples(db, session) == []


def test_the_latency_route_serves_both_engines_for_the_range(db, store, clock):
    _lease(db, OWN, 1)
    store.record(OWN, 1, _own(1500.0))
    client = _client(db, clock)
    r = client.get("/v1/voice/telemetry/latency", params=SPAN, headers={"x-test-user": ROOT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["engines"]) == {"own", "gpt-live"}
    assert body["engines"]["own"]["span_ms"]["p50"] == 1500.0
    assert body["scope"] == "platform"


def test_the_latency_route_is_for_telemetry_readers_and_admins_for_the_platform(db, clock):
    client = _client(db, clock)
    workspace = {k: v for k, v in SPAN.items() if k != "scope"}
    assert client.get("/v1/voice/telemetry/latency", params=workspace,
                      headers={"x-test-user": ALICE}).status_code == 200
    assert client.get("/v1/voice/telemetry/latency", params=SPAN,
                      headers={"x-test-user": ALICE}).status_code == 403
    assert client.get("/v1/voice/telemetry/latency", params={**workspace, "tz": "Mars/Base"},
                      headers={"x-test-user": ALICE}).status_code == 422
