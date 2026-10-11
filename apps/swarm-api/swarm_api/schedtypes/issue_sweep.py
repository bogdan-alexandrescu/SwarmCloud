"""`issue-sweep`: an issue run for each ready open issue (docs/schedules.md §3.1, lane S6).

LANE SWEEP'S MODULE IS THE EXECUTOR (§8.1 step 1). The read, the readiness
rules and the order are `issuesweep.collect_candidates` -- the code
`POST /v1/admin/issues/sweep` runs -- called here with the schedule's
parameters in place of the tenant document's settings:

    SWEEP (tenant document / constant)        this type (§3.1 parameter)
    issue_sweep.max_live_runs (default 8)     max_live_runs (default 8, 1-8)
    issue_sweep.exclude_labels                labels_exclude
    SKIP_LABELS {epic, blocked, security}     the same constant, always
    TRUSTED_AUTHORS                           the same constant, always
    SWEEP_FIX_ROUNDS = 2                      fix_rounds (default 3)
    SWEEP_PLAN_APPROVAL = auto                the gate's `plan` (§4.2)
    SWEEP_AUTO_MERGE = true                   the gate's `merge`: `off` makes
                                              no merge, `approve` waits in the
                                              inbox, `auto` only through SD3
    issue_sweep.submit_as                     the schedule's `owner` (§2.7)
    (none)                                    labels_include, cooldown_hours,
                                              max_new_per_firing

`exclude_issues` has no §3.1 parameter, so a schedule cannot exclude an issue
by number; a label does it. The territory guard is `routes/runs.py`'s, which
holds every auto approval whose files overlap a live run's plan whatever
created the run (§8.1), so `territory_guard` cannot switch it off here.

WHAT A FIRING DOES. As the schedule's owner (`firing.owner`, asked of the
directory at this firing), in the schedule's tenant, over the repositories in
scope NOW (`firing.repo_ids`):

  1. ADOPT. Runs already carrying this firing's id (`IssueRuns.for_firing`)
     were made by an earlier attempt at this firing that stopped before
     recording them (§2.2). They are recorded, never made again.
  2. SELECT with SWEEP's rules, plus `labels_include` and the NOT_READY
     cooldown of §3.1, oldest-updated first.
  3. START up to the room: the least of `max_new_per_firing`, `max_live_runs`
     less the TENANT's live runs (SWEEP's count: every live run holds a
     tenant slot, whoever made it), and `max_concurrent` less this schedule's
     live work. Each run through `routes.runs.start_run`, the path
     `POST /v1/runs` takes, carrying `metadata.schedule` and recorded on the
     firing the moment it exists.

A refusal of one issue's start is that issue's; a firing where every start was
refused and none was made ends `refused` with the first refusal's code (§2.7,
three in a row auto-pause the schedule). A 5xx propagates: the firing stays
claimed and the next tick adopts what this one made.

DRY RUN (§2.8). The same read and selection; it starts nothing and writes
nothing, to Firestore or to GitHub, and returns each issue that would start
and each passed over, with the reason.

INVARIANTS. A firing creates issue runs, whose planners are ordinary QUEUED
tasks admitted like any other (1-3); a waiting run is a document. Every read
is the schedule's tenant's own (9). No caller-supplied image, command or
profile reaches a run: the planner's profile is `issueruns`' (10).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping

from .. import issuesweep, scheduletypes
from ..errors import ApiError
from ..forge import open_work_without
from ..issueruns import IssueRun, IssueRuns, refuse_auto_merge
from ..repositories import Repositories, repo_id_for
from ..routes.runs import start_run

log = logging.getLogger(__name__)

TYPE = "issue-sweep"


# --------------------------------------------------------------------------
# What each run is created with
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOptions:
    """`start_run`'s options, from the schedule's gate (§4.2): the gate is the authority."""

    plan_approval: str
    auto_merge: bool
    merge_approval: str | None
    fix_rounds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_approval": self.plan_approval,
            "auto_merge": self.auto_merge,
            "merge_approval": self.merge_approval,
            "fix_rounds": self.fix_rounds,
        }


def run_options(schedule: Mapping[str, Any], fix_rounds: int, *, plan_required: bool = False) -> RunOptions:
    """`merge: off` opens a pull request and never merges it; `approve` merges
    once a person approves it in the inbox (`merge_approval: required`);
    `auto` merges the reviewed, green pull request with no one asked."""
    entry = scheduletypes.get(str(schedule.get("type")))
    default = entry.default_gate if entry is not None else scheduletypes.GateSpec(run="auto", plan="approve", merge="approve")
    gate = schedule.get("gate") or {}
    plan = str(gate.get("plan") or default.plan)
    merge = str(gate.get("merge") or default.merge)
    return RunOptions(
        plan_approval="required" if plan_required or plan == "approve" else "auto",
        auto_merge=merge != "off",
        merge_approval="required" if merge == "approve" else None,
        fix_rounds=int(fix_rounds),
    )


# --------------------------------------------------------------------------
# Selection: SWEEP's read and filter, bounded by the firing
# --------------------------------------------------------------------------


@dataclass
class Selection:
    report: issuesweep.SweepReport
    candidates: list[issuesweep.Candidate]
    #: Runs this firing already made, found by its id (§2.2).
    adopted: list[IssueRun]
    #: How many runs this firing may still start, and what bounds it.
    room: int
    limits: dict[str, int] = field(default_factory=dict)


#: (the tenant's live runs, this firing's adopted runs) -> named upper bounds
#: on what the firing may start; the room is the least of them.
Limits = Callable[[list[IssueRun], list[IssueRun]], dict[str, int]]


def params_of(firing: Any) -> Any:
    """The schedule's parameters, through its type's own model (defaults filled)."""
    entry = scheduletypes.get(str(firing.schedule["type"]))
    if entry is None:
        raise LookupError(f"schedule type {firing.schedule['type']!r} is not in the catalogue")
    return entry.params_model.model_validate(dict(firing.schedule.get("params") or {}))


def select(firing: Any, params: Any, *, limits: Limits, max_live_runs: int) -> Selection:
    """Adopt, read and filter. Reads only: nothing is written anywhere."""
    ctx = firing.ctx
    tenant_id = str(firing.schedule["tenant_id"])
    tenant = ctx.store.get_tenant(tenant_id)
    runs = IssueRuns(ctx.db, now=ctx.now)
    adopted = runs.for_firing(tenant_id, str(firing.firing["firing_id"]))
    report = issuesweep.SweepReport(tenant_id=tenant_id, enabled=True, cap=max_live_runs)
    live_rows, live_cut = runs.live(tenant_id, limit=issuesweep.LIVE_SCAN)
    report.live_runs = len(live_rows)
    report.truncated = live_cut
    live = {(row.issue.repository.lower(), row.issue.number): row for row in live_rows}
    found = Repositories(ctx.db, now=ctx.now)
    registrations = [r for r in (found.find(tenant_id, repo_id) for repo_id in firing.repo_ids) if r]
    report.repositories = len(registrations)
    config = issuesweep.SweepConfig(
        enabled=True, max_live_runs=max_live_runs, exclude_labels=list(params.labels_exclude),
    )
    candidates = issuesweep.collect_candidates(
        ctx, tenant, tenant_id, registrations, config=config, report=report, runs=runs, live=live,
        clock=time.monotonic, started_at=time.monotonic(),
        labels_include=frozenset(label.lower() for label in params.labels_include),
        not_ready_cooldown=timedelta(hours=params.cooldown_hours),
    )
    bounds = limits(live_rows, adopted)
    if live_cut:
        # More live runs than one read reaches: past any cap this type allows.
        bounds["live_scan"] = 0
    room = max(0, min(bounds.values())) if bounds else 0
    return Selection(report=report, candidates=candidates, adopted=adopted, room=room, limits=bounds)


def _limits(firing: Any, params: Any) -> Limits:
    def bounds(live_rows: list[IssueRun], adopted: list[IssueRun]) -> dict[str, int]:
        return {
            "max_new_per_firing": params.max_new_per_firing - len(adopted),
            "max_live_runs": params.max_live_runs - len(live_rows),
            "max_concurrent": firing.room - len(adopted),
        }
    return bounds


# --------------------------------------------------------------------------
# Starting, and the dry run's answer
# --------------------------------------------------------------------------


def _entry(run: IssueRun) -> dict[str, Any]:
    return {
        "kind": "issue_run",
        "id": run.id,
        "repo_id": repo_id_for(run.tenant_id, run.issue.owner, run.issue.repo),
    }


def start(firing: Any, selection: Selection, options: RunOptions) -> list[dict[str, Any]]:
    """Record the adopted runs, then start up to `selection.room` new ones."""
    ctx = firing.ctx
    schedule_id = str(firing.schedule["schedule_id"])
    work = [firing.recorded(_entry(run)) for run in selection.adopted]
    chosen = selection.candidates[: selection.room]
    if not chosen:
        return work
    if options.auto_merge:
        # The merge step disabled: start nothing rather than runs that cannot
        # merge, as SWEEP does. The firing is refused with the code.
        refuse_auto_merge(True)
    first_failure: Exception | None = None
    started = 0
    for candidate in chosen:
        try:
            run = start_run(
                ctx, firing.owner, candidate.ref,
                open_work=open_work_without(candidate.snapshot, candidate.ref.number),
                plan_approval=options.plan_approval, auto_merge=options.auto_merge,
                fix_rounds=options.fix_rounds, created_by=f"schedule:{schedule_id}",
                schedule=firing.mark, merge_approval=options.merge_approval,
            )
        except ApiError as exc:
            if exc.status_code >= 500:
                raise
            first_failure = first_failure or exc
            log.warning("schedule %s firing %s: %s not started (%s)", schedule_id,
                        firing.firing["firing_id"], candidate.ref.short, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 -- one issue's failure is its own, as in SWEEP
            first_failure = first_failure or exc
            log.warning("schedule %s firing %s: %s not started (%s)", schedule_id,
                        firing.firing["firing_id"], candidate.ref.short, type(exc).__name__)
            continue
        work.append(firing.recorded(_entry(run)))
        started += 1
    if started == 0 and not work and first_failure is not None:
        raise first_failure
    log.info("schedule %s firing %s: started=%d adopted=%d candidates=%d skipped=%s",
             schedule_id, firing.firing["firing_id"], started, len(selection.adopted),
             len(selection.candidates), selection.report.skipped_by_reason)
    return work


def describe(selection: Selection, options: RunOptions) -> dict[str, Any]:
    """§2.8: what a firing would start, and why each other issue is passed over."""
    report = selection.report
    chosen = selection.candidates[: selection.room]
    held_back = selection.candidates[selection.room:]
    bound = min(selection.limits, key=selection.limits.__getitem__) if selection.limits else "room"
    passed = list(report.skipped)
    by_reason = dict(report.skipped_by_reason)
    for candidate in held_back:
        passed.append({"issue": candidate.ref.short, "reason": f"room: {bound}"})
    if held_back:
        by_reason["room"] = by_reason.get("room", 0) + len(held_back)
    return {
        "would_start": [
            {"issue": c.ref.short,
             "updated_at": c.issue.updated_at.isoformat() if c.issue.updated_at else None}
            for c in chosen
        ],
        "passed_over": passed,
        "passed_over_by_reason": dict(sorted(by_reason.items())),
        "adopted": [run.id for run in selection.adopted],
        "room": selection.room,
        "limits": dict(selection.limits),
        "live_runs": report.live_runs,
        "repositories": report.repositories,
        "failures": list(report.failures),
        "truncated": report.truncated,
        "runs": options.as_dict(),
    }


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def create(firing: Any) -> list[dict[str, Any]]:
    params = params_of(firing)
    selection = select(firing, params, limits=_limits(firing, params), max_live_runs=params.max_live_runs)
    return start(firing, selection, run_options(firing.schedule, params.fix_rounds))


def dry_run(firing: Any) -> dict[str, Any]:
    params = params_of(firing)
    selection = select(firing, params, limits=_limits(firing, params), max_live_runs=params.max_live_runs)
    return describe(selection, run_options(firing.schedule, params.fix_rounds))
