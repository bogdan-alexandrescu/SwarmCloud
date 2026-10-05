"""Workflow state, derived from the steps: the one rule, shared.

`derive()` is the single implementation of "what state is this workflow in",
and every reader of a workflow's state goes through it: swarm-api's read routes
and rollup sweep (`swarm_api.rollup`, which re-exports everything here and keeps
the store-bound half -- the write-back and the drift metric), and the
reconciler's workflow stall check (`reconciler.detect.detect_stalled_workflows`,
#616), which compares the stored state against this one and repairs the cache.

WHY A PACKAGE OF ITS OWN. The reconciler image installs `apps/common` and
`apps/reconciler`; the API image installs `apps/common`, `apps/redaction` and
`apps/swarm-api`. Neither can import the other, and `apps/common/swarm_common/`
is frozen. Before this package the derivation lived in `swarm_api.rollup`
alone, so a second consumer would have needed a second copy of the precedence
tables below -- and a stall check comparing the stored state against a copy of
the rule would report the two copies drifting rather than the data. Contract
request 7 (docs/contract-change-requests.md) still asks for this to live in the
frozen contract; nothing here depends on that being decided.

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

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import (
    CONCURRENCY_STATES,
    PENDING_STATES,
    TERMINAL_STATES,
    TaskState,
)

#: `result_summary.skipped` on a step the worker skipped because a step it
#: needed changed nothing (owner decision, 2026-10-05): `{"reason": "nothing
#: to change", "upstream": [task ids]}`. Spelled here and in
#: `agent_worker.expected_outputs.SKIPPED_SUMMARY_KEY`; the API image cannot
#: import the worker's.
SKIPPED_SUMMARY_KEY = "skipped"

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
            "swarm_rollup cannot rank terminal states "
            f"{sorted(s.value for s in missing_terminal)}; add them to "
            "_TERMINAL_SEVERITY in severity order before shipping"
        )
    missing_pending = PENDING_STATES - set(_PENDING_PRECEDENCE)
    if missing_pending:
        raise RuntimeError(
            "swarm_rollup cannot rank pending states "
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
    #: The worker skipped this step: something it needed changed nothing
    #: (`SKIPPED_SUMMARY_KEY`). Only ever true on a SUCCEEDED step, which is
    #: what lets SKIPPED rank as a success with no TaskState of its own.
    skipped: bool = False

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
    #: The steps that ended SKIPPED ("nothing to change"). Each is also
    #: counted under SUCCEEDED in `counts`, because that is what it is in the
    #: frozen state machine and what a "done" count reads.
    skipped_steps: list[str] = field(default_factory=list)

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
            "skipped_steps": list(self.skipped_steps),
        }


def read_steps(
    steps: Sequence[WorkflowStep],
    states: Mapping[str, TaskState],
    *,
    absent: Iterable[str] = (),
    skipped: Iterable[str] = (),
) -> list[StepReading]:
    """Join each step to its task state.

    `states` maps task_id -> state for every step task that WAS read. `absent`
    names the task_ids that were read and found missing, which is how a data
    fault is told apart from a read that never happened. `skipped` names the
    task_ids whose `result_summary` says the worker skipped them
    (`skipped_task_ids`); one that did not SUCCEED is not a skip.
    """
    absent_ids = set(absent)
    skipped_ids = set(skipped)
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
                skipped=state is TaskState.SUCCEEDED and step.task_id in skipped_ids,
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

    A SKIPPED step ("nothing to change", 2026-10-05) is a SUCCEEDED task, so
    it ranks as a success at step 4 and needs no state of its own; it is
    named in `skipped_steps` so a reader can tell it from one that ran.
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
    skipped = [r.step_id for r in readings if r.skipped]
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
            skipped_steps=skipped,
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
            skipped_steps=skipped,
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
            skipped_steps=skipped,
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
            skipped_steps=skipped,
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
        skipped_steps=skipped,
    )


def derive_for(
    workflow: Workflow,
    states: Mapping[str, TaskState],
    *,
    absent: Iterable[str] = (),
    skipped: Iterable[str] = (),
) -> WorkflowRollup:
    """`derive` over a workflow's steps. The one entry point the routes use."""
    return derive(read_steps(workflow.steps, states, absent=absent, skipped=skipped))


def skipped_task_ids(tasks: Iterable[Any]) -> list[str]:
    """The ids of the tasks whose `result_summary` records a skip.

    Read from the task documents the caller already loaded, so a skip costs
    no read of its own. Whether the task SUCCEEDED is `read_steps`' check.
    """
    out: list[str] = []
    for task in tasks:
        summary = getattr(task, "result_summary", None)
        if isinstance(summary, dict) and isinstance(summary.get(SKIPPED_SUMMARY_KEY), dict):
            out.append(task.id)
    return out


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

