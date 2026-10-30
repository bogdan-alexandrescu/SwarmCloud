"""`swarm install --repair <check>`: re-run the one step that satisfies a
manifest entry (plan §1.5 DAYONE-DOCTOR-1).

`swarm doctor` names a finding's repair as `swarm install --repair <id>`; this
is it. The fix lives in `install`, never in `doctor`, because a repair needs
exactly what an install has: its lease, its state record, the step's plan and
its approvals.

1. The entry's `step:` (manifest) names the step; the step must be one this
   release defines (a triggered step too), and must list the entry in its
   `satisfies`.
2. **One typed confirmation**: the person types the entry id. It ignores
   `SWARM_ASSUME_YES` (CLAUDE.md: anything that changes a deployment on a
   person's word asks for the word typed), and with no terminal nothing is
   typed, so the repair is refused (exit 5).
3. The engine runs: lease, then every earlier-stage step and every other
   step of the same stage is CHECKED (read-only, so the step's dependency gate
   sees what is met), then the one step runs through `Engine.run_step`, with
   its own plan, approvals, page and polling. A step whose check is met and
   recorded done is skipped: there is nothing to repair.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .engine import Context, Engine, Result, _Stop
from .exitcodes import ExitCode
from .lease import LeaseHeld, LeaseLost, LeaseRefused
from .manifest import Manifest
from .record import RecordConflict
from .state import StateConflict
from .steps import Step, triggered_of


class RepairUsage(ValueError):
    """The entry cannot be repaired by name (no such entry, no step)."""


def all_steps(steps: Sequence[Step]) -> list[Step]:
    found: list[Step] = []
    pending = list(steps)
    while pending:
        step = pending.pop(0)
        found.append(step)
        pending.extend(triggered_of(step))
    return found


def step_for(entry_id: str, manifest: Manifest, steps: Sequence[Step]) -> Step:
    """The step that satisfies `entry_id`, by the entry's `step:`."""
    entry = manifest.by_id().get(entry_id)
    if entry is None:
        raise RepairUsage(f"{entry_id!r} is not a manifest entry (swarm doctor lists the ids)")
    if not entry.step:
        raise RepairUsage(f"{entry_id} names no step: no install step makes it")
    for step in all_steps(steps):
        if step.id == entry.step:
            if entry_id not in step.satisfies:
                raise RepairUsage(f"{entry_id} names step {step.id}, which does not satisfy it")
            return step
    raise RepairUsage(f"{entry_id} names step {entry.step}, which this release does not define")


def confirmed(entry_id: str, step: Step, confirm: Callable[[str], str]) -> bool:
    """The typed confirmation: the entry id, typed. SWARM_ASSUME_YES is not read."""
    typed = confirm(
        f"This re-runs step {step.id} (stage {step.stage}) to repair {entry_id}, with that step's own "
        f"approvals.\nType {entry_id} to go on: "
    )
    return typed.strip() == entry_id


class RepairEngine(Engine):
    """The engine, run for one step."""

    def run_one(self, target: Step) -> Result:
        self.unknown_in_dry_run = False
        self.met = set()
        state = self.store.load()
        self.ctx = Context(
            project=self.store.project,
            environment=self.store.environment,
            holder=self.holder,
            state=state,
            events=self.events,
            clock=self.clock,
            dry_run=self.dry_run,
            engine=self,
        )
        self.events.emit("run_start", dry_run=self.dry_run, stages=[target.stage], repair=target.id)
        acquired = False
        try:
            self.lease.acquire(self.confirm)
            acquired = True
            self.events.emit("lease", holder=self.holder, expires=self.lease.expires)
            for step in all_steps(self._ordered()):
                if step is target or step.id == target.id:
                    continue
                if step.stage <= target.stage:
                    self._observe_only(step)
            self.run_step(target)
            return self._end("ok", None, "")
        except _Stop as stop:
            return self._end(stop.outcome, stop.step, stop.message)
        except (LeaseHeld, LeaseRefused) as refused:
            return self._end("refused", None, str(refused))
        except (LeaseLost, StateConflict, RecordConflict) as lost:
            return self._end("failed", None, f"{lost}; run swarm install --repair again")
        finally:
            if acquired:
                self.lease.release()


def repair(entry_id: str, *, manifest: Manifest, engine: RepairEngine,
           confirm: Callable[[str], str], say: Callable[[str], None] = print) -> ExitCode:
    """Confirm, then re-run the step that satisfies `entry_id`."""
    step = step_for(entry_id, manifest, engine.steps)
    if not confirmed(entry_id, step, confirm):
        say(f"swarm install --repair: refused: {entry_id} was not typed; nothing was changed")
        return ExitCode.REFUSED
    return engine.run_one(step).exit_code
