"""The issue sweeper's selection: which open issues a sweep may start a run for (owner, 2026-10-08).

For one tenant, for each registered repository it is handed, list the OPEN
issues with the tenant's forge credential -- the token the planner's open-work
read already uses (`forge.read_open_work`) -- and filter them, oldest-updated
first. `collect_candidates` is the read and the filter; it starts nothing.
The `issue-sweep` and `issue-plan-only` schedule types
(`swarm_api/schedtypes/`, docs/schedules.md §8.1 step 1) call it with a
schedule's parameters, decide how many to start, and start them.

THE ROUTE THAT USED TO DRIVE THIS IS GONE (2026-10-12). Until then a
per-tenant Cloud Scheduler job POSTed `/v1/admin/issues/sweep`, which read
the tenant document's `issue_sweep` settings, checked swarm-api's
SWEEP_ENABLED, and started runs as the tenant's `issue_sweep.submit_as`.
Schedules S9 paused the job on 2026-10-11, and its second change deleted the
job, the route, its settings routes, SWEEP_ENABLED and the settings' reads on
2026-10-12, owner-approved with the 7-day watch waived: the tenant's
`issue-sweep` schedule replaced them, and a schedule starts its runs as its
own owner (docs/schedules.md §2.7). What is left here is the part both
shared. A tenant document may still carry an `issue_sweep` map; nothing
reads it.

WHY THE PLANNER DECIDES READINESS, NOT A LABEL (owner decision 2026-10-08).
A `ready` label is a second thing to keep true, and a label nobody applied is
indistinguishable from "not ready". The planner already reads the issue, the
repository and the open work; it answers NOT_READY with a reason and what the
issue needs (`issueruns.NotReadyVerdict`), the run ends holding nothing
(invariant 1), and the reason is posted on the issue. So the sweep only skips
what it can decide without reading the issue's text:

    author      the issue was opened by someone who is not an owner, an
                organisation member or a collaborator of the repository
    label       labelled `epic`, `blocked` or `security`
    excluded    named in the exclusion list, by number or label
    live_run    the issue already has a live run
    open_pull_request
                an open pull request says `Closes/Fixes/Resolves #N` or
                `part of #N` for it
    not_ready_unchanged / unchanged_since_run
                its last run ended -- NOT_READY, or any other end -- and the
                issue has not been edited or commented on since that run last
                wrote to it: planning it again would answer the same

The cap on live runs is the caller's: the schedule type's limits.

Pull requests are never candidates: GitHub's issue list includes them, marked,
and the read drops them (`forge.GitHubIssues.sweep_listing`).

"NOT CHANGED SINCE" MEANS SINCE THE RUN'S OWN LAST WRITE. The run's status
comment is itself an edit of the issue, and moves its `updated_at`; compared
with the verdict's time alone, every NOT_READY issue would look edited and be
planned again every half hour. The run records when its write-back last wrote
(`IssueRun.last_writeback_at`), and the issue counts as changed only when
GitHub's `updated_at` is later than that by more than `WRITEBACK_SLACK`
(clock skew between GitHub and this service).

INVARIANTS. The selection itself creates no infrastructure demand: a planner is an
ordinary task, admitted like any other (invariants 1-3), and a run waiting on
its planner or its verdict is a Firestore document. Its reads and writes are
the named tenant's only (invariant 9): its registrations, its token, its runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from swarm_common.models import Tenant

from .forge import ForgeReadError, SweepIssue, SweepListing, open_work_from_listing
from .issueruns import TERMINAL_RUN_STATES, IssueRun, IssueRuns, RunState, planning_refusal
from .validation import IssueRef

log = logging.getLogger(__name__)

#: Labels never swept, whatever the tenant's exclusion list says.
SKIP_LABELS = frozenset({"epic", "blocked", "security"})

#: The only `author_association` values whose issues the sweep starts work
#: for: the repository's owner, a member of its organisation, a collaborator.
#: Security review 2026-10-09 (docs/security/review-2026-10-09.md, item 1).
#: Every swept run is `plan_approval: auto` and `auto_merge: true` and runs
#: with the tenant's forge credential, so without this an issue ANYBODY opened
#: on a public registered repository became an agent run holding that
#: credential and a merged change no person approved -- the planner's
#: readiness verdict and the review step are an agent reading the outsider's
#: own text, not a gate against it. CONTRIBUTOR (someone who once had a commit
#: merged), FIRST_TIME_CONTRIBUTOR, FIRST_TIMER, MANNEQUIN and NONE hold no
#: write access, and an association GitHub did not send is not proof of one.
#: A person may still start a run for any issue from the console, where they
#: see the plan before approving it, or approve it automatically by choice.
TRUSTED_AUTHORS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Live issue runs per tenant a sweep fills up to, unless its caller says.
DEFAULT_MAX_LIVE_RUNS = 8
MAX_LIVE_RUNS_LIMIT = 50

#: The most live runs read to count against the cap. Above it, the tenant is
#: past any cap this allows, and nothing is started.
LIVE_SCAN = 500
#: The most runs of one issue read to find its last one.
ISSUE_HISTORY = 50
#: The read stops at this many seconds from its start and reports what it did
#: not reach (`SweepReport.truncated`), so one slow forge cannot hold a tick.
SWEEP_BUDGET_SECONDS = 240.0
#: An issue counts as changed after a run only when GitHub's `updated_at` is
#: later than the run's last write by more than this: GitHub's clock and ours.
WRITEBACK_SLACK = timedelta(seconds=60)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

#: The most skipped issues the response lists one by one; `skipped_by_reason`
#: counts every one.
MAX_REPORTED_SKIPS = 200


# --------------------------------------------------------------------------
# the filter's settings
# --------------------------------------------------------------------------

_IssueNumber = Annotated[StrictInt, Field(ge=1, le=9_999_999)]
_Label = Annotated[str, Field(min_length=1, max_length=100)]


class SweepConfig(BaseModel):
    """What the filter is asked to honour: built by a schedule type from its
    parameters (`schedtypes.issue_sweep.select`).

    It was also the tenant document's `issue_sweep` settings until the
    route that read them was removed (2026-10-12). `exclude_issues` and
    `exclude_labels` are a deferred set -- e.g. epic #476's children by
    number, or a label they carry. Strict, so `"8"` and `"true"` are refused
    rather than coerced.
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


def _last_write(run: IssueRun) -> datetime:
    """When `run` last changed or last wrote to its issue, whichever is later."""
    seen = run.updated_at
    if run.last_writeback_at is not None and run.last_writeback_at > seen:
        seen = run.last_writeback_at
    return seen


def _changed_since(issue: SweepIssue, run: IssueRun) -> bool:
    """Whether the issue was edited or commented on after `run` last wrote to it."""
    if issue.updated_at is None:
        return False  # GitHub did not say: not proof of a change
    return issue.updated_at > _last_write(run) + WRITEBACK_SLACK


def skip_reason(
    issue: SweepIssue,
    *,
    repository: str,
    config: SweepConfig,
    live: Mapping[tuple[str, int], IssueRun],
    claimed: Mapping[int, int],
    last_run: Callable[[int], IssueRun | None],
    labels_include: frozenset[str] = frozenset(),
    not_ready_cooldown: timedelta | None = None,
    now: datetime | None = None,
) -> str | None:
    """Why the sweep does NOT start a run for `issue`, or None: it is a candidate.

    The rules in the module docstring's order, cheapest first: the last one
    reads Firestore, so it is asked only of an issue every other rule let
    through. `live` is keyed by (lower-cased `owner/repo`, number); `claimed`
    maps an issue number to the open pull request that claims it.

    The last two keywords are the `issue-sweep` schedule type's (docs/
    schedules.md §3.1), and their defaults are the filter's own rules:
    `labels_include` (lower-cased; empty is any label) passes over an issue
    carrying none of them, and `not_ready_cooldown` ends the
    `not_ready_unchanged` skip once that long has passed since the run last
    wrote, so an unchanged NOT_READY issue is planned again; None keeps it
    skipped until the issue changes.
    """
    association = (issue.author_association or "").strip().upper()
    if association not in TRUSTED_AUTHORS:
        return f"author: {association or 'unknown'}"
    labels = {label.lower() for label in issue.labels}
    never = sorted(labels & SKIP_LABELS)
    if never:
        return f"label: {never[0]}"
    if issue.number in config.exclude_issues:
        return "excluded: issue"
    excluded = sorted(labels & set(config.exclude_labels))
    if excluded:
        return f"excluded: label {excluded[0]}"
    if labels_include and not labels & labels_include:
        return "not_included: no listed label"
    running = live.get((repository.lower(), issue.number))
    if running is not None:
        return f"live_run: {running.id}"
    if issue.number in claimed:
        return f"open_pull_request: #{claimed[issue.number]}"
    previous = last_run(issue.number)
    if previous is not None and previous.state in TERMINAL_RUN_STATES:
        if not _changed_since(issue, previous):
            if previous.state == RunState.NOT_READY:
                if (not_ready_cooldown is not None and now is not None
                        and now - _last_write(previous) >= not_ready_cooldown):
                    return None
                return f"not_ready_unchanged: {previous.id}"
            refused = planning_refusal(previous)
            if refused is not None:
                # Refused at planning for what the issue asks: the same plan
                # would be refused again, so it waits for the issue to change.
                return f"unchanged_since_run: {previous.id} (REFUSED: {refused})"
            return f"unchanged_since_run: {previous.id} ({previous.state.value})"
    return None


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

@dataclass
class SweepReport:
    """What one read and filter found: the caller's bookkeeping, filled in by
    `collect_candidates` (skips, failures, truncation) and by the schedule
    type that called it (live runs, repositories)."""

    tenant_id: str
    enabled: bool
    cap: int = DEFAULT_MAX_LIVE_RUNS
    live_runs: int = 0
    repositories: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    #: A repository that could not be swept, by name and error code.
    failures: list[dict[str, str]] = field(default_factory=list)
    #: More work than one sweep reaches: live runs past LIVE_SCAN, or the
    #: time budget spent.
    truncated: bool = False

    def skip(self, issue: str, reason: str) -> None:
        kind = reason.split(":", 1)[0]
        self.skipped_by_reason[kind] = self.skipped_by_reason.get(kind, 0) + 1
        if len(self.skipped) < MAX_REPORTED_SKIPS:
            self.skipped.append({"issue": issue, "reason": reason})


def collect_candidates(
    ctx: Any,
    tenant: Tenant,
    tenant_id: str,
    registrations: list[Mapping[str, Any]],
    *,
    config: SweepConfig,
    report: SweepReport,
    runs: IssueRuns,
    live: Mapping[tuple[str, int], IssueRun],
    clock: Callable[[], float],
    started_at: float,
    labels_include: frozenset[str] = frozenset(),
    not_ready_cooldown: timedelta | None = None,
) -> list[Candidate]:
    """Read each registration's open issues and pull requests, filter them by
    `skip_reason`, and return the candidates oldest-updated first.

    The read and the filter of one sweep, and nothing else: it starts no run
    and writes nothing, to Firestore or to GitHub. The `issue-sweep` and
    `issue-plan-only` schedule types (`swarm_api/schedtypes/`, docs/
    schedules.md §8.1 step 1) both call it, so the readiness rules exist
    once. Skips and per-repository failures go on `report`; running past
    `SWEEP_BUDGET_SECONDS` from `started_at` sets `report.truncated`.
    """
    now = ctx.now()
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
                claimed=claimed, last_run=last_run, labels_include=labels_include,
                not_ready_cooldown=not_ready_cooldown, now=now,
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
    return candidates


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
