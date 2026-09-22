"""Workflow state: derived from the steps, written back, and checked for drift.

WHY THIS FILE EXISTS. Nothing in this platform ever advanced `Workflow.state`.
`Store.create_workflow` set it once at submission and `Store.cancel_workflow`
only ever touched `cancel_requested`, so every workflow ever created read QUEUED
forever -- `wf_bcdc9180e4fb4a209f31` read QUEUED while its three steps were
SUCCEEDED, FAILED and CANCELLED. The web UI worked around it by computing a
rollup from the steps' own tasks, which is why that screen said "0/3 done"
correctly while the heading beside it still said "queued". Anything that reads
what the API serves -- `sc`, the MCP bridge, a future consumer -- had no such
workaround.

THE SHAPE, which the owner chose deliberately and which is three parts, not two:

  * DERIVE, so the value a reader sees cannot go stale. `derive()` below is the
    single implementation, and `effective_state()` is what every read path
    serves.
  * WRITE, so the value is queryable. A derived field cannot answer "list my
    failed workflows" without loading every workflow's tasks.
  * DRIFT CHECK, because a written value and a derived value are two records of
    one fact, which is the defect shape this repository has spent days removing
    (a pool counter versus a lease sum; Firestore versus the API; a nav label
    versus a page heading). `drift_of()` reports a disagreement; nothing here
    resolves one silently.

WHY THE DERIVATION LIVES IN THIS PACKAGE AND NOT IN THE SCHEDULER, which has a
one-minute Cloud Scheduler tick and would have been the convenient home. The
scheduler image installs `apps/common` and `apps/scheduler` and nothing else
(images/swarm-scheduler/Dockerfile:59-60); the API image installs `apps/common`
and `apps/swarm-api` (images/swarm-api/Dockerfile:59-60). Neither can import the
other, and `apps/common/swarm_common/` is frozen, so a scheduler-side write
would need a SECOND copy of the precedence table below. That is exactly the
restatement this house has learned not to make -- and it would be worse than
usual here, because the thing the second copy would be compared against is the
drift check, which would then report the two copies drifting rather than the
data. The request to move this into the frozen contract, where both could import
it, is filed as entry 7 in docs/contract-change-requests.md.

WHY THERE IS NO NEW ENUM. `Workflow.state` is typed `TaskState`
(models.py:344) -- the task vocabulary, reused for a workflow. It is adequate:
RUNNING, SUCCEEDED, FAILED, CANCELLED, DEAD_LETTERED, READY, PARKED and QUEUED
all carry their task meaning up to the workflow unchanged, and the one thing the
vocabulary cannot say -- "some steps succeeded and some did not" -- is said by
`counts` instead of by inventing a state. A workflow-specific enum
(RUNNING vs PARTIALLY_FAILED vs FAILED) would be a change to the frozen
contract; it is filed as entry 8 in docs/contract-change-requests.md rather than
made here, and nothing below depends on it arriving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import (
    CONCURRENCY_STATES,
    PENDING_STATES,
    TERMINAL_STATES,
    TaskState,
)

log = logging.getLogger(__name__)

#: The state a reader is given when the derivation could not be completed. It is
#: deliberately NOT a TaskState: it must be impossible to write this into
#: Firestore, where `codec.workflow_from_dict` would reject it, and impossible
#: for a caller to mistake it for a state the platform can be in.
UNKNOWN = "UNKNOWN"

#: Terminal states, worst first. A workflow that has finished is only as good as
#: its worst step, so the first of these present is the answer.
#:
#: DEAD_LETTERED outranks FAILED because it is strictly more final -- a
#: dead-lettered step has exhausted its retries and nothing will pick it up.
#: FAILED outranks CANCELLED because when both appear the cancellations are
#: usually CAUSED by the failure: `scheduler/loop.py:295` cancels the dependents
#: of a failed parent with "an upstream workflow step did not succeed". Reporting
#: CANCELLED there would hide the fault behind its own consequence, which is the
#: same mistake as reporting the second error instead of the first.
_TERMINAL_SEVERITY: tuple[TaskState, ...] = (
    TaskState.DEAD_LETTERED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.SUCCEEDED,
)

#: Pending states, most advanced first. A workflow that is not finished and is
#: not holding capacity is described by the step closest to running: a READY step
#: is about to be admitted, a PARKED one is durably waiting, and QUEUED is the
#: floor. This is the only ordering that answers "is this thing moving".
_PENDING_PRECEDENCE: tuple[TaskState, ...] = (
    TaskState.READY,
    TaskState.PARKED,
    TaskState.QUEUED,
    TaskState.SUBMITTED,
)


def _check_coverage() -> None:
    """Fail at import if the frozen contract grew a state this file cannot rank.

    A new terminal state that fell off the end of `_TERMINAL_SEVERITY` would be
    ranked last, which means a workflow containing it would read SUCCEEDED. That
    is the cheerful-answer bug in its purest form, so it is refused loudly at
    start rather than discovered from a dashboard. Raised, not asserted: `python
    -O` strips an assert and this check has to survive it.
    """
    missing_terminal = TERMINAL_STATES - set(_TERMINAL_SEVERITY)
    if missing_terminal:
        raise RuntimeError(
            "swarm_api.rollup cannot rank terminal states "
            f"{sorted(s.value for s in missing_terminal)}; add them to "
            "_TERMINAL_SEVERITY in severity order before shipping"
        )
    missing_pending = PENDING_STATES - set(_PENDING_PRECEDENCE)
    if missing_pending:
        raise RuntimeError(
            "swarm_api.rollup cannot rank pending states "
            f"{sorted(s.value for s in missing_pending)}; add them to "
            "_PENDING_PRECEDENCE in precedence order before shipping"
        )


_check_coverage()


# --------------------------------------------------------------------------
# Reading the steps
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StepReading:
    """One step's contribution to the rollup, and how it was obtained.

    The three ways a step can fail to produce a state are NOT the same thing and
    must not be collapsed -- the same distinction `stepState` in
    apps/swarm-ui/src/types.ts already makes on the client:

      * `state` set            -- the step's task was read.
      * `unstarted`            -- the step carries no task_id at all. The current
                                  submission path gives every step one, so this
                                  is malformed data rather than a normal phase;
                                  it is still not an error and still not UNKNOWN.
      * neither, `absent`      -- the step names a task_id whose document was not
                                  there, or belongs to another tenant.
      * neither, not `absent`  -- the task was never read at all, because the
                                  per-request read budget ran out.

    The last two both make the rollup incomplete. They are reported separately
    because "the document is missing" is a data fault and "we stopped reading" is
    a capacity decision, and an operator needs to tell them apart.
    """

    step_id: str
    task_id: str | None
    state: TaskState | None
    absent: bool = False

    @property
    def unstarted(self) -> bool:
        return self.task_id is None

    @property
    def readable(self) -> bool:
        return self.task_id is None or self.state is not None


@dataclass(frozen=True)
class WorkflowRollup:
    """What the steps say the workflow is.

    `state` is a `TaskState` value, or `UNKNOWN`. `complete` is false whenever
    any step's state could not be established, and when it is false `state` is
    always UNKNOWN -- there is no arrangement in which this object reports a
    confident answer over a partial read.
    """

    state: str
    complete: bool
    reason: str
    counts: dict[str, int] = field(default_factory=dict)
    unreadable_steps: list[str] = field(default_factory=list)
    unstarted_steps: list[str] = field(default_factory=list)
    steps_read: int = 0

    @property
    def terminal(self) -> bool:
        return self.complete and self.state in {s.value for s in TERMINAL_STATES}

    def to_api(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "complete": self.complete,
            "reason": self.reason,
            "counts": dict(self.counts),
            "unreadable_steps": list(self.unreadable_steps),
            "unstarted_steps": list(self.unstarted_steps),
            "steps_read": self.steps_read,
        }


def read_steps(
    steps: Sequence[WorkflowStep],
    states: Mapping[str, TaskState],
    *,
    absent: Iterable[str] = (),
) -> list[StepReading]:
    """Join each step to its task state.

    `states` maps task_id -> state for every step task that WAS read. `absent`
    names the task_ids that were read and found missing, which is how a data
    fault is told apart from a read that never happened.
    """
    absent_ids = set(absent)
    readings: list[StepReading] = []
    for step in steps:
        if not step.task_id:
            readings.append(StepReading(step_id=step.step_id, task_id=None, state=None))
            continue
        state = states.get(step.task_id)
        readings.append(
            StepReading(
                step_id=step.step_id,
                task_id=step.task_id,
                state=state,
                absent=state is None and step.task_id in absent_ids,
            )
        )
    return readings


# --------------------------------------------------------------------------
# The derivation
# --------------------------------------------------------------------------

def derive(readings: Sequence[StepReading]) -> WorkflowRollup:
    """The workflow's state, from its steps. Pure: no clock, no I/O, no store.

    The order of the tests below is the whole design, so it is spelled out:

      1. NO STEPS -> UNKNOWN. `validate_dag` rejects a stepless submission, so
         this is corrupt data. Deriving SUCCEEDED from an empty set is how "all
         of nothing succeeded" gets reported as success.
      2. ANY STEP UNREADABLE -> UNKNOWN. A derivation over a partial read is not
         evidence. This is the rule the rest of the file exists to protect:
         never "SUCCEEDED because the failures did not load".
      3. ANY STEP HOLDING CAPACITY -> RUNNING. Checked BEFORE the terminal
         severity test, and that ordering is load-bearing: when one step has
         FAILED and a sibling is still RUNNING, the workflow is not over and a
         container is still costing money. Reporting FAILED there would tell an
         operator the work had stopped while it had not.
      4. EVERY STEP TERMINAL -> the worst one present (`_TERMINAL_SEVERITY`).
      5. OTHERWISE -> the most advanced pending state present
         (`_PENDING_PRECEDENCE`); an unstarted step counts as QUEUED, because a
         step with no task is work this workflow has not begun.
    """
    if not readings:
        return WorkflowRollup(
            state=UNKNOWN,
            complete=False,
            reason="no_steps",
            counts={},
            steps_read=0,
        )

    counts: dict[str, int] = {}
    unreadable: list[str] = []
    unstarted: list[str] = []
    read = 0
    for r in readings:
        if r.unstarted:
            unstarted.append(r.step_id)
            counts["unstarted"] = counts.get("unstarted", 0) + 1
            continue
        if r.state is None:
            unreadable.append(r.step_id)
            counts["unreadable"] = counts.get("unreadable", 0) + 1
            continue
        read += 1
        counts[r.state.value] = counts.get(r.state.value, 0) + 1

    if unreadable:
        # Which KIND of unreadable is reported, because a missing document and an
        # unread one need different responses from whoever is looking.
        absent_steps = [r.step_id for r in readings if r.absent]
        reason = (
            "step_tasks_missing" if len(absent_steps) == len(unreadable)
            else "step_tasks_unread" if not absent_steps
            else "step_tasks_missing_and_unread"
        )
        return WorkflowRollup(
            state=UNKNOWN,
            complete=False,
            reason=reason,
            counts=counts,
            unreadable_steps=unreadable,
            unstarted_steps=unstarted,
            steps_read=read,
        )

    present = [r.state for r in readings if r.state is not None]

    live = [s for s in present if s in CONCURRENCY_STATES]
    if live:
        # RUNNING covers all four capacity-holding states rather than reporting
        # the most advanced one. LEASED versus STARTING is a scheduling detail; a
        # workflow-level reader is asking "is this moving", and the per-state
        # breakdown is in `counts` for anyone who needs more.
        return WorkflowRollup(
            state=TaskState.RUNNING.value,
            complete=True,
            reason="steps_hold_capacity",
            counts=counts,
            unstarted_steps=unstarted,
            steps_read=read,
        )

    if not unstarted and present and all(s in TERMINAL_STATES for s in present):
        worst = next(s for s in _TERMINAL_SEVERITY if s in present)
        reason = (
            "all_steps_succeeded" if worst is TaskState.SUCCEEDED
            else f"worst_terminal_step_{worst.value.lower()}"
        )
        return WorkflowRollup(
            state=worst.value,
            complete=True,
            reason=reason,
            counts=counts,
            steps_read=read,
        )

    pending = [s for s in present if s in PENDING_STATES]
    if pending:
        state = next(s for s in _PENDING_PRECEDENCE if s in pending)
        return WorkflowRollup(
            state=state.value,
            complete=True,
            reason=f"most_advanced_pending_step_{state.value.lower()}",
            counts=counts,
            unstarted_steps=unstarted,
            steps_read=read,
        )

    # Everything terminal except steps that were never started. The workflow has
    # work left that it has not begun, so QUEUED is the honest floor.
    return WorkflowRollup(
        state=TaskState.QUEUED.value,
        complete=True,
        reason="steps_not_started",
        counts=counts,
        unstarted_steps=unstarted,
        steps_read=read,
    )


def derive_for(
    workflow: Workflow,
    states: Mapping[str, TaskState],
    *,
    absent: Iterable[str] = (),
) -> WorkflowRollup:
    """`derive` over a workflow's steps. The one entry point the routes use."""
    return derive(read_steps(workflow.steps, states, absent=absent))


# --------------------------------------------------------------------------
# Which record wins, and reporting when they disagree
# --------------------------------------------------------------------------

def effective_state(rollup: WorkflowRollup) -> str:
    """The state a READER is served. The derived one wins. Always.

    WHY THE DERIVED VALUE WINS, stated here because the owner asked for the
    justification to sit beside the code. The step tasks are the records the
    workers, the scheduler and the reconciler actually write; the stored
    `Workflow.state` is a CACHE of a computation over them. A cache cannot be
    fresher than its source, and the failure mode of preferring it is precisely
    the bug this file was written for -- a workflow reading QUEUED while its
    steps had finished.

    The one case where the derived value does not win is the one where there is
    no derived value: when a step could not be read, `rollup.state` is UNKNOWN
    and UNKNOWN is what the reader gets. Falling back to the stored value there
    would be answering from the cache precisely when the source is unavailable,
    which is when the cache is least trustworthy. The stored value is still
    served, under `stored_state`, for anyone auditing the cache itself.
    """
    return rollup.state


def drift_of(rollup: WorkflowRollup, stored: TaskState) -> dict[str, Any]:
    """Compare the two records of one fact. Never resolves one; only reports.

    `agrees` is three-valued on purpose, and the third value is the point.
    Modelled on the accounting-drift panel in apps/swarm-ui/src/Holders.tsx and
    on its central caution: a delta computed over a truncated page is not
    evidence. When the rollup is incomplete, the two records have not been
    compared -- they have merely failed to be compared -- and saying "they
    disagree" would manufacture a finding out of a failed read. `agrees` is then
    null, and `reason` says which read did not complete.
    """
    agrees: bool | None
    if not rollup.complete:
        agrees = None
    else:
        agrees = rollup.state == stored.value
    return {
        "stored": stored.value,
        "derived": rollup.state,
        "agrees": agrees,
        "reason": rollup.reason,
        "steps_read": rollup.steps_read,
        "unreadable_steps": list(rollup.unreadable_steps),
        # Set by the write-back below. A disagreement stays reported as a
        # disagreement after it has been repaired -- `agrees` is the verdict at
        # the moment of the read, not after the fix -- so a caller can see that
        # the cache WAS wrong. Flipping it to true on repair would be the silent
        # resolution this check exists to prevent.
        "repaired": False,
    }


# --------------------------------------------------------------------------
# Reading, deriving and writing back
# --------------------------------------------------------------------------

@dataclass
class RollupResult:
    """One workflow's rollup, its drift verdict, and whether it was written."""

    workflow: Workflow
    rollup: WorkflowRollup
    drift: dict[str, Any]
    written: bool = False

    def to_api(self) -> dict[str, Any]:
        """The fields `codec.workflow_to_api` merges into a workflow's JSON.

        No `stored_state` here: the codec serves that from the workflow document
        itself, so this object cannot become a second place the stored value is
        spelled.
        """
        return {
            "state": effective_state(self.rollup),
            "rollup": self.rollup.to_api(),
            "drift": self.drift,
        }


@dataclass
class SweepReport:
    """What a rollup sweep looked at. Truncation is reported, never hidden."""

    examined: int = 0
    written: int = 0
    agreed: int = 0
    disagreed: int = 0
    unknown: int = 0
    truncated: bool = False
    step_reads: int = 0
    step_read_budget_exhausted: bool = False

    def to_api(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "written": self.written,
            "agreed": self.agreed,
            "disagreed": self.disagreed,
            "unknown": self.unknown,
            # A sweep that stopped at its page limit has NOT seen every
            # workflow, and a caller that treats the counts above as a complete
            # census would be wrong in exactly the way Holders.tsx warns about.
            "truncated": self.truncated,
            "step_reads": self.step_reads,
            "step_read_budget_exhausted": self.step_read_budget_exhausted,
        }


class WorkflowRollups:
    """Reads the step tasks, derives, reports drift, and persists the cache.

    Constructed once in the composition root so a test builds it around the same
    in-memory Firestore the rest of the API uses -- no credentials, no emulator.
    """

    def __init__(self, *, store: Any, metrics: Any = None) -> None:
        self._store = store
        self._metrics = metrics

    # -- one workflow ----------------------------------------------------

    def for_workflow(
        self,
        workflow: Workflow,
        states: Mapping[str, TaskState],
        *,
        absent: Iterable[str] = (),
        persist: bool = True,
    ) -> RollupResult:
        rollup = derive_for(workflow, states, absent=absent)
        result = RollupResult(
            workflow=workflow, rollup=rollup, drift=drift_of(rollup, workflow.state)
        )
        self._record(result)
        if persist:
            result.written = self._persist(result)
        return result

    def for_workflow_from_tasks(
        self,
        workflow: Workflow,
        tasks: Iterable[Any],
        *,
        complete: bool = True,
        persist: bool = True,
    ) -> RollupResult:
        """Rollup from tasks the caller has already loaded.

        `complete=False` means the caller's task read was itself truncated, so a
        step missing from `tasks` was not necessarily absent -- it may simply not
        have been on the page. Passing it through as "not absent" is what makes
        the reason come out as `step_tasks_unread` rather than accusing the data
        of a fault the read cannot establish.
        """
        states = {t.id: t.state for t in tasks}
        absent = (
            [s.task_id for s in workflow.steps if s.task_id and s.task_id not in states]
            if complete
            else []
        )
        return self.for_workflow(workflow, states, absent=absent, persist=persist)

    # -- many workflows --------------------------------------------------

    def for_workflows(
        self,
        tenant_id: str,
        workflows: Sequence[Workflow],
        *,
        persist: bool = True,
    ) -> tuple[list[RollupResult], SweepReport]:
        read = self._store.workflow_step_states(tenant_id, workflows)
        report = SweepReport(
            step_reads=read.reads,
            step_read_budget_exhausted=bool(read.unread),
        )
        results: list[RollupResult] = []
        for workflow in workflows:
            result = self.for_workflow(
                workflow, read.states, absent=read.absent, persist=persist
            )
            results.append(result)
            report.examined += 1
            if result.written:
                report.written += 1
            verdict = result.drift["agrees"]
            if verdict is None:
                report.unknown += 1
            elif verdict:
                report.agreed += 1
            else:
                report.disagreed += 1
        return results, report

    def sweep(
        self, tenant_id: str, *, limit: int
    ) -> tuple[list[RollupResult], SweepReport]:
        """Refresh the written cache for one tenant's non-terminal workflows.

        This is the leg that makes the cache converge for a workflow nobody has
        looked at -- the read paths converge the ones somebody has. It reuses
        `list_workflows`, so it needs no index beyond `workflows-tenant-created`,
        which matters because four of the five indexes this module already
        depends on are still missing from terraform (see the header of store.py).

        Already-terminal workflows are skipped: their steps are all terminal,
        nothing will move them again, and re-reading their tasks every sweep
        would make the sweep's cost grow with the tenant's entire history rather
        than with its live work.
        """
        page = self._store.list_workflows(tenant_id, limit=limit)
        live = [w for w in page.items if w.state not in TERMINAL_STATES]
        results, report = self.for_workflows(tenant_id, live, persist=True)
        report.truncated = page.next_page_token is not None
        return results, report

    # -- the write --------------------------------------------------------

    def _persist(self, result: RollupResult) -> bool:
        """Write the derived state back, when and only when it is safe to.

        Three refusals, each of which has a failure it prevents:

          * an INCOMPLETE rollup is never written. Overwriting a real state with
            the product of a failed read is the bug this whole file guards
            against, pointed at the database instead of at the screen.
          * an UNKNOWN state is never written. `codec.workflow_from_dict` calls
            `TaskState(data["state"])`, so a document carrying it would raise on
            every subsequent read -- one bad write would make the workflow
            permanently unreadable.
          * a value that already AGREES is never written, so the steady state
            costs zero Firestore writes and `updated_at` keeps meaning "the
            document changed" rather than "something looked at it".

        No transaction. Two concurrent readers derive from the same step
        documents and therefore write the same value; a reader that raced ahead
        writes a newer one, and the next read of the loser's value re-derives and
        converges. A compare-and-set here would buy nothing and would make every
        read path able to fail on contention.
        """
        if not result.rollup.complete or result.rollup.state == UNKNOWN:
            return False
        if result.drift["agrees"] is not False:
            return False
        state = TaskState(result.rollup.state)
        self._store.set_workflow_state(result.workflow.workflow_id, state)
        # `workflow.state` and `drift["stored"]` are deliberately LEFT at the
        # value that was read. They are the evidence that the cache was wrong,
        # and a response that showed the repaired value in both places would be
        # indistinguishable from one where nothing had ever drifted.
        result.drift["repaired"] = True
        return True

    def _record(self, result: RollupResult) -> None:
        """Report the disagreement BEFORE anything repairs it.

        The write below is a repair, and a repair that leaves no trace is a
        silent resolution -- which is the one thing the owner said this must not
        do. The counter and the log line are taken here, against the drift as it
        was observed, so a systemic disagreement is visible in a dashboard even
        though every individual instance is fixed a moment later.
        """
        verdict = result.drift["agrees"]
        if verdict is True:
            return
        direction = "unknown" if verdict is None else "disagree"
        if self._metrics is not None:
            try:
                self._metrics.workflow_state_drift.labels(direction=direction).inc()
            except Exception:  # pragma: no cover - a metric must never fail a read
                log.debug("could not record workflow state drift", exc_info=True)
        if verdict is None:
            log.info(
                "workflow %s state could not be checked: stored=%s reason=%s "
                "unreadable_steps=%s",
                result.workflow.workflow_id,
                result.workflow.state.value,
                result.rollup.reason,
                result.rollup.unreadable_steps,
            )
        else:
            log.warning(
                "workflow %s state drifted: stored=%s derived=%s reason=%s",
                result.workflow.workflow_id,
                result.workflow.state.value,
                result.rollup.state,
                result.rollup.reason,
            )
