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
     less THIS SCHEDULE's live runs (`schedule_live`), and `max_concurrent`
     less this schedule's live work. Another schedule's run, or a person's,
     holds no place in it: measured 2026-10-11, a stuck run of another
     schedule's (run_e525710be66e) took a slot from sch_88b97e090beb, whose
     firings then started nothing (lane SCHED). The tenant's own ceilings
     are admission's, not this parameter's. Each run through `routes.runs.start_run`, the path
     `POST /v1/runs` takes, carrying `metadata.schedule` and recorded on the
     firing the moment it exists.

A refusal of one issue's start is that issue's; a firing where every start was
refused and none was made ends `refused` with the first refusal's code (§2.7,
three in a row auto-pause the schedule). A 5xx propagates: the firing stays
claimed and the next tick adopts what this one made.

DRY RUN (§2.8). The same read and selection; it starts nothing and writes
nothing, to Firestore or to GitHub, and returns each issue that would start
and each passed over, with the reason.

A REAL FIRING RECORDS THE SAME ANSWER (#1133). `create` once returned only
the runs it made and dropped the selection: a firing whose repositories could
not be read, whose issues were all passed over, or whose room was 0 ended
`succeeded` with `work: []` and nothing saying which -- while its dry run
listed an issue to start. Now `create` and `dry_run` share `select` and
`describe`; `create` hands the description, with what it started and what
was refused, to `firing.explain`, the firing stores it as `selection`, and a
firing that started nothing carries `skip: NOTHING_STARTED` with
`describe`'s `reason`: `no_room` (and the `bound`), `repositories_not_read`,
`no_eligible_issue`, `no_open_issue` or `no_repository`, and `own_live`:
how many of this schedule's runs are live against its own limit, which the
console's history reads as "N of M running from this schedule".

INVARIANTS. A firing creates issue runs, whose planners are ordinary QUEUED
tasks admitted like any other (1-3); a waiting run is a document. Every read
is the schedule's tenant's own (9). No caller-supplied image, command or
profile reaches a run: the planner's profile is `issueruns`' (10).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping

from swarm_common.states import TERMINAL_STATES

from .. import issuesweep, scheduletypes
from ..errors import ApiError
from ..forge import ForgeReadError, open_work_without
from ..issueruns import IssueRun, IssueRuns, refuse_auto_merge
from ..repositories import Repositories, repo_id_for
from ..routes.runs import start_run
from ..validation import workflow_label

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
# Work already under way on an issue, outside any issue run
# --------------------------------------------------------------------------

#: The newest non-terminal workflows a selection reads for `in_flight_workflow`:
#: one page of `GET /v1/workflows?active=true`'s query (`Store.list_workflows`
#: on the stored states a live workflow can carry), newest first. A tenant
#: with more live workflows than this is reported `truncated` in
#: `in_flight.workflows_truncated`; an older one's issue is not passed over.
WORKFLOW_SCAN = 50

_TERMINAL = frozenset(s.value for s in TERMINAL_STATES)

#: `#N` bounded by non-digits, optionally `owner/repo#N`: "part of #N" and
#: "(#N)" are both this. A word character directly before the `#` (`LB#2`) is
#: not a reference.
_ISSUE_MENTION = re.compile(
    r"(?:(?P<repo>[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)|(?<![A-Za-z0-9_]))#(?P<num>[0-9]+)(?![0-9])"
)


def mentioned_issues(text: str, repository: str) -> set[int]:
    """The issue numbers of `repository` a workflow's title or label names.

    A bare `#N` is taken to name an issue in EVERY repository in scope (a
    workflow's title carries no repository): passing an issue over is the
    safe direction to be wrong in. `owner/repo#N` counts only for that
    repository, compared case-insensitively.
    """
    wanted = repository.lower()
    found: set[int] = set()
    for match in _ISSUE_MENTION.finditer(text or ""):
        named = match.group("repo")
        if named is not None and named.lower() != wanted:
            continue
        found.add(int(match.group("num")))
    return found


@dataclass
class InFlight:
    """The tenant's live workflows that name an issue, as (title, issue-run link)."""

    #: (workflow_id, its label or title or "") for each non-terminal workflow read.
    labels: list[tuple[str, str]] = field(default_factory=list)
    #: (workflow_id, "owner/repo#N") from a step task's `metadata.issue_run`.
    links: list[tuple[str, str]] = field(default_factory=list)
    read: int = 0
    truncated: bool = False

    def workflow_for(self, repository: str, number: int) -> str | None:
        short = f"{repository}#{number}".lower()
        for workflow_id, link in self.links:
            if link.lower() == short:
                return workflow_id
        for workflow_id, label in self.labels:
            if number in mentioned_issues(label, repository):
                return workflow_id
        return None


def _issue_run_link(metadata: Mapping[str, Any], live: Mapping[str, IssueRun]) -> str | None:
    """`owner/repo#N` from a task's `metadata.issue_run`: the dict a compiled
    run or a CI fix round carries, or the bare run id a planner carries."""
    link = metadata.get("issue_run")
    if isinstance(link, Mapping):
        issue = link.get("issue")
        if isinstance(issue, str) and "#" in issue:
            return issue
        link = link.get("run_id")
    if isinstance(link, str) and link in live:
        return live[link].issue.short
    return None


def in_flight(ctx: Any, tenant_id: str, live_rows: Iterable[IssueRun]) -> InFlight:
    """The tenant's newest `WORKFLOW_SCAN` live workflows, as the workflow
    list serves them: the stored-state query, then each one's state DERIVED
    from its steps (`WorkflowRollups`, with `persist=False`: a dry run writes
    nothing), keeping only those not terminal. Reads only."""
    found = InFlight()
    page = ctx.store.list_workflows(
        tenant_id, limit=WORKFLOW_SCAN, submitted_by=None,
        stored_states=ctx.store.stored_states_for(active=True, states=None),
    )
    found.truncated = page.next_page_token is not None
    results, _ = ctx.rollups.for_workflows(tenant_id, page.items, persist=False)
    live = {row.id: row for row in live_rows}
    for result in results:
        if result.to_api().get("state") in _TERMINAL:
            continue
        found.read += 1
        workflow_id = result.workflow.workflow_id
        for task in result.step_tasks.values():
            metadata = getattr(task, "metadata", None) or {}
            label = workflow_label(metadata)
            if label:
                found.labels.append((workflow_id, label))
            link = _issue_run_link(metadata, live)
            if link:
                found.links.append((workflow_id, link))
    return found


def _recently_merged(ctx: Any, tenant: Any, candidates: list[issuesweep.Candidate],
                     since: Any) -> dict[str, tuple[dict[int, int] | None, str | None, bool]]:
    """For each repository a candidate is in: {issue number: the merged pull
    request whose title says `part of #N`}, or None and the error code when
    the read failed, and whether the read was cut (`recently_merged_pulls`)."""
    out: dict[str, tuple[dict[int, int] | None, str | None, bool]] = {}
    for candidate in candidates:
        ref = candidate.ref
        repository = ref.repository
        if repository in out:
            continue
        token = ctx.forge_tokens.token_for(tenant)
        try:
            pulls, cut = ctx.forge.recently_merged_pulls(ref.owner, ref.repo, token, since=since)
        except ForgeReadError as refused:
            out[repository] = (None, refused.code, False)
            continue
        except Exception as exc:  # noqa: BLE001 -- one repository's failure is its own
            out[repository] = (None, type(exc).__name__, False)
            continue
        finally:
            token = ""
        worked: dict[int, int] = {}
        for pull in pulls:
            for number in issuesweep.claimed_issues(pull.title, repository):
                worked.setdefault(number, pull.number)
        out[repository] = (worked, None, cut)
    return out


def pass_over_worked(ctx: Any, tenant: Any, tenant_id: str, candidates: list[issuesweep.Candidate],
                     report: issuesweep.SweepReport, live_rows: list[IssueRun], *,
                     cooldown: timedelta) -> tuple[list[issuesweep.Candidate], dict[str, Any]]:
    """Pass over each candidate somebody is already working on, or just worked on.

    `in_flight_workflow: <workflow_id>`: a non-terminal workflow of the tenant
    names the issue in its title or label (`part of #N`, `(#N)`, `#N`), or
    carries an issue-run link to it. `recently_worked: PR <n>`: a pull request
    whose title says `part of #N` merged within `cooldown`. Without these
    the sweep took #1030 -- built all day by ~25 dedicated workflows -- for
    idle in the gap between two of their merges, and planned it a third way
    (2026-10-12, run_21bcbaba3ba14235832a); and opened PR 1136 beside a
    lane's PR 1134 for #1133. A repository whose merged pull requests could
    not be read starts nothing: its candidates are passed over as
    `recently_worked: not read (<code>)`, as SWEEP passes over a repository
    whose open pull requests it could not all see.
    """
    if not candidates:
        return candidates, {}
    flight = in_flight(ctx, tenant_id, live_rows)
    merged = _recently_merged(ctx, tenant, candidates, ctx.now() - cooldown)
    kept: list[issuesweep.Candidate] = []
    for candidate in candidates:
        repository, number = candidate.ref.repository, candidate.ref.number
        workflow_id = flight.workflow_for(repository, number)
        if workflow_id is not None:
            report.skip(candidate.ref.short, f"in_flight_workflow: {workflow_id}")
            continue
        worked, error, _ = merged[repository]
        if worked is None:
            report.skip(candidate.ref.short, f"recently_worked: not read ({error})")
            continue
        if number in worked:
            report.skip(candidate.ref.short, f"recently_worked: PR {worked[number]}")
            continue
        kept.append(candidate)
    for repository, (worked, error, _) in sorted(merged.items()):
        if worked is None:
            report.failures.append({"repository": repository, "error": f"recent_pulls_not_read: {error}"})
    scan = {
        "workflows_read": flight.read,
        "workflow_scan": WORKFLOW_SCAN,
        "workflows_truncated": flight.truncated,
        "merged_since_hours": int(cooldown.total_seconds() // 3600),
        "merged_truncated": sorted(r for r, (_, _, cut) in merged.items() if cut),
    }
    return kept, scan


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
    #: What a real firing started and which starts were refused, by `start`.
    started: list[dict[str, str]] = field(default_factory=list)
    start_failures: list[dict[str, str]] = field(default_factory=list)
    #: What `pass_over_worked` read, and whether either read was cut.
    in_flight: dict[str, Any] = field(default_factory=dict)
    #: This schedule's live runs against its own limit: `{running, limit,
    #: bound}`, the limit's parameter named; None when the type has none.
    own_live: dict[str, Any] | None = None


#: (the tenant's live runs, this firing's adopted runs) -> named upper bounds
#: on what the firing may start; the room is the least of them.
Limits = Callable[[list[IssueRun], list[IssueRun]], dict[str, int]]
#: the tenant's live runs -> this schedule's live runs against its own limit.
OwnLive = Callable[[list[IssueRun]], dict[str, Any]]


def schedule_live(live_rows: Iterable[IssueRun], schedule_id: str) -> list[IssueRun]:
    """The live runs a firing of `schedule_id` started (`metadata.schedule`)."""
    return [row for row in live_rows if (row.schedule or {}).get("schedule_id") == schedule_id]


def params_of(firing: Any) -> Any:
    """The schedule's parameters, through its type's own model (defaults filled)."""
    entry = scheduletypes.get(str(firing.schedule["type"]))
    if entry is None:
        raise LookupError(f"schedule type {firing.schedule['type']!r} is not in the catalogue")
    return entry.params_model.model_validate(dict(firing.schedule.get("params") or {}))


def select(firing: Any, params: Any, *, limits: Limits, max_live_runs: int,
           own_live: OwnLive | None = None) -> Selection:
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
    candidates, scan = pass_over_worked(
        ctx, tenant, tenant_id, candidates, report, live_rows,
        cooldown=timedelta(hours=params.cooldown_hours),
    )
    bounds = limits(live_rows, adopted)
    if live_cut:
        # More live runs than one read reaches: past any cap this type allows.
        bounds["live_scan"] = 0
    room = max(0, min(bounds.values())) if bounds else 0
    return Selection(report=report, candidates=candidates, adopted=adopted, room=room, limits=bounds,
                     in_flight=scan, own_live=own_live(live_rows) if own_live is not None else None)


def _limits(firing: Any, params: Any) -> Limits:
    schedule_id = str(firing.schedule["schedule_id"])

    def bounds(live_rows: list[IssueRun], adopted: list[IssueRun]) -> dict[str, int]:
        # Adopted runs are this schedule's live runs, so `schedule_live`
        # already counts them against `max_live_runs`.
        return {
            "max_new_per_firing": params.max_new_per_firing - len(adopted),
            "max_live_runs": params.max_live_runs - len(schedule_live(live_rows, schedule_id)),
            "max_concurrent": firing.room - len(adopted),
        }
    return bounds


def _own_live(firing: Any, params: Any) -> OwnLive:
    schedule_id = str(firing.schedule["schedule_id"])

    def own(live_rows: list[IssueRun]) -> dict[str, Any]:
        return {"running": len(schedule_live(live_rows, schedule_id)), "limit": params.max_live_runs,
                "bound": "max_live_runs"}
    return own


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
            selection.start_failures.append({"issue": candidate.ref.short, "error": exc.code})
            log.warning("schedule %s firing %s: %s not started (%s)", schedule_id,
                        firing.firing["firing_id"], candidate.ref.short, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 -- one issue's failure is its own, as in SWEEP
            first_failure = first_failure or exc
            selection.start_failures.append({"issue": candidate.ref.short, "error": type(exc).__name__})
            log.warning("schedule %s firing %s: %s not started (%s)", schedule_id,
                        firing.firing["firing_id"], candidate.ref.short, type(exc).__name__)
            continue
        work.append(firing.recorded(_entry(run)))
        selection.started.append({"issue": candidate.ref.short, "run_id": run.id})
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
    out: dict[str, Any] = {
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
        "bound": bound if selection.limits else None,
        "in_flight": dict(selection.in_flight),
        "own_live": None if selection.own_live is None else dict(selection.own_live),
    }
    if not chosen and not selection.adopted:
        out["reason"] = nothing_reason(selection)
    return out


def nothing_reason(selection: Selection) -> str:
    """Why a selection chose nothing: the `NOTHING_STARTED` skip's `reason` (#1133)."""
    report = selection.report
    if selection.candidates:
        return "no_room"
    if report.repositories == 0:
        return "no_repository"
    if report.skipped_by_reason:
        return "no_eligible_issue"
    if report.failures:
        return "repositories_not_read"
    return "no_open_issue"


def fire(firing: Any, selection: Selection, options: RunOptions) -> list[dict[str, Any]]:
    """`create`'s body for this type and `issue-plan-only`: start, then give the
    firing the same description a dry run returns, with what was started."""
    work = start(firing, selection, options)
    firing.explain({
        **describe(selection, options),
        "started": list(selection.started),
        "start_failures": list(selection.start_failures),
    })
    return work


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def create(firing: Any) -> list[dict[str, Any]]:
    params = params_of(firing)
    selection = select(firing, params, limits=_limits(firing, params), max_live_runs=params.max_live_runs,
                       own_live=_own_live(firing, params))
    return fire(firing, selection, run_options(firing.schedule, params.fix_rounds))


def dry_run(firing: Any) -> dict[str, Any]:
    params = params_of(firing)
    selection = select(firing, params, limits=_limits(firing, params), max_live_runs=params.max_live_runs,
                       own_live=_own_live(firing, params))
    return describe(selection, run_options(firing.schedule, params.fix_rounds))
