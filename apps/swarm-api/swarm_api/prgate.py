"""A workflow that waits for named pull requests to merge before its steps start (SPEED-D3).

Owner decision 2026-10-14 (delivery speed plan, D3). RELAY-ROLL, DEVB-GATE
and EGRESS-GATE each depended on another lane's pull request, and each sat
until an operator noticed that pull request had merged and submitted them by
hand. A workflow spec now says what it waits for:

    "after": {"pr": 41}                      # or [{"pr": 41}, {"pr": 42}]

The pull requests are the workflow's OWN repository's: a number, never a
repository, so a spec cannot point the tenant's token at anything else.

WHERE THE WAIT LIVES. The frozen `Workflow` has no metadata (contract change
request 76, docs/contract-change-requests.md), so the gate is written where a
workflow's metadata already lives: on its step tasks. Every ROOT step -- a
step that depends on no other -- is written PARKED on DEPENDENCY_INCOMPLETE
carrying `metadata.after_prs`:

    {"repository": "owner/repo", "pull_requests": [41, 42],
     "merged": [41], "state": "waiting" | "merged" | "closed",
     "closed": 42,           # only when closed
     "checked_at": <when the tick last read>,
     "released_at": <when it read every one merged>}

Every other step already waits on a root, so the whole workflow waits. A park
holds no lease, no pool count and no execution (invariant 1); nothing is
polled from a pod.

WHO READS GITHUB, AND WITH WHAT. This module's `wake_tenant`, on the merge
step's per-tenant wake tick (`POST /v1/admin/merges/wake`, the Cloud
Scheduler job `merge_wake`, every minute), with the named tenant's own `-git`
token (`ctx.forge_tokens.token_for`), as `mergewake` reads a merge step's
pull request -- read at most once per tick, only when a gate is due, and
dropped at the end. The repository is the task's own `repository_url`, on
github.com only; the token goes only to api.github.com, by the writer. Each
gate is read at most once per `issueci.CI_READ_SECONDS`. The scheduler holds
no forge token and must not (a GitHub outage must never slow admission), so
it only reads the gate's `state`.

NO WEBHOOK. The platform has no webhook receiver (mergewake's docstring says
why: a per-registration HMAC secret, a hook in every repository and a public
unauthenticated route). So the tick IS the wake; a merge is noticed within a
minute or two.

WHAT IS WRITTEN. Only `metadata.after_prs`, in a transaction guarded on the
task still being this tenant's, PARKED on DEPENDENCY_INCOMPLETE, its gate
still `waiting` on the same numbers. Every pull request merged: `merged`,
with `released_at`, and the scheduler's dependency sweep promotes the root to
READY (`scheduler.loop._resolve_dependency`). One closed without merging:
`closed`, naming it, and the scheduler dead-letters the root, which fails
the workflow (its dependants are cancelled as a failed parent's). The tick
never moves a task.

QUOTA FIRST, AND BACK OFF WHEN REFUSED (QUOTA-THROTTLE, #1483). The reads
act as the tenant's `-git` slot (`forge_limits.tenant_credential`). Under
`forge_quota_defer_percent` of its limit left, or inside a hold, no gate is
read this tick and none is written: each waits as it was and is read after
the reset, and one line says so. The first rate-limited answer stops the
tick and holds the tenant's gates until the reset GitHub named, rather than
asking for every gate every minute while GitHub refuses.

A FORGED GATE IS THE TENANT'S OWN BUSINESS. Firestore has no document-level
IAM, so any identity of the tenant can write `after_prs`. A forged `merged`
starts the tenant's own workflow early -- which that identity could have done
by submitting it without `after` -- and a forged `closed` fails it. A gate
the scheduler cannot read HOLDS: it is never read as released.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import ParkReason, TaskState

from . import forge_limits
from .errors import ApiError, NotFound, ValidationFailed
from .forgewrite import ForgeRateLimited, ForgeWriteNotFound, GitHubWriter
from .schemas import MAX_AFTER_PRS
from .validation import AFTER_PRS_METADATA_KEY, IssueRef, merge_repository
from .waker import ring

log = logging.getLogger(__name__)

TASKS = "tasks"

#: The gate's states. `scheduler.loop.AFTER_PRS_MERGED` / `AFTER_PRS_CLOSED`
#: and `reconciler.model.AFTER_PRS_MERGED` restate them; the scheduler and the
#: reconciler are separate images and cannot import this one.
#: tests/unit/control_plane/test_workflow_after_pr.py holds them equal.
STATE_WAITING = "waiting"
STATE_MERGED = "merged"
STATE_CLOSED = "closed"

#: The scheduler wake rung when a gate is released or closed, so the root is
#: promoted (or ended) on the next drain rather than the next safety tick.
WAKE_REASON = "after_prs_settled"

__all__ = [
    "AFTER_PRS_METADATA_KEY",
    "MAX_AFTER_PRS",
    "STATE_CLOSED",
    "STATE_MERGED",
    "STATE_WAITING",
    "gate_for",
    "resolve_after",
    "wait_for_api",
    "wake_tenant",
]


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------

def resolve_after(numbers: Sequence[int] | None, repository_url: str | None) -> list[int] | None:
    """The pull requests a workflow waits for, sorted and unique; None without `after`.

    Refused, naming `after`: a workflow with no repository (there is nothing
    for a bare number to be a pull request of), or one not on github.com
    (the only forge the platform reads pull requests from). The numbers'
    range and count are the schema's (`schemas.AfterPullRequest`).
    """
    if numbers is None:
        return None
    if not repository_url:
        raise ValidationFailed(
            "after names pull requests of the workflow's own repository, and this "
            "workflow names no repository_url",
            detail={"after": list(numbers)},
        )
    if merge_repository(repository_url) is None:
        raise ValidationFailed(
            "after waits for pull requests on github.com, and this workflow's "
            "repository is not on github.com",
            detail={"after": list(numbers)},
        )
    return sorted(set(int(n) for n in numbers))


def gate_for(numbers: Sequence[int], repository_url: str) -> dict[str, Any]:
    """The `metadata.after_prs` a root step of the workflow is written with."""
    owner, repo = merge_repository(repository_url) or ("", "")
    return {
        "repository": f"{owner}/{repo}",
        "pull_requests": list(numbers),
        "merged": [],
        "state": STATE_WAITING,
    }


# --------------------------------------------------------------------------
# What the console and `sc workflows` show
# --------------------------------------------------------------------------

def _numbers(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [n for n in value if isinstance(n, int) and not isinstance(n, bool)]


def _named(numbers: Sequence[int]) -> str:
    names = [f"PR #{n}" for n in numbers]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def wait_for_api(tasks: Mapping[str, Any] | Sequence[Any]) -> dict[str, Any] | None:
    """`workflow.after_pull_requests`, from the step tasks a route read; None without a gate.

    The first gate found on any step task is the workflow's: every root
    carries the same one. `text` is the sentence the console and `sc
    workflows` print: "waiting for PR #41 to merge" while it waits.
    """
    values = tasks.values() if isinstance(tasks, Mapping) else tasks
    for task in values:
        metadata = getattr(task, "metadata", None)
        gate = metadata.get(AFTER_PRS_METADATA_KEY) if isinstance(metadata, dict) else None
        if isinstance(gate, dict):
            return _served(gate)
    return None


def _served(gate: Mapping[str, Any]) -> dict[str, Any]:
    wanted = _numbers(gate.get("pull_requests"))
    merged = _numbers(gate.get("merged"))
    pending = [n for n in wanted if n not in merged]
    state = gate.get("state")
    closed = gate.get("closed")
    closed = closed if isinstance(closed, int) and not isinstance(closed, bool) else None
    if state == STATE_CLOSED and closed is not None:
        text = f"PR #{closed} was closed without being merged"
    elif state == STATE_MERGED:
        text = f"{_named(wanted)} merged"
        pending = []
    else:
        state = STATE_WAITING
        text = f"waiting for {_named(pending or wanted)} to merge"
    return {
        "state": state,
        "repository": gate.get("repository") if isinstance(gate.get("repository"), str) else None,
        "pull_requests": wanted,
        "pending": pending,
        "merged": merged,
        "closed": closed,
        "checked_at": gate.get("checked_at"),
        "released_at": gate.get("released_at"),
        "text": text,
    }


# --------------------------------------------------------------------------
# The wake tick
# --------------------------------------------------------------------------

@dataclass
class GateReport:
    """One tick over one tenant's gates. `failures` names each gate not read, by id and code."""

    visited: int = 0
    released: int = 0
    closed: int = 0
    waiting: int = 0
    skipped: int = 0
    failed: int = 0
    #: Due gates not read because the tenant's GitHub quota is low or held
    #: (QUOTA-THROTTLE): left as they were, read after the reset.
    deferred: int = 0
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "visited": self.visited,
            "released": self.released,
            "closed": self.closed,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "failed": self.failed,
            "deferred": self.deferred,
            "truncated": self.truncated,
        }

    def fail(self, task_id: str, code: str) -> None:
        self.failed += 1
        self.failures.append({"task_id": task_id, "code": code})


#: The after-PR gates' hold key in `forge_limits.QUOTAS` (`sweep_key`).
AFTER_PRS_SWEEP = "after_prs"


def _due(gate: Mapping[str, Any], now: datetime) -> bool:
    """Not read in the last CI_READ_SECONDS. A `checked_at` in the future is
    ignored: the document is tenant-writable, and that would stop the reads."""
    # Imported here: `issueci` reaches `codec`, which imports this module
    # for `wait_for_api`.
    from .issueci import CI_READ_SECONDS

    checked = gate.get("checked_at")
    if not isinstance(checked, datetime) or checked > now:
        return True
    return now - checked >= timedelta(seconds=CI_READ_SECONDS)


def _record(db: Any, tenant_id: str, task_id: str, numbers: list[int],
            patch: dict[str, Any]) -> bool:
    """Merge `patch` into `metadata.after_prs`, guarded; True only if written.

    Guarded on what the tick read: the task is still this tenant's, still
    PARKED on DEPENDENCY_INCOMPLETE, its gate still `waiting` on `numbers`.
    A cancel, a scheduler promotion or a second tick that got there first
    wins, and this writes nothing.
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
            or data.get("park_reason") != ParkReason.DEPENDENCY_INCOMPLETE.value
        ):
            return False
        metadata = dict(data.get("metadata") or {})
        gate = metadata.get(AFTER_PRS_METADATA_KEY)
        if (
            not isinstance(gate, dict)
            or gate.get("state") != STATE_WAITING
            or _numbers(gate.get("pull_requests")) != numbers
        ):
            return False
        gate = dict(gate)
        gate.update(patch)
        metadata[AFTER_PRS_METADATA_KEY] = gate
        txn.update(ref, {"metadata": metadata})
        return True

    return bool(_apply(transaction))


def wake_tenant(ctx: Any, tenant_id: str, *, limit: int) -> GateReport:
    """One tick over `tenant_id`'s waiting gates; see the module docstring."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    db = ctx.store.db
    now = ctx.now()
    writer: GitHubWriter = ctx.forge_writer
    report = GateReport()
    # Equality filters only, served by the built-in single-field indexes
    # (merged by Firestore), as the merge wake's query is: no composite index.
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
        .where(filter=FieldFilter("park_reason", "==", ParkReason.DEPENDENCY_INCOMPLETE.value))
        .where(filter=FieldFilter(f"metadata.{AFTER_PRS_METADATA_KEY}.state", "==", STATE_WAITING))
        .limit(limit + 1)
    )
    docs = [snap.to_dict() or {} for snap in query.stream()]
    report.truncated = len(docs) > limit
    token = ""
    token_error = ""
    # One read per pull request per tick, however many roots wait on it.
    readings: dict[tuple[str, str, int], tuple[bool, str]] = {}
    settled = False
    credential = forge_limits.tenant_credential(tenant_id)
    sweep = forge_limits.sweep_key(AFTER_PRS_SWEEP, tenant_id)
    halted = forge_limits.QUOTAS.held(sweep, now=now)
    deferral = forge_limits.QUOTAS.defer(credential, forge_limits.defer_percent(ctx), now=now)
    if halted is not None:
        log.info("github quota: after-prs tick tenant=%s held until %s", tenant_id,
                 halted.isoformat())
    elif deferral is not None:
        forge_limits.log_deferred("after-prs tick", tenant_id, deferral)
    try:
        for doc in docs[:limit]:
            # The query filtered on it; asked again, so a task of another
            # tenant can never be read with this tenant's token.
            if doc.get("tenant_id") != tenant_id:
                continue
            task_id = str(doc.get("id") or "")
            gate = (doc.get("metadata") or {}).get(AFTER_PRS_METADATA_KEY)
            if not isinstance(gate, dict):
                continue
            if not _due(gate, now):
                report.skipped += 1
                continue
            if halted is not None or deferral is not None:
                # Not read and not written: the gate waits as it was.
                report.deferred += 1
                continue
            report.visited += 1
            numbers = _numbers(gate.get("pull_requests"))
            repository = merge_repository(doc.get("repository_url"))
            if not numbers or repository is None:
                # Nothing to read: it waits until cancelled, and says why.
                report.fail(task_id, "gate_unreadable")
                continue
            if token_error:
                report.fail(task_id, token_error)
                continue
            if not token:
                try:
                    token = ctx.forge_tokens.token_for(tenant)
                except Exception as exc:
                    token_error = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                    log.warning("after-prs tick tenant=%s: no forge token (%s)",
                                tenant_id, token_error)
                    report.fail(task_id, token_error)
                    continue
            owner, repo = repository
            merged = [n for n in _numbers(gate.get("merged")) if n in numbers]
            closed: int | None = None
            error = ""
            for number in numbers:
                if number in merged:
                    continue
                key = (owner, repo, number)
                if key not in readings:
                    try:
                        with forge_limits.acting_as(credential):
                            pull = writer.read_pull(
                                IssueRef(owner=owner, repo=repo, number=number), number, token)
                        readings[key] = (bool(pull.merged), str(pull.state or ""))
                    except ForgeWriteNotFound:
                        # Not read as closed: a token that lost the repository
                        # answers 404 too, and failing every waiting workflow
                        # on a rotated token is worse than waiting.
                        error = "pull_request_not_found"
                        break
                    except Exception as exc:
                        error = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                        if isinstance(exc, ForgeRateLimited):
                            # Refused for quota: no other gate is asked this
                            # tick, nor until the reset GitHub named.
                            halted = exc.reset or now + forge_limits.DEFAULT_HOLD
                            forge_limits.QUOTAS.hold(sweep, halted)
                            log.warning("github quota: after-prs tick tenant=%s rate-limited; "
                                        "held until %s", tenant_id, halted.isoformat())
                        break
                was_merged, state = readings[key]
                if was_merged:
                    merged.append(number)
                elif state == "closed":
                    closed = number
                    break
            if error:
                log.warning("after-prs tick %s tenant=%s: not read (%s)", task_id, tenant_id, error)
                report.fail(task_id, error)
                _record(db, tenant_id, task_id, numbers, {"checked_at": now, "error": error})
                continue
            merged = sorted(set(merged))
            if closed is not None:
                if _record(db, tenant_id, task_id, numbers,
                           {"checked_at": now, "merged": merged, "state": STATE_CLOSED,
                            "closed": closed, "error": None}):
                    report.closed += 1
                    settled = True
                    log.info("after-prs %s tenant=%s: #%s closed unmerged", task_id, tenant_id,
                             closed)
                continue
            if merged == numbers:
                if _record(db, tenant_id, task_id, numbers,
                           {"checked_at": now, "merged": merged, "state": STATE_MERGED,
                            "released_at": now, "error": None}):
                    report.released += 1
                    settled = True
                    log.info("after-prs %s tenant=%s: released", task_id, tenant_id)
                continue
            report.waiting += 1
            _record(db, tenant_id, task_id, numbers,
                    {"checked_at": now, "merged": merged, "error": None})
    finally:
        token = ""
    if settled:
        # A latency win only: the scheduler's dependency sweep reads the gate
        # on its next drain whether or not this arrives.
        ring(ctx.waker, ctx.metrics, WAKE_REASON, tenant_id=tenant_id)
    return report
