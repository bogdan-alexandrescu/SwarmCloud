"""Re-run a failed workflow step inside its workflow.

`POST /v1/workflows/{workflow_id}/steps/{step_id}/rerun` (the owner's pick on
PR #3123, "Offer it, new route": the Needs-you row offers Retry on a failed
step, and Retry re-runs the step in its workflow rather than submitting its
prompt alone as a lone agent, which is not the step it was).

A FAILED TASK STAYS FAILED. FAILED and DEAD_LETTERED are terminal in the
frozen state machine (`swarm_common.states`), and an attempt's fencing
generation, its lease history and its events belong to the task that ran. So
a re-run never touches the old task: it writes a NEW task per step it
re-runs, copied from the old one, and points `WorkflowStep.task_id` at it.
Everything that reads a workflow's steps reads them through that pointer
(`rollup`, cancel, the step totals), so the old task drops out of the
workflow's state the moment the pointer moves; it is still readable by id.

WHICH STEPS ARE RE-RUN (`plan`):

* the step asked for, which must have ended FAILED or DEAD_LETTERED;
* every step that ended CANCELLED because of a failure (`FAILED_PARENT`, or
  `WORKFLOW_SWEEP` under `on_step_failure: fail_workflow`): the dependants the
  scheduler cancelled behind the failed step, and the steps the sweep stopped
  before they started. Left CANCELLED, the workflow could never succeed;
* every step downstream of any of those, so no re-run step waits on a task
  the workflow no longer points at.

A step that SUCCEEDED is never re-run: a re-run step that reads it reads the
same task id, and its artifacts are where they were.

ONLY A FINISHED WORKFLOW. Every step's task must be terminal, and the
workflow not cancelled. While anything still runs, the scheduler is still
deciding what the failure cancels (`_sweep_failed_workflow`, the dependency
sweep), and a re-run racing it could be cancelled by the very sweep that
followed the failure. The store re-checks all of it inside the transaction
that writes (`Store.rerun_workflow_steps`), so two people pressing Retry at
once re-run the step once.

THE COPY. The old task's document with its ids remapped (`clone`): every
string that names a re-run step's old task id -- `depends_on`, the
`metadata.input_from` keys, the dispatch block's routing (`builds_on`, the
gate, the integrator's lanes, the branch named after a task) -- names the new
one. Everything an attempt wrote is reset to a task nobody has run: READY,
or PARKED on its parents as submission stores it, no attempts, no lease, generation 0, no end. The spec is signed again
(`specsigning.sign_task_specs`, contract request 34) because the id is inside
the signed bytes. `metadata.rerun_of` records the task it replaces.

INVARIANTS. The new tasks are READY or PARKED and cost nothing until admission leases
them (invariants 1 and 3); admission reserves their capacity as it does any
step's (2). Each new task starts at generation 0 with its own attempts (5).
The runner profile, image and resources are the step's own, copied from what
swarm-api resolved at submission; the caller names a step, never a spec (10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from swarm_common.models import EndCause, ParkReason, Task, Workflow, new_id
from swarm_common.states import TERMINAL_STATES, TaskState, assert_transition

from .codec import task_from_dict
from .errors import Conflict, NotFound
from .validation import AFTER_PRS_METADATA_KEY

#: The states a step must have ended in to be offered a re-run.
RERUNNABLE_STATES: frozenset[TaskState] = frozenset(
    {TaskState.FAILED, TaskState.DEAD_LETTERED}
)

#: Why a CANCELLED step is re-run with the failed one: the platform cancelled
#: it because of a failure. A step a person cancelled (`CANCEL_REQUESTED`) or
#: whose parent a person cancelled (`CANCELLED_PARENT`) stays as it is.
CANCELLED_BY_FAILURE: frozenset[EndCause] = frozenset(
    {EndCause.FAILED_PARENT, EndCause.WORKFLOW_SWEEP}
)

#: `metadata.rerun_of`: the task a re-run task replaces.
RERUN_OF_METADATA_KEY = "rerun_of"

#: What an attempt writes onto its task, reset on the copy to a task nobody
#: has run. `spec_*` are written again by the signature.
_RESET: dict[str, Any] = {
    "attempt_count": 0,
    "next_eligible_at": None,
    "park_reason": None,
    "blocked_by": [],
    "current_lease_id": None,
    "current_generation": 0,
    "cancel_requested": False,
    "started_at": None,
    "completed_at": None,
    "last_error": None,
    "result_summary": None,
    "latest_checkpoint": None,
    "end_cause": None,
    "spec_signature": None,
    "spec_key_version": None,
    "spec_format": None,
    "human_wait": None,
    "platform_refusals": 0,
    "attempt_totals": None,
    "current_account": None,
}


#: Why a re-run was refused, in the refusal's `detail.reason`. The codes are
#: the established `not_found` and `conflict` (a new code ships behind a
#: switch, `swarm_api.refusals`); the reason is what tells them apart.
STEP_NOT_FOUND = "step_not_found"
WORKFLOW_CANCELLED = "workflow_cancelled"
WORKFLOW_STILL_RUNNING = "workflow_still_running"
STEP_NOT_FAILED = "step_not_failed"
STEP_RERUN_RACED = "step_rerun_raced"


def raced(workflow_id: str) -> Conflict:
    """The workflow changed between the read and the write: someone else
    re-ran or cancelled it first. Read it again."""
    return Conflict(
        "this workflow changed while the re-run was prepared; read it again",
        detail={"workflow_id": workflow_id, "reason": STEP_RERUN_RACED},
    )


@dataclass(frozen=True)
class RerunResult:
    workflow_id: str
    step_id: str
    #: step id -> {"from": old task id, "to": new task id}, in workflow order.
    replaced: dict[str, dict[str, str]]


def _downstream(workflow: Workflow, seeds: set[str]) -> set[str]:
    """`seeds` and every step that depends on one of them, transitively,
    through `depends_on` or `input_from` (both name step ids)."""
    out = set(seeds)
    changed = True
    while changed:
        changed = False
        for step in workflow.steps:
            if step.step_id in out:
                continue
            parents = set(step.depends_on) | set(step.input_from or {})
            if parents & out:
                out.add(step.step_id)
                changed = True
    return out


def plan(workflow: Workflow, tasks: Mapping[str, Task], step_id: str) -> list[str]:
    """The step ids a re-run of `step_id` writes new tasks for, in workflow
    order; raises the refusal when it may not run. `tasks` is every step's
    task by task id, as read."""
    by_step = {s.step_id: s for s in workflow.steps}
    step = by_step.get(step_id)
    if step is None:
        raise NotFound(
            f"workflow {workflow.workflow_id!r} has no step {step_id!r}",
            detail={"workflow_id": workflow.workflow_id, "step_id": step_id,
                    "reason": STEP_NOT_FOUND},
        )
    if workflow.cancel_requested:
        raise Conflict(
            "this workflow was cancelled; a cancelled workflow's steps are not re-run",
            detail={"workflow_id": workflow.workflow_id, "reason": WORKFLOW_CANCELLED},
        )
    running = [
        s.step_id for s in workflow.steps
        if not s.task_id or s.task_id not in tasks
        or tasks[s.task_id].state not in TERMINAL_STATES
    ]
    if running:
        raise Conflict(
            "a step can be re-run once every step of its workflow has finished",
            detail={"workflow_id": workflow.workflow_id, "steps": running,
                    "reason": WORKFLOW_STILL_RUNNING},
        )
    own = tasks[step.task_id or ""]
    if own.state not in RERUNNABLE_STATES:
        raise Conflict(
            f"step {step_id!r} ended {own.state.value}; only a failed step is re-run",
            detail={"workflow_id": workflow.workflow_id, "step_id": step_id,
                    "state": own.state.value, "reason": STEP_NOT_FAILED},
        )
    seeds = {step_id} | {
        s.step_id for s in workflow.steps
        if tasks[s.task_id or ""].state == TaskState.CANCELLED
        and tasks[s.task_id or ""].end_cause in CANCELLED_BY_FAILURE
    }
    chosen = _downstream(workflow, seeds)
    # Never a step that succeeded: what reads it reads the task that did.
    return [
        s.step_id for s in workflow.steps
        if s.step_id in chosen and tasks[s.task_id or ""].state != TaskState.SUCCEEDED
    ]


def _remap(value: Any, ids: Mapping[str, str]) -> Any:
    """`value` with every old task id in `ids`, in a string or a map key,
    replaced by its new one. Task ids are random and unique, so a substring
    is only ever that id (a branch `swarm/<task id>` included)."""
    if isinstance(value, str):
        for old, new in ids.items():
            if old in value:
                value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {_remap(k, ids): _remap(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, ids) for v in value]
    if isinstance(value, tuple):
        return tuple(_remap(v, ids) for v in value)
    return value


def clone(old: Task, ids: Mapping[str, str], now: datetime) -> Task:
    """A new task for `old`'s step, READY or PARKED as submission stores it: its spec with `ids` remapped and
    everything an attempt wrote reset. Unsigned; the caller signs it."""
    data = old.to_firestore()
    keep = {"created_at", "updated_at", "started_at", "completed_at", "next_eligible_at"}
    remapped = {k: (v if k in keep else _remap(v, ids)) for k, v in data.items()}
    remapped.update(_RESET)
    remapped["id"] = ids[old.id]
    # The state submission stores (`SubmissionService._build_task`): a step
    # with parents, or a root that waits for pull requests to merge, PARKED
    # on its dependencies for the scheduler's sweep to promote; any other
    # READY for admission. Neither holds anything (invariant 1).
    assert_transition(TaskState.SUBMITTED, TaskState.QUEUED)
    if remapped.get("depends_on") or AFTER_PRS_METADATA_KEY in (remapped.get("metadata") or {}):
        assert_transition(TaskState.QUEUED, TaskState.PARKED)
        remapped["state"] = TaskState.PARKED.value
        remapped["park_reason"] = ParkReason.DEPENDENCY_INCOMPLETE.value
    else:
        assert_transition(TaskState.QUEUED, TaskState.READY)
        remapped["state"] = TaskState.READY.value
    remapped["created_at"] = now
    remapped["updated_at"] = now
    metadata = dict(remapped.get("metadata") or {})
    metadata[RERUN_OF_METADATA_KEY] = old.id
    remapped["metadata"] = metadata
    return task_from_dict(remapped)


def build(
    workflow: Workflow, tasks: Mapping[str, Task], step_id: str, *, now: datetime
) -> tuple[list[str], dict[str, str], list[Task]]:
    """(the steps re-run, old task id -> new task id, the new tasks in
    workflow order). The steps' order is the submission's topological one,
    so a new task is never written before a new parent it names."""
    steps = plan(workflow, tasks, step_id)
    by_step = {s.step_id: s for s in workflow.steps}
    ids = {by_step[s].task_id or "": new_id("task") for s in steps}
    new_tasks = [clone(tasks[by_step[s].task_id or ""], ids, now) for s in steps]
    return steps, ids, new_tasks


def rerun(
    *,
    store: Any,
    sign: Callable[[Sequence[Task]], None],
    tenant_id: str,
    workflow_id: str,
    step_id: str,
    by: str,
    tenant_member: str = "",
    now: datetime,
) -> RerunResult:
    """Read, plan, copy, sign, and swap the pointers in one transaction."""
    workflow = store.get_workflow(tenant_id, workflow_id, submitted_by=None)
    task_ids = [s.task_id for s in workflow.steps if s.task_id]
    tasks = store.tasks_by_id(tenant_id, task_ids)
    steps, ids, new_tasks = build(workflow, tasks, step_id, now=now)
    sign(new_tasks)
    by_step = {s.step_id: s for s in workflow.steps}
    replaced = {s: {"from": by_step[s].task_id or "", "to": ids[by_step[s].task_id or ""]}
                for s in steps}
    store.rerun_workflow_steps(
        tenant_id, workflow_id,
        replaced={s: r["from"] for s, r in replaced.items()},
        tasks=new_tasks, by=by, tenant_member=tenant_member,
    )
    return RerunResult(workflow_id=workflow_id, step_id=step_id, replaced=replaced)
