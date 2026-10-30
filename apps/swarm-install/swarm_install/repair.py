"""`swarm install --repair <check>`: re-run the one step that satisfies a
manifest entry (plan §1.5 DAYONE-DOCTOR-1).

`swarm doctor` names a finding's repair as `swarm install --repair <id>`; this
is it. The fix lives in `install`, never in `doctor`, because a repair needs
exactly what an install has: its lease, its state record, the step's plan and
its approvals.

1. The entry's `step:` (manifest) names the step; the step must be one this
   release defines (a triggered step too). It need not list the entry in its
   `satisfies`: the engine owns about 360 entries through `step:` alone (the
   Terraform passes declare none, `steps/infra.py`), as its own
   `_owning_step_done` does, and `swarm doctor` prints the repair for every
   one (N13).
2. **One typed confirmation**: the person types the entry id. It ignores
   `SWARM_ASSUME_YES` (CLAUDE.md: anything that changes a deployment on a
   person's word asks for the word typed), and with no terminal nothing is
   typed, so the repair is refused (exit 5).
3. The engine runs: lease, then every earlier-stage step and every other
   step of the same stage is CHECKED (read-only, so the step's dependency gate
   sees what is met), then the one step runs through `Engine.run_step`, with
   its own plan, approvals, page and polling. A step whose check is met and
   recorded done is skipped when the entry is in its `satisfies` (its check
   covers the entry, so there is nothing to repair); for an entry owned through
   `step:` alone the step runs again, because its check does not look at the
   entry.
4. **A Terraform pass runs as the latest pass of its root.** Re-applying
   `infra-1` or `infra-2` after `infra-3` would switch the GitHub App off
   (lesson 39; N12), so a pass a later one superseded repairs through the step
   that applies the latest applied pass of the same root (`infra-close`,
   `github.pass-4`, `bootstrap.pass-2`), whose inputs hold every resource the
   earlier pass made.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from . import tfvars
from .engine import Context, Engine, Result, _Stop
from .exitcodes import UNMET, ExitCode
from .lease import LeaseHeld, LeaseLost, LeaseRefused
from .manifest import Manifest
from .record import RecordConflict
from .state import StateConflict
from .steps import Observation, Step, triggered_of
from .steps.infra import latest_applied


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
    """The step that makes `entry_id`, by the entry's `step:`."""
    entry = manifest.by_id().get(entry_id)
    if entry is None:
        raise RepairUsage(f"{entry_id!r} is not a manifest entry (swarm doctor lists the ids)")
    if not entry.step:
        raise RepairUsage(f"{entry_id} names no step: no install step makes it")
    for step in all_steps(steps):
        if step.id == entry.step:
            return step
    raise RepairUsage(f"{entry_id} names step {entry.step}, which this release does not define")


def latest_pass_step(step: Step, steps: Sequence[Step], outputs: Any) -> Step:
    """`step`, or, when it is a Terraform pass a later pass of the same root has
    superseded, the step that applies that root's latest applied pass (N12)."""
    pass_name = getattr(step, "pass_name", "")
    if pass_name not in tfvars.PASSES:
        return step
    latest = latest_applied(outputs, tfvars.ROOT_OF[pass_name])
    if latest is None or latest == pass_name or not tfvars.after(latest, pass_name):
        return step
    carriers = [s for s in all_steps(steps) if getattr(s, "pass_name", "") == latest]
    return carriers[-1] if carriers else step


class Rerun:
    """A step whose first check is unmet, so the engine applies it again: the
    repair of an entry the step's own check does not cover. Everything else,
    the check after the apply included, is the step's own."""

    def __init__(self, step: Step, entry_id: str) -> None:
        self._step = step
        self._entry_id = entry_id
        self._first = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._step, name)

    def check(self, ctx: Any) -> Observation:
        if self._first:
            self._first = False
            return Observation(UNMET, f"swarm install --repair {self._entry_id}: {self._step.id}'s own check does "
                                      f"not cover {self._entry_id}, so the step runs again")
        return self._step.check(ctx)


def confirmed(entry_id: str, step: Step, confirm: Callable[[str], str]) -> bool:
    """The typed confirmation: the entry id, typed. SWARM_ASSUME_YES is not read."""
    latest = (" A Terraform pass a later pass has superseded runs as the latest applied pass of its root, so no "
              "switch a later pass turned on goes off." if getattr(step, "pass_name", "") else "")
    typed = confirm(
        f"This re-runs step {step.id} (stage {step.stage}) to repair {entry_id}, with that step's own "
        f"approvals.{latest}\nType {entry_id} to go on: "
    )
    return typed.strip() == entry_id


class RepairEngine(Engine):
    """The engine, run for one step."""

    def run_one(self, target: Step, entry_id: str | None = None) -> Result:
        """Run `target` alone; `entry_id` names the entry it repairs, and when
        `target` does not list it in `satisfies`, the step runs even if its
        check is met."""
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
        target = latest_pass_step(target, self._ordered(), state.outputs)
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
            rerun = entry_id is not None and entry_id not in target.satisfies
            self.run_step(Rerun(target, entry_id) if rerun else target)  # type: ignore[arg-type]
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
    """Confirm, then re-run the step that makes `entry_id`."""
    step = step_for(entry_id, manifest, engine.steps)
    if not confirmed(entry_id, step, confirm):
        say(f"swarm install --repair: refused: {entry_id} was not typed; nothing was changed")
        return ExitCode.REFUSED
    return engine.run_one(step, entry_id).exit_code
