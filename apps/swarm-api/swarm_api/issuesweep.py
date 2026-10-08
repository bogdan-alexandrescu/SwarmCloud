"""The issue sweeper: start an issue run for each open issue that is ready (owner, 2026-10-08).

    POST /v1/admin/issues/sweep?tenant_id=<t>   the Cloud Scheduler job, every 30 minutes

For one tenant, for each of its registered repositories, list the OPEN issues
with the tenant's forge credential -- the token the planner's open-work read
already uses (`forge.read_open_work`) -- and start an issue run
(`routes.runs.start_run`) for each candidate, oldest-updated first, until the
tenant has `max_live_runs` live runs (default 8). Every run it starts is
`plan_approval: auto`, `auto_merge: true` and `fix_rounds: 2`, and records
`created_by: issue-sweep`.

WHY THE PLANNER DECIDES READINESS, NOT A LABEL (owner decision 2026-10-08).
A `ready` label is a second thing to keep true, and a label nobody applied is
indistinguishable from "not ready". The planner already reads the issue, the
repository and the open work; it answers NOT_READY with a reason and what the
issue needs (`issueruns.NotReadyVerdict`), the run ends holding nothing
(invariant 1), and the reason is posted on the issue. So the sweep only skips
what it can decide without reading the issue's text:

    label       labelled `epic`, `blocked` or `security`
    excluded    named in the tenant's exclusion list, by number or label
    live_run    the issue already has a live run
    open_pull_request
                an open pull request says `Closes/Fixes/Resolves #N` or
                `part of #N` for it
    not_ready_unchanged / unchanged_since_run
                its last run ended -- NOT_READY, or any other end -- and the
                issue has not been edited or commented on since that run last
                wrote to it: planning it again would answer the same
    cap         the tenant already has `max_live_runs` live runs

Pull requests are never candidates: GitHub's issue list includes them, marked,
and the read drops them (`forge.GitHubIssues.sweep_listing`).

"NOT CHANGED SINCE" MEANS SINCE THE RUN'S OWN LAST WRITE. The run's status
comment is itself an edit of the issue, and moves its `updated_at`; compared
with the verdict's time alone, every NOT_READY issue would look edited and be
planned again every half hour. The run records when its write-back last wrote
(`IssueRun.last_writeback_at`), and the issue counts as changed only when
GitHub's `updated_at` is later than that by more than `WRITEBACK_SLACK`
(clock skew between GitHub and this service).

TWO SWITCHES, BOTH OFF (the new-refusals-ship-off rule of PR 873 and the
repository's convention). `SWEEP_ENABLED` on swarm-api turns the route on for
the platform; `issue_sweep.enabled` on the tenant document turns it on for one
tenant (`PUT /v1/admin/tenants/{tenant_id}/issue-sweep`). With either off the
route answers what it would have done -- nothing -- and starts nothing.

AS WHOM. The scheduler's identity is no tenant member and submits nothing as
itself. A run is submitted as the creator of its repository's registration,
asked of the directory first (`routes.admin.registration_owner_auth`, the
repository poll's rule), and the run keeps that member as `on_behalf_of`, so
every later submission -- the auto approval, CI fix rounds, the merge -- is
made as them and only while they are still a member (`run_owner_auth`).

INVARIANTS. The sweep itself creates no infrastructure demand: a planner is an
ordinary task, admitted like any other (invariants 1-3), and a run waiting on
its planner or its verdict is a Firestore document. Its reads and writes are
the named tenant's only (invariant 9): its registrations, its token, its runs.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator

from swarm_common.models import Tenant

from .errors import ApiError, NotFound
from .forge import ForgeReadError, SweepIssue, SweepListing, open_work_from_listing, open_work_without
from .issueruns import TERMINAL_RUN_STATES, IssueRun, IssueRuns, RunState, refuse_auto_merge
from .repositories import Repositories
from .validation import IssueRef

log = logging.getLogger(__name__)

#: What a swept run records as its creator.
SWEEP_CREATOR = "issue-sweep"

#: What every swept run is created with (owner decisions 2026-10-08): the plan
#: auto-approves and the pull request merges through the merge step.
SWEEP_PLAN_APPROVAL = "auto"
SWEEP_AUTO_MERGE = True
SWEEP_FIX_ROUNDS = 2

#: Labels never swept, whatever the tenant's exclusion list says.
SKIP_LABELS = frozenset({"epic", "blocked", "security"})

#: The tenant document's field, beside the frozen `Tenant` type's fields
#: rather than one of them (rule 1), as `findings_epic` is.
TENANT_FIELD = "issue_sweep"

#: Live issue runs per tenant the sweep fills up to, unless the tenant says.
DEFAULT_MAX_LIVE_RUNS = 8
MAX_LIVE_RUNS_LIMIT = 50

#: The most live runs read to count against the cap. Above it, the tenant is
#: past any cap this allows, and nothing is started.
LIVE_SCAN = 500
#: Registrations read per sweep.
MAX_REPOSITORIES = 100
#: The most runs of one issue read to find its last one.
ISSUE_HISTORY = 50
#: The route stops STARTING work at this many seconds -- the Cloud Scheduler
#: job's deadline is 300 s -- and reports what it did not reach.
SWEEP_BUDGET_SECONDS = 240.0
#: An issue counts as changed after a run only when GitHub's `updated_at` is
#: later than the run's last write by more than this: GitHub's clock and ours.
WRITEBACK_SLACK = timedelta(seconds=60)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

#: The most skipped issues the response lists one by one; `skipped_by_reason`
#: counts every one.
MAX_REPORTED_SKIPS = 200


# --------------------------------------------------------------------------
# the tenant's settings
# --------------------------------------------------------------------------

_IssueNumber = Annotated[StrictInt, Field(ge=1, le=9_999_999)]
_Label = Annotated[str, Field(min_length=1, max_length=100)]


class SweepConfig(BaseModel):
    """`issue_sweep` on the tenant document, and the body of its PUT.

    `enabled` defaults OFF. `exclude_issues` and `exclude_labels` are the
    owner's deferred set -- e.g. epic #476's children by number, or a label
    they carry. Strict, so `"8"` and `"true"` are refused rather than coerced.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    max_live_runs: StrictInt = Field(default=DEFAULT_MAX_LIVE_RUNS, ge=1, le=MAX_LIVE_RUNS_LIMIT)
    exclude_issues: list[_IssueNumber] = Field(default_factory=list, max_length=1_000)
    exclude_labels: list[_Label] = Field(default_factory=list, max_length=100)

    @field_validator("exclude_labels")
    @classmethod
    def _labels(cls, value: list[str]) -> list[str]:
        return sorted({label.strip().lower() for label in value if label.strip()})

    @field_validator("exclude_issues")
    @classmethod
    def _numbers(cls, value: list[int]) -> list[int]:
        return sorted(set(value))


def get_config(db: Any, tenant_id: str) -> SweepConfig:
    """The tenant's sweep settings; the defaults -- OFF -- when unset.

    A stored value that no longer validates reads as OFF and is logged: a
    sweep must never start because its settings could not be read.
    """
    snap = db.collection("tenants").document(tenant_id).get()
    if not snap.exists:
        raise NotFound(f"tenant {tenant_id!r} does not exist")
    stored = (snap.to_dict() or {}).get(TENANT_FIELD)
    if not isinstance(stored, Mapping):
        return SweepConfig()
    try:
        return SweepConfig.model_validate(dict(stored))
    except ValidationError:
        log.warning("issue sweep tenant=%s: stored settings unreadable; read as off", tenant_id)
        return SweepConfig()


def set_config(db: Any, tenant_id: str, config: SweepConfig) -> SweepConfig:
    """Store the tenant's sweep settings whole. NotFound for no tenant."""
    ref = db.collection("tenants").document(tenant_id)
    if not ref.get().exists:
        raise NotFound(f"tenant {tenant_id!r} does not exist")
    ref.update({TENANT_FIELD: config.model_dump()})
    return config


# --------------------------------------------------------------------------
# what an open pull request claims
# --------------------------------------------------------------------------

_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_REPO = r"[A-Za-z0-9._-]{1,100}"
#: `Closes #12`, `fixed: owner/repo#12`, `Resolves https://github.com/o/r/issues/12`,
#: `part of #12` -- GitHub's closing keywords, and this platform's own
#: partial-fix phrase (CLAUDE.md, "Closes #N only when it is unconditionally true").
_CLAIM = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|part\s+of)\b:?\s+"
    rf"(?:(?:https?://github\.com/(?P<uowner>{_OWNER})/(?P<urepo>{_REPO})/issues/(?P<unum>[0-9]+))"
    rf"|(?:(?P<owner>{_OWNER})/(?P<repo>{_REPO}))?#(?P<num>[0-9]+))",
    re.IGNORECASE,
)


def claimed_issues(text: str, repository: str) -> set[int]:
    """The issue numbers of `repository` that `text` closes or is `part of`.

    A bare `#N` is the pull request's own repository's; `owner/repo#N` and an
    issue URL count only when they name `repository` (case-insensitively, as
    GitHub compares names).
    """
    numbers: set[int] = set()
    wanted = repository.lower()
    for match in _CLAIM.finditer(text or ""):
        if match.group("unum") is not None:
            named = f"{match.group('uowner')}/{match.group('urepo')}".lower()
            if named == wanted:
                numbers.add(int(match.group("unum")))
            continue
        if match.group("owner") is not None:
            named = f"{match.group('owner')}/{match.group('repo')}".lower()
            if named != wanted:
                continue
        numbers.add(int(match.group("num")))
    return numbers


# --------------------------------------------------------------------------
# the candidate filter
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    """One open issue the filter let through, in the order the sweep starts them."""

    ref: IssueRef
    issue: SweepIssue
    registration: Mapping[str, Any]
    snapshot: Mapping[str, Any]


def _changed_since(issue: SweepIssue, run: IssueRun) -> bool:
    """Whether the issue was edited or commented on after `run` last wrote to it."""
    if issue.updated_at is None:
        return False  # GitHub did not say: not proof of a change
    seen = run.updated_at
    if run.last_writeback_at is not None and run.last_writeback_at > seen:
        seen = run.last_writeback_at
    return issue.updated_at > seen + WRITEBACK_SLACK


def skip_reason(
    issue: SweepIssue,
    *,
    repository: str,
    config: SweepConfig,
    live: Mapping[tuple[str, int], IssueRun],
    claimed: Mapping[int, int],
    last_run: Callable[[int], IssueRun | None],
) -> str | None:
    """Why the sweep does NOT start a run for `issue`, or None: it is a candidate.

    The rules in the module docstring's order, cheapest first: the last one
    reads Firestore, so it is asked only of an issue every other rule let
    through. `live` is keyed by (lower-cased `owner/repo`, number); `claimed`
    maps an issue number to the open pull request that claims it.
    """
    labels = {label.lower() for label in issue.labels}
    never = sorted(labels & SKIP_LABELS)
    if never:
        return f"label: {never[0]}"
    if issue.number in config.exclude_issues:
        return "excluded: issue"
    excluded = sorted(labels & set(config.exclude_labels))
    if excluded:
        return f"excluded: label {excluded[0]}"
    running = live.get((repository.lower(), issue.number))
    if running is not None:
        return f"live_run: {running.id}"
    if issue.number in claimed:
        return f"open_pull_request: #{claimed[issue.number]}"
    previous = last_run(issue.number)
    if previous is not None and previous.state in TERMINAL_RUN_STATES:
        if not _changed_since(issue, previous):
            if previous.state == RunState.NOT_READY:
                return f"not_ready_unchanged: {previous.id}"
            return f"unchanged_since_run: {previous.id} ({previous.state.value})"
    return None


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

@dataclass
class SweepReport:
    tenant_id: str
    enabled: bool
    #: Why nothing was swept, when `enabled` is False.
    disabled_by: str | None = None
    cap: int = DEFAULT_MAX_LIVE_RUNS
    live_runs: int = 0
    repositories: int = 0
    started: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    #: A repository that could not be swept, by name and error code.
    failures: list[dict[str, str]] = field(default_factory=list)
    #: More work than one sweep reaches: registrations past MAX_REPOSITORIES,
    #: live runs past LIVE_SCAN, or the time budget spent.
    truncated: bool = False

    def skip(self, issue: str, reason: str) -> None:
        kind = reason.split(":", 1)[0]
        self.skipped_by_reason[kind] = self.skipped_by_reason.get(kind, 0) + 1
        if len(self.skipped) < MAX_REPORTED_SKIPS:
            self.skipped.append({"issue": issue, "reason": reason})

    def to_api(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "enabled": self.enabled,
            "disabled_by": self.disabled_by,
            "cap": self.cap,
            "live_runs": self.live_runs,
            "repositories": self.repositories,
            "started": list(self.started),
            "skipped": list(self.skipped),
            "skipped_by_reason": dict(sorted(self.skipped_by_reason.items())),
            "failures": list(self.failures),
            "truncated": self.truncated,
        }


#: (ctx, auth, ref, open_work=..., plan_approval=..., auto_merge=...,
#: fix_rounds=..., created_by=...) -> IssueRun: `routes.runs.start_run`,
#: passed in so this module does not import the routes.
StartRun = Callable[..., IssueRun]
#: registration -> the AuthContext a run is submitted as, raising when its
#: creator may not submit: `routes.admin.registration_owner_auth(ctx, tenant)`.
OwnerAuth = Callable[[Mapping[str, Any]], Any]


def sweep_tenant(
    ctx: Any,
    tenant_id: str,
    *,
    owner_auth: OwnerAuth,
    start_run: StartRun,
    clock: Callable[[], float] = time.monotonic,
) -> SweepReport:
    """One sweep of one tenant: read, filter, start oldest-updated first, report."""
    platform_on = bool(getattr(ctx.settings, "sweep_enabled", False))
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    config = get_config(ctx.db, tenant_id)
    report = SweepReport(tenant_id=tenant_id, enabled=platform_on and config.enabled,
                         cap=config.max_live_runs)
    if not platform_on:
        report.disabled_by = "SWEEP_ENABLED"
        return report
    if not config.enabled:
        report.disabled_by = "tenant"
        return report
    started_at = clock()
    runs = IssueRuns(ctx.db, now=ctx.now)

    live_rows, live_cut = runs.live(tenant_id, limit=LIVE_SCAN)
    report.live_runs = len(live_rows)
    if live_cut:
        report.truncated = True
    live = {(row.issue.repository.lower(), row.issue.number): row for row in live_rows}

    registrations, more = Repositories(ctx.db, now=ctx.now).list(tenant_id, limit=MAX_REPOSITORIES)
    report.truncated = report.truncated or more is not None
    report.repositories = len(registrations)

    candidates: list[Candidate] = []
    for registration in registrations:
        if clock() - started_at > SWEEP_BUDGET_SECONDS:
            report.truncated = True
            break
        repository = f"{registration.get('owner')}/{registration.get('repo')}"
        if registration.get("archived"):
            report.failures.append({"repository": repository, "error": "archived"})
            continue
        try:
            listing, snapshot = _read(ctx, tenant, registration)
        except ForgeReadError as refused:
            log.info("issue sweep tenant=%s repository=%s not read (%s)",
                     tenant_id, repository, refused.code)
            report.failures.append({"repository": repository, "error": refused.code})
            continue
        except Exception as exc:  # noqa: BLE001 -- one repository's failure is its own
            log.warning("issue sweep tenant=%s repository=%s not read (%s)",
                        tenant_id, repository, type(exc).__name__)
            report.failures.append({"repository": repository, "error": type(exc).__name__})
            continue
        if listing.pulls_truncated:
            # Unseen pull requests may claim any issue: none is safe to start.
            report.failures.append({"repository": repository, "error": "too_many_pull_requests"})
            continue
        claimed: dict[int, int] = {}
        for pull in listing.pulls:
            for number in claimed_issues(f"{pull.title}\n{pull.body}", listing.repository):
                claimed.setdefault(number, pull.number)

        def last_run(number: int, _owner=registration.get("owner"), _repo=registration.get("repo")):
            rows = runs.for_issue(
                tenant_id, IssueRef(owner=_owner, repo=_repo, number=number), limit=ISSUE_HISTORY,
            )
            return rows[0] if rows else None

        for issue in listing.issues:
            short = f"{listing.repository}#{issue.number}"
            reason = skip_reason(
                issue, repository=listing.repository, config=config, live=live,
                claimed=claimed, last_run=last_run,
            )
            if reason is not None:
                report.skip(short, reason)
                continue
            candidates.append(Candidate(
                ref=IssueRef(owner=str(registration.get("owner")),
                             repo=str(registration.get("repo")), number=issue.number),
                issue=issue, registration=registration, snapshot=snapshot,
            ))

    # Oldest-updated first, across every repository: the issue nobody has
    # touched longest is the one most likely waiting on nobody. An issue
    # GitHub gave no time for goes last.
    candidates.sort(key=lambda c: (
        c.issue.updated_at is None, c.issue.updated_at or _EPOCH, c.ref.short,
    ))
    room = max(0, config.max_live_runs - report.live_runs)
    try:
        refuse_auto_merge(SWEEP_AUTO_MERGE)
    except ApiError as refused:
        # Every swept run merges through the merge step; with the step
        # disabled the sweep starts nothing rather than runs that cannot.
        report.failures.append({"repository": "*", "error": refused.code})
        for candidate in candidates:
            report.skip(candidate.ref.short, f"start_failed: {refused.code}")
        return report
    for candidate in candidates:
        short = candidate.ref.short
        if room <= 0:
            report.skip(short, f"cap: {config.max_live_runs} live runs")
            continue
        if clock() - started_at > SWEEP_BUDGET_SECONDS:
            report.truncated = True
            report.skip(short, "budget: the sweep's time ran out")
            continue
        try:
            auth = owner_auth(candidate.registration)
            run = start_run(
                ctx, auth, candidate.ref,
                open_work=open_work_without(candidate.snapshot, candidate.ref.number),
                plan_approval=SWEEP_PLAN_APPROVAL, auto_merge=SWEEP_AUTO_MERGE,
                fix_rounds=SWEEP_FIX_ROUNDS, created_by=SWEEP_CREATOR,
            )
        except Exception as exc:  # noqa: BLE001 -- one issue's refusal is its own
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.warning("issue sweep tenant=%s issue=%s not started (%s)", tenant_id, short, code)
            report.skip(short, f"start_failed: {code}")
            continue
        room -= 1
        report.live_runs += 1
        report.started.append({"issue": short, "run_id": run.id})
        log.info("issue sweep tenant=%s started %s for %s", tenant_id, run.id, short)
    log.info(
        "issue sweep tenant=%s started=%d skipped=%s failures=%d live=%d cap=%d truncated=%s",
        tenant_id, len(report.started), report.skipped_by_reason, len(report.failures),
        report.live_runs, config.max_live_runs, report.truncated,
    )
    return report


def _read(ctx: Any, tenant: Tenant, registration: Mapping[str, Any]) -> tuple[SweepListing, dict]:
    """One repository's listing and its masked open-work snapshot, with the
    tenant's token -- read in this frame only, and a known literal to the
    masking, as `forge.read_open_work` holds it."""
    token = ctx.forge_tokens.token_for(tenant)
    try:
        listing = ctx.forge.sweep_listing(
            str(registration.get("owner")), str(registration.get("repo")), token,
        )
        snapshot = open_work_from_listing(listing, read_at=ctx.now(), literals=(token,))
    finally:
        token = ""
    return listing, snapshot
