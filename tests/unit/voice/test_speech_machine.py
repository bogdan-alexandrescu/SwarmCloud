"""The speech machine's lease and lifecycle (docs/design/voice-it-own-speech.md
§4.2, §4.4-§4.8, §5.3, §11.4; swarm_api/voice/speech_machine.py).

Start, readiness's record, the lease and its generation, idle, a closed tab,
a stock-out with no second zone tried, the sweep, and the Compute REST calls.
Firestore is the in-memory fake, Compute a fake of the one instance; no
network, no cloud credential, no emulator.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_catalogue as cat  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)
ZONE = "us-central1-a"
CONFIG = sm.SpeechConfig(project="proj", zone=ZONE, instance="swarm-speech-a",
                         address="10.40.0.9", image="reg/swarm-speech@sha256:" + "a" * 64)
ALICE = sm.SessionRef(tenant_id="eng", person="alice@example.com",
                      project_id="vp_" + "1" * 32, session_id="vs_" + "1" * 32)
BOB = sm.SessionRef(tenant_id="eng", person="bob@example.com",
                    project_id="vp_" + "2" * 32, session_id="vs_" + "2" * 32)
CHOSEN = dict(cat.defaults((ZONE,)))


class FakeCompute:
    def __init__(self, status: str = sm.TERMINATED, *, machine_type: str = "g2-standard-4",
                 running_after: int = 1, stockout: bool = False) -> None:
        self.status = status
        self.machine_type = machine_type
        self.metadata: dict[str, str] = {sm.META_IMAGE: "", sm.META_MODELS: "{}"}
        self.fingerprint = "fp1"
        self.running_after = running_after
        self.stockout = stockout
        self.calls: list[tuple[str, Any]] = []
        self._gets_after_start = 0
        self.started = False

    def get(self) -> sm.Instance:
        self.calls.append(("get", None))
        if self.started and self.status != sm.INSTANCE_RUNNING:
            self._gets_after_start += 1
            if self.running_after and self._gets_after_start >= self.running_after:
                self.status = sm.INSTANCE_RUNNING
        return sm.Instance(status=self.status, machine_type=self.machine_type,
                           metadata=dict(self.metadata), fingerprint=self.fingerprint)

    def start(self) -> None:
        self.calls.append(("start", None))
        if self.stockout:
            raise sm.ComputeError("no L4", code="ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS")
        self.started = True
        self.status = "PROVISIONING"

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self.status = sm.TERMINATED
        self.started = False

    def set_machine_type(self, machine_type: str) -> None:
        assert self.status == sm.TERMINATED, "a machine type is only set on a stopped machine"
        self.calls.append(("set_machine_type", machine_type))
        self.machine_type = machine_type

    def set_metadata(self, items: Mapping[str, str], fingerprint: str) -> None:
        assert self.status == sm.TERMINATED
        assert fingerprint == self.fingerprint
        self.calls.append(("set_metadata", dict(items)))
        self.metadata = dict(items)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class Clock:
    def __init__(self) -> None:
        self.wall = T0
        self.mono = 1000.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.wall

    def clock(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


def _mark_live(db: FakeFirestore, session: sm.SessionRef) -> None:
    db.docs[f"{vs.LIVE_MARKERS}/{vs.marker_id('', session.person)}"] = {
        "tenant_id": session.tenant_id, "project_id": session.project_id,
        "session_id": session.session_id, "at": T0}


def _machine(db: FakeFirestore, compute: FakeCompute, clock: Clock, applied: list | None = None
             ) -> sm.SpeechMachine:
    return sm.SpeechMachine(db, compute, config=CONFIG, now=clock.now, sleep=clock.sleep,
                            clock=clock.clock,
                            applied=(lambda values, image: applied.append((dict(values), image)))
                            if applied is not None else None)


def _lease(db: FakeFirestore) -> dict[str, Any]:
    return db.docs[f"{sm.COLLECTION}/{sm.LEASE}"]


def _audit(db: FakeFirestore, action: str) -> list[dict[str, Any]]:
    return [row for path, row in db.docs.items()
            if path.startswith(vs.AUDIT + "/") and row.get("action") == action]


@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    _mark_live(store, ALICE)
    _mark_live(store, BOB)
    return store


# ---- start (§4.2) -----------------------------------------------------------


def test_a_start_applies_settings_to_the_stopped_machine_then_polls_it_to_running(db):
    compute, clock = FakeCompute(machine_type="g2-standard-8", running_after=3), Clock()
    started = _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)

    assert compute.names()[:4] == ["get", "set_machine_type", "set_metadata", "start"]
    assert ("set_machine_type", "g2-standard-4") in compute.calls
    meta = next(v for n, v in compute.calls if n == "set_metadata")
    assert meta[sm.META_IMAGE] == CONFIG.image
    assert meta[sm.META_IDLE] == "5"
    assert json.loads(meta[sm.META_MODELS])["stt"]["name"] == CHOSEN["stt_model"]
    assert compute.status == sm.INSTANCE_RUNNING
    assert clock.slept and set(clock.slept) == {sm.POLL_SECONDS}
    assert started.image_changed is True
    assert started.holder == sm.Holder(session_id=ALICE.session_id, generation=1)
    lease = _lease(db)
    assert lease["state"] == sm.STARTING and lease["generation"] == 1
    assert lease["start_phases"]["start.boot"] == pytest.approx(started.boot_seconds)
    assert lease["applied"]["machine"] == "g2-standard-4" and lease["applied"]["zone"] == ZONE


def test_an_unchanged_machine_gets_no_set_call_and_no_update_status(db):
    compute, clock = FakeCompute(), Clock()
    compute.metadata = sm.wanted_metadata(CHOSEN, CONFIG.image)
    started = _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert "set_machine_type" not in compute.names() and "set_metadata" not in compute.names()
    assert started.image_changed is False


def test_the_models_metadata_is_what_the_speech_server_parses():
    # The speech server's own parser, loaded from its file: stdlib only, so
    # this environment needs none of the server's GPU dependencies.
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "apps/swarm-speech/swarm_speech/models.py"
    spec = importlib.util.spec_from_file_location("_speech_models_under_test", path)
    models = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = models  # dataclasses look their module up
    spec.loader.exec_module(models)
    parsed = models.parse_models(sm.models_metadata(CHOSEN))
    assert parsed.stt.name == CHOSEN["stt_model"] and parsed.tts.name == CHOSEN["tts_model"]


def test_a_start_for_a_session_that_is_not_the_persons_live_one_is_refused(db):
    stranger = sm.SessionRef(tenant_id="eng", person=ALICE.person, project_id=ALICE.project_id,
                             session_id="vs_" + "9" * 32)
    compute = FakeCompute()
    with pytest.raises(vs.VoiceSessionNotLive):
        _machine(db, compute, Clock()).start(stranger, chosen=CHOSEN)
    assert compute.calls == []


def test_readiness_runs_the_lease_and_marks_settings_applied(db):
    compute, clock, applied = FakeCompute(), Clock(), []
    machine = _machine(db, compute, clock, applied)
    started = machine.start(ALICE, chosen=CHOSEN)
    assert sm.SpeechMachine.started_of(_lease(db)) == started
    machine.ready(started.holder, started)
    assert _lease(db)["state"] == sm.RUNNING
    assert applied == [(started.applied, CONFIG.image)]
    # Once ready, a re-attaching socket has no start to apply.
    assert sm.SpeechMachine.started_of(_lease(db)) is None


# ---- one session at a time, and the generation (§4.6, invariant 5) ----------


def test_a_second_session_is_refused_while_the_lease_is_held(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    machine.start(ALICE, chosen=CHOSEN)
    with pytest.raises(sm.MachineInUse):
        machine.start(BOB, chosen=CHOSEN)
    assert _lease(db)["session_id"] == ALICE.session_id


def test_a_lease_with_no_heartbeat_is_taken_over_and_the_old_holder_is_fenced(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    first = machine.start(ALICE, chosen=CHOSEN).holder
    clock.advance(sm.HEARTBEAT_STALE_SECONDS + 1)
    second = machine.start(BOB, chosen=CHOSEN).holder
    assert second.generation == first.generation + 1

    with pytest.raises(sm.StaleLease):
        machine.beat(first, socket_id="sk_old")
    stops_before = compute.names().count("stop")
    assert machine.stop(first, cause=sm.END_BY_PERSON) is False
    assert compute.names().count("stop") == stops_before, "a stale holder touched the machine"
    assert _lease(db)["session_id"] == BOB.session_id and _lease(db)["state"] == sm.STARTING


def test_the_start_refreshes_the_heartbeat_while_it_polls(db):
    compute, clock = FakeCompute(running_after=40), Clock()
    _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    lease = _lease(db)
    assert sm.holds(lease, clock.now()), "a slow start read as an abandoned lease"
    assert clock.now() - lease["heartbeat_at"] < timedelta(seconds=sm.HEARTBEAT_SECONDS + 1)


# ---- no free L4, not ready (§4.1, §4.7) -------------------------------------


def test_a_stock_out_falls_back_with_no_second_zone_tried(db):
    compute, clock = FakeCompute(stockout=True), Clock()
    with pytest.raises(sm.NoFreeL4) as raised:
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert raised.value.reason == sm.FALLBACK_NO_L4 and raised.value.zone == ZONE
    assert compute.names().count("start") == 1, "the start was retried"
    lease = _lease(db)
    assert lease["state"] == sm.STOPPED and lease["cause"] == sm.FALLBACK_NO_L4
    assert lease["zone"] == ZONE
    rows = _audit(db, sm.AUDIT_NO_L4)
    assert len(rows) == 1 and rows[0]["detail"]["zone"] == ZONE
    # The next Start tries the same machine again.
    compute.stockout = False
    assert _machine(db, compute, clock).start(ALICE, chosen=CHOSEN).holder.generation == 2


def test_a_machine_not_running_in_four_minutes_is_stopped_and_not_ready(db):
    compute, clock = FakeCompute(running_after=0), Clock()
    with pytest.raises(sm.MachineNotReady):
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert compute.names()[-1] == "stop"
    assert _lease(db)["state"] == sm.STOPPED
    assert sum(clock.slept) <= sm.READY_SECONDS + sm.POLL_SECONDS


# ---- stop, idle, a closed tab, the sweep (§4.4, §4.5, §4.8) ------------------


class Ender:
    def __init__(self) -> None:
        self.ended: list[tuple[str, str]] = []

    def end(self, lease: Mapping[str, Any], cause: str) -> None:
        self.ended.append((str(lease.get("session_id")), cause))


def test_a_stop_counts_the_minutes_from_the_start_call(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    holder = machine.start(ALICE, chosen=CHOSEN).holder
    clock.advance(600)
    ender = Ender()
    assert machine.end_session(holder, cause=sm.END_BY_PERSON, ender=ender) is True
    assert ender.ended == [(ALICE.session_id, sm.END_BY_PERSON)]
    assert compute.status == sm.TERMINATED
    lease = _lease(db)
    assert lease["state"] == sm.STOPPED and lease["minutes"] >= 10.0
    row = _audit(db, sm.AUDIT_STOPPED)[-1]["detail"]
    assert row["cause"] == sm.END_BY_PERSON and row["generation"] == holder.generation
    assert row["usd"] == pytest.approx(row["minutes"] / 60 * cat.machine("g2-standard-4").usd_per_hour,
                                       abs=1e-3)


def test_idle_is_due_after_the_idle_time_without_activity(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    started = machine.start(ALICE, chosen=CHOSEN)
    machine.ready(started.holder, started)
    lease = machine.beat(started.holder, socket_id="sk_a", activity_at=clock.now())
    assert sm.due_cause(lease, clock.now()) is None
    clock.advance(started.idle_seconds - 1)
    lease = machine.beat(started.holder, socket_id="sk_a")
    assert sm.due_cause(lease, clock.now()) is None
    clock.advance(2)
    lease = machine.beat(started.holder, socket_id="sk_a")
    assert sm.due_cause(lease, clock.now()) == sm.END_IDLE


def test_a_tab_closed_for_a_minute_is_due_and_a_rotation_is_not(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    holder = machine.start(ALICE, chosen=CHOSEN).holder
    machine.attach(holder, socket_id="sk_old")
    machine.attach(holder, socket_id="sk_new")
    lease = machine.detach(holder, socket_id="sk_old")  # a rotation
    assert lease["detached_at"] is None
    lease = machine.detach(holder, socket_id="sk_new")
    clock.advance(sm.TAB_CLOSED_SECONDS - 1)
    assert sm.due_cause(lease, clock.now()) is None
    clock.advance(1)
    assert sm.due_cause(lease, clock.now()) == sm.END_TAB_CLOSED


def test_the_sweep_ends_a_lease_that_lost_its_heartbeat_and_stops_the_machine(db):
    compute, clock = FakeCompute(), Clock()
    machine = _machine(db, compute, clock)
    machine.start(ALICE, chosen=CHOSEN)
    ender = Ender()
    assert machine.sweep(ender)["ended"] is None, "a fresh lease was swept"
    clock.advance(sm.HEARTBEAT_STALE_SECONDS + 1)
    report = machine.sweep(ender)
    assert report["ended"] == sm.END_SWEPT and report["stopped"] is True
    assert ender.ended == [(ALICE.session_id, sm.END_SWEPT)]
    assert compute.status == sm.TERMINATED and _lease(db)["state"] == sm.STOPPED


def test_the_sweep_stops_a_running_instance_no_lease_holds(db):
    compute, clock = FakeCompute(status=sm.INSTANCE_RUNNING), Clock()
    report = _machine(db, compute, clock).sweep(Ender())
    assert report["stopped"] is True and compute.names() == ["get", "stop"]


def test_the_sweep_leaves_a_stopped_instance_alone(db):
    compute = FakeCompute()
    report = _machine(db, compute, Clock()).sweep(Ender())
    assert report["stopped"] is False and compute.names() == ["get"]


def test_fall_back_makes_the_session_a_text_session(db):
    path = f"{vs.PROJECTS}/{ALICE.project_id}/{vs.SESSIONS}/{ALICE.session_id}"
    db.docs[path] = {"tenant_id": "eng", "person": ALICE.person, "mode": sm.MODE_SPEECH,
                     "state": vs.LIVE}
    _machine(db, FakeCompute(), Clock()).fall_back(ALICE, reason=sm.FALLBACK_NO_L4)
    assert db.docs[path]["mode"] == vs.MODE_TEXT
    assert db.docs[path]["fallback"]["from"] == "own"
    assert db.docs[path]["fallback"]["reason"] == sm.FALLBACK_NO_L4


# ---- configuration and the REST calls ---------------------------------------


def test_the_config_reads_the_swarm_speech_environment():
    config = sm.SpeechConfig.from_env({
        "SWARM_SPEECH_PROJECT": "proj", "SWARM_SPEECH_ZONES": ZONE,
        "SWARM_SPEECH_INSTANCE": "swarm-speech-a", "SWARM_SPEECH_ADDR": "10.40.0.9",
        "SWARM_SPEECH_SWEEP_AUDIENCE": "https://swarm-api.dev.swarm.internal",
        "SWARM_SPEECH_SWEEP_CALLERS": "Tick@proj.iam.gserviceaccount.com"})
    assert config.enabled and config.stream_url == "ws://10.40.0.9:8080/v1/stream"
    assert config.audience == "swarm-speech"
    assert config.sweep_callers == ("tick@proj.iam.gserviceaccount.com",)
    assert not sm.SpeechConfig.from_env({}).enabled


class _Response:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code, self._body = status, body

    def json(self) -> dict[str, Any]:
        return self._body


class _Session:
    def __init__(self, answers: list[_Response]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, json: Any = None, timeout: float = 0) -> _Response:
        self.requests.append((method, url, json))
        return self.answers.pop(0)


def test_rest_compute_reads_a_stock_out_from_the_start_operation():
    session = _Session([
        _Response(200, {"name": "op-1", "status": "RUNNING"}),
        _Response(200, {"name": "op-1", "status": "DONE", "error": {"errors": [
            {"code": "ZONE_RESOURCE_POOL_EXHAUSTED", "message": "no L4"}]}}),
    ])
    with pytest.raises(sm.ComputeError) as raised:
        sm.RestCompute(CONFIG, session=session).start()
    assert raised.value.code in sm.STOCKOUT_CODES
    base = "https://compute.googleapis.com/compute/v1/projects/proj/zones/us-central1-a"
    assert session.requests[0][:2] == ("POST", f"{base}/instances/swarm-speech-a/start")
    assert session.requests[1][:2] == ("POST", f"{base}/operations/op-1/wait")
    assert all(ZONE in url for _m, url, _b in session.requests), "another zone was named"


def test_rest_compute_sets_the_machine_type_and_metadata_by_name():
    session = _Session([_Response(200, {"status": "DONE"}), _Response(200, {"status": "DONE"}),
                        _Response(200, {"status": "TERMINATED", "machineType":
                                        "https://x/zones/us-central1-a/machineTypes/g2-standard-8",
                                        "metadata": {"fingerprint": "f",
                                                     "items": [{"key": "k", "value": "v"}]}})])
    compute = sm.RestCompute(CONFIG, session=session)
    compute.set_machine_type("g2-standard-8")
    compute.set_metadata({"k": "v"}, "f")
    assert session.requests[0][2] == {"machineType": "zones/us-central1-a/machineTypes/g2-standard-8"}
    assert session.requests[1][2] == {"fingerprint": "f", "items": [{"key": "k", "value": "v"}]}
    instance = compute.get()
    assert instance.machine_type == "g2-standard-8" and instance.metadata == {"k": "v"}
