"""The install engine: the step loop, resume, drift and dry run (design §2.2-§2.4).

**The loop, for each stage in order and each step in the stage:**

1. `check` the step (read-only). A check that raises is `unknown`.
2. Met, and the state says done: skip it (resume re-checks, it never re-runs).
   Met and not recorded: `verify` it and record it done (adopted).
3. Recorded done but the check now fails: that is **drift**; it is reported,
   with the step that repairs it (this one), and repaired.
4. Before anything is applied, every manifest entry the step's entries
   `depends_on` must already be met: a step that lists it in `satisfies` is
   done, or the step its entry names (`entry.step`, how the terraform passes
   own what they make) is. If one is not, the stage refuses and names the
   entry and the step that satisfies it (lesson 8: infra applied before
   bootstrap fails on a missing custom role).
5. `plan`, print it, renew the lease, `apply`; for a human step, open its
   page once (`steps.open_page`, before the first poll, never on every poll);
   for a human or wait step, poll the check with a countdown up to the step's bound (running the wait's
   triggered steps on every poll); else re-`check`. Then `verify`.
6. **Record the result in the state store after every step**, never only at
   the end of a stage, so a crash between two steps resumes at the second.

`--dry-run` checks every step and prints each unmet step's plan: it takes no
lease and writes no state. It runs stage 0 (design §2.4) through the stage-0
steps that declare `read_only = True` (preflight asks and reads); every other
step's `apply` is never called.

The result is an `exitcodes.ExitCode`: 0 only when every step in scope is met
and verified; 1 a step failed (named, with how to continue); 3 a check could
not run; 4 paused on a human step or a wait past its bound; 5 refused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import exitcodes
from .events import Events
from .exitcodes import MET, UNKNOWN, ExitCode
from .lease import Lease, LeaseHeld, LeaseLost, LeaseRefused
from .manifest import Entry, Manifest
from .record import InstallRecord, RecordConflict
from .state import (
    STEP_DONE,
    STEP_DRIFT,
    STEP_FAILED,
    STEP_PAUSED,
    InstallState,
    ObjectStore,
    StateConflict,
    StateStore,
    StepRecord,
)
from .steps import (
    HUMAN_STEP,
    WAIT_STEP,
    Observation,
    Plan,
    Step,
    TriggeredStep,
    check_steps,
    deep_link_of,
    kind_of,
    open_page,
    triggered_of,
    wait_of,
)
from .waits import Clock, WaitGaveUp, WaitSpec, wait_until

#: A human step that names no bound of its own waits this long for its check.
HUMAN_WAIT = WaitSpec(
    reason="a person finishing the step in a console",
    expected="a few minutes",
    expected_s=5 * 60,
    bound_s=30 * 60,
    look_at="the step's instructions above; run swarm install again once it is done",
)


@dataclass
class Result:
    outcome: str
    exit_code: ExitCode
    step: str | None = None
    message: str = ""


class _Stop(Exception):
    """Ends the run with an outcome (internal)."""

    def __init__(self, outcome: str, step: str | None, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.step = step
        self.message = message


@dataclass
class Context:
    """What a step sees: the project, the state, the events, the clock."""

    project: str
    environment: str
    holder: str
    state: InstallState
    events: Events
    clock: Clock
    dry_run: bool
    engine: Engine = field(repr=False)

    @property
    def outputs(self) -> dict[str, Any]:
        return self.state.outputs

    def attach_bucket(self, bucket: ObjectStore) -> None:
        """The state bucket exists (stage 1): upload the local state once and
        move the lease into the bucket."""
        self.engine.attach_bucket(bucket)

    def attach_record(self, record: InstallRecord) -> None:
        """Firestore exists (stage 5): mirror the state to the install record."""
        self.engine.record = record
        self.engine.mirror()

    def read_console(self, name: str) -> dict[str, Any] | None:
        """A console document, copied into the state (plan §5.4); `None` before
        Firestore exists or while swarm-api has not written it."""
        record = self.engine.record
        if record is None:
            return None
        found = record.read_console(name)
        if found is not None and not self.dry_run:
            self.engine.save(lambda s: s.console.__setitem__(name, found))
        elif found is not None:
            self.state.console[name] = found
        return found


class Engine:
    def __init__(
        self,
        steps: Sequence[Step],
        *,
        store: StateStore,
        lease: Lease,
        events: Events,
        clock: Clock,
        holder: str,
        manifest: Manifest | None = None,
        record: InstallRecord | None = None,
        dry_run: bool = False,
        confirm: Callable[[str], str] | None = None,
    ) -> None:
        check_steps(steps)
        self.steps = list(steps)
        self.store = store
        self.lease = lease
        self.events = events
        self.clock = clock
        self.holder = holder
        self.manifest = manifest
        self.record = record
        self.dry_run = dry_run
        self.confirm = confirm
        self.met: set[str] = set()
        # Step id -> whether this run found it met; a dependency whose manifest
        # entry names its owning step (`entry.step`) is met once that step is.
        self._step_met: dict[str, bool] = {}
        self._owner: dict[str, Step] = {}
        self._stage_of: dict[str, int] = {}
        pending: list[Step] = list(steps)
        while pending:
            step = pending.pop(0)
            self._stage_of[step.id] = step.stage
            for entry in step.satisfies:
                self._owner.setdefault(entry, step)
            pending.extend(triggered_of(step))
        self.ctx: Context | None = None

    # -- state ------------------------------------------------------------

    def save(self, change: Callable[[InstallState], None]) -> None:
        """Apply `change` to the stored state (re-reading on a conflict) and mirror it."""
        assert self.ctx is not None
        self.ctx.state = self.store.update(change)
        self.mirror()

    def mirror(self) -> None:
        if self.record is None or self.ctx is None or self.dry_run:
            return
        self.record.write(self.ctx.state, self._stage_of, lease=self.lease.as_record())

    def _record(self, step: Step, state: str, *, plan: Plan | None = None,
                outputs: dict[str, Any] | None = None, detail: str | None = None) -> None:
        if self.dry_run:
            return
        entry = StepRecord(
            state=state,
            at=self.clock.now(),
            by=self.holder,
            plan_digest=plan.digest() if plan is not None else None,
            outputs=outputs or {},
            detail=detail,
        )

        def change(s: InstallState) -> None:
            s.steps[step.id] = entry
            if outputs:
                s.outputs.update(outputs)

        self.save(change)

    def attach_bucket(self, bucket: ObjectStore) -> None:
        assert self.ctx is not None
        self.ctx.state = self.store.attach_bucket(bucket)
        if self.dry_run:
            return
        moved = Lease(holder=self.holder, store=bucket, clock=self.clock.now)
        moved.acquire(self.confirm)
        self.lease = moved

    # -- the loop ---------------------------------------------------------

    def _observe(self, step: Step, what: Callable[[Any], Observation]) -> Observation:
        try:
            return what(self.ctx)
        except Exception as exc:  # a probe that cannot run is unknown, not fine
            return Observation(UNKNOWN, f"{type(exc).__name__}: {exc}")

    def _mark(self, step: Step, met: bool) -> None:
        self._step_met[step.id] = met
        if met:
            self.met.update(step.satisfies)

    def _owning_step_done(self, entry: Entry | None) -> bool:
        """Whether the step a manifest entry names (`entry.step`) is done: met
        in this run, or, when this run has not looked at it, recorded done.

        The terraform passes declare no `satisfies`; what they make (the
        cluster, the service accounts, Firestore, the Scheduler jobs, the
        platform keys) is owned through `entry.step` alone."""
        if entry is None or not entry.step:
            return False
        if entry.step in self._step_met:
            return self._step_met[entry.step]
        return self.ctx is not None and self.ctx.state.done(entry.step)

    def _unmet_dependencies(self, step: Step) -> list[str]:
        if self.manifest is None:
            return []
        by_id = self.manifest.by_id()
        own = set(step.satisfies)
        needed: list[str] = []
        for entry_id in step.satisfies:
            entry = by_id.get(entry_id)
            for dep in entry.depends_on if entry is not None else ():
                if dep in own or dep in self.met or dep in needed:
                    continue
                if self._owning_step_done(by_id.get(dep)):
                    continue
                needed.append(dep)
        return needed

    def _refuse_for(self, step: Step, unmet: list[str]) -> _Stop:
        by_id = self.manifest.by_id() if self.manifest is not None else {}
        named = []
        for dep in unmet:
            owner = self._owner.get(dep)
            entry = by_id.get(dep)
            if owner is not None:
                named.append(f"{dep} (satisfied by step {owner.id}, stage {owner.stage})")
            elif entry is not None and entry.step in self._stage_of:
                named.append(f"{dep} (made by step {entry.step}, stage {self._stage_of[entry.step]})")
            else:
                named.append(f"{dep} (no step in this release satisfies it)")
        message = (
            f"stage {step.stage} refuses: step {step.id} needs {', '.join(named)}, "
            "which is not met. Run swarm install from the earlier stage."
        )
        self.events.emit("stage_refused", step=step.id, stage=step.stage, needs=unmet, message=message)
        return _Stop("blocked", step.id, message)

    def _verified(self, step: Step, plan: Plan | None, outputs: dict[str, Any]) -> None:
        verified = self._observe(step, step.verify)
        if verified.status != MET:
            self._record(step, STEP_FAILED, plan=plan, detail=verified.detail)
            raise self._failed(step, f"verify is {verified.status}: {verified.detail}")
        self._record(step, STEP_DONE, plan=plan, outputs={**outputs, **dict(verified.outputs)})
        self._mark(step, True)
        self.events.emit("step_done", step=step.id, stage=step.stage)

    def _failed(self, step: Step, why: str) -> _Stop:
        message = f"step {step.id} failed: {why}. {step.explain()} Fix it, then run swarm install again."
        self.events.emit("step_failed", step=step.id, stage=step.stage, message=message)
        return _Stop("failed", step.id, message)

    def _heartbeat(self) -> None:
        if self.dry_run or not self.lease.heartbeat_due():
            return
        self.lease.renew()
        self.events.emit("lease", holder=self.holder, expires=self.lease.expires)
        self.mirror()

    def _run_triggered(self, waiting: Step, done: set[str]) -> None:
        for triggered in triggered_of(waiting):
            if triggered.id in done:
                continue
            if self.ctx is not None and self.ctx.state.done(triggered.id):
                if self._observe(triggered, triggered.check).status == MET:
                    done.add(triggered.id)
                    self._mark(triggered, True)
                    continue
            if not self._trigger_landed(triggered):
                continue
            self.events.emit("step_triggered", step=triggered.id, by_wait=waiting.id)
            self.run_step(triggered)
            done.add(triggered.id)

    def _trigger_landed(self, step: TriggeredStep) -> bool:
        try:
            return bool(step.trigger(self.ctx))
        except Exception:  # an unreadable trigger has not landed; the next poll asks again
            return False

    def run_step(self, step: Step) -> None:
        assert self.ctx is not None
        observed = self._observe(step, step.check)
        was_done = self.ctx.state.done(step.id)
        self.events.emit(
            "step_check", step=step.id, stage=step.stage, state=observed.status, detail=observed.detail
        )
        if observed.status == MET:
            if was_done:
                self._mark(step, True)
                self.events.emit("step_skip", step=step.id, stage=step.stage, state=MET)
                return
            if self.dry_run:
                self._mark(step, True)
                return
            self._verified(step, None, dict(observed.outputs))
            return
        if was_done:
            self.events.emit(
                "step_drift",
                step=step.id,
                stage=step.stage,
                state=observed.status,
                detail=observed.detail,
                repaired_by=step.id,
                message=f"{step.id} was done and its check is now {observed.status}; "
                f"step {step.id} will repair it",
            )
            self._record(step, STEP_DRIFT, detail=observed.detail)
        if observed.status == UNKNOWN and kind_of(step) != WAIT_STEP:
            if self.dry_run:
                self.unknown_in_dry_run = True
            else:
                raise _Stop(
                    "unknown",
                    step.id,
                    f"step {step.id}'s check could not run: {observed.detail}. "
                    "Nothing was applied; fix access, then run swarm install again.",
                )
        self._mark(step, False)
        unmet = self._unmet_dependencies(step)
        plan = self._plan(step)
        if self.dry_run:
            self.events.emit(
                "step_plan",
                step=step.id,
                stage=step.stage,
                plan=plan.summary if plan else "",
                actions=list(plan.actions) if plan else [],
                waits_on=unmet,
                explain=step.explain(),
            )
            if step.stage == 0 and getattr(step, "read_only", False) and not unmet and plan:
                # Design §2.4: the dry run RUNS stage 0. A preflight step that
                # declares `read_only` only asks and reads; nothing is recorded.
                self.events.emit("step_apply", step=step.id, stage=step.stage, explain=step.explain())
                try:
                    step.apply(self.ctx, plan)
                except Exception as exc:
                    raise self._failed(step, f"apply raised {type(exc).__name__}: {exc}") from exc
                if self._observe(step, step.check).status == MET:
                    self._mark(step, True)
            return
        if unmet:
            raise self._refuse_for(step, unmet)
        if plan is None:
            raise self._failed(step, "its plan could not be made")
        self.events.emit(
            "step_plan", step=step.id, stage=step.stage, plan=plan.summary,
            actions=list(plan.actions), digest=plan.digest(),
        )
        self.lease.renew()
        self.events.emit("step_apply", step=step.id, stage=step.stage, explain=step.explain())
        try:
            step.apply(self.ctx, plan)
        except _Stop:
            raise
        except (WaitGaveUp, LeaseLost):
            raise
        except Exception as exc:
            self._record(step, STEP_FAILED, plan=plan, detail=f"{type(exc).__name__}: {exc}")
            raise self._failed(step, f"apply raised {type(exc).__name__}: {exc}") from exc
        kind = kind_of(step)
        if kind == HUMAN_STEP:
            self._open_page(step, plan)
        if kind in (HUMAN_STEP, WAIT_STEP):
            spec = wait_of(step) or HUMAN_WAIT
            done: set[str] = set()
            try:
                wait_until(
                    step.id,
                    spec,
                    lambda: self._observe(step, step.check).status,
                    clock=self.clock,
                    events=self.events,
                    on_poll=lambda: (self._heartbeat(), self._run_triggered(step, done)),
                )
            except WaitGaveUp as gave_up:
                self._record(step, STEP_PAUSED, plan=plan, detail=str(gave_up))
                self.events.emit("step_paused", step=step.id, stage=step.stage, message=str(gave_up))
                raise _Stop("paused", step.id, str(gave_up)) from gave_up
            rechecked = self._observe(step, step.check)
        else:
            rechecked = self._observe(step, step.check)
            if rechecked.status != MET:
                self._record(step, STEP_FAILED, plan=plan, detail=rechecked.detail)
                raise self._failed(step, f"after apply its check is {rechecked.status}: {rechecked.detail}")
        self._verified(step, plan, dict(rechecked.outputs))

    def _open_page(self, step: Step, plan: Plan) -> None:
        """A human step's exact page, opened once before its check is polled."""
        try:
            link = deep_link_of(step, self.ctx, plan)
        except Exception:  # noqa: BLE001 - no link is printed then; the plan already named the steps
            return
        if link:
            open_page(link, why=f"{step.id}: {plan.summary}")

    def _plan(self, step: Step) -> Plan | None:
        try:
            return step.plan(self.ctx)
        except Exception as exc:
            if self.dry_run:
                return Plan(f"(the plan could not be made: {type(exc).__name__}: {exc})")
            raise self._failed(step, f"plan raised {type(exc).__name__}: {exc}") from exc

    def run(self, stages: Iterable[int] | None = None) -> Result:
        """Run every stage in scope, in order; `stages=None` is every stage.

        A scoped run still checks (read-only) every step of the earlier stages,
        so a stage never runs on an earlier entry nobody looked at.
        """
        scope = None if stages is None else set(stages)
        self.unknown_in_dry_run = False
        self.met = set()
        self._step_met = {}
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
        self.events.emit("run_start", dry_run=self.dry_run, stages=sorted(scope) if scope else None)
        acquired = False
        try:
            if not self.dry_run:
                self.lease.acquire(self.confirm)
                acquired = True
                self.events.emit("lease", holder=self.holder, expires=self.lease.expires)
            first = min(scope) if scope else 0
            current_stage: int | None = None
            for step in self._ordered():
                if scope is not None and step.stage not in scope:
                    if step.stage < first:
                        self._observe_only(step)
                    continue
                if step.stage != current_stage:
                    current_stage = step.stage
                    self.events.emit("stage_start", stage=step.stage)
                self.run_step(step)
            if self.dry_run and self.unknown_in_dry_run:
                return self._end("unknown", None, "a check could not run; see the events above")
            return self._end("ok", None, "")
        except _Stop as stop:
            return self._end(stop.outcome, stop.step, stop.message)
        except LeaseHeld as held:
            return self._end("refused", None, str(held))
        except LeaseRefused as refused:
            return self._end("refused", None, str(refused))
        except (LeaseLost, StateConflict, RecordConflict) as lost:
            return self._end("failed", None, f"{lost}; run swarm install again to resume")
        finally:
            if acquired:
                self.lease.release()

    def _ordered(self) -> list[Step]:
        """Stages in order; within a stage, a step after every same-stage step
        that satisfies an entry it depends on, else in registration order."""
        by_id = self.manifest.by_id() if self.manifest is not None else {}
        ordered: list[Step] = []
        for stage in sorted({s.stage for s in self.steps}):
            pending = [s for s in self.steps if s.stage == stage]
            while pending:
                for candidate in pending:
                    needs = {
                        dep
                        for entry in candidate.satisfies
                        for dep in (by_id[entry].depends_on if entry in by_id else ())
                        if dep not in candidate.satisfies
                    }
                    same_stage = {
                        dep for dep in needs
                        if dep in self._owner and self._owner[dep] in pending
                        and self._owner[dep] is not candidate
                    }
                    if not same_stage:
                        break
                else:  # a cycle: keep registration order; the dependency gate names it
                    candidate = pending[0]
                ordered.append(candidate)
                pending.remove(candidate)
        return ordered

    def _observe_only(self, step: Step) -> None:
        observed = self._observe(step, step.check)
        self.events.emit(
            "step_check", step=step.id, stage=step.stage, state=observed.status, detail=observed.detail
        )
        self._mark(step, observed.status == MET)

    def _end(self, outcome: str, step: str | None, message: str) -> Result:
        code = exitcodes.for_outcome(outcome)
        self.events.emit("run_end", outcome=outcome, exit_code=int(code), step=step, message=message)
        return Result(outcome=outcome, exit_code=code, step=step, message=message)
