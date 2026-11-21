"""A virtual clock for the worker's beat thread, moved by the test (issue #75).

`Worker._heartbeat_meanwhile` reads the time from `worker.heartbeat_clock` and
waits with `worker.heartbeat_wait(stop, seconds)`; `Worker._heartbeat` stamps
each beat from the same clock. Production keeps `time.monotonic` and
`threading.Event.wait`. A test installs a `VirtualClock` in their place and,
where its stand-in used to `time.sleep` through a slow call, calls `advance`:
the beat thread's waits end at the virtual instant they asked for, and each
beat lands before the clock moves on. Counting beats against wall time was
timing-sensitive on a loaded runner and cost about a minute of real sleeps.

The clock starts at a real `time.monotonic()` reading and only moves forward,
so a beat stamped on it stays within the test's own few seconds of the
supervision loop's real clock, and a test can run the whole loop with it
installed. Only the beat threads wait on it; the loop never does.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

#: How far apart two virtual times may be and still count as the same instant
#: in a test's assertion: far above float rounding, far below any interval a
#: test uses. The clock itself compares exactly: a wait ends when `now` reaches
#: its target, and `advance` steps to that target, so a wait never ends a
#: rounding early -- the beat thread would read `clock() < due()` and spin.
FLOAT_SLACK = 1e-3
#: The production name of `_heartbeat_meanwhile`'s thread.
BEAT_THREAD = "heartbeat-meanwhile"
#: Real seconds a beat thread may take to settle, only as a backstop: a beat
#: on the fake control plane takes milliseconds.
SETTLE_BACKSTOP_SECONDS = 10.0


class VirtualClock:
    """A monotonic clock that moves only when `advance` moves it.

    `advance(seconds)` steps from one due instant to the next -- a beat
    thread's wait ending, an action given to `at` -- and before each step
    lets every live beat thread finish what the last step woke it for and
    block again (or end). So a beat due at t lands at exactly t on every run,
    and the test reads the property, not a band around it.
    """

    def __init__(self, start: float | None = None) -> None:
        self.now = time.monotonic() if start is None else start
        self._cond = threading.Condition()
        self._waiting: dict[threading.Thread, float] = {}
        self._scheduled: list[tuple[float, Callable[[], object]]] = []
        # Beat threads alive before the clock was made (a previous test's,
        # on the real clock) are none of its business.
        self._foreign = {t for t in threading.enumerate() if t.name == BEAT_THREAD}

    def install(self, worker) -> "VirtualClock":
        """Make `worker`'s beats read this clock and its beat threads wait on it."""
        worker.heartbeat_clock = self.monotonic
        worker.heartbeat_wait = self.wait
        return self

    def monotonic(self) -> float:
        return self.now

    def at(self, when: float, action: Callable[[], object]) -> None:
        """Run `action` once the clock reaches `when`, inside `advance`."""
        with self._cond:
            self._scheduled.append((when, action))
            self._scheduled.sort(key=lambda entry: entry[0])

    def wait(self, event: threading.Event, seconds: float) -> bool:
        """`Event.wait` on virtual time: True once `event` is set."""
        me = threading.current_thread()
        with self._cond:
            target = self.now + max(seconds, 0.0)
            self._waiting[me] = target
            self._cond.notify_all()
            while self.now < target and not event.is_set():
                # Setting the event does not notify the condition: poll it.
                self._cond.wait(timeout=0.01)
            self._waiting.pop(me, None)
            return event.is_set()

    def _beat_threads(self) -> list[threading.Thread]:
        return [
            t for t in threading.enumerate()
            if t.name == BEAT_THREAD and t not in self._foreign and t.is_alive()
        ]

    def _settled(self) -> bool:
        # Every live beat thread is blocked on a time still to come. One that
        # is beating, has just started, or was just woken is not.
        return all(
            self._waiting.get(t, -1.0) > self.now for t in self._beat_threads()
        )

    def _settle(self) -> None:
        deadline = time.monotonic() + SETTLE_BACKSTOP_SECONDS
        with self._cond:
            while not self._settled():
                assert time.monotonic() < deadline, "a beat thread did not settle"
                self._cond.wait(timeout=0.01)

    def advance(self, seconds: float) -> None:
        """Move the clock `seconds` forward, beat by beat."""
        end = self.now + seconds
        while True:
            self._settle()
            with self._cond:
                if self.now >= end:
                    return
                due = [end, *self._waiting.values(), *(w for w, _ in self._scheduled)]
                self.now = min(t for t in due if t > self.now)
                while self._scheduled and self._scheduled[0][0] <= self.now:
                    _, action = self._scheduled.pop(0)
                    action()
                self._cond.notify_all()


def count_beats(worker, clock: VirtualClock) -> list[float]:
    """The virtual times of `worker`'s beats from here on, in order."""
    beats: list[float] = []
    real = worker._heartbeat

    def counted():
        beats.append(clock.now)
        return real()

    worker._heartbeat = counted  # type: ignore[method-assign]
    return beats
