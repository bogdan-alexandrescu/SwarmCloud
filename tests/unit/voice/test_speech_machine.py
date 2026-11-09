"""The speech machine's lease and lifecycle (docs/design/voice-it-own-speech.md
§4.2, §4.4-§4.8, §5.3, §11.4; swarm_api/voice/speech_machine.py).

Start, readiness's record, the lease and its generation, idle, a closed tab,
the zones in order on a stockout (owner, 2026-11-09), the sweep, and the
Compute REST calls. Firestore is the in-memory fake, Compute a fake of the
speech machine in each zone; no network, no cloud credential, no emulator.
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
REGION = "us-central1"
ZONES = (f"{REGION}-a", f"{REGION}-b", f"{REGION}-c")
ZONE, ZONE_B, ZONE_C = ZONES
CONFIG = sm.SpeechConfig(project="proj", zones=ZONES, instance="swarm-speech-",
                         template=f"projects/proj/regions/{REGION}/instanceTemplates/swarm-speech-1",
                         address="10.40.0.9", image="reg/swarm-speech@sha256:" + "a" * 64)
ALICE = sm.SessionRef(tenant_id="eng", person="alice@example.com",
                      project_id="vp_" + "1" * 32, session_id="vs_" + "1" * 32)
BOB = sm.SessionRef(tenant_id="eng", person="bob@example.com",
                    project_id="vp_" + "2" * 32, session_id="vs_" + "2" * 32)
CHOSEN = dict(cat.defaults((ZONE,)))
STOCKOUT = "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"


class FakeCompute:
    """Compute Engine's speech machines, one at most per zone. `status`,
    `metadata` and `machine_type` read and write the one machine there is
    (made in `zone` unless `status` is None); `stockout` makes every zone
    answer a stockout, `full` only those zones. Each call is recorded as
    (verb, zone), and the set calls as (verb, value). `cost` makes an
    operation take that many seconds of `clock`, waited on in pieces of
    RestCompute.TIMEOUT_SECONDS with the `Wait`'s tick between them, and
    TIMEOUT once the `Wait`'s seconds are spent."""

    def __init__(self, status: str | None = sm.TERMINATED, *, machine_type: str = "g2-standard-4",
                 running_after: int = 1, stockout: bool = False, zone: str = ZONE,
                 full: tuple[str, ...] = ()) -> None:
        self.machines: dict[str, dict[str, Any]] = {}
        if status is not None:
            self.add(zone, status, machine_type=machine_type)
        self.running_after = running_after
        self.stockout = stockout
        self.full = set(full)
        self.calls: list[tuple[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        #: Every machine that existed at once, by zone, after each call.
        self.at_once: list[list[str]] = []
        self.clock: Clock | None = None
        self.cost: dict[str, float] = {}
        self.waits: list[tuple[str, float]] = []

    def add(self, zone: str, status: str, *, machine_type: str = "g2-standard-4") -> None:
        self.machines[zone] = {"status": status, "machine_type": machine_type, "fingerprint": "fp1",
                               "metadata": {sm.META_IMAGE: "", sm.META_MODELS: "{}"},
                               "started": False, "gets": 0}

    def _one(self) -> dict[str, Any]:
        return next(iter(self.machines.values()))

    @property
    def status(self) -> str:
        return self._one()["status"] if self.machines else ""

    @status.setter
    def status(self, value: str) -> None:
        self._one()["status"] = value

    @property
    def metadata(self) -> dict[str, str]:
        return self._one()["metadata"]

    @metadata.setter
    def metadata(self, value: dict[str, str]) -> None:
        self._one()["metadata"] = dict(value)

    @property
    def machine_type(self) -> str:
        return self._one()["machine_type"]

    def _record(self, verb: str, value: Any) -> None:
        self.calls.append((verb, value))
        self.at_once.append(sorted(self.machines))

    def _take_time(self, verb: str, wait: sm.Wait | None) -> None:
        if wait is not None:
            self.waits.append((verb, wait.seconds))
        cost = self.cost.get(verb, 0.0)
        if not cost or self.clock is None:
            return
        spent = 0.0
        while spent < cost:
            if wait is not None and spent >= wait.seconds:
                raise sm.ComputeError("the compute operation did not finish", code=sm.TIMEOUT)
            piece = min(sm.RestCompute.TIMEOUT_SECONDS, cost - spent,
                        wait.seconds - spent if wait is not None else cost)
            if wait is not None:
                wait.tick()
            self.clock.advance(piece)
            spent += piece

    def _no_l4(self, zone: str) -> bool:
        return self.stockout or zone in self.full

    def get(self, zone: str) -> sm.Instance | None:
        self._record("get", zone)
        m = self.machines.get(zone)
        if m is None:
            return None
        if m["started"] and m["status"] != sm.INSTANCE_RUNNING:
            m["gets"] += 1
            if self.running_after and m["gets"] >= self.running_after:
                m["status"] = sm.INSTANCE_RUNNING
        return sm.Instance(status=m["status"], machine_type=m["machine_type"],
                           metadata=dict(m["metadata"]), fingerprint=m["fingerprint"], zone=zone)

    def create(self, zone: str, *, machine_type: str, metadata: Mapping[str, str],
               wait: sm.Wait | None = None) -> None:
        assert zone in CONFIG.zones
        assert zone not in self.machines, f"a second machine was created in {zone}"
        self._record("create", zone)
        self._take_time("create", wait)
        if self._no_l4(zone):
            raise sm.ComputeError("no L4", code=STOCKOUT)
        assert not self.machines, "a second machine was created while one exists"
        self.add(zone, "PROVISIONING", machine_type=machine_type)
        self.machines[zone].update(started=True, metadata=dict(metadata))
        self.created.append({"zone": zone, "machine_type": machine_type, "metadata": dict(metadata)})

    def delete(self, zone: str, *, wait: sm.Wait | None = None) -> None:
        self._record("delete", zone)
        self._take_time("delete", wait)
        self.machines.pop(zone, None)

    def start(self, zone: str, *, wait: sm.Wait | None = None) -> None:
        self._record("start", zone)
        self._take_time("start", wait)
        if self._no_l4(zone):
            raise sm.ComputeError("no L4", code=STOCKOUT)
        self.machines[zone].update(started=True, status="PROVISIONING", gets=0)

    def stop(self, zone: str) -> None:
        self._record("stop", zone)
        m = self.machines.get(zone)
        if m is None:
            raise sm.ComputeError("not found", code=sm.NOT_FOUND)
        m.update(status=sm.TERMINATED, started=False)

    def set_machine_type(self, zone: str, machine_type: str, *, wait: sm.Wait | None = None) -> None:
        assert self.machines[zone]["status"] == sm.TERMINATED, \
            "a machine type is only set on a stopped machine"
        self._record("set_machine_type", machine_type)
        self.machines[zone]["machine_type"] = machine_type

    def set_metadata(self, zone: str, items: Mapping[str, str], fingerprint: str, *,
                     wait: sm.Wait | None = None) -> None:
        assert self.machines[zone]["status"] == sm.TERMINATED
        assert fingerprint == self.machines[zone]["fingerprint"]
        self._record("set_metadata", dict(items))
        self.machines[zone]["metadata"] = dict(items)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def changes(self) -> list[tuple[str, Any]]:
        """The calls that are not a `get`."""
        return [(n, v) for n, v in self.calls if n != "get"]


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

    assert [n for n, _ in compute.changes()][:3] == ["set_machine_type", "set_metadata", "start"]
    assert compute.changes()[2] == ("start", ZONE)
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


def test_a_start_with_no_machine_creates_it_from_the_template_in_the_first_zone(db):
    compute, clock = FakeCompute(status=None), Clock()
    started = _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert [z for n, z in compute.calls if n == "create"] == [ZONE]
    made = compute.created[0]
    assert made["machine_type"] == CHOSEN["machine"]
    assert made["metadata"] == sm.wanted_metadata(CHOSEN, CONFIG.image)
    assert started.image_changed is True and compute.machines[ZONE]["status"] == sm.INSTANCE_RUNNING
    lease = _lease(db)
    assert lease["zone"] == ZONE and lease["stockouts"] == []
    assert _audit(db, sm.AUDIT_ZONE_STOCKOUT) == [] and _audit(db, sm.AUDIT_NO_L4) == []


def test_a_stockout_in_the_first_zone_creates_it_in_the_second_then_the_third(db):
    compute, clock = FakeCompute(status=None, full=(ZONE, ZONE_B)), Clock()
    _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert [z for n, z in compute.calls if n == "create"] == [ZONE, ZONE_B, ZONE_C]
    assert list(compute.machines) == [ZONE_C]
    lease = _lease(db)
    assert lease["state"] == sm.STARTING and lease["zone"] == ZONE_C
    assert [s["zone"] for s in lease["stockouts"]] == [ZONE, ZONE_B]
    assert [r["detail"]["zone"] for r in _audit(db, sm.AUDIT_ZONE_STOCKOUT)] == [ZONE, ZONE_B]
    assert _audit(db, sm.AUDIT_NO_L4) == [], "a start that found an L4 was counted refused"


def test_a_stockout_in_every_zone_falls_back_to_text_and_is_counted_once(db):
    compute, clock = FakeCompute(status=None, stockout=True), Clock()
    with pytest.raises(sm.NoFreeL4) as raised:
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert raised.value.reason == sm.FALLBACK_NO_L4 and raised.value.zone == ZONE_C
    assert [z for n, z in compute.calls if n == "create"] == list(ZONES), "a zone was tried twice"
    assert compute.machines == {}
    lease = _lease(db)
    assert lease["state"] == sm.STOPPED and lease["cause"] == sm.FALLBACK_NO_L4
    assert [s["zone"] for s in lease["stockouts"]] == list(ZONES)
    rows = _audit(db, sm.AUDIT_NO_L4)
    assert len(rows) == 1 and rows[0]["detail"]["zones"] == list(ZONES)
    assert len(_audit(db, sm.AUDIT_ZONE_STOCKOUT)) == 3
    # The next Start tries again from the first zone.
    compute.stockout = False
    assert _machine(db, compute, clock).start(ALICE, chosen=CHOSEN).holder.generation == 2
    assert list(compute.machines) == [ZONE]


def test_a_stopped_machine_starts_in_its_own_zone_and_no_other_is_made(db):
    compute, clock = FakeCompute(zone=ZONE_B), Clock()
    _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert ("start", ZONE_B) in compute.calls
    assert "create" not in compute.names() and "delete" not in compute.names()
    assert _lease(db)["zone"] == ZONE_B


def test_a_stopped_machine_whose_zone_has_no_l4_is_deleted_then_created_in_the_next(db):
    compute, clock = FakeCompute(zone=ZONE_B, full=(ZONE_B,)), Clock()
    _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    changes = compute.changes()
    start_at = changes.index(("start", ZONE_B))
    assert changes[start_at + 1:] == [("delete", ZONE_B), ("create", ZONE_C)]
    assert list(compute.machines) == [ZONE_C]
    # Never two machines: the delete finished before the create.
    assert all(len(zones) <= 1 for zones in compute.at_once)
    assert _lease(db)["zone"] == ZONE_C
    assert [s["zone"] for s in _lease(db)["stockouts"]] == [ZONE_B]


def test_a_moving_machine_wraps_round_once_and_tries_each_zone_at_most_once(db):
    compute, clock = FakeCompute(zone=ZONE_B, stockout=True), Clock()
    with pytest.raises(sm.NoFreeL4):
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    tried = [z for n, z in compute.calls if n in ("start", "create")]
    assert tried == [ZONE_B, ZONE_C, ZONE]
    assert compute.machines == {}
    assert _audit(db, sm.AUDIT_NO_L4)[0]["detail"]["zones"] == [ZONE_B, ZONE_C, ZONE]


def test_two_machines_found_become_one_keeping_the_running_one(db):
    compute, clock = FakeCompute(zone=ZONE), Clock()
    compute.add(ZONE_C, sm.INSTANCE_RUNNING)
    _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert ("delete", ZONE) in compute.calls and list(compute.machines) == [ZONE_C]
    assert "start" not in compute.names() and "create" not in compute.names()
    assert _lease(db)["zone"] == ZONE_C


def test_two_stopped_machines_become_one_keeping_the_first_in_zone_order(db):
    compute, clock = FakeCompute(zone=ZONE_C), Clock()
    compute.add(ZONE_B, sm.TERMINATED)
    _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert ("delete", ZONE_C) in compute.calls and list(compute.machines) == [ZONE_B]
    assert ("start", ZONE_B) in compute.calls


def test_the_lease_is_held_by_the_start_through_every_zone(db):
    compute, clock = FakeCompute(zone=ZONE, full=(ZONE, ZONE_B)), Clock()
    machine = _machine(db, compute, clock)
    seen: list[tuple[str, Any]] = []
    create, delete = compute.create, compute.delete

    def watch(verb: str, call: Any) -> Any:
        def wrapped(zone: str, **kwargs: Any) -> Any:
            lease = _lease(db)
            seen.append((verb, lease["session_id"], lease["state"], lease["generation"]))
            assert sm.holds(lease, clock.now())
            return call(zone, **kwargs)
        return wrapped

    compute.create, compute.delete = watch("create", create), watch("delete", delete)
    started = machine.start(ALICE, chosen=CHOSEN)
    assert [v for v, *_ in seen] == ["delete", "create", "create"]
    assert all(s[1:] == (ALICE.session_id, sm.STARTING, started.holder.generation) for s in seen)
    with pytest.raises(sm.MachineInUse):
        machine.start(BOB, chosen=CHOSEN)


def test_a_later_zone_is_not_tried_once_the_four_minutes_are_spent(db):
    compute, clock = FakeCompute(status=None, full=(ZONE,)), Clock()
    create = compute.create

    def slow(zone: str, **kwargs: Any) -> None:
        clock.advance(sm.READY_SECONDS)  # the first zone's answer took the whole budget
        create(zone, **kwargs)

    compute.create = slow
    with pytest.raises(sm.MachineNotReady) as raised:
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert [z for n, z in compute.calls if n == "create"] == [ZONE]
    assert ZONE in str(raised.value) and ZONE_B in str(raised.value)
    assert _lease(db)["state"] == sm.STOPPED


def test_a_machine_not_running_in_four_minutes_is_stopped_and_not_ready(db):
    compute, clock = FakeCompute(running_after=0), Clock()
    with pytest.raises(sm.MachineNotReady):
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert compute.names()[-1] == "stop"
    assert _lease(db)["state"] == sm.STOPPED
    assert sum(clock.slept) <= sm.READY_SECONDS + sm.POLL_SECONDS


def _heartbeat_gaps(db: FakeFirestore, clock: Clock) -> list[float]:
    """Seconds since the lease's heartbeat, read after each tick of `clock`
    while the start holds the lease."""
    gaps: list[float] = []
    advance = clock.advance

    def watched(seconds: float) -> None:
        advance(seconds)
        lease = db.docs.get(f"{sm.COLLECTION}/{sm.LEASE}") or {}
        if lease.get("state") == sm.STARTING:
            gaps.append((clock.now() - lease["heartbeat_at"]).total_seconds())

    clock.advance = watched  # type: ignore[method-assign]
    return gaps


@pytest.mark.parametrize("cost, ends", [(50.0, sm.NoFreeL4), (100.0, sm.MachineNotReady)])
def test_a_slow_stockout_in_every_zone_ends_inside_the_budget_with_the_lease_fresh(db, cost, ends):
    # A stopped machine in the first zone; every zone stocks out, and each
    # operation (the start, the delete, each create) takes `cost` seconds.
    compute, clock = FakeCompute(zone=ZONE, stockout=True), Clock()
    compute.clock, compute.cost = clock, {"start": cost, "delete": cost, "create": cost}
    gaps = _heartbeat_gaps(db, clock)
    began = clock.clock()
    with pytest.raises(ends) as raised:
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert clock.clock() - began <= sm.READY_SECONDS, "the start outlasted its four minutes"
    assert gaps and max(gaps) < sm.HEARTBEAT_STALE_SECONDS, "the lease read as abandoned"
    # No operation was given longer than what was left of the budget.
    assert all(seconds <= sm.READY_SECONDS for _verb, seconds in compute.waits)
    if ends is sm.MachineNotReady:
        # start(a) and delete(a) took 200 s; create(b) ran out at 240 s.
        assert str(raised.value).endswith(f"{ZONE_C} not tried")
        assert ZONE in str(raised.value) and ZONE_B in str(raised.value)
        assert [z for n, z in compute.calls if n == "create"] == [ZONE_B]
    else:
        assert [z for n, z in compute.calls if n == "create"] == [ZONE_B, ZONE_C]
    assert _lease(db)["state"] == sm.STOPPED


def test_a_stopped_machine_is_not_deleted_once_its_start_spent_the_budget(db):
    compute, clock = FakeCompute(zone=ZONE, stockout=True), Clock()
    compute.clock, compute.cost = clock, {"start": sm.READY_SECONDS}
    with pytest.raises(sm.MachineNotReady) as raised:
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert "delete" not in compute.names() and "create" not in compute.names()
    assert list(compute.machines) == [ZONE], "the machine left its zone with no time to move"
    assert f"{ZONE_B}, {ZONE_C} not tried" in str(raised.value)


def test_a_start_whose_lease_was_taken_falls_back_without_touching_the_machine(db):
    compute, clock = FakeCompute(status=None, full=(ZONE,)), Clock()
    create = compute.create

    def taken(zone: str, **kwargs: Any) -> None:
        # The sweep (or another take) ends the lease while Compute answers.
        _lease(db)["generation"] += 1
        create(zone, **kwargs)

    compute.create = taken
    with pytest.raises(sm.MachineNotReady):
        _machine(db, compute, clock).start(ALICE, chosen=CHOSEN)
    assert [z for n, z in compute.calls if n == "create"] == [ZONE]
    assert "stop" not in compute.names() and "delete" not in compute.names()


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
    assert report["stopped"] is True and compute.names() == ["get", "get", "get", "stop"]


def test_the_sweep_leaves_a_stopped_instance_alone(db):
    compute = FakeCompute()
    report = _machine(db, compute, Clock()).sweep(Ender())
    assert report["stopped"] is False and compute.changes() == []


def test_the_sweep_stops_an_unheld_machine_in_whichever_zone_it_is(db):
    compute = FakeCompute(status=sm.INSTANCE_RUNNING, zone=ZONE_C)
    report = _machine(db, compute, Clock()).sweep(Ender())
    assert report["stopped"] is True and compute.changes() == [("stop", ZONE_C)]


def test_a_stop_stops_the_machine_in_the_zone_the_start_used(db):
    compute, clock = FakeCompute(status=None, full=(ZONE,)), Clock()
    machine = _machine(db, compute, clock)
    holder = machine.start(ALICE, chosen=CHOSEN).holder
    assert machine.stop(holder, cause=sm.END_BY_PERSON) is True
    assert compute.changes()[-1] == ("stop", ZONE_B)
    assert _audit(db, sm.AUDIT_STOPPED)[-1]["detail"]["zone"] == ZONE_B


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
        "SWARM_SPEECH_PROJECT": "proj", "SWARM_SPEECH_ZONES": ",".join(ZONES),
        "SWARM_SPEECH_INSTANCE": "swarm-speech-", "SWARM_SPEECH_ADDR": "10.40.0.9",
        "SWARM_SPEECH_TEMPLATE": CONFIG.template,
        "SWARM_SPEECH_SWEEP_AUDIENCE": "https://swarm-api.dev.swarm.internal",
        "SWARM_SPEECH_SWEEP_CALLERS": "Tick@example-project.iam.gserviceaccount.com"})
    assert config.enabled and config.stream_url == "ws://10.40.0.9:8080/v1/stream"
    assert config.audience == "swarm-speech"
    assert config.zones == ZONES and config.zone == ZONE
    assert [config.name_in(z) for z in ZONES] == ["swarm-speech-a", "swarm-speech-b", "swarm-speech-c"]
    # A machine made before the template is the one its zone's name finds.
    assert sm.SpeechConfig(instance="swarm-speech-a").name_in(ZONE_B) == "swarm-speech-a"
    assert config.sweep_callers == ("tick@example-project.iam.gserviceaccount.com",)
    assert not sm.SpeechConfig.from_env({}).enabled


_REPO = Path(__file__).resolve().parents[3]


def _swarm_api_env_block() -> str:
    text = (_REPO / "terraform/infra/locals.tf").read_text()
    start = text.index('"swarm-api" = merge(')
    return text[start:text.index('"swarm-scheduler" = merge(', start)]


def test_terraform_gives_swarm_api_every_speech_setting_the_config_reads():
    import re

    read = set(re.findall(r'"(SWARM_SPEECH_[A-Z_]+)"', (
        _REPO / "apps/swarm-api/swarm_api/voice/speech_machine.py").read_text()))
    read.add(cat.ZONES_ENV)
    block = _swarm_api_env_block()
    set_here = set(re.findall(r"^\s*(SWARM_SPEECH_[A-Z_]+)\s*=", block, re.M))
    assert read == set_here
    # Off means empty (the engine is not deployed), never a guessed address.
    for name in set_here - {"SWARM_SPEECH_AUDIENCE"}:
        line = re.search(rf"^\s*{name}\s*=(.*)$", block, re.M).group(1)
        assert line.strip().startswith("local.voice_speech_on ?") and line.rstrip().endswith(': ""'), name


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
        sm.RestCompute(CONFIG, session=session).start(ZONE_B)
    assert raised.value.code in sm.STOCKOUT_CODES
    base = f"https://compute.googleapis.com/compute/v1/projects/proj/zones/{ZONE_B}"
    assert session.requests[0][:2] == ("POST", f"{base}/instances/swarm-speech-b/start")
    assert session.requests[1][:2] == ("POST", f"{base}/operations/op-1/wait")
    assert all(ZONE_B in url for _m, url, _b in session.requests), "another zone was named"


def test_rest_compute_creates_from_the_template_keeping_its_metadata():
    template = {"properties": {"metadata": {"items": [
        {"key": "swarm-speech-caller", "value": "api@example-project.iam.gserviceaccount.com"},
        {"key": sm.META_IMAGE, "value": ""}]}}}
    session = _Session([_Response(200, template), _Response(200, {"name": "op-2", "status": "DONE"})])
    sm.RestCompute(CONFIG, session=session).create(
        ZONE_C, machine_type="g2-standard-8", metadata={sm.META_IMAGE: CONFIG.image})
    assert session.requests[0][:2] == ("GET", f"{sm.RestCompute.BASE}/{CONFIG.template}")
    method, url, body = session.requests[1]
    assert method == "POST" and url == (
        f"{sm.RestCompute.BASE}/projects/proj/zones/{ZONE_C}/instances"
        f"?sourceInstanceTemplate={CONFIG.template}")
    assert body["name"] == "swarm-speech-c"
    assert body["machineType"] == f"zones/{ZONE_C}/machineTypes/g2-standard-8"
    items = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert items == {"swarm-speech-caller": "api@example-project.iam.gserviceaccount.com",
                     sm.META_IMAGE: CONFIG.image}


def test_rest_compute_waits_no_longer_than_the_wait_and_ticks_between_pieces():
    import requests

    class _Slow(_Session):
        def request(self, method: str, url: str, json: Any = None, timeout: float = 0) -> _Response:
            self.timeouts.append(timeout)
            if url.endswith("/wait") and not self.answers:
                raise requests.exceptions.ReadTimeout("the piece ended")
            return super().request(method, url, json, timeout)

    session = _Slow([_Response(200, {"name": "op-4", "status": "RUNNING"})])
    session.timeouts = []
    ticks: list[int] = []
    compute = sm.RestCompute(CONFIG, session=session)
    compute.TIMEOUT_SECONDS = 0.01
    with pytest.raises(sm.ComputeError) as raised:
        compute.start(ZONE, wait=sm.Wait(seconds=0.05, tick=lambda: ticks.append(1)))
    assert raised.value.code == sm.TIMEOUT
    assert ticks, "the heartbeat was not given a turn between the wait's pieces"
    assert all(t <= 0.01 for t in session.timeouts[1:]), "a wait piece outlasted its share"


def test_rest_compute_reads_a_missing_machine_as_none_and_deletes_by_name():
    session = _Session([_Response(404, {"error": {"errors": [{"reason": "notFound"}]}}),
                        _Response(200, {"name": "op-3", "status": "DONE"}),
                        _Response(404, {"error": {"errors": [{"reason": "notFound"}]}})])
    compute = sm.RestCompute(CONFIG, session=session)
    assert compute.get(ZONE) is None
    compute.delete(ZONE_B)
    compute.delete(ZONE_B)  # already gone: nothing to wait for
    assert session.requests[1][:2] == (
        "DELETE", f"{sm.RestCompute.BASE}/projects/proj/zones/{ZONE_B}/instances/swarm-speech-b")


def test_rest_compute_sets_the_machine_type_and_metadata_by_name():
    session = _Session([_Response(200, {"status": "DONE"}), _Response(200, {"status": "DONE"}),
                        _Response(200, {"status": "TERMINATED", "machineType":
                                        "https://x/zones/us-central1-a/machineTypes/g2-standard-8",
                                        "metadata": {"fingerprint": "f",
                                                     "items": [{"key": "k", "value": "v"}]}})])
    compute = sm.RestCompute(CONFIG, session=session)
    compute.set_machine_type(ZONE, "g2-standard-8")
    compute.set_metadata(ZONE, {"k": "v"}, "f")
    assert session.requests[0][2] == {"machineType": f"zones/{ZONE}/machineTypes/g2-standard-8"}
    assert session.requests[1][2] == {"fingerprint": "f", "items": [{"key": "k", "value": "v"}]}
    instance = compute.get(ZONE)
    assert instance.machine_type == "g2-standard-8" and instance.metadata == {"k": "v"}
    assert instance.zone == ZONE
