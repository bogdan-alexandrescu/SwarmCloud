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

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from swarm_common.admission import _snapshot
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

#: The step id of a merge fix round (lane SPEED-D2): `ci-fix:<n>`,
#: `conflict-fix:<n>` or `pick-fix:<n>`, appended to the merge's workflow by
#: swarm-api's wake tick (`swarm_api.mergewake.round_step_id`) when it hands
#: a red or conflicting pull request, or one whose owner picked an option the
#: branch did not build, to a round. `pick` since ROLLUP-PICK-FIX (BGD,
#: 2026-10-26: a pick-fix round is a step of the lane, as the other rounds
#: are); every kind in `mergewake.ROUND_KIND` must match, or the tick's
#: append is refused -- tests/unit/control_plane/test_merge_auto_fix_rounds.py
#: holds the two equal. A caller can never submit one: a
#: submitted step id holds no ':' (`WorkflowStepCreate.step_id`'s pattern),
#: so the id alone marks a step only the platform appended. The id, not the
#: step's input, because the reconciler decodes steps without their input
#: (`reconciler.model.WorkflowView`) and must derive what this derives.
#:
#: A FIX LOOP's rounds share the pattern (FL-ROLLUP, part of #2509;
#: docs/design/fix-loop.md §4.2 and §6.1): `review-fix:<k>` and
#: `re-review:<k>`, k >= 1, appended by the `fix-loop` step's driver. They are
#: judged by the loop step, whose own ending (SUCCEEDED, or FAILED
#: `fix_loop_exhausted`) is the verdict on them, exactly as the merge step's
#: is on its rounds -- so `derive` needs no second rule. A bare `re-review`,
#: the step `on_merge_verdict` appends (`validation.REREVIEW_STEP_ID`), is NOT
#: a round: it is the lane's own review and fails the workflow like any step,
#: and the required `:<n>` is what keeps it out.
FIX_ROUND_STEP_RE = re.compile(
    r"^(?:(?:ci|conflict|pick|review)-fix|re-review):[1-9][0-9]{0,2}$")


def is_fix_round_step(step_id: str) -> bool:
    """Whether `step_id` names a round the platform appended: a merge fix
    round or a fix loop's review-fix / re-review round."""
    return bool(FIX_ROUND_STEP_RE.fullmatch(step_id or ""))


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
    #: The merge and fix-loop rounds among the steps (`is_fix_round_step`). Counted in
    #: `counts` as what their tasks are; see `derive` for how they rank.
    fix_round_steps: list[str] = field(default_factory=list)

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
            "fix_round_steps": list(self.fix_round_steps),
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

    A MERGE FIX ROUND (`is_fix_round_step`, SPEED-D2) is counted like any
    step at 2, 3 and 5 -- unreadable is UNKNOWN, holding capacity is
    RUNNING, waiting is pending -- but it does not take part in step 4's
    severity while the workflow has steps of its own: a round is the merge
    step's attempt to make the pull request mergeable, and the merge step's
    own ending is the verdict on it. A round that failed before a second one
    fixed CI, under a merge that then SUCCEEDED, is a SUCCEEDED workflow; a
    merge that refused after its rounds is FAILED by its own state. The
    round still shows in `counts` as what its task is, and is named in
    `fix_round_steps`.

    A FIX LOOP's rounds (`review-fix:<k>`, `re-review:<k>`) take the same
    rule, judged by the loop step's own ending: a `review-fix:2` that FAILED
    under a loop that ended SUCCEEDED is a SUCCEEDED workflow; a loop that
    ended FAILED fails it.
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
    rounds = [r.step_id for r in readings if is_fix_round_step(r.step_id)]
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
            fix_round_steps=rounds,
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
            fix_round_steps=rounds,
        )

    if not unstarted and present and all(s in TERMINAL_STATES for s in present):
        own = [r.state for r in readings if r.state is not None
               and not is_fix_round_step(r.step_id)]
        judged = own or present
        worst = next(s for s in _TERMINAL_SEVERITY if s in judged)
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
            fix_round_steps=rounds,
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
            fix_round_steps=rounds,
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
        fix_round_steps=rounds,
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


#: Firestore collections `derived_state_write` reads. The same names as
#: `swarm_api.store`, `scheduler.store` and the worker's control plane; spelled
#: here because none of those packages can be imported from this one.
TASKS = "tasks"
WORKFLOWS = "workflows"


# --------------------------------------------------------------------------
# Writing the derived state where a step's state is written (#2838)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _StepRow:
    """The two fields of a workflow step `read_steps` reads, from the raw
    document: the transaction holds the dict, not a decoded `Workflow`."""

    step_id: str
    task_id: str | None


def derived_state_write(
    txn: Any,
    db: Any,
    *,
    tenant_id: str,
    workflow_id: Any,
    now: datetime,
    changed: Mapping[str, Mapping[str, Any]] | None = None,
    steps: Sequence[Mapping[str, Any]] | None = None,
    call_options: Mapping[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]] | None:
    """The workflow-document write that keeps `state` equal to `derive()`.

    WHY HERE (#2838, follow-up). It lives beside `derive()`, not in
    swarm-api, because the scheduler (READY->LEASED, a woken park, the
    `fail_workflow` cancels) and the worker's direct-Firestore path make step
    transitions too, and neither image can import swarm-api. It uses only the
    Firestore transaction API, so every writer of a step's state calls this
    one function.

    WHY (#2838). The stored state was written only by the read paths, the
    API's sweep and the reconciler's `state_drift` repair, never where a step
    changed state. So every park, wake, finish and round step left the stored
    value behind the derived one until something next looked, and the
    reconciler reported and repaired the same workflows pass after pass.

    Called INSIDE the transaction that writes the step's change, and only
    READS: it returns `(ref, patch)` for the caller to `txn.update(...)` with
    its other writes, or None when nothing should be written. Firestore wants
    every read of a transaction before its first write, so a caller calls this
    before it writes anything. One transaction means the workflow's state and
    the step's state commit together, and a sibling step that moves while it
    runs makes the commit retry and re-derive, never write a stale value.

    `changed` is each step task as THIS transaction will leave it (task_id ->
    the merged document): it is not committed yet, so reading it back would
    derive from the state it is leaving. `steps` replaces the stored step
    list when the transaction is appending a step. The derivation is
    `swarm_rollup.derive`, the rule the reconciler checks against, so the two
    cannot disagree about what to write.

    Refuses what swarm-api's `WorkflowRollups._persist` refuses, for the same reasons: an
    incomplete read or an UNKNOWN state is never written, and a value that
    already agrees costs no write. A workflow of another tenant is not read
    further: a task's `workflow_id` is tenant-writable.

    `call_options` (`retry`, `timeout`) go to every `txn.get`, so a caller
    whose own reads run under a budget -- the worker inside its startup
    window -- keeps these reads under it too.
    """
    if not isinstance(workflow_id, str) or not workflow_id:
        return None
    changed = changed or {}
    options = dict(call_options or {})
    ref = db.collection(WORKFLOWS).document(workflow_id)
    snap = _snapshot(txn.get(ref, **options))
    data = (snap.to_dict() or {}) if snap.exists else None
    if data is None or data.get("tenant_id") != tenant_id:
        return None
    rows = steps if steps is not None else (data.get("steps") or [])
    views = [
        _StepRow(step_id=str(row.get("step_id") or ""), task_id=row.get("task_id") or None)
        for row in rows
        if isinstance(row, Mapping)
    ]
    states: dict[str, TaskState] = {}
    absent: set[str] = set()
    skipped: list[str] = []
    for task_id in dict.fromkeys(v.task_id for v in views if v.task_id):
        doc = changed.get(task_id)
        if doc is None:
            task_snap = _snapshot(txn.get(db.collection(TASKS).document(task_id), **options))
            doc = (task_snap.to_dict() or {}) if task_snap.exists else None
        if doc is None or doc.get("tenant_id", tenant_id) != tenant_id:
            absent.add(task_id)
            continue
        try:
            states[task_id] = TaskState(doc.get("state"))
        except ValueError:
            # Unreadable, not absent: the rollup comes out incomplete and
            # nothing is written.
            continue
        summary = doc.get("result_summary")
        if isinstance(summary, dict) and isinstance(summary.get(SKIPPED_SUMMARY_KEY), dict):
            skipped.append(task_id)
    rollup = derive(read_steps(views, states, absent=absent, skipped=skipped))
    if not rollup.complete or rollup.state == UNKNOWN:
        return None
    if rollup.state == data.get("state"):
        return None
    return ref, {"state": TaskState(rollup.state).value, "updated_at": now}
