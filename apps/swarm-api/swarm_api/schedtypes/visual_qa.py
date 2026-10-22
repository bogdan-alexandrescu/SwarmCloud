"""`visual-qa`: a registered QA target's run, on a schedule (docs/schedules.md §3.17;
lane VQ-7 TYPE of docs/design/agent-autonomy.md §6, designed in
docs/design/visual-qa-any-app.md §6; part of #1933).

A FIRING IS THE TARGET'S `:run`, made by the schedule's owner. It goes through
the same three calls `POST /v1/qa-targets/{id}:run` makes, in its order:
`QaTargets.runnable` (every refusal before anything spends), `render_run` (the
workflow, its browser steps stamped `metadata.qa_target`, written here and
never taken from the schedule), and `QaTargets.record_run` (the run record the
worker's route reads before it serves a target to a task). The one difference
is the submission: `firing.submit_workflow`, so the workflow is the owner's,
carries the firing's `metadata.schedule` mark, and spends one place of the
firing's room. It is made inside `validation.qa_target_mark_allowed(mark)`,
as `:run`'s is: `metadata.qa_target` is a reserved key (VQ2-RESERVE), and
without the exemption every browser step is refused 422. A firing files issues (`file: "issues"`, `:run`'s default);
nothing in the schedule can make it push or merge.

A REFUSAL, NOT A FAILURE. `runnable`'s refusals are `ApiError`s below 500
(`qa_target_host_pending` 409, `issue_writes_off` 403, a credential not set,
the repository no longer registered), and this module lets them propagate:
the tick ends the firing `refused` with that code (`schedulefire.Ticker._finish`)
and submits nothing. A pending host is the platform admin's to approve, not a
defect of the run.

THE TENANT IS THE SCHEDULE'S. The target is read with `QaTargets.get(tenant,
id)`, so another tenant's target answers exactly as a missing one (404) at
every firing. `check_target`, the create and edit hook, refuses it earlier,
with one code and one sentence for "another tenant's" and "none at all", so
an id is never an oracle for another tenant (the rule `resolve_scope` keeps
for repositories). It also holds §6's creator: a workspace admin of the
tenant (a target sends the tenant's agents to an outside site and files in
its name, §1), which the catalogue's `creators` cannot express because its
roles are platform roles.

WHAT IS NOT CLOSED HERE. A crash between the submission and `record_run`
leaves a workflow the retried firing adopts by its mark (§2.2) with no run
record, so its browser steps are served no target and capture nothing behind
a sign-in. `:run` has the same window between the same two calls.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter

from .. import scheduletypes
from ..errors import ApiError
from ..qatargets import QaTargets, render_run
from ..schedules import ScheduleInvalid
from ..schemas import WorkflowCreate
from ..store import TASKS
from ..validation import qa_target_mark_allowed

log = logging.getLogger(__name__)

TYPE = "visual-qa"

#: `:run`'s `file`: a scheduled run files its findings (§5). The target's own
#: `file` settings bound how many and at what severity.
FILE = "issues"

#: Measured, then set by the owner: the first full-console QA run (run 5b,
#: wf_025ae735511546d99eff: 17 steps, 362 screenshots) cost $23.62, 94% of the
#: provisional $25 per run. BGD set $40 a run and $80 a day on 2026-10-22
#: (VQ7-CAPS, #1933): headroom over that run, and the daily firing plus one
#: run-now. One at a time: a firing makes one workflow, and a second live one
#: would QA the same application twice. A schedule may set lower figures;
#: raising them is a change to this line, in a pull request.
BUDGET_CAPS = scheduletypes.BudgetCaps(per_run_usd=40, per_day_usd=80, max_concurrent=1)

#: `check_target`'s refusal: the same for another tenant's target and for none.
TARGET_NOT_FOUND = "qa_target_not_found"
TARGET_NOT_FOUND_MESSAGE = "the target is not a QA target registered in this workspace"


def params_of(schedule: Mapping[str, Any]) -> Any:
    entry = scheduletypes.get(TYPE)
    return entry.params_model.model_validate(dict(schedule.get("params") or {}))


def _targets(ctx: Any) -> QaTargets:
    return QaTargets(ctx.db, now=ctx.now)


def check_target(ctx: Any, schedule: Mapping[str, Any], auth: Any) -> None:
    """At create and edit (routes/schedules.py): `auth` administers the
    schedule's tenant, and the target is that tenant's. A pending host is
    not refused here: it is the firing's refusal, so a schedule can be made
    while a platform admin has still to approve the host."""
    # Imported here: a route module, and schedtypes import nothing at load.
    from ..routes.qatargets import require_workspace_admin

    tenant_id = str(schedule["tenant_id"])
    require_workspace_admin(ctx, auth, tenant_id)
    if _targets(ctx).find(tenant_id, params_of(schedule).target) is None:
        raise ScheduleInvalid(TARGET_NOT_FOUND, TARGET_NOT_FOUND_MESSAGE)


def browser_task_ids(db: Any, tenant_id: str, workflow_id: str, browser: Sequence[str]) -> list[str]:
    """The workflow's browser step tasks, in THIS tenant: what the run record names."""
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("workflow_id", "==", workflow_id))
    )
    wanted = set(browser)
    rows = [snap.to_dict() or {} for snap in query.stream()]
    # The filter again, in the application: a fake or an index mistake must
    # not move the tenant boundary.
    return sorted(str(r["id"]) for r in rows if r.get("tenant_id") == tenant_id
                  and r.get("workflow_id") == workflow_id and r.get("step_id") in wanted)


def create(firing: Any) -> list[dict[str, Any]]:
    schedule = firing.schedule
    tenant_id = str(schedule["tenant_id"])
    target_id = params_of(schedule).target
    if firing.room <= 0:
        firing.explain({"reason": "no_room", "target": target_id})
        log.info("schedule %s firing %s: no room for a QA run", schedule["schedule_id"],
                 firing.firing["firing_id"])
        return []
    targets = _targets(firing.ctx)
    # Every refusal of `:run` propagates: the firing ends `refused` with its code.
    target = targets.runnable(tenant_id, target_id, file=FILE)
    workflow, mark, browser = render_run(target, screens=None, file=FILE)
    # The mark is reserved (`validation.QA_TARGET_METADATA_KEY`, VQ2-RESERVE):
    # only this exact value, only for this submission, gets past the refusal,
    # as in `run_target`. `firing.submit_workflow` opens the schedule's own
    # exemption inside it; the two are separate and both are needed.
    with qa_target_mark_allowed(mark):
        item = firing.submit_workflow(WorkflowCreate.model_validate(workflow),
                                      repo_id=str(target.get("repo_id") or "") or None)
    task_ids = browser_task_ids(firing.ctx.db, tenant_id, item["id"], browser)
    targets.record_run(tenant_id=tenant_id, target=target, workflow_id=item["id"],
                       task_ids=task_ids, file=FILE, by=(firing.owner.email or "").strip().lower())
    firing.explain({"target": target_id, "revision": mark.get("revision"), "file": FILE})
    log.info("schedule %s firing %s: QA run %s of target %s", schedule["schedule_id"],
             firing.firing["firing_id"], item["id"], target_id)
    return [item]


def dry_run(firing: Any) -> dict[str, Any]:
    """What `create` would do, as data. Reads only: nothing is submitted or recorded."""
    schedule = firing.schedule
    tenant_id = str(schedule["tenant_id"])
    target_id = params_of(schedule).target
    answer: dict[str, Any] = {"target": target_id, "file": FILE, "room": firing.room,
                              "browser_steps": []}
    try:
        target = _targets(firing.ctx).runnable(tenant_id, target_id, file=FILE)
        _workflow, mark, browser = render_run(target, screens=None, file=FILE)
    except ApiError as exc:
        if exc.status_code >= 500:
            raise
        return {**answer, "would": exc.code}
    would = "run" if firing.room > 0 else "room"
    return {**answer, "would": would, "revision": mark.get("revision"), "browser_steps": browser}
