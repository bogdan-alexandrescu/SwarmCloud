"""Voice it's own-speech telemetry, stored and served
(docs/design/voice-it-own-speech.md §12, build plan row L10).

The store (swarm_api/voice/speech_telemetry.py) and its router
(swarm_api/routes/voice_telemetry.py), tested directly with FakeFirestore:
no cloud credentials and no emulator. L6 mounts the router; here it is
mounted alone.
"""

from __future__ import annotations

import asyncio
import json
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
from swarm_api.voice import speech_machine as sm  # noqa: E402
from swarm_api.voice import speech_telemetry as tel  # noqa: E402

# 03:00 UTC on 6 November is still 5 November in Los Angeles (UTC-8).
NOW = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
ALICE = "alice@example.com"   # a Voice it person in workspace eng
BOB = "bob@example.com"       # a Voice it person in workspace ops
ROOT = "root@example.com"     # a platform admin
CAROL = "carol@example.com"   # neither
TENANTS = {ALICE: "eng", BOB: "ops", ROOT: "eng", CAROL: "eng"}

ENG = sm.SessionRef(tenant_id="eng", person=ALICE, project_id="vp_1", session_id="vs_eng1")
OPS = sm.SessionRef(tenant_id="ops", person=BOB, project_id="vp_2", session_id="vs_ops1")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _lease(db: FakeFirestore, owner: sm.SessionRef, generation: int,
           state: str = sm.RUNNING, **more: Any) -> None:
    db.collection(sm.COLLECTION).document(sm.LEASE).set({
        "session_id": owner.session_id, "tenant_id": owner.tenant_id,
        "generation": generation, "state": state, **more})


def _metrics(**over: Any) -> dict[str, Any]:
    frame = {"t": "metrics", "at": 1.0,
             "gpu": {"util_pct": 40.0, "mem_used_mib": 9000, "mem_total_mib": 23034},
             "cpu_pct": 12.5, "memory": {"used_mib": 4000, "total_mib": 15000},
             "disk": {"used_gb": 60.1, "total_gb": 150.0},
             "vllm": {"queue": 0, "kv_cache_pct": 3.0},
             "counters": {"utterances": 1, "stt_errors": 0, "stt_audio_seconds": 1.5,
                          "syntheses": 1, "tts_errors": 0, "tts_cancelled": 0,
                          "tts_audio_seconds": 2.0, "protocol_errors": 0},
             "open_sockets": 1, "phase": "ready"}
    frame.update(over)
    return frame


def _stt(final_ms: int, *, ok: bool = True) -> dict[str, Any]:
    return {"t": "timing", "kind": "stt", "seq": 3, "final_ms": final_ms,
            "first_partial_ms": 200, "audio_s": 1.5, "processing_s": 0.3, "rtf": 0.2,
            "ok": ok, "cut": False}


def _tts(first_audio_ms: int) -> dict[str, Any]:
    return {"t": "timing", "kind": "tts", "id": "say_1", "first_audio_ms": first_audio_ms,
            "audio_s": 2.0, "synth_s": 0.5, "rtf": 0.25, "cancelled": False, "ok": True}


def _samples(db: FakeFirestore, session_id: str) -> dict[str, dict[str, Any]]:
    prefix = f"{tel.SESSIONS}/{session_id}/{tel.SAMPLES}/"
    return {path[len(prefix):]: doc for path, doc in db.docs.items() if path.startswith(prefix)}


def _session_doc(db: FakeFirestore, session_id: str) -> dict[str, Any] | None:
    return db.docs.get(f"{tel.SESSIONS}/{session_id}")


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def store(db, clock) -> tel.Telemetry:
    return tel.Telemetry(db, now=clock)


# --------------------------------------------------------------------------
# Writing a sample
# --------------------------------------------------------------------------


def test_a_metrics_frame_is_stored_as_a_sample_with_the_machine_generation(db, store):
    _lease(db, ENG, 7)
    seq = store.record(ENG, 7, _metrics())
    assert seq is not None
    sample = _samples(db, ENG.session_id)[seq]
    assert sample["kind"] == tel.KIND_METRICS
    assert sample["generation"] == 7
    assert sample["tenant_id"] == "eng"
    assert sample["session_id"] == ENG.session_id
    assert sample["at"] == NOW
    assert sample["data"]["gpu"]["util_pct"] == 40.0
    assert "t" not in sample["data"]
    session = _session_doc(db, ENG.session_id)
    assert session["generation"] == 7
    assert (session["tenant_id"], session["person"], session["project_id"]) == \
        ("eng", ALICE, "vp_1")


def test_a_sample_carries_its_retention_field_thirty_days_on(db, store):
    _lease(db, ENG, 1)
    seq = store.record(ENG, 1, _stt(400))
    sample = _samples(db, ENG.session_id)[seq]
    assert sample["expire_at"] == NOW + timedelta(days=30)
    assert tel.SAMPLE_RETENTION == timedelta(days=30)
    # The session's own document is kept: the TTL policy is on `samples` only.
    assert "expire_at" not in _session_doc(db, ENG.session_id)


def test_a_stale_generation_writes_nothing(db, store):
    _lease(db, ENG, 4)
    assert store.record(ENG, 3, _metrics()) is None
    _lease(db, OPS, 5)  # another session took the machine
    assert store.record(ENG, 4, _metrics()) is None
    assert _samples(db, ENG.session_id) == {}
    assert _session_doc(db, ENG.session_id) is None


def test_sample_ids_sort_by_time_and_never_collide(db, store, clock):
    _lease(db, ENG, 2)
    first = store.record(ENG, 2, _metrics())
    second = store.record(ENG, 2, _metrics())
    clock.at = NOW + timedelta(seconds=5)
    third = store.record(ENG, 2, _metrics())
    assert len({first, second, third}) == 3
    assert max(first, second) < third


@pytest.mark.parametrize("frame, field", [
    ({**_stt(300), "text": "open the pull request"}, "text"),
    ({**_tts(100), "audio": "UklGRg=="}, "audio"),
    ({**_metrics(), "token": "x"}, "token"),
    ({**_metrics(), "gpu": {"util_pct": 1.0, "serial": "GPU-1234"}}, "gpu.serial"),
    ({**_stt(300), "transcript": ["go"]}, "transcript"),
])
def test_a_sample_carrying_a_forbidden_field_is_refused(db, store, frame, field):
    _lease(db, ENG, 1)
    with pytest.raises(tel.SampleRefused) as refused:
        store.record(ENG, 1, frame)
    assert field in str(refused.value)
    assert _samples(db, ENG.session_id) == {}
    assert _session_doc(db, ENG.session_id) is None


def test_a_refusal_names_the_field_and_never_its_value(db, store):
    value = secrets.token_hex(12)
    with pytest.raises(tel.SampleRefused) as refused:
        store.record(ENG, 1, {**_metrics(), "key": value})
    assert value not in str(refused.value)


@pytest.mark.parametrize("frame", [
    {"t": "final", "seq": 1, "text": "hello"},
    {"t": "partial", "seq": 1, "text": "hel"},
    {"t": "timing", "kind": "other"},
    _stt("400"),  # a number sent as a string
    {**_metrics(), "phase": "a whole sentence of words"},
])
def test_a_frame_that_is_not_numbers_of_a_telemetry_kind_is_refused(store, frame):
    with pytest.raises(tel.SampleRefused):
        store.record(ENG, 1, frame)


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------


def test_a_session_summary_is_its_percentiles_totals_and_peaks(db, store, clock):
    _lease(db, ENG, 3)
    for i, final_ms in enumerate([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]):
        clock.at = NOW + timedelta(seconds=i)
        store.record(ENG, 3, _stt(final_ms, ok=final_ms != 1000))
    store.record(ENG, 3, _tts(250))
    store.record(ENG, 3, _metrics(cpu_pct=80.0))
    clock.at = NOW + timedelta(minutes=12)
    store.record(ENG, 3, _metrics(gpu={"util_pct": 95.0, "mem_used_mib": 12000,
                                       "mem_total_mib": 23034}))
    span = tel.parse_span("2026-11-06", "2026-11-06", "UTC")
    [row] = store.sessions(span, tenant_id="eng")
    assert row["session_id"] == ENG.session_id
    assert row["generation"] == 3
    assert row["utterances"] == 10 and row["syntheses"] == 1
    assert row["stt_errors"] == 1 and row["tts_errors"] == 0
    assert row["stt_audio_s"] == 15.0
    assert row["percentiles"]["stt_final_ms"] == {"p50": 500.0, "p90": 900.0, "max": 1000.0}
    assert row["percentiles"]["tts_first_audio_ms"]["p50"] == 250.0
    assert row["peaks"]["cpu_pct"] == 80.0
    assert row["peaks"]["gpu_util_pct"] == 95.0
    assert row["minutes"] == 12.0
    assert row["metrics_samples"] == 2


def test_a_session_keeps_the_starts_phases_from_the_lease(db, store):
    _lease(db, ENG, 2, start_phases={"start.boot": 41.5, "start.load": 12.0})
    store.record(ENG, 2, _metrics())
    [row] = store.sessions(tel.parse_span("2026-11-06", "2026-11-06", "UTC"), tenant_id="eng")
    assert row["start_phases"] == {"start.boot": 41.5, "start.load": 12.0}


def test_no_percentile_or_day_is_stored_only_samples_and_the_session(db, store):
    _lease(db, ENG, 1)
    store.record(ENG, 1, _stt(100))
    session = _session_doc(db, ENG.session_id)
    assert "percentiles" not in session and "day" not in session and "minutes" not in session
    assert {p.split("/")[0] for p in db.docs} == {tel.SESSIONS, sm.COLLECTION}


# --------------------------------------------------------------------------
# Days, computed on read in the viewer's zone
# --------------------------------------------------------------------------


def test_the_day_is_computed_on_read_in_the_viewers_zone(db, store):
    _lease(db, ENG, 1)
    store.record(ENG, 1, _stt(400))
    utc = store.days(tel.parse_span("2026-11-05", "2026-11-06", "UTC"), tenant_id="eng")
    la = store.days(tel.parse_span("2026-11-05", "2026-11-06", "America/Los_Angeles"),
                    tenant_id="eng")
    assert [(d["day"], d["sessions"]) for d in utc] == [("2026-11-05", 0), ("2026-11-06", 1)]
    assert [(d["day"], d["sessions"]) for d in la] == [("2026-11-05", 1), ("2026-11-06", 0)]
    assert la[0]["utterances"] == 1 and la[0]["stt_final_ms_p90"] == 400.0
    [row] = store.sessions(tel.parse_span("2026-11-05", "2026-11-05", "America/Los_Angeles"),
                           tenant_id="eng")
    assert row["day"] == "2026-11-05"


def test_a_day_pools_its_sessions_values(db, store, clock):
    _lease(db, ENG, 1)
    for ms in (100, 200, 300):
        store.record(ENG, 1, _stt(ms))
    clock.at = NOW + timedelta(hours=1)
    _lease(db, OPS, 2)
    for ms in (700, 800):
        store.record(OPS, 2, _stt(ms))
    [day] = store.days(tel.parse_span("2026-11-06", "2026-11-06", "UTC"), tenant_id=None)
    assert day["sessions"] == 2 and day["utterances"] == 5
    assert day["stt_final_ms_p90"] == 800.0
    [eng_only] = store.days(tel.parse_span("2026-11-06", "2026-11-06", "UTC"), tenant_id="eng")
    assert eng_only["sessions"] == 1 and eng_only["stt_final_ms_p90"] == 300.0


@pytest.mark.parametrize("start, end, tz, field", [
    ("2026-11-01", "2026-11-02", "PDT", "tz"),
    ("2026-11-01", "2026-11-02", None, "tz"),
    ("yesterday", "2026-11-02", "UTC", "from"),
    ("2026-11-03", "2026-11-02", "UTC", "to"),
    ("2025-01-01", "2026-11-02", "UTC", "to"),
])
def test_a_bad_span_is_refused_with_its_field(start, end, tz, field):
    with pytest.raises(ApiError) as refused:
        tel.parse_span(start, end, tz)
    assert refused.value.status_code == 422
    assert refused.value.detail["field"] == field


def test_percentiles_are_nearest_rank_and_empty_is_none():
    assert tel.percentiles([]) == {"p50": None, "p90": None, "max": None}
    assert tel.percentiles([5]) == {"p50": 5.0, "p90": 5.0, "max": 5.0}
    assert tel.percentiles([4, 1, 3, 2]) == {"p50": 2.0, "p90": 4.0, "max": 4.0}


# --------------------------------------------------------------------------
# The relay's hand-off
# --------------------------------------------------------------------------


def test_the_relays_hand_off_stores_off_the_event_loop_and_never_raises(db, store):
    _lease(db, ENG, 6)
    on_frame = tel.sink(store, ENG, 6)

    async def relay() -> None:
        on_frame(_metrics())
        on_frame({**_metrics(), "text": "never stored"})  # refused, not raised
        on_frame(_stt(150))
        await asyncio.sleep(0)

    asyncio.run(relay())
    stored = _samples(db, ENG.session_id)
    assert sorted(s["kind"] for s in stored.values()) == ["metrics", "stt"]
    assert {s["generation"] for s in stored.values()} == {6}


def test_the_hand_off_of_a_stale_holder_writes_nothing(db, store):
    _lease(db, ENG, 9)
    tel.sink(store, ENG, 8)(_metrics())
    assert _samples(db, ENG.session_id) == {}


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------


def _client(db: FakeFirestore, clock: Clock) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)

    @app.exception_handler(ApiError)
    async def _error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status_code)

    ctx = SimpleNamespace(db=db, now=clock, settings=SimpleNamespace(voice_people=(ALICE, BOB)))

    def _auth(request: Request) -> AuthContext:
        email = request.headers.get("x-test-user", CAROL)
        return AuthContext(principal=Principal(email=email, subject=email, domain="example.com"),
                           tenant_id=TENANTS[email], is_admin=email == ROOT)

    def _tenant(request: Request) -> str:
        return TENANTS[request.headers.get("x-test-user", CAROL)]

    async def _sleep(_seconds: float) -> None:
        return None

    app.state.voice_telemetry_timing = {"poll_seconds": 0.0, "sleep": _sleep, "max_polls": 3}
    app.dependency_overrides[current_auth] = _auth
    app.dependency_overrides[tenant_scope] = _tenant
    app.dependency_overrides[get_context] = lambda: ctx
    return TestClient(app)


def _as(user: str) -> dict[str, str]:
    return {"x-test-user": user}


@pytest.fixture
def seeded(db, store, clock) -> FakeFirestore:
    _lease(db, ENG, 1)
    store.record(ENG, 1, _stt(300))
    clock.at = NOW + timedelta(minutes=5)
    _lease(db, OPS, 2)
    store.record(OPS, 2, _stt(900))
    return db


SPAN = {"from": "2026-11-05", "to": "2026-11-06", "tz": "America/Los_Angeles"}


def test_a_workspace_read_is_that_workspaces_sessions_only(seeded, clock):
    client = _client(seeded, clock)
    answer = client.get("/v1/voice/telemetry/sessions", params=SPAN, headers=_as(ALICE))
    assert answer.status_code == 200
    assert [s["session_id"] for s in answer.json()["sessions"]] == [ENG.session_id]
    assert answer.json()["scope"] == "workspace"
    bob = client.get("/v1/voice/telemetry/sessions", params=SPAN, headers=_as(BOB))
    assert [s["session_id"] for s in bob.json()["sessions"]] == [OPS.session_id]


def test_the_platform_view_is_for_admins_only(seeded, clock):
    client = _client(seeded, clock)
    params = {**SPAN, "scope": "platform"}
    refused = client.get("/v1/voice/telemetry/days", params=params, headers=_as(ALICE))
    assert refused.status_code == 403
    # The gate runs before the parameters: a non-admin learns nothing from a bad one.
    bad = client.get("/v1/voice/telemetry/days", params={**params, "tz": "PDT"},
                     headers=_as(ALICE))
    assert bad.status_code == 403
    admin = client.get("/v1/voice/telemetry/sessions", params=params, headers=_as(ROOT))
    assert admin.status_code == 200
    assert {s["session_id"] for s in admin.json()["sessions"]} == \
        {ENG.session_id, OPS.session_id}
    days = client.get("/v1/voice/telemetry/days", params=params, headers=_as(ROOT)).json()
    assert [(d["day"], d["sessions"]) for d in days["days"]] == \
        [("2026-11-05", 2), ("2026-11-06", 0)]


def test_someone_neither_admin_nor_on_voice_it_is_refused(seeded, clock):
    answer = _client(seeded, clock).get("/v1/voice/telemetry/days", params=SPAN,
                                        headers=_as(CAROL))
    assert answer.status_code == 403


def test_another_workspaces_session_is_not_found(seeded, clock):
    client = _client(seeded, clock)
    tz = {"tz": "UTC"}
    mine = client.get(f"/v1/voice/telemetry/sessions/{ENG.session_id}", params=tz,
                      headers=_as(ALICE))
    assert mine.status_code == 200
    assert mine.json()["session"]["utterances"] == 1
    assert [s["generation"] for s in mine.json()["samples"]] == [1]
    theirs = client.get(f"/v1/voice/telemetry/sessions/{OPS.session_id}", params=tz,
                        headers=_as(ALICE))
    assert theirs.status_code == 404
    admin = client.get(f"/v1/voice/telemetry/sessions/{OPS.session_id}",
                       params={**tz, "scope": "platform"}, headers=_as(ROOT))
    assert admin.status_code == 200


def test_a_bad_zone_or_scope_is_a_422_naming_it(seeded, clock):
    client = _client(seeded, clock)
    zone = client.get("/v1/voice/telemetry/days", params={**SPAN, "tz": "PDT"},
                      headers=_as(ALICE))
    assert zone.status_code == 422 and zone.json()["detail"]["field"] == "tz"
    scope = client.get("/v1/voice/telemetry/days", params={**SPAN, "scope": "everyone"},
                       headers=_as(ALICE))
    assert scope.status_code == 422 and scope.json()["detail"]["field"] == "scope"


def _events(body: str) -> list[tuple[str | None, str, Any]]:
    found = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        found.append((fields.get("id"), fields["event"], json.loads(fields["data"])))
    return found


def test_live_streams_the_running_sessions_samples_and_resumes_by_seq(db, store, clock):
    _lease(db, ENG, 4)
    first = store.record(ENG, 4, _metrics())
    clock.at = NOW + timedelta(seconds=5)
    second = store.record(ENG, 4, _stt(320))
    client = _client(db, clock)
    events = _events(client.get("/v1/voice/telemetry/live", headers=_as(ALICE)).text)
    assert events[0][1] == "session" and events[0][2]["generation"] == 4
    assert [(e[0], e[2]["kind"]) for e in events if e[1] == "sample"] == \
        [(first, "metrics"), (second, "stt")]
    resumed = _events(client.get("/v1/voice/telemetry/live",
                                 headers={**_as(ALICE), "Last-Event-ID": first}).text)
    assert [e[0] for e in resumed if e[1] == "sample"] == [second]


def test_live_ends_when_the_lease_goes_and_is_idle_for_another_workspace(db, store, clock):
    _lease(db, ENG, 4)
    store.record(ENG, 4, _metrics())
    client = _client(db, clock)
    assert [e[1] for e in _events(client.get("/v1/voice/telemetry/live",
                                             headers=_as(BOB)).text)] == ["idle"]
    _lease(db, ENG, 4, state=sm.STOPPED)
    assert [e[1] for e in _events(client.get("/v1/voice/telemetry/live",
                                             headers=_as(ALICE)).text)] == ["idle"]


def test_live_sends_end_once_the_session_lets_the_machine_go(db, store, clock):
    _lease(db, ENG, 4)
    store.record(ENG, 4, _metrics())
    released = {"done": False}

    async def _sleep(_seconds: float) -> None:
        if not released["done"]:
            released["done"] = True
            _lease(db, ENG, 5)  # a new generation: this stream's session is over

    client = _client(db, clock)
    client.app.state.voice_telemetry_timing = {"poll_seconds": 0.0, "sleep": _sleep,
                                               "max_polls": 5}
    events = _events(client.get("/v1/voice/telemetry/live", headers=_as(ALICE)).text)
    assert [e[1] for e in events] == ["session", "sample", "end"]


def test_a_bad_resume_cursor_is_a_422(db, clock):
    answer = _client(db, clock).get("/v1/voice/telemetry/live", params={"after": "../x"},
                                    headers=_as(ALICE))
    assert answer.status_code == 422
    assert answer.json()["detail"]["field"] == "after"
