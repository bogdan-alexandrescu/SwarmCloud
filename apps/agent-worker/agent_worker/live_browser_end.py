"""A live browser gone for good ends the attempt retryably, named on the task (issue 272).

Its own module rather than `lifecycle.py`, which is being split by concern
(issue 68) and may only get shorter: `Worker` inherits `LiveBrowserEndMixin`,
so the method keeps its `self` access and `worker._live_browser_after_tick`
still resolves. `Outcome` lives in `lifecycle` and is imported where it is
used: a module-level import of `lifecycle` from here would be circular.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm_common.models import EndCause

from . import handoff as handoff_mod
from .errors import ExitCode

if TYPE_CHECKING:
    from .lifecycle import Outcome
    from .procman import ChildProcess


class LiveBrowserEndMixin:
    """`Worker._live_browser_after_tick`; every attribute it reads is `Worker`'s."""

    handoff: Any
    cfg: Any
    ws: Any
    control: Any
    log: Any
    _fenced_exit: bool
    _writes_forbidden: bool
    _control_plane_down: bool

    def _live_browser_after_tick(self, child: ChildProcess, *, parking: bool) -> Outcome | None:
        """The live browser's restarts and loss, after a hand-off tick (issue 272).

        Each note `HandoffPath` kept is recorded once on the task's events:
        `live_browser_restarted` per restart, `live_browser_crashed` for the
        loss, with the exit's cause word, signal, exit code and `oom_kills`
        (never Chromium's stderr: the gate does not pass it on).

        Gone for good while the agent runs, the attempt ends RETRYABLY, as
        `_fail_live_gate_missing` does before it starts: only a new attempt
        brings a new sidecar, and left alone the agent reported "not rendered"
        on a task that looked successful. In order: end an open hand-off, stop
        the agent, checkpoint (invariant 8, the retry continues from the
        tree), `fail_retryably` -- lease and capacity released in its one
        transaction (invariant 1). None, and nothing written, when `parking`
        (a hold just ran out, or the channel dropped, with a person waited
        for: that park is unchanged), on a fenced attempt, with writes
        forbidden or the control plane down (invariants 3 and 5: those exits
        are the loop's own), or with no live browser or a wiped one.
        """
        from .lifecycle import Outcome  # a module-level import would be circular

        handoff = self.handoff
        for note in handoff.drain_browser_notes():
            last = note.get("last_exit") or {}
            self._emit_startup_mark(
                handoff_mod.LIVE_BROWSER_RESTARTED if note.get("event") == "browser_restarted"
                else handoff_mod.LIVE_BROWSER_CRASHED,
                {"restarts": note.get("restarts"), "exit_cause": last.get("cause"),
                 "signal": last.get("signal"), "exit_code": last.get("exit_code"),
                 "oom_kills": last.get("oom_kills")},
            )
        gone = handoff.browser_gone
        if (gone is None or parking or not handoff.live or handoff.wiped
                or self._fenced_exit or self._writes_forbidden or self._control_plane_down):
            return None
        cfg = self.cfg
        ws = self.ws
        assert ws is not None
        cause = handoff_mod.LIVE_BROWSER_CRASHED
        last = gone.get("last_exit") or {}
        restarts = int(gone.get("restarts") or 0)
        oom = last.get("oom_kills")
        how = (f"signal {last['signal']}" if last.get("signal")
               else f"exit code {last['exit_code']}" if last.get("exit_code") is not None
               else "cause not known")
        error = (
            f"{cause}: the live browser's Chromium exited ({how}) and could not be started "
            f"again after {restarts} restart{'' if restarts == 1 else 's'}"
            + (f"; the display sidecar ran out of memory ({oom} OOM kill(s))"
               if isinstance(oom, int) and oom > 0 else "")
            + "; the agent was stopped and the attempt is retried on a new pod while the "
            "task has attempts left."
        )
        self.log.error("the live browser is gone for good; failing the attempt retryably",
                       cause=cause, restarts=restarts, exit_cause=last.get("cause"))
        handoff.end(ws.work, outcome="unavailable", cause=cause)
        child.terminate(cfg.termination_grace_seconds, reason=cause)
        child.finish()
        self._child_ended()
        self._checkpoint(cause)
        with self._metrics_after_the_record():
            state = self.control.fail_retryably(
                exit_code=None,
                error=error,
                cause=cause,
                result_summary={"live_browser": dict(gone)},
                retry_delay_seconds=handoff_mod.LIVE_GATE_RETRY_DELAY_SECONDS,
                detail={"restarts": restarts, "exit_cause": last.get("cause"),
                        "signal": last.get("signal"), "exit_code": last.get("exit_code"),
                        "oom_kills": oom},
                # No frozen member names the browser (contract request in the
                # PR); RUNNER_ERROR is the worker's mid-run failure.
                end_cause=EndCause.RUNNER_ERROR,
            )
        return Outcome(exit_code=ExitCode.FAILED, state=state)
