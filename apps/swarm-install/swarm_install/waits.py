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

A check may answer an Observation instead of a bare status: its `detail` (a
certificate's FAILED_NOT_VISIBLE, "Google refused ...", which run failed and
how) is what names the problem, so the countdown carries it whenever it
changes (audit 5b M1). A check that sees a definite failure no poll can mend
says so with `FAILED: True` in its Observation's outputs (or raises
`CheckFailed`): the wait ends at once with its detail instead of polling to
the bound. `look_at` may be a function of the step context, so the give-up
text names the real project, hostname and run ids, never a `<placeholder>`
for the person to fill in (audit 5b M2).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

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
    to look at past it: words, or a function of the step context (it may be
    given None) that names the real project, hostname or run ids.
    """

    reason: str
    expected: str
    expected_s: float
    bound_s: float
    look_at: str | Callable[[Any], str]
    interval_s: float = 10.0

    def __post_init__(self) -> None:
        if not 0 < self.interval_s <= self.bound_s:
            raise ValueError("a wait polls at an interval greater than 0 and within its bound")
        if self.expected_s > self.bound_s:
            raise ValueError("a wait's expected end is past its bound")

    def look_at_for(self, ctx: Any = None) -> str:
        """What to look at past the bound, filled from `ctx` when it is a function."""
        if not callable(self.look_at):
            return self.look_at
        try:
            return str(self.look_at(ctx))
        except Exception as exc:  # noqa: BLE001 - a give-up text must never hide the give-up
            return f"(what to look at could not be named: {type(exc).__name__}: {exc})"


#: Google Cloud console pages a give-up text links to.
CLOUD_CONSOLE = "https://console.cloud.google.com"


def project_of(ctx: Any) -> str:
    """The project a give-up text links to, or "" when the context has none."""
    return str(getattr(ctx, "project", "") or "")


def cloud_page(path: str, ctx: Any) -> str:
    """A Google Cloud console page, for the context's project when it has one."""
    project = project_of(ctx)
    return f"{CLOUD_CONSOLE}/{path}" + (f"?project={project}" if project else "")


def _iam_look_at(ctx: Any) -> str:
    return (f"the project's IAM page, {cloud_page('iam-admin/iam', ctx)}: a grant listed there is still "
            "propagating; a grant missing there is what the step above asked for")


#: The bounds history gave (design §2.2, lessons H19 and the frontend module).
IAM_PROPAGATION = WaitSpec(
    reason="IAM propagation",
    expected="45 s to 10 min",
    expected_s=45,
    bound_s=10 * 60,
    look_at=_iam_look_at,
)
CERTIFICATE = WaitSpec(
    reason="the managed certificate leaving PROVISIONING",
    expected="up to 60 min",
    expected_s=20 * 60,
    bound_s=60 * 60,
    look_at="the hostname's A record must resolve to the reserved address",
    interval_s=30,
)


#: The verb that resumes a wait the install itself runs. A day-two verb
#: (`swarm team add`, `swarm upgrade`) passes its own: running `swarm install`
#: again resumes neither (audit 4e F13).
RESUME = "swarm install"


class WaitGaveUp(RuntimeError):
    exit_code = ExitCode.PAUSED

    def __init__(self, step: str, spec: WaitSpec, resume: str = RESUME, *, look_at: str | None = None,
                 detail: str = "") -> None:
        look_at = spec.look_at_for(None) if look_at is None else look_at
        said = f" Its last check said: {detail}." if detail else ""
        super().__init__(
            f"{step}: gave up waiting for {spec.reason} after {spec.bound_s:.0f} s.{said} "
            f"Look at: {look_at}. Run {resume} again to resume."
        )
        self.step = step
        self.spec = spec
        self.resume = resume
        self.look_at = look_at
        self.detail = detail


class CheckFailed(RuntimeError):
    """A check's definite failure, which no later poll can mend (a run that
    cloned with a personal token, a run relaunched to its limit): it ends the
    wait at once with `detail` instead of polling to the bound. A check says
    it with `FAILED` in its Observation's outputs, or raises this. The engine
    records the step failed (exit 1); outside a wait it reads as `unmet`."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


#: The Observation output a check sets (`{FAILED: True}`, with an unmet
#: status) to say its failure is definite: the wait ends at once.
FAILED = "wait_failed"


def _status_and_detail(seen: Any) -> tuple[str, str]:
    """A check's answer: a bare status, or an Observation (`status`, `detail`).
    An unmet Observation whose outputs carry `FAILED` raises `CheckFailed`."""
    if isinstance(seen, str):
        return seen, ""
    status, detail = str(seen.status), str(getattr(seen, "detail", "") or "")
    if status != MET and (getattr(seen, "outputs", None) or {}).get(FAILED):
        raise CheckFailed(detail or "its check failed")
    return status, detail


def wait_until(
    step: str,
    spec: WaitSpec,
    check: Callable[[], Any],
    *,
    clock: Clock,
    events: Events,
    on_poll: Callable[[], None] | None = None,
    resume: str = RESUME,
    ctx: Any = None,
) -> None:
    """Poll `check` (it returns a status, or an Observation) until it is `met`
    or the bound passes; past it, `WaitGaveUp` tells the person what to look
    at (`spec.look_at_for(ctx)`) and to run `resume` again. The countdown
    carries the check's `detail` whenever it changes; a `CheckFailed` from the
    check ends the wait at once (`wait_failed`) and is raised on."""
    started = clock.now()
    expected_end = started + spec.expected_s
    deadline = started + spec.bound_s
    shown = ""
    while True:
        if on_poll is not None:
            on_poll()
        try:
            status, detail = _status_and_detail(check())
        except CheckFailed as failed:
            events.emit("wait_failed", step=step, reason=spec.reason, detail=failed.detail)
            raise
        now = clock.now()
        if status == MET:
            return
        if now >= deadline:
            look_at = spec.look_at_for(ctx)
            events.emit(
                "wait_gave_up",
                step=step,
                reason=spec.reason,
                bound_s=spec.bound_s,
                look_at=look_at,
                state=status,
                detail=detail,
            )
            raise WaitGaveUp(step, spec, resume, look_at=look_at, detail=detail)
        fields: dict[str, Any] = {}
        if detail and detail != shown:
            fields["detail"] = detail
            shown = detail
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
            **fields,
        )
        clock.sleep(min(spec.interval_s, deadline - now))
