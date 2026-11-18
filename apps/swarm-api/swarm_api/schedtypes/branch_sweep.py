"""`branch-sweep`: delete the branches a merged pull request left behind (docs/schedules.md §3.19).

The owner asked on 2026-11-02 for "a scheduled job that comes and double
checks for leftovers or zombie branches left behind and failed to delete".
That night the SwarmCloud repository held 4,756 branches: 2,228 whose pull
request merged, 2,406 with no pull request, 107 closed unmerged, 15 open.
Merges are squashed, so git ancestry called only 13 of them merged: A MERGED
BRANCH IS KNOWN BY ITS PULL REQUEST, never by `git branch --merged`.

AN API ACTION, NO AGENT (SD8, §2.7), like `cost-report`. A firing creates no
task, takes no capacity and needs no worker (invariants 1-3).

THE RULE (the owner's, 2026-11-02). A branch is deleted only when a pull
request from it (from this repository, never a fork's same-named branch)
MERGED and the branch's head is still that pull request's head commit:
nothing was pushed after the merge. NEVER deleted, whatever else holds:

  * the default branch, a branch GitHub marks protected, a branch under
    `keep/` (`protected_or_keep`);
  * a branch with an open pull request (`open_pull_request`);
  * a branch a live task or workflow still reads (`in_use`): the branch of a
    task that has not ended, of a task a live task depends on (a step's
    hand-off), continues (`metadata.dispatch.continues`) or names anywhere in
    its dispatch metadata (an integrator's contributors), and a live task's
    `repository_ref`. Branches are `swarm/<task id>`; any task id in a branch's
    name that is one of those keeps it.

Everything else is KEPT and only counted, by kind: `no_pull_request`,
`closed_unmerged`, `head_moved_after_merge`. A branch the rule allows but
the firing could not prove safe -- the repository's open pull requests or
the tenant's live tasks were not read whole -- is `unverified`, kept.

WHAT A FIRING DOES, per repository in scope (the schedule's scope, narrowed
by `repositories`), with the SCHEDULE'S TENANT's forge credential, read once,
held in this frame, handed to the redaction as a known literal and dropped:

  1. READ the default branch, every OPEN pull request (all of them, or the
     repository deletes nothing), the branches, then the closed pull
     requests, newest first, page by page within `MAX_REQUESTS`.
  2. JUDGE each branch by the rule.
  3. DELETE the allowed ones (`GitHubWriter.delete_branch`), the deletes an
     earlier firing of this schedule recorded as failed first, then by name,
     at most `max_deletes` across the firing. What the cap, the request
     budget or the time budget left is `deferred`, and the report says which.
  4. RECORD the report as the api_action `report` (succeeded). A repository
     that could not be read, or a delete GitHub refused, is ALSO on a second
     item, `refusals`, which ends `failed`: the firing is `partial` (§2.9).

A WINDOW REMAINS between the read and the delete: GitHub's ref delete takes
no expected sha. The open pull requests are read first and the deletes
follow within the firing's seconds; a branch pushed to in that window is
lost from the branch list but its pull request keeps the commits and
GitHub's "Restore branch" brings it back.

`dry_run` (the parameter) records the same report with `would_delete` in
place of `deleted` and deletes nothing. The firing's own dry run (§2.8)
returns that report and writes nothing anywhere.

THE TOKEN never reaches the report: every name stored is passed through
`neutral_line` with the token as a known literal, and every error is
forgewrite's constant sentence.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter
from swarm_common.states import TERMINAL_STATES, TaskState

from ..errors import ApiError
from ..forge import NoForgeCredential, neutral_line
from ..forgewrite import (
    CONTENTS_WRITE,
    ForgeRateLimited,
    ForgeWriteError,
    ForgeWriteForbidden,
    ForgeWriteUnauthorized,
    GitHubWriter,
)
from ..forgewrite import PAGE_SIZE as GITHUB_PAGE_SIZE
from ..repositories import Repositories
from ..scheduletypes import BudgetCaps
from ..validation import IssueRef
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "branch-sweep"

#: SD4 leaves a type's caps to its lane. A firing runs no model and no
#: worker, so it costs $0.00: the dollar caps are the smallest a schedule's
#: budget accepts (a cent) with room for five firings a day, as
#: `cost-report`'s. `max_concurrent` is 2: the report and the refusals.
BUDGET_CAPS = BudgetCaps(per_run_usd=0.01, per_day_usd=0.05, max_concurrent=2)

REPORT = "report"
REFUSALS = "refusals"

#: GitHub requests one firing may make, reads and deletes together. The
#: GitHub quota is the OWNER's: a GitHub App installation gets 5,000
#: requests an hour, and 600 is 12% of one hour of it, once a day. After the
#: 2026-11-02 clean-up the SwarmCloud repository lists in about 50 pages
#: (some 2,500 branches and 2,400 closed pull requests at 100 a page), which
#: leaves the rest for `max_deletes`' default of 300.
MAX_REQUESTS = 600
#: Wall-clock seconds one firing spends on GitHub, as `pr-shepherd`'s: the
#: tick stops starting work at 240 s and must leave room for other schedules.
BUDGET_SECONDS = 90.0
#: Names kept per kind in the report; the count is always whole.
NAMES_PER_KIND = 20
#: Characters of one branch name, and of one refusal's sentence, stored.
MAX_NAME_CHARS = 255
MAX_MESSAGE_CHARS = 400
#: Live tasks read to find the branches still in use. Past it, nothing the
#: rule allows is deleted (`unverified`): an unread task may read any branch.
MAX_LIVE_TASKS = 2000
LIVE_PAGE = 300
#: Earlier firings read for the deletes they recorded as failed.
EARLIER_FIRINGS = 20
EARLIER_AGE = timedelta(days=14)
FIRINGS = "schedule_firings"

KEEP_PREFIX = "keep/"
TASK_ID = re.compile(r"task_[A-Za-z0-9]+")

DELETABLE = "deletable"
KEPT_KINDS = (
    "no_pull_request",
    "closed_unmerged",
    "head_moved_after_merge",
    "open_pull_request",
    "protected_or_keep",
    "in_use",
    "unverified",
    "deferred",
    "delete_failed",
)

LIVE_STATES = tuple(state for state in TaskState if state not in TERMINAL_STATES)


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class Pull:
    head_ref: str
    head_sha: str
    state: str  # open | closed
    merged: bool


def pull_of(entry: Mapping[str, Any], full_name: str) -> Pull | None:
    """A listed pull request from THIS repository's branch, or None (a fork's)."""
    head = _mapping(entry.get("head"))
    repo = str(_mapping(head.get("repo")).get("full_name") or "")
    ref, sha = head.get("ref"), head.get("sha")
    if repo.lower() != full_name.lower() or not isinstance(ref, str) or not isinstance(sha, str):
        return None
    return Pull(head_ref=ref, head_sha=sha, state=str(entry.get("state") or ""),
                merged=bool(entry.get("merged_at")))


def judge(name: str, sha: str, protected: bool, *, default_branch: str,
          pulls: Iterable[Pull], in_use: bool) -> str:
    """The kind of one branch: `deletable`, or why it is kept."""
    if name == default_branch or protected or name.startswith(KEEP_PREFIX):
        return "protected_or_keep"
    pulls = list(pulls)
    if any(p.state == "open" for p in pulls):
        return "open_pull_request"
    if in_use:
        return "in_use"
    merged = [p for p in pulls if p.merged]
    if any(p.head_sha == sha for p in merged):
        return DELETABLE
    if merged:
        return "head_moved_after_merge"
    if pulls:
        return "closed_unmerged"
    return "no_pull_request"


def in_use_by(name: str, live_ids: set[str], live_refs: set[str]) -> bool:
    return name in live_refs or any(tid in live_ids for tid in TASK_ID.findall(name))


# --------------------------------------------------------------------------
# Reads outside GitHub
# --------------------------------------------------------------------------


def _task_ids(value: Any, out: set[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        out.update(TASK_ID.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            _task_ids(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _task_ids(item, out, depth + 1)


def live_reads(store: Any, tenant_id: str) -> tuple[set[str], set[str], bool]:
    """(task ids whose branches live work reads, refs it names, whether the read was whole)."""
    ids: set[str] = set()
    refs: set[str] = set()
    seen = 0
    token: str | None = None
    while True:
        page = store.list_tasks(tenant_id, state=list(LIVE_STATES), limit=LIVE_PAGE,
                                page_token=token, submitted_by=None)
        for task in page.items:
            if task.tenant_id != tenant_id:
                continue
            seen += 1
            ids.add(task.id)
            ids.update(task.depends_on or [])
            if task.parent_task_id:
                ids.add(task.parent_task_id)
            _task_ids(_mapping(task.metadata).get("dispatch"), ids)
            if task.repository_ref:
                refs.add(task.repository_ref)
        token = page.next_page_token
        if not token:
            return ids, refs, True
        if seen >= MAX_LIVE_TASKS:
            return ids, refs, False


def _aware(moment: Any) -> datetime | None:
    if isinstance(moment, datetime):
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    if isinstance(moment, str):
        try:
            return _aware(datetime.fromisoformat(moment.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def earlier_failures(db: Any, schedule: Mapping[str, Any], now: datetime) -> dict[str, set[str]]:
    """Deletes this schedule's recent firings recorded as failed: repository -> names."""
    query = (db.collection(FIRINGS)
             .where(filter=FieldFilter("schedule_id", "==", schedule["schedule_id"]))
             .limit(EARLIER_FIRINGS))
    failed: dict[str, set[str]] = {}
    for snap in query.stream():
        doc = snap.to_dict() or {}
        if doc.get("tenant_id") != schedule.get("tenant_id"):
            continue
        fired = _aware(doc.get("fired_at")) or _aware(doc.get("slot"))
        if fired is not None and fired < now - EARLIER_AGE:
            continue
        for item in list(doc.get("work") or []) + list(doc.get("recorded") or []):
            if not isinstance(item, Mapping) or item.get("kind") != "api_action" \
                    or item.get("id") != REPORT:
                continue
            for repo in _mapping(item.get("detail")).get("repositories") or []:
                repo = _mapping(repo)
                names = _mapping(repo.get("delete_failed")).get("names") or []
                if repo.get("repository") and names:
                    failed.setdefault(str(repo["repository"]).lower(), set()).update(
                        str(n) for n in names)
    return failed


def registrations(firing: Any, wanted: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """The scope's live registrations, narrowed to `wanted` (owner/repo) when given,
    and the wanted names the scope does not hold."""
    found = Repositories(firing.ctx.db, now=firing.ctx.now)
    tenant_id = str(firing.schedule["tenant_id"])
    rows = []
    for repo_id in firing.repo_ids:
        row = found.find(tenant_id, repo_id)
        # The registration is the tenant's own (invariant 9); asked again.
        if row and row.get("tenant_id", tenant_id) == tenant_id and not row.get("archived"):
            rows.append(row)
    if not wanted:
        return rows, []
    names = {f"{r.get('owner')}/{r.get('repo')}".lower(): r for r in rows}
    return ([names[w.lower()] for w in wanted if w.lower() in names],
            [w for w in wanted if w.lower() not in names])


# --------------------------------------------------------------------------
# One firing's pass
# --------------------------------------------------------------------------


class _OutOfBudget(Exception):
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


@dataclass
class _Budget:
    clock: Callable[[], float]
    started: float
    used: int = 0
    stopped: str | None = None

    def spend(self) -> None:
        if self.stopped:
            raise _OutOfBudget(self.stopped)
        if self.used >= MAX_REQUESTS:
            self.stopped = "request_budget"
        elif self.clock() - self.started > BUDGET_SECONDS:
            self.stopped = "time_budget"
        if self.stopped:
            raise _OutOfBudget(self.stopped)
        self.used += 1


@dataclass
class _Repo:
    repository: str
    kinds: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in KEPT_KINDS})
    done: list[str] = field(default_factory=list)
    branches: int = 0
    truncated: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    default_branch: str | None = None

    def describe(self, done_key: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "repository": self.repository,
            "default_branch": self.default_branch,
            "branches": self.branches,
            done_key: {"count": len(self.done), "names": list(self.done)},
            "kept": {k: {"count": len(v), "names": v[:NAMES_PER_KIND]}
                     for k, v in self.kinds.items() if k != "delete_failed" and v},
            "truncated": list(self.truncated),
        }
        failed = self.kinds["delete_failed"]
        if failed:
            # Every name: the next firing retries them first.
            out["delete_failed"] = {"count": len(failed), "names": list(failed),
                                    "errors": self.failures[:NAMES_PER_KIND]}
        return out


def _pages(budget: _Budget, read: Callable[[int], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], bool]:
    """Every page `read` gives, within the budget; True when whole."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            budget.spend()
        except _OutOfBudget:
            return rows, False
        entries = read(page)
        rows += entries
        if len(entries) < GITHUB_PAGE_SIZE:
            return rows, True
        page += 1


def _refusal(repo: str, action: str, exc: ApiError, token: str, **extra: Any) -> dict[str, Any]:
    # forgewrite's sentences hold no token, but they quote the branch's name,
    # which GitHub's caller chose: masked with the token as a known literal.
    return {"repository": repo, "action": action, "code": exc.code,
            "message": neutral_line(exc.message, MAX_MESSAGE_CHARS, literals=(token,)), **extra}


def sweep(firing: Any, params: Any, *, delete: bool,
          clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read, judge and (with `delete`) delete. Returns the report and its refusals."""
    ctx = firing.ctx
    tenant_id = str(firing.schedule["tenant_id"])
    done_key = "deleted" if delete else "would_delete"
    report: dict[str, Any] = {
        "type": TYPE, "dry_run": not delete, "max_deletes": params.max_deletes,
        "request_budget": MAX_REQUESTS, "requests": 0, "stopped": None,
        "repositories": [], "refused": [],
    }
    rows, missing = registrations(firing, list(params.repositories or []))
    for name in missing:
        report["refused"].append({"repository": name, "action": "scope", "code": "not_in_scope",
                                  "message": "not a repository this schedule's scope holds now"})
    writer: GitHubWriter | None = getattr(ctx, "forge_writer", None)
    tokens = getattr(ctx, "forge_tokens", None)
    if not rows:
        return _totals(report, [], done_key)
    if writer is None or tokens is None:
        report["refused"].append({"repository": "*", "action": "read", "code": "forge_unconfigured",
                                  "message": "swarm-api has no GitHub client configured"})
        return _totals(report, [], done_key)
    token = ""
    budget = _Budget(clock=clock, started=clock())
    done: list[_Repo] = []
    try:
        try:
            token = tokens.token_for(ctx.store.get_tenant(tenant_id))
        except NoForgeCredential as exc:
            report["refused"].append({"repository": "*", "action": "read", "code": exc.code,
                                      "message": exc.message})
            return _totals(report, [], done_key)
        live_ids, live_refs, live_whole = live_reads(ctx.store, tenant_id)
        if not live_whole:
            report["live_tasks_truncated"] = True
        retry = earlier_failures(ctx.db, firing.schedule, firing.now)
        left = params.max_deletes
        for row in rows:
            owner, name = str(row.get("owner") or ""), str(row.get("repo") or "")
            result = _Repo(repository=f"{owner}/{name}")
            done.append(result)
            left -= _one(result, IssueRef(owner=owner, repo=name, number=1), writer, token, budget,
                         report, live_ids, live_refs, live_whole,
                         retry.get(result.repository.lower(), set()), left, delete)
    finally:
        token = ""
    report["requests"] = budget.used
    report["stopped"] = budget.stopped
    return _totals(report, done, done_key)


def _one(result: _Repo, ref: IssueRef, writer: GitHubWriter, token: str, budget: _Budget,
         report: dict[str, Any], live_ids: set[str], live_refs: set[str], live_whole: bool,
         retry: set[str], left: int, delete: bool) -> int:
    """One repository. Returns how many it deleted (or would)."""
    repo = result.repository

    def shown(text: str) -> str:
        return neutral_line(text, MAX_NAME_CHARS, literals=(token,))

    try:
        budget.spend()
        result.default_branch = writer.default_branch(ref, token)
        open_rows, open_whole = _pages(budget, lambda p: writer.pulls_page(ref, "open", p, token))
        branch_rows, branches_whole = _pages(budget, lambda p: writer.branches_page(ref, p, token))
        closed_rows, closed_whole = _pages(budget, lambda p: writer.pulls_page(ref, "closed", p, token))
    except _OutOfBudget:
        result.truncated.append("not_read")
        return 0
    except ApiError as exc:
        if exc.status_code >= 500 and not isinstance(exc, ForgeWriteError) \
                and not isinstance(exc, ForgeRateLimited):
            raise
        report["refused"].append(_refusal(repo, "read", exc, token))
        if isinstance(exc, ForgeRateLimited):
            budget.stopped = "rate_limited"
        return 0
    for what, whole in (("open_pulls", open_whole), ("branches", branches_whole),
                        ("closed_pulls", closed_whole)):
        if not whole:
            result.truncated.append(what)
    by_head: dict[str, list[Pull]] = {}
    for entry in open_rows + closed_rows:
        pull = pull_of(entry, repo)
        if pull is not None:
            by_head.setdefault(pull.head_ref, []).append(pull)
    deletable: list[tuple[str, str]] = []
    for entry in branch_rows:
        branch = entry.get("name")
        sha = _mapping(entry.get("commit")).get("sha")
        if not isinstance(branch, str) or not isinstance(sha, str):
            continue
        result.branches += 1
        kind = judge(branch, sha, entry.get("protected") is True,
                     default_branch=result.default_branch or "",
                     pulls=by_head.get(branch, ()),
                     in_use=in_use_by(branch, live_ids, live_refs))
        if kind == DELETABLE and not (open_whole and live_whole):
            kind = "unverified"
        if kind == DELETABLE:
            deletable.append((branch, shown(branch)))
        else:
            result.kinds[kind].append(shown(branch))
    # The deletes an earlier firing recorded as failed first, then by name.
    deletable.sort(key=lambda pair: (pair[1] not in retry, pair[0]))
    count = 0
    refused_credential = False
    for branch, label in deletable:
        if count >= left:
            result.kinds["deferred"].append(label)
            report["capped"] = True
            continue
        if not delete:
            result.done.append(label)
            count += 1
            continue
        if refused_credential:
            result.kinds["deferred"].append(label)
            continue
        try:
            budget.spend()
            writer.delete_branch(ref, branch, token)  # False: already gone, which is the aim
            result.done.append(label)
            count += 1
        except _OutOfBudget:
            result.kinds["deferred"].append(label)
        except ApiError as exc:
            if exc.status_code >= 500 and not isinstance(exc, ForgeWriteError) \
                    and not isinstance(exc, ForgeRateLimited):
                raise
            result.kinds["delete_failed"].append(label)
            row = _refusal(repo, "delete", exc, token, branch=label)
            if isinstance(exc, (ForgeWriteForbidden, ForgeWriteUnauthorized)):
                # Every other delete would be refused the same way.
                refused_credential = True
                row["needs"] = CONTENTS_WRITE
            if isinstance(exc, ForgeRateLimited):
                budget.stopped = "rate_limited"
            result.failures.append({k: v for k, v in row.items() if k != "repository"})
            report["refused"].append(row)
    return count


def _totals(report: dict[str, Any], repos: list[_Repo], done_key: str) -> dict[str, Any]:
    names = [f"{r.repository}:{n}" for r in repos for n in r.done]
    report[done_key] = {"count": len(names), "names": names}
    kept: dict[str, dict[str, Any]] = {}
    for kind in KEPT_KINDS:
        rows = [f"{r.repository}:{n}" for r in repos for n in r.kinds[kind]]
        kept[kind] = {"count": len(rows), "names": rows[:NAMES_PER_KIND]}
    report["kept"] = kept
    report["repositories"] = [r.describe(done_key) for r in repos]
    if report.get("capped"):
        report["capped_message"] = (
            f"stopped at max_deletes ({report['max_deletes']}): "
            f"{kept['deferred']['count']} more branches the rule allows wait for the next firing")
    elif report.get("stopped"):
        report["capped_message"] = (
            f"stopped at the {report['stopped'].replace('_', ' ')}: what was not read or not "
            "deleted waits for the next firing")
    return report


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    schedule_id, fid = firing.schedule["schedule_id"], firing.firing["firing_id"]
    if firing.room < 1:
        log.info("schedule %s firing %s: no room for the branch sweep's report", schedule_id, fid)
        return []
    report = sweep(firing, params, delete=not params.dry_run)
    refused = report.pop("refused")
    if refused and firing.room < 2:
        # No room for a second item: the refusals ride on the report.
        report["refused"] = refused
    work = [firing.api_action(REPORT, outcome="succeeded", detail=report)]
    if refused and firing.room >= 2:
        work.append(firing.api_action(REFUSALS, outcome="failed", detail={"refused": refused}))
    done = report.get("would_delete" if params.dry_run else "deleted") or {}
    log.info("schedule %s firing %s: branch sweep %s %d, refused %d, requests %d%s",
             schedule_id, fid, "would delete" if params.dry_run else "deleted",
             done.get("count", 0), len(refused), report["requests"],
             f", stopped: {report['stopped']}" if report.get("stopped") else "")
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    return sweep(firing, params, delete=False)
