"""`visual-qa`: a registered QA target's run, on a schedule (docs/schedules.md §3.17;
lane VQ-7 TYPE of docs/design/agent-autonomy.md §6, designed in
docs/design/visual-qa-any-app.md §6; part of #1933).

A FIRING IS THE TARGET'S `:run`, made by the schedule's owner. It goes through
the same calls `POST /v1/qa-targets/{id}:run` makes, in its order:
`QaTargets.runnable_legs` (every leg's refusal before anything spends, and each
leg's `render_run`: the workflow, its browser steps stamped
`metadata.qa_target`, written there and never taken from the schedule),
`QaTargets.open_group`, and per leg `QaTargets.record_run` (the run record the
worker's route reads before it serves a target to a task). The one difference
is the submission: `firing.submit_workflow`, so each workflow is the owner's,
carries the firing's `metadata.schedule` mark, and spends one place of the
firing's room. It is made inside `validation.qa_target_mark_allowed(mark)`,
as `:run`'s is: `metadata.qa_target` is a reserved key (VQ2-RESERVE), and
without the exemption every browser step is refused 422. A firing files issues (`file: "issues"`, `:run`'s default);
nothing in the schedule can make it push or merge.

A FIRING IS ONE WORK ITEM PER LEG (docs/design/qa-environments.md §2.2, §7;
lane QAENV-RUN, BGD's Q2 (a) and Q9 (a), 2026-10-23). `params.environments`
names the target's legs (every entry when unset), checked against the target
at create, at edit (`check_target`) and at every firing (`_names`). Every
named leg is checked with `runnable_legs` before any is submitted, exactly as
`:run` checks them; then the run group is written, and each leg is one
workflow, one work item, within the firing's room. A leg the room cannot
hold is not started, said in the group and in the firing's selection
(`not_started`), and the next firing runs it.

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
from ..qatargets import QaTargets
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
#: provisional $25 per run, and BGD set $40 a run, $80 a day and one at a time
#: on 2026-10-22 (VQ7-CAPS, #1933). A firing is now one work item PER LEG, so
#: BGD set these on 2026-10-23 (docs/design/qa-environments.md §10, Q9 (a)):
#: `per_run_usd` 40 bounds each LEG (a work item), as it bounded a run;
#: `max_concurrent` 2 lets a two-leg firing start both legs; `per_day_usd` 160
#: keeps "the daily firing plus one run-now" for both legs. A schedule may set
#: lower figures; raising them is a change to this line, in a pull request.
BUDGET_CAPS = scheduletypes.BudgetCaps(per_run_usd=40, per_day_usd=160, max_concurrent=2)

#: `check_target`'s refusal: the same for another tenant's target and for none.
TARGET_NOT_FOUND = "qa_target_not_found"
TARGET_NOT_FOUND_MESSAGE = "the target is not a QA target registered in this workspace"
#: A name in `environments` the target has no entry for, at create, edit or firing.
ENVIRONMENT_NOT_FOUND = "qa_target_environment_unknown"
ENVIRONMENT_NOT_FOUND_MESSAGE = "the schedule names an environment the QA target does not have"
#: The selection's reason for a leg the firing's room could not hold.
NO_ROOM = "no_room"


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
    params = params_of(schedule)
    target = _targets(ctx).find(tenant_id, params.target)
    if target is None:
        raise ScheduleInvalid(TARGET_NOT_FOUND, TARGET_NOT_FOUND_MESSAGE)
    _names(params, target)


def _names(params: Any, target: Mapping[str, Any]) -> list[str] | None:
    """`params.environments`, each an entry of `target` (the API's read, every
    entry present); None, every entry, when unset. Refused by the first name
    the target lacks, named in the detail and never in the sentence."""
    if params.environments is None:
        return None
    have = {str(e.get("name")) for e in target.get("environments") or []}
    for name in params.environments:
        if name not in have:
            raise ScheduleInvalid(ENVIRONMENT_NOT_FOUND, ENVIRONMENT_NOT_FOUND_MESSAGE, environment=name)
    return list(params.environments)


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
    params = params_of(schedule)
    target_id = params.target
    if firing.room <= 0:
        firing.explain({"reason": NO_ROOM, "target": target_id})
        log.info("schedule %s firing %s: no room for a QA run", schedule["schedule_id"],
                 firing.firing["firing_id"])
        return []
    targets = _targets(firing.ctx)
    # Another tenant's target is 404 here, as a missing one; then the names.
    names = _names(params, targets.get(tenant_id, target_id))
    # Every refusal of `:run` propagates, for any leg, before any is submitted:
    # the firing ends `refused` with its code.
    legs = targets.runnable_legs(tenant_id, target_id, file=FILE, environments=names)
    specs = [WorkflowCreate.model_validate(leg.workflow) for leg in legs]
    by = (firing.owner.email or "").strip().lower()
    group_id = targets.open_group(tenant_id=tenant_id, legs=legs, file=FILE, by=by,
                                  schedule_id=str(schedule["schedule_id"]))
    items: list[dict[str, Any]] = []
    not_started: list[dict[str, Any]] = []
    refusals: list[ApiError] = []
    for leg, spec in zip(legs, specs):
        if len(items) >= firing.room:
            targets.leg_not_started(group_id, leg.name, code=NO_ROOM,
                                    message="the schedule's max_concurrent left no room for this leg")
            not_started.append({"environment": leg.name, "reason": NO_ROOM})
            continue
        try:
            # The mark is reserved (`validation.QA_TARGET_METADATA_KEY`, VQ2-RESERVE):
            # only this leg's exact value, only for this submission, gets past the
            # refusal, as in `run_target`. `firing.submit_workflow` opens the
            # schedule's own exemption inside it; the two are separate and both are needed.
            with qa_target_mark_allowed(leg.mark):
                item = firing.submit_workflow(spec, repo_id=str(leg.target.get("repo_id") or "") or None)
        except ApiError as exc:
            if exc.status_code >= 500:
                raise
            targets.leg_not_started(group_id, leg.name, code=exc.code, message=exc.message)
            not_started.append({"environment": leg.name, "reason": exc.code})
            refusals.append(exc)
            continue
        task_ids = browser_task_ids(firing.ctx.db, tenant_id, item["id"], leg.browser)
        targets.record_run(tenant_id=tenant_id, target=leg.target, workflow_id=item["id"],
                           task_ids=task_ids, file=FILE, by=by, group_id=group_id, leg=leg)
        targets.leg_submitted(group_id, leg.name, workflow_id=item["id"])
        items.append(item)
        log.info("schedule %s firing %s: QA run %s of target %s, leg %s", schedule["schedule_id"],
                 firing.firing["firing_id"], item["id"], target_id, leg.name)
    if not items and refusals:
        # Nothing started: the refusal is the firing's, as for one leg.
        raise refusals[0]
    firing.explain({"target": target_id, "revision": legs[0].mark.get("revision"), "file": FILE,
                    "group_id": group_id, "legs": [leg.name for leg in legs],
                    **({"not_started": not_started} if not_started else {})})
    return items


def dry_run(firing: Any) -> dict[str, Any]:
    """What `create` would do, as data. Reads only: nothing is submitted or recorded."""
    schedule = firing.schedule
    tenant_id = str(schedule["tenant_id"])
    params = params_of(schedule)
    target_id = params.target
    answer: dict[str, Any] = {"target": target_id, "file": FILE, "room": firing.room,
                              "browser_steps": [], "legs": []}
    targets = _targets(firing.ctx)
    try:
        names = _names(params, targets.get(tenant_id, target_id))
        legs = targets.runnable_legs(tenant_id, target_id, file=FILE, environments=names)
    except ApiError as exc:
        if exc.status_code >= 500:
            raise
        return {**answer, "would": exc.code}
    would = "run" if firing.room > 0 else "room"
    return {**answer, "would": would, "revision": legs[0].mark.get("revision"),
            "browser_steps": sorted({step for leg in legs for step in leg.browser}),
            "legs": [{"environment": leg.name, "browser_steps": leg.browser,
                      "would": "run" if i < firing.room else "room"} for i, leg in enumerate(legs)]}
