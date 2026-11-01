"""The harvest and the publish keep the lease beating (#2686).

`_record_finalised` beat the lease through `_checkpoint("final")` and then
ran the harvest, the upload and the publish (`_upload_outputs`,
`_publish_checked`, `_publish_git`) with nothing beating it: the supervision
loop had already stopped. task_f06e7358 went 94-111 s silent there on each of
its three attempts, task_118cd828 108 s and 97 s, and the reconciler's
lease-silent check fenced a live, finishing worker each time. The owner's fix
(2026-11-01): the stretch runs under `_heartbeat_meanwhile("harvest/publish")`.

1. A publish that takes 150 s -- longer than the reconciler's 90 s grace --
   sees the lease beat at the loop's cadence throughout. Before, no beat.
2. A generation fenced while it publishes stops beating at the next ask and
   never makes its forge write; the run ends as a stale generation
   (invariant 5).

The beat thread runs on a virtual clock (`_Clock`, through the worker's
`heartbeat_clock` and `heartbeat_wait`), installed once the final checkpoint
has returned, so the 150 s publish costs milliseconds and every beat lands at
the same virtual instant on every run.

MUTATION: drop the `with self._heartbeat_meanwhile("harvest/publish")` from
`_record_finalised` and both tests fail on "no beat while publishing".
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from agent_worker.errors import ExitCode

from worker_seeds import seed_attempt

#: The production beat (`config.WorkerConfig.heartbeat_interval_seconds`).
INTERVAL = 22
#: The reconciler's lease-silent grace the incidents were fenced by.
STALE_AFTER = 90
#: How long the stand-in publish takes, in virtual seconds.
PUBLISH_SECONDS = 150


class _Clock:
    """A virtual monotonic clock the beat thread waits on and the publish moves.

    The beat thread's `wait` blocks until the clock reaches the time it asked
    for (or its stop event is set); `advance` moves the clock one second at a
    time and, after each, lets every live beat thread finish what that second
    woke it for, so a beat lands at the virtual instant it was due. With no
    beat thread alive, `advance` just moves the clock.
    """

    def __init__(self, start: float) -> None:
        self.now = start
        self._cond = threading.Condition()
        self._waiting: dict[threading.Thread, float] = {}
        self._scheduled: list[tuple[float, Callable[[], object]]] = []
        self._threads: list[threading.Thread] = []

    def monotonic(self) -> float:
        return self.now

    def at(self, when: float, action: Callable[[], object]) -> None:
        self._scheduled.append((when, action))
        self._scheduled.sort(key=lambda entry: entry[0])

    def wait(self, event: threading.Event, seconds: float) -> bool:
        me = threading.current_thread()
        with self._cond:
            if me not in self._threads:
                self._threads.append(me)
            target = self.now + max(seconds, 0.0)
            self._waiting[me] = target
            self._cond.notify_all()
            while self.now < target and not event.is_set():
                # The stop event does not notify the condition: poll it.
                self._cond.wait(timeout=0.01)
            self._waiting.pop(me, None)
            return event.is_set()

    def _settled(self) -> bool:
        return all(
            not thread.is_alive() or self._waiting.get(thread, -1.0) > self.now
            for thread in self._threads
        )

    def advance(self, seconds: int) -> None:
        for _ in range(seconds):
            with self._cond:
                self.now += 1
                while self._scheduled and self._scheduled[0][0] <= self.now:
                    _, action = self._scheduled.pop(0)
                    action()
                self._cond.notify_all()
            # Real seconds, only as a backstop: a beat on the fake control
            # plane takes milliseconds.
            deadline = time.monotonic() + 10
            while True:
                with self._cond:
                    if self._settled():
                        break
                    assert time.monotonic() < deadline, "a beat thread did not settle"
                    self._cond.wait(timeout=0.01)


def _run_with_a_slow_publish(db, worker_factory, *, fence_after: float | None = None):
    """Run one attempt whose publish takes PUBLISH_SECONDS on the virtual clock.

    Returns the exit code, the beats' virtual times, the publish's start and
    end, and whether the forge write was made.
    """
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=INTERVAL,
        heartbeat_meanwhile_max_seconds=360,
        heartbeat_meanwhile_ceiling_seconds=360,
    )
    clock: list[_Clock] = []
    beats: list[float] = []
    window: dict[str, float] = {}
    pushed: list[bool] = []

    real_checkpoint = worker._checkpoint

    def checkpoint(label: str, **kwargs: Any):
        record = real_checkpoint(label, **kwargs)
        if label == "final":
            # From here on, every beat reads the virtual clock. It starts at
            # the real clock's reading, which `_last_beat_started` was taken on.
            clock.append(_Clock(time.monotonic()))
            worker.heartbeat_clock = clock[0].monotonic
            worker.heartbeat_wait = clock[0].wait
        return record

    real_heartbeat = worker._heartbeat

    def heartbeat():
        if clock:
            beats.append(clock[0].now)
        return real_heartbeat()

    def harvest(**_kwargs: Any) -> dict[str, Any]:
        repo = worker.ws.root
        worker._deferred_publish = {"repo": repo, "work_head": "b" * 40, "publish_repo": repo}
        return {"base": "a" * 40, "head": "b" * 40, "commit_count": 1, "dirty_count": 0,
                "patch": None, "patch_bytes": 0, "patch_omitted": False,
                "published": False, "publish_reason": "deferred"}

    def publish(**_kwargs: Any) -> dict[str, Any]:
        window["start"] = clock[0].now
        if fence_after is not None:
            clock[0].at(
                window["start"] + fence_after,
                lambda: db.doc("tasks/task_1").update({"current_generation": 2}),
            )
        clock[0].advance(PUBLISH_SECONDS)
        window["end"] = clock[0].now
        # The real publish reads the fence before every forge write (#2693).
        worker._own_before_forge_write("push")
        pushed.append(True)
        return {"published": True, "publish_reason": "pushed (stand-in)"}

    worker._checkpoint = checkpoint  # type: ignore[method-assign]
    worker._heartbeat = heartbeat  # type: ignore[method-assign]
    worker._harvest_git = harvest  # type: ignore[method-assign]
    worker._publish_git = publish  # type: ignore[method-assign]

    code = worker.run()
    assert window, "the publish was never reached"
    return code, beats, window, bool(pushed)


def test_a_long_publish_keeps_the_lease_beating(db, worker_factory):
    code, beats, window, pushed = _run_with_a_slow_publish(db, worker_factory)
    start, end = window["start"], window["end"]
    during = [b for b in beats if start < b <= end]
    # MUTATION: no `_heartbeat_meanwhile` around the stretch and this is empty:
    # the lease ages for the whole publish.
    assert during, f"no beat while publishing for {PUBLISH_SECONDS} s: beats={beats}"
    marks = [start, *during, end]
    silence = max(later - earlier for earlier, later in zip(marks, marks[1:]))
    assert silence <= INTERVAL, (
        f"the lease went {silence} s without a beat while publishing: {during}"
    )
    assert silence < STALE_AFTER
    assert pushed
    assert code == ExitCode.OK, code


def test_a_generation_fenced_while_it_publishes_stops_and_never_pushes(db, worker_factory):
    fence_after = 30.5
    code, beats, window, pushed = _run_with_a_slow_publish(
        db, worker_factory, fence_after=fence_after
    )
    fenced_at = window["start"] + fence_after
    before_fence = [b for b in beats if window["start"] < b < fenced_at]
    after_fence = [b for b in beats if b > fenced_at]
    # It was beating (the fix), so stopping is the fence's doing, not silence.
    assert before_fence, f"no beat while publishing before the fence: beats={beats}"
    # Invariant 5: a stale generation never extends a lease that is no longer
    # its own, and never writes to the forge over the newer generation.
    assert not after_fence, f"beats after the attempt was fenced: {after_fence}"
    assert not pushed, "a fenced generation made its forge write"
    assert code == ExitCode.GENERATION_FENCED, code
