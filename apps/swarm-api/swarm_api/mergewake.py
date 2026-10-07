"""The merge step's wake tick: mark a CI_PENDING park whose checks have settled.

docs/merge-step.md "Revised 2026-10-06" §1, lane MS2. A merge step that finds
its pull request's checks still running parks CI_PENDING, holding no lease,
no pool count and no Job execution (invariants 1 and 3), and records what it
waits on in `metadata.merge_wait`: the head the checks were read at, the pull
request, the pending names (`agent_worker.control.ControlPlane.
park_ci_pending`). Something has to notice when CI has finished.

WHY A TICK, NOT A WEBHOOK. The platform has no webhook receiver. One would
need an HMAC secret per registration, a hook installed in every repository
(`admin:repo_hook`, a wider token than the merge needs) and a public
unauthenticated route. So the signal is a cheap re-read on the per-tenant
Cloud Scheduler job `merge_wake` (terraform/modules/scheduler/jobs.tf), as
the rollup sweeper, every minute: `POST /v1/admin/merges/wake?tenant_id=<t>`.

WHY HERE, NOT IN THE SCHEDULER. The scheduler holds no forge token and must
not: a GitHub outage must never slow admission. swarm-api already reads each
tenant's `-git` token for exactly these reads (`issueci.from_checks`), under
that tenant only.

WHAT IS READ, AND WITH WHAT (invariant 9). One indexed query for the named
tenant's tasks PARKED on CI_PENDING -- equality filters, served by the
built-in single-field indexes. For each, at most once per
`issueci.CI_READ_SECONDS`: the pull request, the base branch's rules, and the
check runs and commit statuses at the RECORDED head, through the same
`forgewrite.GitHubWriter` reads and the same `forgechecks.evaluate` the
issue-run loop uses, with the named tenant's own token
(`ctx.forge_tokens.token_for`), read at most once per tick, only when a park
is due, and dropped at the end. Nothing a caller sent selects the token, the
tenant or the repository; the repository is the task's own
`repository_url`, on github.com only, and the token is only ever sent to
api.github.com by the writer.

WHAT IS WRITTEN. When nothing the park waits on is still pending -- the
checks green or red, the head moved, the pull request closed or merged --
`merge_wait.wake_requested_at` and `merge_wait.wake_reason`, in a
transaction guarded on the task still being this tenant's, PARKED on
CI_PENDING, at the head that was read. Every read records
`merge_wait.checked_at` the same way, which is what bounds the reads to one
per CI_READ_SECONDS: the issue-run loop's `pull_request.checked_at`, kept on
the task because the task is what is read. That is all the tick writes. It
never moves the task: the scheduler's `_promote_ci_waits` returns it to
READY on the marker, and only admission takes capacity.

A FORGED MARKER IS HARMLESS. Firestore has no document-level IAM, so any
identity of the tenant can write `merge_wait`. A forged marker costs one
early wake, whose worker re-reads every fact and trusts nothing this tick
saw; a forged `checked_at` in the future is ignored; a forged head only
points these reads at another commit of the same repository with the same
tenant's token.

A RED READING WITH CI-FIX ROUNDS LEFT IS A ROUND, NOT A WAKE (lane MS7,
§6 MS7). A workflow that is not an issue run asks for up to
`metadata.merge_fix_rounds` (0-5, `validation.resolve_merge_fix_rounds`)
rounds. When the reading is red and the merge task has rounds left, the tick
claims the next round in the merge task's `metadata.merge_fix` in a
transaction guarded on the task still being PARKED/CI_PENDING at the head
read and on the number of rounds it read (`_claim_round`), so two ticks that
both read red claim, and submit, once. Then it submits the continuation the
issue-run loop builds (`issueci.ci_fix_continuation`, `continues_task` the
signed target's task) as the merge task's submitter, only while they are
still a member of the tenant (`_submitter`, the issue run's
`run_owner_auth` rule, invariant 9), and records the round's workflow and
task. The merge is NOT woken: it stays parked, holding nothing, until the
round pushes a new head (`head_moved`, with `head_pushed_by` naming the
round's task, `issueci.pushing_task`), the round ends at the red head
(`fix_round_ended`), its submission is refused (`fix_round_refused`) or its
record is lost (`fix_round_lost`); then the worker reads everything again.
The worker accepts a round's head only if the round's signed spec continues
the signed target's root (`agent_worker.merge._fix_round_heads`). A round
is only submitted for a pull request whose head branch is the root's own
(`swarm/<root>`, `cifix.BRANCH_PREFIX`): a tenant-written target cannot aim
a round at another branch. With no round left, red wakes the step, which
refuses `checks_failed`.

AN UPDATE GITHUB HAS NOT MADE YET IS NOT A READING EITHER. update-branch is
asynchronous: a worker that saw no new head after its bounded re-read parks
at the old head with `BRANCH_UPDATE_PENDING`. That head's checks are green
-- it was only behind -- so reading them would wake the step for nothing,
every CI_READ_SECONDS. The tick waits for the head to move instead, and
the worker's fallback instant covers an update GitHub never makes.

A PULL REQUEST IN ITS BASE'S MERGE QUEUE IS NOT A READING OF ITS CHECKS
(lane C3H). A worker whose merge GitHub answered "Changes must be made
through the merge queue" enqueued the pull request and parked MERGE_QUEUED.
Its checks were green when it did, so reading them would wake it for
nothing, every CI_READ_SECONDS. The tick reads the pull request and, while
it is open at the recorded head, one GraphQL query with the same token:
still in the queue is a wait; out of it, unmerged, wakes the step as
`dequeued`, and the worker reads GitHub's reason itself and refuses
`merge_dequeued`. Merged, closed or moved wakes it as for any park.

A READ THAT FAILS IS NOT A READING. GitHub down, a credential without
`checks: read`, a pull request not visible: nothing is marked, the failure is
reported by task id and code, and the worker's fallback instant
(`MERGE_CI_FALLBACK_SECONDS`) wakes the park anyway.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import ParkReason, TaskState

from swarm_common.identity import Principal
from swarm_common.states import TERMINAL_STATES

from .auth import AuthContext
from .cifix import BRANCH_PREFIX
from .continuation import TASK_ID_RE
from .errors import ApiError, Forbidden, NotFound, UpstreamUnavailable
from .forgechecks import GREEN, RED, CiReading, evaluate, required_status_checks
from .forge import GITHUB_API_HOST
from .forgewrite import PULLS_WRITE, ForgeWriteError, GitHubWriter, PullSnapshot
from .issueci import (
    CI_READ_SECONDS,
    LOST_ROUND_SECONDS,
    ci_fix_continuation,
    excerpt_at,
    pushing_task,
)
from .issueruns import failure_text
from .repositories import parse_repository
from .validation import (
    DISPATCH_METADATA_KEY,
    MERGE_FIX_ROUNDS_KEY,
    MERGE_FIX_ROUNDS_MAX,
    MERGE_TARGET_FIELD,
    MERGE_WAIT_METADATA_KEY,
    IssueRef,
)

log = logging.getLogger(__name__)

#: The marker inside `metadata.merge_wait` (`validation.MERGE_WAIT_METADATA_KEY`,
#: the worker's park record) that the scheduler wakes on
#: (`scheduler.loop.MERGE_WAKE_MARKER`). tests/unit/worker/test_merge_action.py
#: holds the restatements equal.
WAKE_MARKER = "wake_requested_at"

TASKS = "tasks"

#: The merge task's record of its CI-fix rounds (lane MS7): `rounds`, oldest
#: first, each `{round, head, claimed_at, workflow_id, task_id}` or, for a
#: round whose submission was refused, `error` in place of the ids. Written
#: only by this tick; read by the worker
#: (`agent_worker.merge.MERGE_FIX_METADATA_KEY`, held equal by
#: tests/unit/worker/test_merge_fix_round.py). Beside `merge_wait`, not in
#: it: the worker's park rewrites `merge_wait` whole.
MERGE_FIX_METADATA_KEY = "merge_fix"
#: The worker's park code after an update-branch whose new head GitHub had
#: not made yet (`agent_worker.merge.BRANCH_UPDATE_PENDING`).
BRANCH_UPDATE_PENDING = "branch_update_pending"

#: The worker's park code after it put the pull request in its base's merge
#: queue (`agent_worker.merge.MERGE_QUEUED`).
MERGE_QUEUED = "merge_queued"
#: The wake reason of a queued pull request that left the queue unmerged.
DEQUEUED = "dequeued"

#: Whether the pull request is still in its base's merge queue. GraphQL,
#: because REST does not say: `isInMergeQueue` is the pull request's own field.
_QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) { isInMergeQueue } } }"
)

#: Every wake reason a fix round gives, beside the readings' own.
FIX_ROUND_ENDED = "fix_round_ended"
FIX_ROUND_REFUSED = "fix_round_refused"
FIX_ROUND_LOST = "fix_round_lost"

_SHA = re.compile(r"^[0-9a-f]{40}$")
#: The task's own repository, on github.com: https, or the scp-like ssh form.
_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


@dataclass
class WakeReport:
    """One tick over one tenant. `failures` names each park not read, by id and code."""

    visited: int = 0
    woken: int = 0
    waiting: int = 0
    skipped: int = 0
    failed: int = 0
    #: Parks whose red reading this tick handed to a CI-fix round (MS7).
    fixing: int = 0
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "visited": self.visited,
            "woken": self.woken,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "failed": self.failed,
            "fixing": self.fixing,
            "truncated": self.truncated,
        }

    def fail(self, task_id: str, code: str) -> None:
        self.failed += 1
        self.failures.append({"task_id": task_id, "code": code})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _target(doc: Mapping[str, Any], wait: Mapping[str, Any]) -> tuple[IssueRef, str] | None:
    """(the pull request, the head) the park recorded, or None when it recorded none."""
    head = wait.get("head")
    number = wait.get("pull_request")
    url = doc.get("repository_url")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        return None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    if matched is None:
        return None
    try:
        owner, repo = parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        return None
    return IssueRef(owner=owner, repo=repo, number=number), head


def settled(writer: GitHubWriter, ref: IssueRef, head: str, token: str) -> str | None:
    """Why the park at `head` should wake now, or None while it waits on something.

    Nothing still pending: the pull request merged or closed, its head moved,
    or every required check (every check, on a branch that requires none)
    concluded, green or red. A red reading wakes the step too: the worker
    refuses `checks_failed` itself, naming every failing check. No check
    reported at all, on a branch that requires none, is still a wait.
    """
    return _read(writer, ref, head, token).reason


@dataclass
class _Reading:
    reason: str | None
    pull: PullSnapshot
    checks: CiReading | None = None


def in_merge_queue(writer: GitHubWriter, ref: IssueRef, token: str) -> bool:
    """Whether pull request `ref.number` is in its base's merge queue.

    Through the writer's own request path -- the pinned host, the token in the
    Authorization header only, its status mapping -- since it has no GraphQL
    read of its own. GraphQL answers a refusal 200 with `errors`: raised, so
    it is never read as "not queued".
    """
    what = f"the merge queue of {ref.repository}#{int(ref.number)}"
    data = writer._call(
        "POST", f"https://{GITHUB_API_HOST}/graphql", token, what, needs=PULLS_WRITE,
        payload={"query": _QUEUE_QUERY,
                 "variables": {"owner": ref.owner, "name": ref.repo, "number": int(ref.number)}},
    )
    pull = _mapping(_mapping(_mapping(_mapping(data).get("data")).get("repository"))
                    .get("pullRequest"))
    queued = pull.get("isInMergeQueue")
    if _mapping(data).get("errors") or not isinstance(queued, bool):
        raise ForgeWriteError(f"GitHub's answer for {what} does not say")
    return queued


def _read(writer: GitHubWriter, ref: IssueRef, head: str, token: str, *,
          update_pending: bool = False, queued: bool = False) -> _Reading:
    """`settled`, keeping what was read: the pull request and, when read, the checks.

    `update_pending`: the park waits on an update GitHub has not made yet, so
    an unmoved head is a wait and its checks are not read (module docstring).
    `queued`: the park waits on the merge queue, which is read in place of
    the checks.
    """
    pull = writer.read_pull(ref, ref.number, token)
    if pull.merged:
        return _Reading("merged", pull)
    if pull.state == "closed":
        return _Reading("closed", pull)
    if pull.head_sha != head:
        return _Reading("head_moved", pull)
    if update_pending:
        return _Reading(None, pull)
    if queued:
        return _Reading(None if in_merge_queue(writer, ref, token) else DEQUEUED, pull)
    rules = writer.branch_rules(ref, pull.base_ref, token) if pull.base_ref else []
    reading = evaluate(
        required_status_checks(rules),
        writer.check_runs(ref, head, token),
        writer.commit_statuses(ref, head, token),
    )
    if reading.state == GREEN:
        return _Reading("green", pull, reading)
    if reading.state == RED:
        return _Reading("red", pull, reading)
    return _Reading(None, pull, reading)


def _record(db: Any, tenant_id: str, task_id: str, head: str, patch: dict[str, Any]) -> bool:
    """Merge `patch` into `metadata.merge_wait`, guarded; True only if written.

    Guarded on what the tick read: the task is still this tenant's, still
    PARKED on CI_PENDING, still waiting at `head`. A worker that re-parked it
    at another head, or a scheduler that already promoted it, wins.
    """
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if (
            data is None
            or data.get("tenant_id") != tenant_id
            or data.get("state") != TaskState.PARKED.value
            or data.get("park_reason") != ParkReason.CI_PENDING.value
        ):
            return False
        metadata = dict(_mapping(data.get("metadata")))
        wait = dict(_mapping(metadata.get(MERGE_WAIT_METADATA_KEY)))
        if wait.get("head") != head:
            return False
        wait.update(patch)
        metadata[MERGE_WAIT_METADATA_KEY] = wait
        txn.update(ref, {"metadata": metadata})
        return True

    return bool(_apply(transaction))


# --------------------------------------------------------------------------
# A red reading with rounds left: the CI-fix hand-off (lane MS7)
# --------------------------------------------------------------------------

class SubmitterNotMember(Forbidden):
    """The merge task's submitter is no longer a member of its tenant: no round
    is submitted as them (the issue run's `RunOwnerNotMember` rule)."""

    code = "owner_not_member"


def _rounds_asked(metadata: Mapping[str, Any]) -> int:
    """`metadata.merge_fix_rounds`, as validated at submission, capped again.

    The document is tenant-writable: anything but a whole number asks for
    none, and no write raises it past MERGE_FIX_ROUNDS_MAX.
    """
    value = metadata.get(MERGE_FIX_ROUNDS_KEY)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return min(value, MERGE_FIX_ROUNDS_MAX)


def _rounds(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rounds = _mapping(metadata.get(MERGE_FIX_METADATA_KEY)).get("rounds")
    return [r for r in rounds if isinstance(r, Mapping)] if isinstance(rounds, list) else []


def _target_and_root(ctx: Any, tenant_id: str, doc: Mapping[str, Any]) -> tuple[str, str] | None:
    """(the task the merge's dispatch block names, the root whose branch the
    pull request is on), read under `tenant_id`; None when either is not there."""
    block = _mapping(_mapping(doc.get("metadata")).get(DISPATCH_METADATA_KEY))
    target = _mapping(block.get(MERGE_TARGET_FIELD)).get("pull_request")
    if not isinstance(target, str) or not TASK_ID_RE.match(target):
        return None
    try:
        task = ctx.store.get_task(tenant_id, target, submitted_by=None)
    except NotFound:
        return None
    upstream = _mapping(_mapping(task.metadata).get(DISPATCH_METADATA_KEY)).get("continues")
    root = upstream if isinstance(upstream, str) and TASK_ID_RE.match(upstream) else target
    return target, root


def _submitter(ctx: Any, tenant: Any, doc: Mapping[str, Any]) -> AuthContext:
    """Who a round is submitted as: the merge task's submitter, while still a member.

    `routes.runs.run_owner_auth`'s shape: built from the stored task and the
    stored tenant, never from the caller, nothing wider than an ordinary
    member, and membership asked of the directory again every time
    (invariant 9). A lookup that fails raises `UpstreamUnavailable`.
    """
    email = str(doc.get("submitted_by") or "")
    if not email or not ctx.authenticator.is_tenant_member(email, tenant):
        raise SubmitterNotMember(
            f"the merge step's submitter {email or '(not recorded)'} is no longer a member "
            f"of tenant {tenant.tenant_id!r}, so no CI fix round is submitted on their behalf"
        )
    return AuthContext(
        principal=Principal(
            email=email,
            # Not a token subject: this context was never authenticated.
            subject=f"merge-fix:{doc.get('id')}",
            domain=email.rsplit("@", 1)[-1],
            groups=(),
        ),
        tenant_id=tenant.tenant_id,
        is_admin=False,
        tenant_principal=tenant.principal,
    )


def _claim_round(db: Any, tenant_id: str, task_id: str, head: str, *, seen: int,
                 now: datetime) -> int | None:
    """Claim round `seen + 1` at `head`; its number, or None when the claim lost.

    Guarded on what the tick read: the task is still this tenant's, PARKED on
    CI_PENDING at `head` with no wake marker, and has exactly `seen` rounds.
    A second tick that read the same red reading finds `seen + 1` rounds and
    claims nothing, so it submits nothing.
    """
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> int | None:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if (
            data is None
            or data.get("tenant_id") != tenant_id
            or data.get("state") != TaskState.PARKED.value
            or data.get("park_reason") != ParkReason.CI_PENDING.value
        ):
            return None
        metadata = dict(_mapping(data.get("metadata")))
        wait = dict(_mapping(metadata.get(MERGE_WAIT_METADATA_KEY)))
        rounds = _rounds(metadata)
        if wait.get("head") != head or wait.get(WAKE_MARKER) or len(rounds) != seen:
            return None
        record = dict(_mapping(metadata.get(MERGE_FIX_METADATA_KEY)))
        record["rounds"] = [dict(r) for r in rounds] + [
            {"round": seen + 1, "head": head, "claimed_at": now}
        ]
        metadata[MERGE_FIX_METADATA_KEY] = record
        wait["checked_at"] = now
        metadata[MERGE_WAIT_METADATA_KEY] = wait
        txn.update(ref, {"metadata": metadata})
        return seen + 1

    return _apply(transaction)


def _record_round(db: Any, tenant_id: str, task_id: str, round_no: int,
                  patch: dict[str, Any]) -> bool:
    """Merge `patch` into round `round_no`'s entry; True only if written."""
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            return False
        metadata = dict(_mapping(data.get("metadata")))
        rounds = [dict(r) for r in _rounds(metadata)]
        mine = [r for r in rounds if r.get("round") == round_no]
        if not mine:
            return False
        mine[-1].update(patch)
        record = dict(_mapping(metadata.get(MERGE_FIX_METADATA_KEY)))
        record["rounds"] = rounds
        metadata[MERGE_FIX_METADATA_KEY] = record
        txn.update(ref, {"metadata": metadata})
        return True

    return bool(_apply(transaction))


def _round_at_head(ctx: Any, db: Any, tenant_id: str, task_id: str,
                   entry: Mapping[str, Any], now: datetime) -> str | None:
    """A round was claimed at the red head: wait on it, or why to wake the step.

    The worker, woken, finds the round's end itself (its task terminal, or
    an `error` on the entry) and refuses `checks_failed`; a round claimed and
    never recorded is given that `error` here, after LOST_ROUND_SECONDS, so
    the worker can tell it from a round still being submitted.
    """
    if entry.get("error"):
        return FIX_ROUND_REFUSED
    fix_task = entry.get("task_id")
    if not isinstance(fix_task, str) or not TASK_ID_RE.match(fix_task):
        claimed = entry.get("claimed_at")
        if (
            isinstance(claimed, datetime)
            and claimed <= now
            and now - claimed < timedelta(seconds=LOST_ROUND_SECONDS)
        ):
            return None
        _record_round(db, tenant_id, task_id, int(entry.get("round") or 0), {
            "error": "the round was claimed and its submission never recorded",
        })
        return FIX_ROUND_LOST
    try:
        task = ctx.store.get_task(tenant_id, fix_task, submitted_by=None)
    except NotFound:
        _record_round(db, tenant_id, task_id, int(entry.get("round") or 0), {
            "error": f"the round's task {fix_task} is not in this tenant",
        })
        return FIX_ROUND_LOST
    return FIX_ROUND_ENDED if task.state in TERMINAL_STATES else None


#: What `_on_red` did: handed the reading to a round, waited on one, or woke.
_FIXING = "fixing"
_WAITING = "waiting"
_WAKE = "wake"


def _on_red(ctx: Any, db: Any, tenant: Any, doc: Mapping[str, Any], ref: IssueRef,
            head: str, read: _Reading, token: str, now: datetime) -> tuple[str, str | None]:
    """A red reading at `head`: (`_FIXING` | `_WAITING` | `_WAKE`, the wake reason)."""
    tenant_id = tenant.tenant_id
    task_id = str(doc.get("id") or "")
    metadata = _mapping(doc.get("metadata"))
    rounds = _rounds(metadata)
    if rounds and rounds[-1].get("head") == head:
        reason = _round_at_head(ctx, db, tenant_id, task_id, rounds[-1], now)
        return (_WAITING, None) if reason is None else (_WAKE, reason)
    asked = _rounds_asked(metadata)
    if len(rounds) >= asked:
        return _WAKE, "red"
    resolved = _target_and_root(ctx, tenant_id, doc)
    if resolved is None or read.pull.head_ref != f"{BRANCH_PREFIX}{resolved[1]}":
        # Not the branch a round would push to: nothing to hand off.
        return _WAKE, "red"
    target, _root = resolved

    refused = ""
    owner = None
    try:
        owner = _submitter(ctx, tenant, doc)
    except UpstreamUnavailable:
        log.warning("merge wake %s tenant=%s: fix round waits: membership unresolved",
                    task_id, tenant_id)
        return _WAITING, None
    except Exception as exc:
        refused = exc.message if isinstance(exc, ApiError) else type(exc).__name__

    round_no = _claim_round(db, tenant_id, task_id, head, seen=len(rounds), now=now)
    if round_no is None:
        # Another tick claimed it, or the task moved: it submits, not this one.
        return _WAITING, None
    if owner is None:
        _record_round(db, tenant_id, task_id, round_no, {"error": failure_text(refused)})
        log.warning("merge wake %s tenant=%s: fix round %d refused (%s)",
                    task_id, tenant_id, round_no, refused)
        return _WAKE, FIX_ROUND_REFUSED
    try:
        excerpt = excerpt_at(ctx.forge_writer, ref, head, read.checks, token,
                             label=f"merge {task_id}") if read.checks is not None else ""
        spec = ci_fix_continuation(
            continues_task=target,
            repository_url=str(doc.get("repository_url") or ""),
            lead=(
                f"CI is red on pull request #{ref.number} ({read.pull.url}), which this "
                "workflow's merge step merges once its checks are green."
            ),
            round_no=round_no,
            rounds=asked,
            excerpt=excerpt,
            step_input={},
            metadata={"merge_fix_round": {
                "merge_task": task_id,
                "round": round_no,
                "head_sha": head,
                "pull_request": ref.number,
            }},
        )
        submission = ctx.submissions.submit_workflow(owner, spec)
    except Exception as exc:
        reason = exc.message if isinstance(exc, ApiError) else type(exc).__name__
        _record_round(db, tenant_id, task_id, round_no, {"error": failure_text(reason)})
        log.warning("merge wake %s tenant=%s: fix round %d refused (%s)",
                    task_id, tenant_id, round_no, reason)
        return _WAKE, FIX_ROUND_REFUSED
    workflow = submission.workflow
    fix_task = next((s.task_id for s in workflow.steps if s.task_id), None)
    _record_round(db, tenant_id, task_id, round_no,
                  {"workflow_id": workflow.workflow_id, "task_id": fix_task})
    log.info("merge wake %s tenant=%s: CI fix round %d of %d submitted as %s",
             task_id, tenant_id, round_no, asked, workflow.workflow_id)
    return _FIXING, None


def _pushed_by(ctx: Any, tenant_id: str, doc: Mapping[str, Any], head: str) -> str | None:
    """Which of this merge's fix rounds pushed `head`, by `issueci.pushing_task`'s rule."""
    workflows = [str(r["workflow_id"]) for r in _rounds(_mapping(doc.get("metadata")))
                 if isinstance(r.get("workflow_id"), str)]
    if not workflows or not head:
        return None
    return pushing_task(ctx, tenant_id, head, fix_workflows=workflows, root_task_id=None)


def _due(wait: Mapping[str, Any], now: datetime) -> bool:
    """Not read in the last CI_READ_SECONDS. A `checked_at` in the future is
    ignored: the document is tenant-writable, and that would stop the reads."""
    checked = wait.get("checked_at")
    if not isinstance(checked, datetime) or checked > now:
        return True
    return now - checked >= timedelta(seconds=CI_READ_SECONDS)


def wake_tenant(ctx: Any, tenant_id: str, *, limit: int) -> WakeReport:
    """One tick over `tenant_id`'s CI_PENDING parks; see the module docstring."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    db = ctx.store.db
    now = ctx.now()
    writer: GitHubWriter = ctx.forge_writer
    report = WakeReport()
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
        .where(filter=FieldFilter("park_reason", "==", ParkReason.CI_PENDING.value))
        .limit(limit + 1)
    )
    docs = [snap.to_dict() or {} for snap in query.stream()]
    report.truncated = len(docs) > limit
    token = ""
    token_error = ""
    try:
        for doc in docs[:limit]:
            # The query filtered on it; asked again, so a task of another
            # tenant can never be read with this tenant's token.
            if doc.get("tenant_id") != tenant_id:
                continue
            task_id = str(doc.get("id") or "")
            wait = _mapping(_mapping(doc.get("metadata")).get(MERGE_WAIT_METADATA_KEY))
            if wait.get(WAKE_MARKER):
                # Marked already: the scheduler wakes it. Nothing to read.
                continue
            if not _due(wait, now):
                report.skipped += 1
                continue
            report.visited += 1
            target = _target(doc, wait)
            if target is None:
                # Nothing to read it at; the worker's fallback instant wakes it.
                report.fail(task_id, "wait_unrecorded")
                continue
            ref, head = target
            if token_error:
                report.fail(task_id, token_error)
                continue
            if not token:
                try:
                    token = ctx.forge_tokens.token_for(tenant)
                except Exception as exc:
                    token_error = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                    log.warning("merge wake tenant=%s: no forge token (%s)", tenant_id, token_error)
                    report.fail(task_id, token_error)
                    continue
            patch: dict[str, Any] = {}
            try:
                read = _read(writer, ref, head, token,
                             update_pending=wait.get("code") == BRANCH_UPDATE_PENDING,
                             queued=wait.get("code") == MERGE_QUEUED)
                reason = read.reason
                if reason == "red":
                    kind, reason = _on_red(ctx, db, tenant, doc, ref, head, read, token, now)
                    if kind == _FIXING:
                        report.fixing += 1
                        continue
                elif reason == "head_moved":
                    pushed_by = _pushed_by(ctx, tenant_id, doc, read.pull.head_sha)
                    if pushed_by:
                        patch["head_pushed_by"] = pushed_by
            except Exception as exc:
                code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                log.warning("merge wake %s tenant=%s: checks not read (%s)",
                            task_id, tenant_id, code)
                report.fail(task_id, code)
                _record(db, tenant_id, task_id, head, {"checked_at": now})
                continue
            if reason is None:
                report.waiting += 1
                _record(db, tenant_id, task_id, head, {"checked_at": now})
                continue
            if _record(db, tenant_id, task_id, head,
                       {"checked_at": now, WAKE_MARKER: now, "wake_reason": reason, **patch}):
                report.woken += 1
                log.info("merge wake %s tenant=%s: %s", task_id, tenant_id, reason)
    finally:
        token = ""
    return report
