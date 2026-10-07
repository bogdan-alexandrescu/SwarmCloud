"""The lease is heartbeated while the worker clones, and every clone try is recorded (#742).

MEASURED 2026-10-06: the worker beat before the clone and after it, never
during. In C2F's review step a clone hung about 134 s before an in-process
retry landed, and the reconciler fenced the healthy attempt as `dead_worker`
2.2 s after its agent started. Six chunk-3 implement steps went 82-92 s
between beats around the clone, against the reconciler's 90 s grace. The
failed try appeared in no `clone_timed` event.

Pinned here:

* a 200 s clone (a 134 s try that fails, a 10 s wait, a 56 s try that lands)
  keeps the lease beating on a fake clock, every gap inside the grace;
* a stale generation -- the lease reclaimed while the clone runs -- gets no
  beat from that thread, and a worker whose generation is already stale at
  start exits without a beat or a clone (invariant 5);
* a failed-then-ok clone records both tries, in order, in `clone_timed`, and
  keeps the summary fields every earlier reader reads;
* `egress_ready_seconds` counts from process start, not the probe's start
  (observer P28).
"""

from __future__ import annotations

import threading
import time
import types
from typing import Any

import pytest

from agent_worker import config as config_mod
from agent_worker import egress, gitops, lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode
from agent_worker.gitops import CloneResult
from swarm_common.models import utcnow
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_clone_transient_retry import (  # noqa: F401 - `origin` is a fixture
    CLONE_CONNECT,
    _flaky_worker,
    _logger,
    _network_calls,
    needs_git,
    origin,
)

URL = "https://github.com/acme/widgets.git"
#: The reconciler's grace at the platform's defaults, and the worker's beat.
GRACE = config_mod.heartbeat_grace_seconds(30)
BEAT = 22


# ---------------------------------------------------------------------------
# a fake clock both the clone and the heartbeat thread run on
# ---------------------------------------------------------------------------


class _Clock:
    """Monotonic seconds with ONE writer: the thread that runs the clone.

    The review of #742 measured the first version of this clock flaking: the
    heartbeat thread's wait moved the clock too, and while it slept 2 ms of
    real time the clone moved it on by seconds, so a beat landed 2-3 fake
    seconds late by however the scheduler felt. Now the heartbeat thread only
    parks until a fake time. Every step of the clone advances the clock and
    then SETTLES: it waits until each thread started through
    `_fake_threading` is parked on a time still ahead, or has ended. So a
    beat lands at the first whole step at or after it is due, every run.
    """

    #: Real seconds a settle waits for a heartbeat thread before failing the
    #: test rather than hanging it.
    SETTLE_SECONDS = 10.0

    def __init__(self) -> None:
        # A whole number, so 1 s steps add up exactly.
        self.now = float(round(time.monotonic()))
        self.cond = threading.Condition()
        self.parked: dict[Any, float] = {}
        self.busy: set[Any] = set()

    def __call__(self) -> float:
        with self.cond:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.cond:
            self.now += max(0.0, float(seconds))
            self.cond.notify_all()
        self.settle()

    def settle(self) -> None:
        give_up = time.monotonic() + self.SETTLE_SECONDS
        with self.cond:
            while self.busy or any(at <= self.now for at in self.parked.values()):
                left = give_up - time.monotonic()
                assert left > 0, f"a heartbeat thread never settled: {self.busy} {self.parked}"
                self.cond.wait(left)

    def park(self, until: float, event: threading.Event) -> bool:
        """Block the calling heartbeat thread until `until` or `event`."""
        me = threading.current_thread()
        with self.cond:
            self.busy.discard(me)
            self.parked[me] = until
            self.cond.notify_all()
            while self.now < until and not event.is_set():
                self.cond.wait()
            del self.parked[me]
            self.busy.add(me)
            return event.is_set()


def _fake_threading(clock: _Clock) -> types.SimpleNamespace:
    """`threading` for the lifecycle module, on `clock`.

    A thread it starts is the clock's to wait for: `start` returns once the
    thread has parked or ended, and its `Event.wait` parks until the clone's
    thread moves the clock past the timeout. A wait on any other thread --
    the clone's own -- moves the clock itself, as that thread is the writer.
    """

    class Thread(threading.Thread):
        def start(self) -> None:
            with clock.cond:
                clock.busy.add(self)
            super().start()
            clock.settle()

        def run(self) -> None:
            try:
                super().run()
            finally:
                with clock.cond:
                    clock.busy.discard(self)
                    clock.cond.notify_all()

    class Event:
        def __init__(self) -> None:
            self._real = threading.Event()

        def set(self) -> None:
            with clock.cond:
                self._real.set()
                clock.cond.notify_all()

        def is_set(self) -> bool:
            return self._real.is_set()

        def wait(self, timeout: float | None = None) -> bool:
            if self._real.is_set():
                return True
            if isinstance(threading.current_thread(), Thread):
                return clock.park(clock() + (timeout or 0.0), self._real)
            clock.advance(timeout or 0.0)
            return self._real.is_set()

    return types.SimpleNamespace(Event=Event, Thread=Thread)


def _on_the_clock(monkeypatch, clock: _Clock) -> None:
    monkeypatch.setattr(lifecycle, "threading", _fake_threading(clock))
    monkeypatch.setattr(
        lifecycle, "time", types.SimpleNamespace(monotonic=clock, sleep=time.sleep, time=time.time)
    )


def _takes(clock: _Clock, seconds: float) -> None:
    """Spend `seconds` of fake time, a second at a time, as a stalled git would."""
    end = clock() + seconds
    while clock() < end:
        clock.advance(1.0)


def _stalling_clone(clock: _Clock, plan: list[tuple[float, BaseException | None]]):
    """`shallow_clone`: each call takes its planned seconds, then fails or lands."""
    calls: list[float] = []

    def clone(**kwargs: Any) -> CloneResult:
        seconds, failure = plan[len(calls)]
        calls.append(clock())
        _takes(clock, seconds)
        if failure is not None:
            raise failure
        return CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="a" * 40, duration_seconds=seconds,
        )

    return clone, calls


def _ready_to_clone(db, worker_factory, monkeypatch, clock: _Clock):
    seed_attempt(db)
    worker, config, _ = worker_factory(
        repository_url=URL, heartbeat_interval_seconds=BEAT, timeout_seconds=3600,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": "task_1"}
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    # The beat before the clone, as `_prepare` makes it after the restore.
    worker._heartbeat()
    beats: list[float] = [clock()]
    real = worker._heartbeat

    def counted() -> None:
        beats.append(clock())
        real()

    worker._heartbeat = counted  # type: ignore[method-assign]
    worker.forge_sleep = lambda seconds: _takes(clock, seconds)
    return worker, beats


def _gaps(beats: list[float], end: float) -> list[float]:
    points = [*beats, end]
    return [round(b - a, 3) for a, b in zip(points, points[1:])]


# ---------------------------------------------------------------------------
# the lease stays fresh through a slow clone
# ---------------------------------------------------------------------------


def test_a_200_s_clone_keeps_the_lease_beating_inside_the_grace(db, worker_factory, monkeypatch):
    """The C2F shape: a 134 s try that cannot connect, the 10 s wait, a 56 s
    try that lands -- 200 s of clone. Every gap between beats, from the beat
    before the clone to the clone's end, is inside the reconciler's grace.
    MUTATION: clone without `_heartbeat_meanwhile` and the only beat inside
    is the retry's own, after 134 s of silence."""
    clock = _Clock()
    _on_the_clock(monkeypatch, clock)
    clone, calls = _stalling_clone(
        clock, [(134.0, gitops.GitTransient(CLONE_CONNECT)), (56.0, None)]
    )
    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    worker, beats = _ready_to_clone(db, worker_factory, monkeypatch, clock)
    worker._deadline = clock() + 3600
    started = clock()

    info = worker._clone_keeping_lease(worker._task)
    ended = clock()

    assert info is not None and info["commit"] == "a" * 40
    assert len(calls) == 2
    assert ended - started >= 200.0
    inside = [t for t in beats[1:] if started <= t <= ended]
    assert len(inside) >= 200 // BEAT - 1, f"{len(inside)} beats in a {ended - started:.0f} s clone"
    gaps = _gaps(beats, ended)
    assert max(gaps) < GRACE, f"a {max(gaps)} s silence against a {GRACE} s grace: {gaps}"
    # And at the beat's own cadence: the clock moves in 1 s steps, so a beat
    # lands at most one step after it is due.
    assert max(gaps) <= BEAT + 1, gaps
    assert db.doc("leases/lease_1")["heartbeat_at"] is not None


def test_the_startup_clone_runs_under_the_heartbeat(db, worker_factory, monkeypatch):
    """Through `run`, on the real clock: a clone of 2.5 s on a 1 s beat is
    beaten during. MUTATION: call `_maybe_clone` from `_prepare` and no beat
    lands while the clone runs."""
    seed_attempt(db)
    beats_during: list[float] = []
    cloning = threading.Event()

    def clone(**kwargs: Any) -> CloneResult:
        cloning.set()
        time.sleep(2.5)
        cloning.clear()
        return CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="a" * 40, duration_seconds=2.5,
        )

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    worker, _, _ = worker_factory(repository_url=URL, heartbeat_interval_seconds=1)
    worker.egress_connect = lambda target, timeout=None: types.SimpleNamespace(close=lambda: None)
    real = worker._heartbeat

    def counted() -> None:
        if cloning.is_set():
            beats_during.append(time.monotonic())
        real()

    worker._heartbeat = counted  # type: ignore[method-assign]

    assert worker.run() == ExitCode.OK
    assert len(beats_during) >= 2, beats_during


# ---------------------------------------------------------------------------
# fencing is unchanged: a stale generation is not beaten for
# ---------------------------------------------------------------------------


def test_a_lease_reclaimed_during_the_clone_gets_no_beat(db, worker_factory, monkeypatch):
    """The reconciler reclaims the lease and another generation owns the task
    while the clone runs: the thread asks before its first beat, sees the
    fence, and stops. Nothing extends the lease. MUTATION: drop the fence
    check in `_heartbeat_meanwhile` and the thread beats every 22 s."""
    clock = _Clock()
    _on_the_clock(monkeypatch, clock)
    worker_ref: dict[str, Any] = {}

    def clone(**kwargs: Any) -> CloneResult:
        db.documents["tasks/task_1"]["current_generation"] = 2
        db.documents["leases/lease_1"]["released_at"] = utcnow()
        worker_ref["heartbeat_at"] = db.doc("leases/lease_1")["heartbeat_at"]
        _takes(clock, 200.0)
        return CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="a" * 40, duration_seconds=200.0,
        )

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    worker, beats = _ready_to_clone(db, worker_factory, monkeypatch, clock)
    worker._deadline = clock() + 3600
    started = clock()

    worker._clone_keeping_lease(worker._task)

    assert [t for t in beats[1:] if t >= started] == [], "a fenced attempt was beaten for"
    assert db.doc("leases/lease_1")["heartbeat_at"] == worker_ref["heartbeat_at"]


def test_a_stale_generation_at_start_neither_beats_nor_clones(db, worker_factory, monkeypatch):
    """Another generation owns the task before this worker starts: it exits
    at the generation check, before the clone and its heartbeat exist."""
    seed_attempt(db, task_generation=2)
    cloned: list[Any] = []
    monkeypatch.setattr(lifecycle, "shallow_clone", lambda **kwargs: cloned.append(kwargs))
    worker, _, _ = worker_factory(repository_url=URL, heartbeat_interval_seconds=1)
    worker.egress_connect = lambda target, timeout=None: types.SimpleNamespace(close=lambda: None)
    beats: list[float] = []
    real = worker._heartbeat

    def counted() -> None:
        beats.append(time.monotonic())
        real()

    worker._heartbeat = counted  # type: ignore[method-assign]

    code = worker.run()

    assert code != ExitCode.OK
    assert cloned == [] and beats == []
    assert db.doc("leases/lease_1")["heartbeat_at"] is None


# ---------------------------------------------------------------------------
# every clone try is recorded
# ---------------------------------------------------------------------------


def test_retry_clone_records_a_failed_try_then_the_one_that_landed(tmp_path):
    failed = gitops.GitTransient(CLONE_CONNECT)
    failed.phases = {"dns_seconds": 0.01}
    landed = types.SimpleNamespace(phases={"connect_seconds": 0.042})
    outcomes: list[Any] = [failed, landed]
    ticks = iter([0.0, 134.0, 144.0, 200.0])
    record: list[dict[str, Any]] = []

    def call():
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    result = gitops.retry_clone(
        call, destination=tmp_path / "repo", sleep=lambda _s: None,
        max_wait_seconds=45, remaining_seconds=lambda: 3600, logger=_logger(),
        record=record, clock=lambda: next(ticks),
    )

    assert result is landed
    # No peers on these tries' phases (#721, P27): None, None, unpinned.
    none = {"probe_peer": None, "git_peer": None, "peer_pinned": False}
    assert record == [
        {"connect_seconds": None, "ok": False, "error_class": "GitTransient", "seconds": 134.0,
         **none},
        {"connect_seconds": 0.042, "ok": True, "error_class": None, "seconds": 56.0, **none},
    ]


def test_retry_clone_records_a_permanent_failure_before_raising_it(tmp_path):
    record: list[dict[str, Any]] = []

    def call():
        raise gitops.GitError("remote: Repository not found.")

    with pytest.raises(gitops.GitError):
        gitops.retry_clone(
            call, destination=tmp_path / "repo", sleep=lambda _s: None,
            max_wait_seconds=45, remaining_seconds=lambda: 3600, logger=_logger(),
            record=record,
        )
    assert [(r["ok"], r["error_class"]) for r in record] == [(False, "GitError")]


def test_each_git_error_has_its_own_phases():
    """A class-level dict would be shared by every failure, so one try's
    timings could surface on another's record."""
    first, second = gitops.GitError("a"), gitops.GitTransient(CLONE_CONNECT)
    first.phases["connect_seconds"] = 1.0
    assert second.phases == {} and first.phases is not second.phases
    assert second.tries == 1 and str(first) == "a"


def _clone_marks(db) -> list[dict]:
    return [
        event["detail"]["clone"] for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == "clone_timed"
    ]


@needs_git
def test_a_failed_then_ok_clone_records_two_tries_in_clone_timed(
    db, store, worker_factory, monkeypatch, tmp_path, origin  # noqa: F811
):
    seed_attempt(db)
    worker, counter, slept = _flaky_worker(
        worker_factory, monkeypatch, tmp_path, origin, failures=1
    )

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert _network_calls(counter) == 2 and slept == [10.0]

    (clone,) = _clone_marks(db)
    log = clone["try_log"]
    assert [(t["ok"], t["error_class"]) for t in log] == [
        (False, "GitTransient"), (True, None),
    ], log
    assert all(
        set(t) == {
            "connect_seconds", "ok", "error_class", "seconds",
            "probe_peer", "git_peer", "peer_pinned",
        }
        for t in log
    )
    assert all(isinstance(t["seconds"], float) and t["seconds"] >= 0 for t in log)
    # The summary fields every earlier reader reads, unchanged in meaning.
    assert clone["tries"] == 2 and clone["ok"] is True and clone["pinned"] is False
    assert clone["seconds"] >= 0


@needs_git
def test_a_clone_that_never_lands_still_records_every_try(
    db, store, worker_factory, monkeypatch, tmp_path, origin  # noqa: F811
):
    seed_attempt(db)
    worker, _, _ = _flaky_worker(worker_factory, monkeypatch, tmp_path, origin, failures=99)

    worker.run()

    (clone,) = _clone_marks(db)
    assert clone["ok"] is False and clone["tries"] == 3
    assert [t["error_class"] for t in clone["try_log"]] == ["GitTransient"] * 3


# ---------------------------------------------------------------------------
# egress_ready_seconds counts from process start (observer P28)
# ---------------------------------------------------------------------------


def test_egress_ready_counts_from_the_origin_it_is_given():
    clock = types.SimpleNamespace(now=100.0)

    def connect(target, timeout=None):
        clock.now += 1.0
        return types.SimpleNamespace(close=lambda: None)

    probe = egress.EgressProbe(
        [egress.DEFAULT_TARGET], connect=connect, clock=lambda: clock.now,
        interval_seconds=0.0, origin=96.5,
    )
    probe.start()
    assert probe.wait(egress.DEFAULT_TARGET, timeout=5) is True
    probe.stop()
    # 3.5 s before the probe started, plus its one 1 s connect.
    assert probe.result()["egress_ready_seconds"] == pytest.approx(4.5)


def test_the_worker_measures_egress_ready_from_process_start(db, worker_factory, monkeypatch):
    """The process started 5 s before the probe did (the entrypoint's
    `Phases` clock): the mark reads at least 5 s. MUTATION: count from the
    probe's own start and it reads a few milliseconds."""
    seed_attempt(db)
    monkeypatch.setattr(
        lifecycle, "shallow_clone",
        lambda **kwargs: CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="a" * 40, duration_seconds=0.1,
        ),
    )
    worker, _, _ = worker_factory(repository_url=URL)
    worker.egress_connect = lambda target, timeout=None: types.SimpleNamespace(close=lambda: None)
    worker.egress_interval_seconds = 0.0
    worker.phases._t0 -= 5.0

    assert worker.run() == ExitCode.OK

    (event,) = [
        e for e in db.events("task_1")
        if (e.get("detail") or {}).get("cause") == "egress_ready"
    ]
    ready = event["detail"]["egress"]["egress_ready_seconds"]
    assert 5.0 <= ready < 60.0, ready
