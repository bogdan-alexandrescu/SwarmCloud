"""Stranded pull requests: SwarmCloud-opened PRs nothing is going to merge (part of #295).

    GET  /v1/stranded-prs                          the caller's tenant (an admin: all theirs)
    POST /v1/admin/stranded-prs/sweep?tenant_id=   the Cloud Scheduler job, every 30 minutes

WHY THIS EXISTS (measured 2026-10-10 across the eng tenant's repositories).
40 pull requests SwarmCloud opened on 2026-10-09 were left open, and nobody
was told. 30 had NO merge step at all: a plugin implement -> review -> fix
workflow ends at the pull request, a manual issue run defaults to
`auto_merge: false`, and one repository has no auto-merge workflow. 7 merge
steps failed `behind_too_often`: the ruleset requires a branch to be up to
date, 16 merge steps ran against main at once, each merge that landed put the
rest behind, and a step then updated its branch at most
`agent_worker.merge.MERGE_MAX_BRANCH_UPDATES` (3 then; 5 since 2026-10-10,
counted only while the step holds its repository's merge slot) times. A user-owned
repository has no GitHub merge queue (docs/ci.md). Nothing on the platform
looked at an open pull request after the run that opened it ended.

So this module looks. For each registered repository of the named tenant it
lists the open pull requests, keeps the ones THIS TENANT'S tasks opened, and
classifies every one open longer than `STRANDED_AFTER` (2 hours):

    held           labelled `hold`: reported, NEVER acted on, whatever else is true
    conflict       GitHub says it does not merge cleanly into its base
    checks_red     a required check (or, with none required, any check) is red at the head
    ci_never_ran   nothing CI reported at the head: none of the required checks, or --
                   with none required -- no check run and no status at all. Two hours
                   after the head was pushed, a CI that has not started will not
    behind         the base moved and the ruleset requires an up-to-date branch
    merge_failed   a merge step for it ended without merging (its refusal, from the worker)
    no_merge_step  no merge step was ever submitted for it

in that order: the first that holds is the row's reason, because each is
what stands between the pull request and a merge before the next could
matter. A conflict is not fixed by a merge step, nor red CI by a rebase. A
`behind` pull request whose last merge step failed `behind_too_often` is
`behind`, with the failure in its detail, because a new merge step is what
lands it -- the seven above. A pull request with a merge step still LIVE is
not stranded (the step is working on it) unless it is held, which is
reported however it got there.

Each row says which action would land it (`remedy`):

    merge_pr   submit a `merge_pr` workflow (behind, merge_failed, no_merge_step)
    fix_ci     a CI fix round, or a person (checks_red, ci_never_ran)
    rebase     resolve the conflict onto the base (conflict)
    none       a person decided it waits (held)

WHICH PULL REQUESTS ARE SWARMCLOUD'S, AND THIS TENANT'S (invariant 9). The
worker pushes `swarm/<task id>` (`agent_worker.continuation.publish_branch`,
`cifix.BRANCH_PREFIX`); a continuation pushes the branch of the task it
continues, so a fix round's pull request is its root's. With the console
links switched on, the body also carries `/agents/live/<task id>`
(`agent_worker.lifecycle.pr_body_with_console_links`). A pull request is the
tenant's only when the task its branch -- or failing that its body -- names
EXISTS IN THIS TENANT (`Store.tasks_by_id`, tenant-checked). Two tenants may
register one repository; each sees only the pull requests its own tasks
opened, and a body naming another tenant's task names nothing here.

READ WITH THE TENANT'S OWN TOKEN, NO NEW CREDENTIAL. The pull request, the
base branch's rules, the check runs and statuses at the head are read with
`ctx.forge_tokens.token_for(tenant)` -- the `-git` secret the merge step and
the issue-run CI loop already read -- through `forgewrite.GitHubWriter`, the
same pinned, redirect-refusing client, and judged by `forgechecks.evaluate`,
the issue run's port of the merge step's own rule. Read once per tenant per
sweep and dropped at the end.

MERGE STEPS ARE FOUND IN THE TENANT'S TASKS. One indexed query (tenant,
runner_profile = merge, newest first; firestore index for GET
/v1/tasks?runner_profile=) reads up to `MERGE_SCAN` merge tasks. Each names
its pull request by number (`result_summary.merge.pull_request` once the
worker read it, or the signed `merge_target.number` of a `merge_pr`
workflow) or by the task that opened it (`merge_target.pull_request`, whose
branch is `swarm/<that task>`).

WHAT THE SWEEP WRITES. One document per tenant, `stranded_prs/<tenant>`:
the rows, when they were read, and when each pull request was last logged.
GET serves that document and reads nothing from GitHub, so the console can
poll it at no cost to anyone's rate limit.

ONE `pr_stranded` LOG ENTRY PER PULL REQUEST PER `LOG_EVERY` (6 hours).
`{"event": "pr_stranded", ...}` at WARNING, which the log-based metric
`<prefix>/pr-stranded` counts and an alert pages on
(terraform/modules/monitoring/stranded_prs.tf). The sweep runs every 30
minutes; logging every row every time would page twelve times for one
forgotten pull request. A pull request no longer stranded is forgotten, so
one stranded again is logged at once.

REDRIVE (`redrive: true`, never from the scheduler). For each `no_merge_step`
or `behind` row whose checks are GREEN at the head, submit the `merge_pr`
workflow (`continuation.resolve_merge_pr`, #352) at exactly that head, as the
member who submitted the opening task, and only while they are still a
member of the tenant (asked of the directory on each submission, as
`mergewake._submitter` does). NEVER a held pull request, never one whose
checks are red, pending or unread. AT MOST `REDRIVE_PER_REPOSITORY` (1) per
repository per sweep, and none in a repository where a merge step is live:
the 16 concurrent merge steps that each put the rest behind are the failure
this exists to recover from, and redriving every row at once would repeat
it. The next sweep takes the next one.

INVARIANTS. The sweep itself creates no infrastructure demand (1-3): a
redriven merge step is an ordinary task, admitted like any other. Every read
and write is the named tenant's (9). A redrive names a runner profile by name
and a pull request by number and sha, nothing else (10).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from swarm_common.identity import Principal
from swarm_common.states import TERMINAL_STATES

from .auth import AuthContext
from .errors import ApiError, NotFound
from .forge import NoForgeCredential, neutral_line
from .forgechecks import GREEN, NONE, RED, evaluate, required_status_checks
from .forgewrite import GitHubWriter
from .repositories import Repositories
from .schemas import WorkflowCreate
from .validation import MERGE_STEP_ID, MERGE_TARGET_FIELD, IssueRef, merge_repository

log = logging.getLogger(__name__)

#: A pull request open this long, with nothing going to merge it, is stranded.
#: Two hours: a direct-pr run's CI and its merge step finish well inside it,
#: so a younger pull request is most likely still being worked on.
STRANDED_AFTER = timedelta(hours=2)
#: The most often one pull request is logged as `pr_stranded`. The sweep runs
#: every 30 minutes; the alert should page once per forgotten pull request
#: per working session, not twelve times a day.
LOG_EVERY = timedelta(hours=6)
#: The label a person puts on a pull request to say "leave it": reported,
#: never acted on.
HOLD_LABEL = "hold"

#: The log entry's `event`, which the log-based metric matches on
#: (terraform/modules/monitoring/stranded_prs.tf). Renaming it silences the alert.
EVENT = "pr_stranded"

HELD, CONFLICT, CHECKS_RED, CI_NEVER_RAN = "held", "conflict", "checks_red", "ci_never_ran"
BEHIND, MERGE_FAILED, NO_MERGE_STEP = "behind", "merge_failed", "no_merge_step"
#: Every reason, in the order a pull request is tested for them.
REASONS = (HELD, CONFLICT, CHECKS_RED, CI_NEVER_RAN, BEHIND, MERGE_FAILED, NO_MERGE_STEP)

MERGE_PR, FIX_CI, REBASE, NO_REMEDY = "merge_pr", "fix_ci", "rebase", "none"
REMEDIES = (MERGE_PR, FIX_CI, REBASE, NO_REMEDY)
#: Which action would land a pull request stranded for each reason.
REMEDY = {
    HELD: NO_REMEDY,
    CONFLICT: REBASE,
    CHECKS_RED: FIX_CI,
    CI_NEVER_RAN: FIX_CI,
    BEHIND: MERGE_PR,
    MERGE_FAILED: MERGE_PR,
    NO_MERGE_STEP: MERGE_PR,
}
#: The reasons a redrive may submit a merge for, and only with green checks.
#: `merge_failed` is not one: a merge step already looked and refused, and
#: its refusal is a person's to read before another is spent on it.
REDRIVABLE = frozenset({NO_MERGE_STEP, BEHIND})

#: One document per tenant: the last sweep's rows and the log dedupe.
COLLECTION = "stranded_prs"

#: The worker's branch for a task (`cifix.BRANCH_PREFIX` + `continuation.TASK_ID_RE`).
_BRANCH = re.compile(r"^swarm/(task_[0-9a-f]{20})$")
#: The console's agent link the worker appends to a body when the switch is on.
_BODY_TASK = re.compile(r"/agents/live/(task_[0-9a-f]{20})\b")

#: Pages of open pull requests read per repository, 100 each.
MAX_PULL_PAGES = 3
PAGE_SIZE = 100
#: Registrations read per sweep.
MAX_REPOSITORIES = 100
#: Merge tasks read per sweep, newest first, in pages of MERGE_PAGE.
MERGE_SCAN = 500
MERGE_PAGE = 100
#: Rows stored and served per tenant. Past it, `truncated` says so.
MAX_ROWS = 300
#: Redriven merges per repository per sweep (module docstring, REDRIVE).
REDRIVE_PER_REPOSITORY = 1
#: The sweep stops READING GitHub at this many seconds -- the Cloud Scheduler
#: job's deadline is 300 s -- and reports `truncated`.
SWEEP_BUDGET_SECONDS = 240.0
#: Bounds on text read from GitHub or a worker's refusal, as stored and served.
MAX_TITLE_CHARS = 200
MAX_DETAIL_CHARS = 500

#: What the read needs of the credential, named in a 403's message.
PULLS_READ = "pull_requests: read"

#: GitHub's `mergeable_state` values that are verdicts this module reads.
_DIRTY, _BEHIND = "dirty", "behind"


# --------------------------------------------------------------------------
# what is read, and the pure classification
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenPull:
    """One open pull request as the listing and its own read report it."""

    number: int
    title: str
    head_ref: str
    head_sha: str
    base_ref: str
    labels: tuple[str, ...]
    created_at: datetime | None
    url: str = ""
    #: `owner/repo` of the head; "" when GitHub named none (a deleted fork).
    head_repo: str = ""
    body: str = ""


@dataclass(frozen=True)
class MergeStep:
    """One merge task that named this pull request."""

    task_id: str
    live: bool
    #: The worker recorded that it merged (or found it already merged).
    merged: bool
    #: Why it did not merge: the worker's refusal, else the task's error,
    #: else its end state. "" while live or merged.
    error: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class CiFacts:
    """What CI says at the head: `forgechecks.evaluate`'s state, and whether anything ran."""

    #: green | pending | red | none, or "unread" when the read failed.
    state: str
    never_ran: bool = False
    failing: tuple[str, ...] = ()
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    reason: str
    detail: str
    remedy: str


def opener_task_id(pull: OpenPull) -> str | None:
    """The task id the pull request's branch -- or failing that its body -- names."""
    match = _BRANCH.match(pull.head_ref or "")
    if match:
        return match.group(1)
    found = _BODY_TASK.search(pull.body or "")
    return found.group(1) if found else None


def ci_facts(
    rules: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
) -> CiFacts:
    """The reading at one sha, plus whether CI reported anything there at all.

    `never_ran` is the half `evaluate` cannot say: it reads an unreported
    required check as PENDING, which is right while CI may still start. With
    required checks, CI never ran when not one of them has a check run or a
    commit status at the head -- whatever other apps (a labeler, a CLA bot)
    reported. With none required, when nothing at all reported (`none`).
    """
    required = required_status_checks(rules)
    reading = evaluate(required, runs, statuses)
    names = tuple(sorted({check.context for check in required}))
    if required:
        reported = {r.get("name") for r in runs if isinstance(r, Mapping)}
        reported |= {s.get("context") for s in statuses if isinstance(s, Mapping)}
        never_ran = not any(name in reported for name in names)
    else:
        never_ran = reading.state == NONE
    return CiFacts(
        state=reading.state, never_ran=never_ran,
        failing=tuple(reading.failing_names()), required=names,
    )


def _short(sha: str) -> str:
    return (sha or "")[:12] or "(no head)"


def classify(
    pull: OpenPull,
    *,
    mergeable_state: str,
    mergeable: bool | None,
    ci: CiFacts,
    merges: Sequence[MergeStep],
) -> Verdict | None:
    """Why `pull` is stranded, in REASONS order, or None when a merge step is live on it.

    Pure: everything it reads is an argument. Age is the caller's test.
    """
    head = _short(pull.head_sha)
    labels = {label.lower() for label in pull.labels}
    if HOLD_LABEL in labels:
        return Verdict(HELD, f"labelled `{HOLD_LABEL}`: reported, never acted on", REMEDY[HELD])
    if any(step.live for step in merges):
        return None
    ended = sorted(
        (step for step in merges if not step.live and not step.merged),
        key=lambda step: (step.created_at is None, step.created_at or _EPOCH),
    )
    last_failure = ended[-1] if ended else None
    failure_note = (
        f"; its last merge step {last_failure.task_id} ended: {last_failure.error}"
        if last_failure is not None else ""
    )
    if mergeable_state == _DIRTY or mergeable is False:
        return Verdict(
            CONFLICT,
            f"it does not merge cleanly into {pull.base_ref or 'its base'} at {head}",
            REMEDY[CONFLICT],
        )
    if ci.state == RED:
        return Verdict(
            CHECKS_RED,
            f"CI is red at {head}: " + (", ".join(ci.failing) or "a check failed"),
            REMEDY[CHECKS_RED],
        )
    if ci.state != "unread" and ci.never_ran:
        what = (
            "none of the required checks (" + ", ".join(ci.required) + ")"
            if ci.required else "no check run and no commit status"
        )
        return Verdict(CI_NEVER_RAN, f"{what} reported at {head}", REMEDY[CI_NEVER_RAN])
    if mergeable_state == _BEHIND:
        return Verdict(
            BEHIND,
            f"{pull.base_ref or 'its base'} moved past it and the branch must be up to date; "
            f"CI is {ci.state} at {head}{failure_note}",
            REMEDY[BEHIND],
        )
    if last_failure is not None:
        return Verdict(
            MERGE_FAILED,
            f"merge step {last_failure.task_id} ended without merging: {last_failure.error}",
            REMEDY[MERGE_FAILED],
        )
    return Verdict(
        NO_MERGE_STEP,
        f"no merge step was ever submitted for it; CI is {ci.state} at {head}",
        REMEDY[NO_MERGE_STEP],
    )


def redrive_refusal(row: "StrandedRow") -> str:
    """Why a row is NOT redriven, or "" when it may be. Held and red first, always."""
    if row.reason == HELD:
        return "held"
    if row.checks == RED:
        return "checks_red"
    if row.reason not in REDRIVABLE:
        return f"reason_{row.reason}"
    if row.checks != GREEN:
        return f"checks_{row.checks}"
    if not row.head_sha:
        return "no_head"
    return ""


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _github_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def age_hours(opened_at: datetime | None, now: datetime) -> float | None:
    opened = _aware(opened_at)
    if opened is None:
        return None
    return round(max(0.0, (now - opened).total_seconds()) / 3600.0, 1)


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

@dataclass
class StrandedRow:
    tenant_id: str
    repo: str
    number: int
    title: str
    url: str
    opened_at: datetime | None
    reason: str
    detail: str
    head_sha: str
    remedy: str
    #: CI's state at the head (green | pending | red | none | unread).
    checks: str
    #: The task whose branch or body names it: whose submitter a redrive is made as.
    opened_by_task: str

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    def to_doc(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "number": self.number, "title": self.title, "url": self.url,
            "opened_at": self.opened_at, "reason": self.reason, "detail": self.detail,
            "head_sha": self.head_sha, "remedy": self.remedy, "checks": self.checks,
            "opened_by_task": self.opened_by_task,
        }

    @classmethod
    def from_doc(cls, tenant_id: str, doc: Mapping[str, Any]) -> "StrandedRow":
        number = doc.get("number")
        return cls(
            tenant_id=tenant_id,
            repo=str(doc.get("repo") or ""),
            number=number if isinstance(number, int) and not isinstance(number, bool) else 0,
            title=str(doc.get("title") or ""),
            url=str(doc.get("url") or ""),
            opened_at=_aware(doc.get("opened_at")),
            reason=str(doc.get("reason") or ""),
            detail=str(doc.get("detail") or ""),
            head_sha=str(doc.get("head_sha") or ""),
            remedy=str(doc.get("remedy") or NO_REMEDY),
            checks=str(doc.get("checks") or ""),
            opened_by_task=str(doc.get("opened_by_task") or ""),
        )

    def to_api(self, now: datetime) -> dict[str, Any]:
        """The console's row (a later lane renders it): flat, every field always present."""
        opened = _aware(self.opened_at)
        return {
            "tenant_id": self.tenant_id,
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "opened_at": opened.isoformat() if opened else None,
            "age_hours": age_hours(opened, now),
            "reason": self.reason,
            "detail": self.detail,
            "head_sha": self.head_sha,
            "remedy": self.remedy,
            "checks": self.checks,
            "opened_by_task": self.opened_by_task,
        }


@dataclass
class SweepReport:
    tenant_id: str
    swept_at: datetime
    redrive: bool
    repositories: int = 0
    rows: list[StrandedRow] = field(default_factory=list)
    #: Open SwarmCloud pull requests of this tenant with a merge step live on them.
    in_flight: int = 0
    #: Pull requests logged as `pr_stranded` by this sweep (the rest were
    #: logged inside LOG_EVERY).
    logged: list[str] = field(default_factory=list)
    redriven: list[dict[str, Any]] = field(default_factory=list)
    not_redriven: list[dict[str, str]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False

    def to_api(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "swept_at": self.swept_at.isoformat(),
            "redrive": self.redrive,
            "repositories": self.repositories,
            "stranded": [row.to_api(self.swept_at) for row in self.rows],
            "by_reason": _by_reason(self.rows),
            "in_flight": self.in_flight,
            "logged": list(self.logged),
            "redriven": list(self.redriven),
            "not_redriven": list(self.not_redriven),
            "failures": list(self.failures),
            "truncated": self.truncated,
        }


def _by_reason(rows: Iterable[StrandedRow]) -> dict[str, int]:
    counts = {reason: 0 for reason in REASONS}
    for row in rows:
        counts[row.reason] = counts.get(row.reason, 0) + 1
    return counts


# --------------------------------------------------------------------------
# merge steps, from the tenant's tasks
# --------------------------------------------------------------------------

def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _merge_error(task: Any, summary: Mapping[str, Any]) -> str:
    refusal = _mapping(summary.get("refusal"))
    if refusal:
        return f"{refusal.get('code')}: {refusal.get('message') or ''}".rstrip(": ")
    if getattr(task, "last_error", None):
        return str(task.last_error)
    state = getattr(task, "state", None)
    return f"it ended {getattr(state, 'value', state)}"


def merge_index(tasks: Iterable[Any]) -> dict[tuple[str, str, Any], list[MergeStep]]:
    """Each merge task under every key it names a pull request by.

    `("n", repo, number)` for one naming the pull request by number;
    `("b", repo, "swarm/<task>")` for one naming the task that opened it.
    `repo` is `owner/repo`, lower-cased.
    """
    index: dict[tuple[str, str, Any], list[MergeStep]] = {}
    for task in tasks:
        metadata = _mapping(getattr(task, "metadata", None))
        target = _mapping(_mapping(metadata.get("dispatch")).get(MERGE_TARGET_FIELD))
        summary = _mapping(_mapping(getattr(task, "result_summary", None)).get("merge"))
        repository = summary.get("repository")
        if not isinstance(repository, str) or "/" not in repository:
            pair = merge_repository(getattr(task, "repository_url", None))
            repository = f"{pair[0]}/{pair[1]}" if pair else ""
        if not repository:
            continue
        repo = repository.lower()
        state = getattr(task, "state", None)
        live = state not in TERMINAL_STATES
        merged = bool(summary.get("merged_by_this_task") or summary.get("already_merged"))
        step = MergeStep(
            task_id=str(getattr(task, "id", "")),
            live=live,
            merged=merged,
            error="" if live or merged else neutral_line(
                _merge_error(task, summary), MAX_DETAIL_CHARS),
            created_at=_aware(getattr(task, "created_at", None)),
        )
        keys: set[tuple[str, str, Any]] = set()
        number = _int(summary.get("pull_request")) or _int(target.get("number"))
        if number is not None:
            keys.add(("n", repo, number))
        opener = target.get("pull_request")
        if isinstance(opener, str) and opener:
            keys.add(("b", repo, f"swarm/{opener}"))
        for key in keys:
            index.setdefault(key, []).append(step)
    return index


def merges_for(index: Mapping[tuple[str, str, Any], list[MergeStep]], repo: str,
               pull: OpenPull) -> list[MergeStep]:
    seen: dict[str, MergeStep] = {}
    for key in (("n", repo.lower(), pull.number), ("b", repo.lower(), pull.head_ref)):
        for step in index.get(key, []):
            seen.setdefault(step.task_id, step)
    return list(seen.values())


def _merge_tasks(ctx: Any, tenant_id: str) -> tuple[list[Any], bool]:
    """Up to MERGE_SCAN of the tenant's merge tasks, newest first; True when cut."""
    found: list[Any] = []
    token: str | None = None
    while len(found) < MERGE_SCAN:
        page = ctx.store.list_tasks(
            tenant_id, runner_profile=MERGE_STEP_ID, limit=MERGE_PAGE,
            page_token=token, submitted_by=None,
        )
        found.extend(page.items)
        token = page.next_page_token
        if not token:
            return found, False
    return found[:MERGE_SCAN], True


# --------------------------------------------------------------------------
# GitHub, through the writer's client and the tenant's token
# --------------------------------------------------------------------------

def _labels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names = []
    for entry in value:
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if isinstance(name, str):
            names.append(name)
    return tuple(names)


def _open_pull(entry: Mapping[str, Any]) -> OpenPull | None:
    number = _int(entry.get("number"))
    if number is None:
        return None
    head, base = _mapping(entry.get("head")), _mapping(entry.get("base"))
    body = entry.get("body")
    url = entry.get("html_url")
    return OpenPull(
        number=number,
        title=str(entry.get("title") or ""),
        head_ref=str(head.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        base_ref=str(base.get("ref") or ""),
        labels=_labels(entry.get("labels")),
        created_at=_github_time(entry.get("created_at")),
        url=url if isinstance(url, str) else "",
        head_repo=str(_mapping(head.get("repo")).get("full_name") or ""),
        body=body if isinstance(body, str) else "",
    )


def list_open_pulls(writer: GitHubWriter, ref: IssueRef, token: str) -> tuple[list[OpenPull], bool]:
    """The repository's open pull requests, oldest first; True when more than were read.

    Through the writer's own request path -- the pinned host, the token in
    the Authorization header only, its status mapping -- as
    `mergewake.in_merge_queue` reads what the writer has no method for.
    """
    pulls: list[OpenPull] = []
    what = f"the open pull requests of {ref.repository}"
    for page in range(1, MAX_PULL_PAGES + 1):
        entries = writer._call(
            "GET",
            writer._url(ref, "pulls?state=open&sort=created&direction=asc"
                             f"&per_page={PAGE_SIZE}&page={page}"),
            token, what, needs=PULLS_READ,
        )
        if not isinstance(entries, list):
            raise ApiError(f"GitHub's answer for {what} is not a list")
        for entry in entries:
            pull = _open_pull(entry) if isinstance(entry, Mapping) else None
            if pull is not None:
                pulls.append(pull)
        if len(entries) < PAGE_SIZE:
            return pulls, False
    return pulls, True


def read_mergeable(writer: GitHubWriter, ref: IssueRef, number: int,
                   token: str) -> tuple[str, bool | None, str]:
    """GitHub's `mergeable_state`, `mergeable` and head sha for one pull request.

    Only the single-pull read carries them; the listing does not. `unknown`
    while GitHub is still computing, which reads as neither conflict nor
    behind -- the next sweep reads it again.
    """
    data = writer._call(
        "GET", writer._url(ref, f"pulls/{int(number)}"), token,
        f"{ref.repository}#{int(number)}", needs=PULLS_READ,
    )
    data = _mapping(data)
    state = data.get("mergeable_state")
    mergeable = data.get("mergeable")
    return (
        state if isinstance(state, str) else "unknown",
        mergeable if isinstance(mergeable, bool) else None,
        str(_mapping(data.get("head")).get("sha") or ""),
    )


# --------------------------------------------------------------------------
# the stored document
# --------------------------------------------------------------------------

def _doc(db: Any, tenant_id: str) -> Any:
    return db.collection(COLLECTION).document(tenant_id)


def read_stored(db: Any, tenant_id: str) -> dict[str, Any]:
    snap = _doc(db, tenant_id).get()
    return (snap.to_dict() or {}) if snap.exists else {}


def stored_rows(db: Any, tenant_id: str) -> tuple[list[StrandedRow], dict[str, Any]]:
    """The last sweep's rows for one tenant, and its stamp (`swept_at`, `truncated`, `failures`)."""
    stored = read_stored(db, tenant_id)
    rows = [
        StrandedRow.from_doc(tenant_id, doc)
        for doc in stored.get("rows") or []
        if isinstance(doc, Mapping)
    ]
    swept = _aware(stored.get("swept_at"))
    return rows, {
        "tenant_id": tenant_id,
        "swept_at": swept.isoformat() if swept else None,
        "truncated": bool(stored.get("truncated")),
        "failures": [dict(f) for f in stored.get("failures") or [] if isinstance(f, Mapping)],
    }


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

class SubmitterNotMember(Exception):
    """The opening task's submitter is no longer a member: nothing is submitted for them."""

    code = "submitter_not_member"


def _submitter(ctx: Any, tenant: Any, task: Any) -> AuthContext:
    """Who a redriven merge is submitted as: the opening task's submitter, while a member.

    `mergewake._submitter`'s shape: built from the stored task and tenant,
    never the caller; nothing wider than an ordinary member; membership asked
    of the directory again on every submission (invariant 9).
    """
    email = str(getattr(task, "submitted_by", "") or "").strip().lower()
    if not email or not ctx.authenticator.is_tenant_member(email, tenant):
        raise SubmitterNotMember(
            f"the opening task's submitter {email or '(not recorded)'} is not a current "
            f"member of tenant {tenant.tenant_id!r}"
        )
    return AuthContext(
        principal=Principal(
            email=email,
            # Not a token subject: this context was never authenticated.
            subject=f"stranded-pr:{getattr(task, 'id', '')}",
            domain=email.rsplit("@", 1)[-1],
            groups=(),
        ),
        tenant_id=tenant.tenant_id,
        is_admin=False,
        tenant_principal=tenant.principal,
    )


def merge_pr_workflow(repository_url: str, number: int, head_sha: str) -> WorkflowCreate:
    """ONE merge step merging pull request `number` at `head_sha` (#352's shape)."""
    return WorkflowCreate.model_validate({
        "strategy": "direct-pr",
        "repository_url": repository_url,
        "merge_pr": {"number": number, "head_sha": head_sha},
        "steps": [{"step_id": MERGE_STEP_ID, "runner_profile": MERGE_STEP_ID}],
        "metadata": {"stranded_pr_redrive": {"pull_request": number, "head_sha": head_sha}},
    })


def _log_stranded(row: StrandedRow, now: datetime) -> None:
    log.warning(
        "pr_stranded tenant=%s %s reason=%s remedy=%s", row.tenant_id, row.key,
        row.reason, row.remedy,
        extra={
            "event": EVENT,
            "tenant_id": row.tenant_id,
            "repository": row.repo,
            "number": row.number,
            "reason": row.reason,
            "remedy": row.remedy,
            "age_hours": age_hours(row.opened_at, now),
            "head_sha": row.head_sha,
            "detail": row.detail,
        },
    )


def sweep_tenant(
    ctx: Any,
    tenant_id: str,
    *,
    redrive: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> SweepReport:
    """One sweep of one tenant: read, classify, store, log, and -- asked -- redrive."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    now = _aware(ctx.now()) or datetime.now(timezone.utc)
    report = SweepReport(tenant_id=tenant_id, swept_at=now, redrive=redrive)
    started = clock()

    registrations, more = Repositories(ctx.db, now=ctx.now).list(tenant_id, limit=MAX_REPOSITORIES)
    report.truncated = more is not None
    report.repositories = len(registrations)
    tasks, cut = _merge_tasks(ctx, tenant_id)
    report.truncated = report.truncated or cut
    index = merge_index(tasks)
    #: Repositories with a merge step live right now: no redrive there.
    busy = {key[1] for key, steps in index.items() if any(s.live for s in steps)}
    openers: dict[str, Any] = {}
    urls: dict[str, str] = {}

    writer: GitHubWriter | None = getattr(ctx, "forge_writer", None)
    tokens = getattr(ctx, "forge_tokens", None)
    token = ""
    try:
        if registrations and (writer is None or tokens is None):
            report.failures.append({"repository": "*", "error": "forge_unconfigured"})
            registrations = []
        elif registrations:
            try:
                token = tokens.token_for(tenant)
            except NoForgeCredential as refused:
                report.failures.append({"repository": "*", "error": refused.code})
                registrations = []
        for registration in registrations:
            if clock() - started > SWEEP_BUDGET_SECONDS:
                report.truncated = True
                break
            owner, name = str(registration.get("owner") or ""), str(registration.get("repo") or "")
            repo = f"{owner}/{name}"
            if registration.get("archived"):
                continue
            urls[repo] = str(registration.get("repository_url") or f"https://github.com/{repo}")
            ref = IssueRef(owner=owner, repo=name, number=1)  # names the repository only
            try:
                pulls, pulls_cut = list_open_pulls(writer, ref, token)
            except Exception as exc:  # noqa: BLE001 -- one repository's failure is its own
                report.failures.append({"repository": repo, "error": _code(exc)})
                continue
            report.truncated = report.truncated or pulls_cut
            named = {pull.number: opener_task_id(pull) for pull in pulls}
            found = ctx.store.tasks_by_id(tenant_id, [t for t in named.values() if t])
            openers.update(found)
            rules_by_base: dict[str, list[dict[str, Any]]] = {}
            for pull in pulls:
                task_id = named.get(pull.number)
                if not task_id or task_id not in found:
                    # Not SwarmCloud's, or another tenant's task (invariant 9).
                    continue
                if pull.created_at is None or now - pull.created_at < STRANDED_AFTER:
                    continue
                if clock() - started > SWEEP_BUDGET_SECONDS:
                    report.truncated = True
                    break
                merges = merges_for(index, repo, pull)
                if any(step.live for step in merges) and HOLD_LABEL not in {
                    label.lower() for label in pull.labels
                }:
                    report.in_flight += 1
                    continue
                verdict, ci, head = _judge(writer, ref, pull, merges, token, rules_by_base,
                                           report, repo)
                if verdict is None:
                    report.in_flight += 1
                    continue
                if len(report.rows) >= MAX_ROWS:
                    report.truncated = True
                    continue
                report.rows.append(StrandedRow(
                    tenant_id=tenant_id, repo=repo, number=pull.number,
                    title=neutral_line(pull.title, MAX_TITLE_CHARS, literals=(token,)),
                    url=pull.url, opened_at=pull.created_at, reason=verdict.reason,
                    detail=neutral_line(verdict.detail, MAX_DETAIL_CHARS, literals=(token,)),
                    head_sha=head, remedy=verdict.remedy, checks=ci.state,
                    opened_by_task=task_id,
                ))
    finally:
        token = ""

    _store_and_log(ctx.db, report, now)
    if redrive:
        _redrive(ctx, tenant, report, busy, openers, urls)
    log.info(
        "stranded-pr sweep tenant=%s repositories=%d stranded=%s in_flight=%d logged=%d "
        "redriven=%d failures=%d truncated=%s", tenant_id, report.repositories,
        _by_reason(report.rows), report.in_flight, len(report.logged), len(report.redriven),
        len(report.failures), report.truncated,
    )
    return report


def _code(exc: Exception) -> str:
    return exc.code if isinstance(exc, ApiError) else type(exc).__name__


def _judge(
    writer: GitHubWriter, ref: IssueRef, pull: OpenPull, merges: list[MergeStep], token: str,
    rules_by_base: dict[str, list[dict[str, Any]]], report: SweepReport, repo: str,
) -> tuple[Verdict | None, CiFacts, str]:
    """Read the pull request's merge state and its CI, then classify it."""
    pref = IssueRef(owner=ref.owner, repo=ref.repo, number=pull.number)
    head = pull.head_sha
    try:
        state, mergeable, read_head = read_mergeable(writer, pref, pull.number, token)
        head = read_head or head
    except Exception as exc:  # noqa: BLE001 -- one pull request's failure is its own
        report.failures.append({"repository": repo, "number": str(pull.number),
                                "error": _code(exc)})
        state, mergeable = "unknown", None
    ci = CiFacts(state="unread")
    if head:
        try:
            if pull.base_ref and pull.base_ref not in rules_by_base:
                rules_by_base[pull.base_ref] = writer.branch_rules(pref, pull.base_ref, token)
            ci = ci_facts(
                rules_by_base.get(pull.base_ref, []),
                writer.check_runs(pref, head, token),
                writer.commit_statuses(pref, head, token),
            )
        except Exception as exc:  # noqa: BLE001 -- CI unread is a reading of its own
            report.failures.append({"repository": repo, "number": str(pull.number),
                                    "error": _code(exc)})
    verdict = classify(pull, mergeable_state=state, mergeable=mergeable, ci=ci, merges=merges)
    return verdict, ci, head


def _store_and_log(db: Any, report: SweepReport, now: datetime) -> None:
    """Write the tenant's document; log each row not logged inside LOG_EVERY."""
    stored = read_stored(db, report.tenant_id)
    previous = _mapping(stored.get("logged"))
    logged: dict[str, Any] = {}
    for row in report.rows:
        last = _aware(previous.get(row.key))
        if last is not None and now - last < LOG_EVERY:
            logged[row.key] = last
            continue
        _log_stranded(row, now)
        logged[row.key] = now
        report.logged.append(row.key)
    # A pull request no longer stranded drops out of `logged`, so one that is
    # stranded again is logged at once rather than after the window.
    _doc(db, report.tenant_id).set({
        "tenant_id": report.tenant_id,
        "swept_at": now,
        "rows": [row.to_doc() for row in report.rows],
        "failures": list(report.failures),
        "truncated": report.truncated,
        "logged": logged,
    })


def _redrive(ctx: Any, tenant: Any, report: SweepReport, busy: set[str],
             openers: Mapping[str, Any], urls: Mapping[str, str]) -> None:
    per_repo: dict[str, int] = {}
    # Oldest first: the pull request waiting longest is landed first.
    for row in sorted(report.rows, key=lambda r: (r.opened_at is None, r.opened_at or _EPOCH)):
        refusal = redrive_refusal(row)
        if not refusal and row.repo.lower() in busy:
            refusal = "repository_has_a_live_merge"
        if not refusal and per_repo.get(row.repo, 0) >= REDRIVE_PER_REPOSITORY:
            refusal = "one_merge_per_repository_per_sweep"
        if refusal:
            report.not_redriven.append({"pull_request": row.key, "reason": refusal})
            continue
        try:
            owner = _submitter(ctx, tenant, openers[row.opened_by_task])
            submission = ctx.submissions.submit_workflow(
                owner, merge_pr_workflow(urls[row.repo], row.number, row.head_sha),
            )
        except Exception as exc:  # noqa: BLE001 -- one refusal is its own
            code = getattr(exc, "code", None) or type(exc).__name__
            log.warning("stranded-pr redrive tenant=%s %s refused (%s)",
                        row.tenant_id, row.key, code)
            report.not_redriven.append({"pull_request": row.key, "reason": f"refused: {code}"})
            continue
        per_repo[row.repo] = per_repo.get(row.repo, 0) + 1
        workflow_id = submission.workflow.workflow_id
        report.redriven.append({"pull_request": row.key, "head_sha": row.head_sha,
                                "workflow_id": workflow_id})
        log.info("stranded-pr redrive tenant=%s %s at %s submitted as %s",
                 row.tenant_id, row.key, row.head_sha[:12], workflow_id)

