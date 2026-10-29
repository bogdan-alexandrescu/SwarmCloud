"""Bounded waits with a countdown (docs/design/day-one-install.md §2.2).

Certificate provisioning, DNS, IAM propagation, a person in a console: each is
polled with a visible countdown, a stated reason and an upper bound taken from
history (IAM 45 s to 10 min, H19; a certificate up to 60 min). Every poll
emits a `wait_countdown` event carrying what is usually expected and the hard
deadline, so a face (the CLI, `/sc:install`, the console) can draw it. Past
the bound the wait says what to look at and raises `WaitGaveUp`, which exits 4
(paused): the engine never loops for ever.

`on_poll` runs on every poll before the check: the engine uses it to renew
the lease and to run the wait's triggered steps (plan §5.5, review finding 9).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .events import Events
from .exitcodes import MET, ExitCode


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class WaitSpec:
    """One bounded wait.

    `reason` says what is being waited for; `expected` how long it usually
    takes, in words ("45 s to 10 min"); `expected_s` the usual worst case, for
    the countdown's expected end; `bound_s` the hard deadline; `look_at` what
    to look at past it.
    """

    reason: str
    expected: str
    expected_s: float
    bound_s: float
    look_at: str
    interval_s: float = 10.0

    def __post_init__(self) -> None:
        if not 0 < self.interval_s <= self.bound_s:
            raise ValueError("a wait polls at an interval greater than 0 and within its bound")
        if self.expected_s > self.bound_s:
            raise ValueError("a wait's expected end is past its bound")


#: The bounds history gave (design §2.2, lessons H19 and the frontend module).
IAM_PROPAGATION = WaitSpec(
    reason="IAM propagation",
    expected="45 s to 10 min",
    expected_s=45,
    bound_s=10 * 60,
    look_at="swarm doctor tells propagation apart from a missing grant",
)
CERTIFICATE = WaitSpec(
    reason="the managed certificate leaving PROVISIONING",
    expected="up to 60 min",
    expected_s=20 * 60,
    bound_s=60 * 60,
    look_at="the hostname's A record must resolve to the reserved address",
    interval_s=30,
)


class WaitGaveUp(RuntimeError):
    exit_code = ExitCode.PAUSED

    def __init__(self, step: str, spec: WaitSpec) -> None:
        super().__init__(
            f"{step}: gave up waiting for {spec.reason} after {spec.bound_s:.0f} s. "
            f"Look at: {spec.look_at}. Run swarm install again to resume."
        )
        self.step = step
        self.spec = spec


def wait_until(
    step: str,
    spec: WaitSpec,
    check: Callable[[], str],
    *,
    clock: Clock,
    events: Events,
    on_poll: Callable[[], None] | None = None,
) -> None:
    """Poll `check` (it returns a status) until it is `met` or the bound passes."""
    started = clock.now()
    expected_end = started + spec.expected_s
    deadline = started + spec.bound_s
    while True:
        if on_poll is not None:
            on_poll()
        status = check()
        now = clock.now()
        if status == MET:
            return
        if now >= deadline:
            events.emit(
                "wait_gave_up",
                step=step,
                reason=spec.reason,
                bound_s=spec.bound_s,
                look_at=spec.look_at,
                state=status,
            )
            raise WaitGaveUp(step, spec)
        events.emit(
            "wait_countdown",
            step=step,
            reason=spec.reason,
            state=status,
            expected=spec.expected,
            started=started,
            expected_end=expected_end,
            deadline=deadline,
            remaining_s=deadline - now,
        )
        clock.sleep(min(spec.interval_s, deadline - now))
