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

from .errors import ApiError, NotFound
from .forgechecks import GREEN, RED, evaluate, required_status_checks
from .forgewrite import GitHubWriter
from .issueci import CI_READ_SECONDS
from .repositories import parse_repository
from .validation import MERGE_WAIT_METADATA_KEY, IssueRef

log = logging.getLogger(__name__)

#: The marker inside `metadata.merge_wait` (`validation.MERGE_WAIT_METADATA_KEY`,
#: the worker's park record) that the scheduler wakes on
#: (`scheduler.loop.MERGE_WAKE_MARKER`). tests/unit/worker/test_merge_action.py
#: holds the restatements equal.
WAKE_MARKER = "wake_requested_at"

TASKS = "tasks"

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
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "visited": self.visited,
            "woken": self.woken,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "failed": self.failed,
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
    pull = writer.read_pull(ref, ref.number, token)
    if pull.merged:
        return "merged"
    if pull.state == "closed":
        return "closed"
    if pull.head_sha != head:
        return "head_moved"
    rules = writer.branch_rules(ref, pull.base_ref, token) if pull.base_ref else []
    reading = evaluate(
        required_status_checks(rules),
        writer.check_runs(ref, head, token),
        writer.commit_statuses(ref, head, token),
    )
    if reading.state == GREEN:
        return "green"
    if reading.state == RED:
        return "red"
    return None


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
            try:
                reason = settled(writer, ref, head, token)
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
                       {"checked_at": now, WAKE_MARKER: now, "wake_reason": reason}):
                report.woken += 1
                log.info("merge wake %s tenant=%s: %s", task_id, tenant_id, reason)
    finally:
        token = ""
    return report
