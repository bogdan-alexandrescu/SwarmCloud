"""The warm request (docs/design/voice-it-own-speech.md §4.1 "The warm request";
swarm_api/voice/speech_machine.py THE WARM; routes/voice.py `warm_speech`).

The Talk page asks once, when it opens, for the speech machine to be started
in the background, so the first spoken turn does not wait out a cold start.
The owner's condition (2026-11-23): the idle stop is unchanged. So:

  * the warm starts the machine only when no lease holds it, through the same
    start a session uses, and only for a caller a session start would admit
    (the Voice it list, swarm-voice deployed, the day cap) with own speech the
    engine Settings select;
  * it is idempotent and rate-limited per person, and answers at once;
  * a warmed machine nobody talks to is stopped by the same idle rule
    (`activity_at` + the lease's `idle_seconds`, Settings' idle) as today, and
    a warm never extends, resets or bypasses a held lease's idle timer;
  * a session that starts while the machine is warm adopts it.

Firestore is the in-memory fake, Compute a fake, the clock fake; the
background runner runs inline or not at all: no network, no cloud credential,
no emulator.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

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
    Ender,
    FakeCompute,
    _audit,
    _lease,
    _machine,
    _mark_live,
)

from swarm_api import modelprices  # noqa: E402
from swarm_api.deps import get_context, tenant_scope  # noqa: E402
from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.routes import accounts as raccounts  # noqa: E402
from swarm_api.routes import voice as rv  # noqa: E402
from swarm_api.voice import ledger as vledger  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import settings as vsettings  # noqa: E402
from swarm_api.voice import speech_catalogue as cat  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402

WARM_ROUTE = "/v1/voice/speech/warm"
#: Talk seconds per dollar at the seeded price (test_voice_budget.py's figure).
PER_DOLLAR = 1200


def _warm(machine: sm.SpeechMachine, who: sm.SessionRef = ALICE) -> tuple[str, sm.Holder | None]:
    return machine.begin_warm(tenant_id=who.tenant_id, person=who.person, chosen=CHOSEN)


def _warmed(db: FakeFirestore, compute: FakeCompute, clock: Clock) -> tuple[sm.SpeechMachine, sm.Holder]:
    """A machine the page warmed, its start finished, nobody talking."""
    machine = _machine(db, compute, clock)
    outcome, holder = _warm(machine)
    assert outcome == sm.WARM_STARTED and holder is not None
    machine.finish_warm(holder, tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=CHOSEN)
    return machine, holder


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


# ---- the machine helper -----------------------------------------------------------


def test_a_warm_takes_a_lease_for_no_session_and_starts_the_machine_through_the_start(db):
    compute, clock = FakeCompute(), Clock()
    machine, holder = _warmed(db, compute, clock)

    assert holder.session_id.startswith(sm.WARM_PREFIX)
    assert compute.status == sm.INSTANCE_RUNNING
    # The same start a session's makes: Settings applied to the stopped
    # machine, then `instances.start` in its own zone; no other call shape.
    assert [n for n, _ in compute.changes()] == ["set_metadata", "start"]
    lease = _lease(db)
    assert lease[sm.WARM] is True and lease["project_id"] == ""
    assert lease["person"] == ALICE.person and lease["tenant_id"] == ALICE.tenant_id
    assert lease["state"] == sm.RUNNING and lease["warmed_at"] == clock.now()
    assert lease["activity_at"] == clock.now()
    assert lease["idle_seconds"] == sm.idle_seconds_of(CHOSEN)


def test_a_warm_needs_no_live_session_marker(db):
    # A session's take needs the person's live marker; the warm has no session.
    assert vs.LIVE_MARKERS not in {p.split("/")[0] for p in db.docs}
    outcome, holder = _warm(_machine(db, FakeCompute(), Clock()))
    assert outcome == sm.WARM_STARTED and holder is not None


def test_a_warm_while_a_session_holds_the_machine_changes_nothing(db):
    compute, clock = FakeCompute(), Clock()
    _mark_live(db, ALICE)
    machine = _machine(db, compute, clock)
    started = machine.start(ALICE, chosen=CHOSEN)
    machine.ready(started.holder, started)
    machine.beat(started.holder, socket_id="sk_a", activity_at=clock.now())
    clock.advance(200)
    before = dict(_lease(db))
    calls = list(compute.calls)

    assert _warm(machine) == (sm.WARM_HELD, None)
    assert _warm(machine, BOB) == (sm.WARM_HELD, None)
    # No new generation, no heartbeat, no activity: the idle timer is untouched.
    assert _lease(db) == before
    assert compute.calls == calls


def test_a_second_warm_is_idempotent_while_the_first_holds(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    outcome, holder = _warm(machine)
    assert outcome == sm.WARM_STARTED and holder is not None
    before = dict(_lease(db))
    clock.advance(5)
    assert _warm(machine) == (sm.WARM_HELD, None)
    assert _lease(db) == before


def test_a_page_reloaded_in_a_loop_starts_the_machine_once_per_interval(db):
    compute, clock = FakeCompute(), Clock()
    machine, _holder = _warmed(db, compute, clock)
    idle = sm.idle_seconds_of(CHOSEN)
    clock.advance(idle)
    machine.sweep(Ender())
    assert compute.status == sm.TERMINATED
    starts = compute.names().count("start")

    # Reloaded every ten seconds until just before the interval is up.
    while clock.now() < _lease(db)["started_at"] + timedelta(seconds=sm.WARM_EVERY_SECONDS - 10):
        assert _warm(machine) == (sm.WARM_RATE_LIMITED, None)
        clock.advance(10)
    assert compute.names().count("start") == starts
    assert compute.status == sm.TERMINATED

    clock.advance(10)
    outcome, holder = _warm(machine)
    assert outcome == sm.WARM_STARTED and holder is not None


def test_the_rate_limit_is_per_person(db):
    compute, clock = FakeCompute(), Clock()
    machine, _holder = _warmed(db, compute, clock)
    clock.advance(sm.idle_seconds_of(CHOSEN))
    machine.sweep(Ender())
    assert _warm(machine) == (sm.WARM_RATE_LIMITED, None)
    outcome, _ = _warm(machine, BOB)
    assert outcome == sm.WARM_STARTED


def test_the_rate_limit_keeps_no_email_in_a_document_name(db):
    _warmed(db, FakeCompute(), Clock())
    assert not any(ALICE.person in path for path in db.docs)


# ---- the idle stop is unchanged ---------------------------------------------------


def test_a_warmed_machine_nobody_uses_is_stopped_by_the_idle_rule_after_the_idle_time(db):
    compute, clock = FakeCompute(), Clock()
    machine, holder = _warmed(db, compute, clock)
    idle = sm.idle_seconds_of(CHOSEN)
    assert idle == sm.DEFAULT_IDLE_SECONDS == 300

    # Long past the heartbeat's two minutes: a warm lease has no socket and
    # nobody heartbeats it, so only the idle rule ends it.
    clock.advance(idle - 1)
    assert sm.due_cause(_lease(db), clock.now()) is None
    assert sm.holds(_lease(db), clock.now()) is True
    report = machine.sweep(Ender())
    assert report["ended"] is None and report["stopped"] is False
    assert compute.status == sm.INSTANCE_RUNNING

    clock.advance(1)
    assert sm.due_cause(_lease(db), clock.now()) == sm.END_IDLE
    ender = Ender()
    report = machine.sweep(ender)
    assert report["ended"] == sm.END_IDLE and report["stopped"] is True
    assert compute.status == sm.TERMINATED
    # No session to end: swarm-voice is told nothing.
    assert ender.ended == []
    row = _audit(db, sm.AUDIT_STOPPED)[-1]
    assert row["detail"]["cause"] == sm.END_IDLE and row["session_id"] == holder.session_id
    assert row["by"] == ALICE.person and row["detail"]["usd"] is not None


@pytest.mark.parametrize("idle", ["2m", "15m"])
def test_the_warm_uses_settings_idle_time_as_a_start_does(db, idle):
    compute, clock = FakeCompute(), Clock()
    chosen = {**CHOSEN, "idle": idle}
    machine = _machine(db, compute, clock)
    _outcome, holder = machine.begin_warm(tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=chosen)
    assert holder is not None
    machine.finish_warm(holder, tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=chosen)
    seconds = cat.idle_seconds(idle)
    assert _lease(db)["idle_seconds"] == seconds
    clock.advance(seconds - 1)
    assert sm.due_cause(_lease(db), clock.now()) is None
    clock.advance(1)
    assert sm.due_cause(_lease(db), clock.now()) == sm.END_IDLE


def test_the_longest_a_warm_runs_with_nobody_speaking_is_the_start_plus_the_idle_plus_a_sweep(db):
    # The worst case: the start uses its whole budget, Settings' longest idle,
    # and the sweep (every two minutes) arrives just short of the idle being due.
    compute, clock = FakeCompute(running_after=0), Clock()
    chosen = {**CHOSEN, "idle": cat.IDLE[-1].name}
    machine = _machine(db, compute, clock)
    _outcome, holder = machine.begin_warm(tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=chosen)
    assert holder is not None
    started_at = clock.now()

    def boots_at_the_last_poll(zone: str) -> sm.Instance | None:
        found = FakeCompute.get(compute, zone)
        if found is not None and clock.clock() >= began + sm.READY_SECONDS - sm.POLL_SECONDS:
            compute.machines[zone]["status"] = sm.INSTANCE_RUNNING
            found = FakeCompute.get(compute, zone)
        return found

    began = clock.clock()
    compute.get = boots_at_the_last_poll  # type: ignore[method-assign]
    machine.finish_warm(holder, tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=chosen)
    assert compute.status == sm.INSTANCE_RUNNING
    clock.advance(cat.IDLE[-1].seconds - 1)
    machine.sweep(Ender())
    assert compute.status == sm.INSTANCE_RUNNING
    clock.advance(SWEEP_EVERY_SECONDS)
    machine.sweep(Ender())
    assert compute.status == sm.TERMINATED
    ran = (clock.now() - started_at).total_seconds()
    assert ran <= sm.READY_SECONDS + cat.IDLE[-1].seconds + SWEEP_EVERY_SECONDS
    assert ran / 60 <= 21


#: terraform/infra/voice_speech_sweep.tf's schedule, `*/2 * * * *`.
SWEEP_EVERY_SECONDS = 120


def test_the_sweep_schedule_is_the_two_minutes_the_bound_counts():
    tf = Path(__file__).resolve().parents[3] / "terraform" / "infra" / "voice_speech_sweep.tf"
    assert 'schedule  = "*/2 * * * *"' in tf.read_text()


def test_a_warm_lease_is_swept_like_any_start_when_its_start_stops_beating(db):
    # swarm-api went away mid-start: the warm's heartbeat goes stale and the
    # sweep stops it exactly as it stops a session's abandoned start.
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    _outcome, holder = _warm(machine)
    assert holder is not None
    clock.advance(sm.HEARTBEAT_STALE_SECONDS)
    assert sm.due_cause(_lease(db), clock.now()) == sm.END_SWEPT


def test_a_session_adopts_a_warm_machine_and_the_idle_rule_is_its_own(db):
    compute, clock = FakeCompute(), Clock()
    machine, warm = _warmed(db, compute, clock)
    warm_started = _lease(db)["started_at"]
    clock.advance(90)
    _mark_live(db, ALICE)
    started = machine.start(ALICE, chosen=CHOSEN)

    lease = _lease(db)
    assert started.holder.generation == warm.generation + 1
    assert lease["session_id"] == ALICE.session_id and not lease.get(sm.WARM)
    # The machine was running: the session's start serves it as it is.
    assert compute.names().count("start") == 1
    # The session's ledger line counts from the warm's start call.
    assert lease["started_at"] == warm_started
    # The take sets activity_at as every take does; from here the session's
    # own heartbeat and idle rule apply, exactly as for a cold start.
    assert lease["activity_at"] == clock.now() and lease["state"] == sm.STARTING
    assert sm.holds(lease, clock.now())
    clock.advance(sm.HEARTBEAT_STALE_SECONDS)
    assert sm.due_cause(_lease(db), clock.now()) == sm.END_SWEPT
    # The warm's holder is fenced out.
    assert machine.stop(warm, cause=sm.END_IDLE) is False


def test_a_session_adopts_a_warm_still_starting_and_the_warm_stops_writing(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    _outcome, warm = _warm(machine)
    assert warm is not None
    _mark_live(db, ALICE)
    started = machine.start(ALICE, chosen=CHOSEN)
    assert started.holder.session_id == ALICE.session_id
    # The warm's thread, run after the take, finds its lease gone and touches nothing.
    machine.finish_warm(warm, tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=CHOSEN)
    assert _lease(db)["session_id"] == ALICE.session_id
    assert _lease(db)["generation"] == started.holder.generation


def test_a_warm_that_finds_no_l4_releases_its_lease_and_raises_nothing(db):
    compute, clock = FakeCompute(status=None, stockout=True), Clock()
    machine = _machine(db, compute, clock)
    _outcome, holder = _warm(machine)
    assert holder is not None
    machine.finish_warm(holder, tenant_id=ALICE.tenant_id, person=ALICE.person, chosen=CHOSEN)
    assert _lease(db)["state"] == sm.STOPPED
    assert sm.holds(_lease(db), clock.now()) is False


# ---- the route ------------------------------------------------------------------


class FakeVoice:
    def end(self, session_id: str, *, cause: str) -> bool:
        return True


class Untouchable:
    def voice_pool(self, person: str) -> dict:
        raise AssertionError("the warm never asks the OpenAI pool")


class Rig:
    def __init__(self, *, speech: sm.SpeechConfig = CONFIG, voice: Any = "fake",
                 run: bool = False) -> None:
        self.db = FakeFirestore()
        self.clock = Clock()
        self.compute = FakeCompute()
        self.machine = _machine(self.db, self.compute, self.clock)
        self.runs: list[Callable[[], None]] = []
        app = FastAPI()
        app.include_router(rv.router)

        @app.exception_handler(ApiError)
        async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
            return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

        ctx = SimpleNamespace(db=self.db, now=self.clock.now, settings=SimpleNamespace(
            voice_people=(ALICE.person,), child_key="", worker_class_models={},
            voice_url="http://voice.internal"))
        app.state.ctx = ctx
        app.state.voice_client = FakeVoice() if voice == "fake" else voice
        app.state.speech_config = speech
        app.state.speech_machine = self.machine

        def runner(job: Callable[[], None]) -> None:
            self.runs.append(job)
            if run:
                job()

        app.state.speech_warm_runner = runner
        app.state.voice_budget_ledger = vledger.VoiceLedger(
            self.db, now=self.clock.now, prices=lambda: modelprices.SEED, time_zone=lambda email: None)
        app.dependency_overrides[tenant_scope] = lambda: ALICE.tenant_id
        app.dependency_overrides[get_context] = lambda: ctx
        app.dependency_overrides[raccounts.account_pool] = lambda: Untouchable()
        self.app = app
        self.client = TestClient(app)

    def as_person(self, email: str = ALICE.person) -> "Rig":
        self.app.dependency_overrides[rv.allowed_person] = lambda: email
        return self

    def warm(self) -> Any:
        return self.client.post(WARM_ROUTE)


def test_the_route_answers_at_once_and_starts_the_machine_in_the_background():
    rig = Rig().as_person()
    answer = rig.warm()
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"warming": True, "reason": sm.WARM_STARTED,
                             "within_seconds": int(sm.READY_SECONDS)}
    # Answered before any Compute call: the start is the runner's.
    assert rig.compute.calls == [] and len(rig.runs) == 1
    rig.runs[0]()
    assert rig.compute.status == sm.INSTANCE_RUNNING and _lease(rig.db)[sm.WARM] is True


def test_the_route_is_idempotent_and_starts_one_machine():
    rig = Rig(run=True).as_person()
    assert rig.warm().json()["warming"] is True
    again = rig.warm()
    assert again.status_code == 200
    assert again.json()["warming"] is False and again.json()["reason"] == sm.WARM_HELD
    assert len(rig.runs) == 1 and rig.compute.names().count("start") == 1


def test_the_route_refuses_whom_a_session_start_refuses():
    # Not on the Voice it list: the same 403, nothing written.
    rig = Rig()
    rig.app.dependency_overrides[get_context] = lambda: rig.app.state.ctx
    from swarm_api.deps import current_auth
    rig.app.dependency_overrides[current_auth] = lambda: SimpleNamespace(email="mallory@example.com")
    refused = rig.warm()
    assert refused.status_code == 403
    assert rig.db.docs == {} and rig.runs == []


def test_the_route_is_unavailable_without_swarm_voice_as_a_start_is():
    rig = Rig(voice=None).as_person()
    rig.app.state.ctx.settings.voice_url = ""
    refused = rig.warm()
    assert refused.status_code == 503, refused.text
    assert rig.runs == [] and rig.compute.calls == []


def test_the_route_refuses_at_the_day_cap_as_a_start_does():
    rig = Rig().as_person()
    project = "vp_" + "3" * 32
    now = rig.clock.now()
    rig.db.docs[f"{vs.PROJECTS}/{project}"] = {
        "tenant_id": ALICE.tenant_id, "person": ALICE.person, "title": "t", "status": "active",
        "created_at": now, "updated_at": now}
    rig.db.docs[f"{vs.PROJECTS}/{project}/{vs.SESSIONS}/vs_{'4' * 32}"] = {
        "tenant_id": ALICE.tenant_id, "person": ALICE.person, "project_id": project,
        "state": vs.ENDED, "created_at": now, "live_at": now, "talk_seconds": 300 * PER_DOLLAR}
    before = dict(rig.db.docs)
    refused = rig.warm()
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == vledger.DAY_CAP_REACHED
    assert rig.db.docs == before and rig.runs == [] and rig.compute.calls == []


def test_the_route_warms_nothing_when_settings_select_gpt_live():
    rig = Rig().as_person()
    rig.db.docs[f"{vsettings.COLLECTION}/{vsettings.DOCUMENT}"] = {"engine": cat.ENGINE_GPT_LIVE}
    answer = rig.warm()
    assert answer.status_code == 200
    assert answer.json()["warming"] is False and answer.json()["reason"] == sm.WARM_NOT_SELECTED
    assert rig.runs == [] and rig.compute.calls == []
    assert not any(p.startswith(sm.COLLECTION + "/") for p in rig.db.docs)


def test_the_route_warms_nothing_where_the_engine_is_not_deployed():
    rig = Rig(speech=sm.SpeechConfig()).as_person()
    answer = rig.warm()
    assert answer.status_code == 200
    assert answer.json()["warming"] is False and answer.json()["reason"] == sm.WARM_NOT_SELECTED
    assert rig.runs == [] and rig.compute.calls == []


def test_the_route_warms_nothing_for_a_person_already_in_a_session():
    # A second start would be 409 (one live session per person): so is nothing warmed.
    rig = Rig().as_person()
    _mark_live(rig.db, ALICE)
    rig.db.docs[f"{vs.PROJECTS}/{ALICE.project_id}/{vs.SESSIONS}/{ALICE.session_id}"] = {
        "tenant_id": ALICE.tenant_id, "person": ALICE.person, "project_id": ALICE.project_id,
        "state": vs.LIVE, "mode": vs.MODE_TEXT, "created_at": rig.clock.now(),
        "live_at": rig.clock.now(), "activity_at": rig.clock.now()}
    answer = rig.warm()
    assert answer.status_code == 200, answer.text
    assert answer.json()["warming"] is False and answer.json()["reason"] == sm.WARM_LIVE_SESSION
    assert rig.runs == [] and rig.compute.calls == []


def test_the_route_takes_no_body_field():
    rig = Rig().as_person()
    refused = rig.client.post(WARM_ROUTE, json={"machine": "g2-standard-96"})
    assert refused.status_code == 422
    assert rig.runs == []
