"""Delete a finished workflow's step hand-off branches (the owner, 2026-11-02).

WHAT. A workflow whose steps hand their work on through branches (`carrier:
branches`, D13) leaves one `swarm/<task_id>` branch per step: the worker
pushes it at each checkpoint (`Worker._push_carrier_branch`) and records it
on the step's task as `result_summary.branch = {name, head}`; the next step
starts from that head. Nothing deleted them: on 2026-11-02 the SwarmCloud
repository held 2,066 task branches with no pull request, most of them these.

WHEN. Once the workflow is finished and no step of it can still read them:
the stored state is SUCCEEDED or CANCELLED (the final ones,
`store._final_workflow_states`: a FAILED workflow may still be retried, and a
retried step starts from its parent's hand-off), and every step's task is
terminal. The pull request a workflow publishes is on a branch that descends
from every hand-off it built on (a carrier push is a fast-forward,
`gitops.commit_tree_onto`), so its content is in that pull request; a
workflow that ended without one has nothing left that reads them.

THE OWNER'S RULE, held per branch. Only a step's OWN hand-off branch
(`swarm/<its task id>`) that its task did not open a pull request from (that
branch is the merge step's, `agent_worker.merge._delete_head_branch`, or
GitHub's auto-delete); only while GitHub's head is still the head the task
recorded (nothing pushed after); never the default branch, a protected
branch, a `keep/` branch, a branch an open pull request is from, or one a
live continuation pushes to or reads (`metadata.dispatch.continues` or
`publish_to_pr.task_id` naming the task). Everything else is KEPT and only
counted. A branch already gone (404, or 422 "Reference does not exist")
counts as deleted.

WHERE AND HOW OFTEN. The merge wake tick (`mergewake.wake_tenant`) calls
`sweep_tenant` once per tick; it runs at most once per `SWEEP_SECONDS` per
tenant, fenced by a transaction on the tenant's `branch_sweeps` document, so
two swarm-api instances never sweep one tenant at once. A workflow's outcome
is recorded in `branch_cleanups/<workflow_id>`; one recorded `done` is not
read again (idempotent), one with a failed delete is retried next sweep. Only
workflows created in the last `LOOKBACK` are read, newest first, through the
`workflows-tenant-state-created` index; older ones are the daily sweep's.

INVARIANTS. No lease, pool count or fencing generation is read or written
here. The forge token is read once per sweep with work to do
(`ctx.forge_tokens.token_for`), handed to `GitHubWriter` only, and never
logged or recorded: a failure records the error's code, never its text.
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
from swarm_common.states import TERMINAL_STATES, TaskState

from . import forge_limits
from .errors import ApiError
from .forgewrite import ForgeRateLimited, ForgeWriteError, GitHubWriter
from .repositories import parse_repository
from .validation import DISPATCH_METADATA_KEY, IssueRef

log = logging.getLogger(__name__)

TASKS = "tasks"
WORKFLOWS = "workflows"
#: One document per tenant: when its hand-off branches were last swept.
BRANCH_SWEEPS = "branch_sweeps"
#: One document per finished workflow: what was done with its branches.
BRANCH_CLEANUPS = "branch_cleanups"
#: How often one tenant is swept. Hourly: a hand-off branch costs nothing
#: while it waits, and each sweep reads up to `WORKFLOWS_READ` workflows.
SWEEP_SECONDS = 3600
#: How far back a sweep looks, by the workflow's `created_at`.
LOOKBACK = timedelta(days=7)
#: Most workflows one sweep reads.
WORKFLOWS_READ = 200
#: Most live continuations read per branch, per dispatch field.
CONTINUATIONS_READ = 20
#: The workflow states after which no step runs again.
FINISHED_STATES = (TaskState.SUCCEEDED.value, TaskState.CANCELLED.value)
BRANCH_PREFIX = "swarm/"
KEEP_PREFIX = "keep/"
PUBLISH_TO_PR_FIELD = "publish_to_pr"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)
_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)


@dataclass
class SweepReport:
    """One sweep of one tenant. `swept` False: not due, or another instance's."""

    swept: bool = False
    workflows: int = 0
    deleted: int = 0
    already_deleted: int = 0
    kept: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {"workflows": self.workflows, "deleted": self.deleted,
                "already_deleted": self.already_deleted, "kept": self.kept,
                "failed": self.failed}


@dataclass(frozen=True)
class _Candidate:
    task_id: str
    branch: str
    head: str
    ref: IssueRef


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, ApiError) else type(exc).__name__


def _repository(url: Any) -> IssueRef | None:
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    if matched is None:
        return None
    try:
        owner, repo = parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        return None
    return IssueRef(owner=owner, repo=repo, number=1)


def _claim(db: Any, tenant_id: str, now: datetime) -> bool:
    """Take this tenant's sweep for `SWEEP_SECONDS`, or False when it was
    taken inside them. A `swept_at` in the future is not honoured."""
    ref = db.collection(BRANCH_SWEEPS).document(tenant_id)

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = (snap.to_dict() or {}) if snap.exists else {}
        swept = data.get("swept_at")
        if (isinstance(swept, datetime) and swept <= now
                and now - swept < timedelta(seconds=SWEEP_SECONDS)):
            return False
        txn.set(ref, {"tenant_id": tenant_id, "swept_at": now})
        return True

    return bool(_apply(db.transaction()))


def _finished_workflows(db: Any, tenant_id: str, now: datetime, limit: int) -> list[dict[str, Any]]:
    query = (
        db.collection(WORKFLOWS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "in", list(FINISHED_STATES)))
        .where(filter=FieldFilter("created_at", ">=", now - LOOKBACK))
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    out = []
    for snap in query.stream():
        doc = snap.to_dict() or {}
        # Checked again here: a fake or an index mistake must not move the
        # tenant boundary (invariant 9).
        if doc.get("tenant_id") == tenant_id and doc.get("state") in FINISHED_STATES:
            out.append({**doc, "workflow_id": str(doc.get("workflow_id") or snap.id)})
    return out


def _done(db: Any, tenant_id: str, workflow_id: str) -> bool:
    snap = db.collection(BRANCH_CLEANUPS).document(workflow_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    return data.get("tenant_id") == tenant_id and data.get("done") is True


def _step_tasks(db: Any, tenant_id: str, workflow: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]] | None:
    """Every step's task, or None when a step has none, is another tenant's,
    or is not terminal yet: a step that can still run can still read a
    hand-off."""
    out = []
    for row in workflow.get("steps") or []:
        task_id = _mapping(row).get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        snap = db.collection(TASKS).document(task_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else None
        if doc is None or doc.get("tenant_id") != tenant_id or doc.get("state") not in _TERMINAL:
            return None
        out.append((task_id, doc))
    return out


def _candidate(task_id: str, doc: Mapping[str, Any]) -> _Candidate | None:
    """The task's own hand-off branch, when it pushed one and opened no pull
    request from it."""
    summary = _mapping(doc.get("result_summary"))
    branch = _mapping(summary.get("branch"))
    name, head = branch.get("name"), branch.get("head")
    if name != f"{BRANCH_PREFIX}{task_id}" or not isinstance(head, str) or not _SHA.match(head):
        return None
    if _mapping(_mapping(summary.get("git")).get("pull_request")).get("number") is not None:
        return None
    ref = _repository(doc.get("repository_url"))
    if ref is None:
        return None
    return _Candidate(task_id=task_id, branch=name, head=head, ref=ref)


def _continued(db: Any, tenant_id: str, task_id: str) -> bool:
    """Whether a live task of this tenant continues `task_id`'s branch."""
    dispatch = f"metadata.{DISPATCH_METADATA_KEY}"
    for path in (f"{dispatch}.continues", f"{dispatch}.{PUBLISH_TO_PR_FIELD}.task_id"):
        query = (
            db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter(path, "==", task_id))
            .limit(CONTINUATIONS_READ)
        )
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if doc.get("tenant_id") == tenant_id and doc.get("state") not in _TERMINAL:
                return True
    return False


def _one(db: Any, writer: GitHubWriter, tenant_id: str, candidate: _Candidate, token: str,
         defaults: dict[str, str]) -> dict[str, Any]:
    """What happened to one hand-off branch. Raises the forge's errors."""
    row: dict[str, Any] = {"task_id": candidate.task_id, "branch": candidate.branch,
                           "head": candidate.head}

    def kept(reason: str) -> dict[str, Any]:
        return {**row, "outcome": "kept", "reason": reason}

    if candidate.branch.startswith(KEEP_PREFIX):
        return kept("kept_branch")
    if _continued(db, tenant_id, candidate.task_id):
        return kept("continued")
    ref = candidate.ref
    now_head = writer.branch_head(ref, candidate.branch, token)
    if now_head is None:
        return {**row, "outcome": "already_deleted"}
    if now_head != candidate.head:
        return {**kept("head_moved"), "branch_head": now_head}
    if ref.repository not in defaults:
        defaults[ref.repository] = writer.default_branch(ref, token)
    if candidate.branch == defaults[ref.repository]:
        return kept("default_branch")
    if writer.branch_protected(ref, candidate.branch, token):
        return kept("protected")
    if writer.find_open_pull(ref, candidate.branch, token) is not None:
        return kept("open_pull_request")
    deleted = writer.delete_branch(ref, candidate.branch, token)
    return {**row, "outcome": "deleted" if deleted else "already_deleted"}


def sweep_tenant(ctx: Any, tenant: Any, tenant_id: str, *, now: datetime,
                 limit: int = WORKFLOWS_READ) -> SweepReport:
    """One sweep of `tenant_id`'s finished workflows; see the module docstring."""
    db = ctx.store.db
    report = SweepReport()
    if not _claim(db, tenant_id, now):
        return report
    report.swept = True
    writer: GitHubWriter = ctx.forge_writer
    token = ""
    defaults: dict[str, str] = {}
    try:
        for workflow in _finished_workflows(db, tenant_id, now, limit):
            workflow_id = workflow["workflow_id"]
            if _done(db, tenant_id, workflow_id):
                continue
            tasks = _step_tasks(db, tenant_id, workflow)
            if tasks is None:
                continue
            report.workflows += 1
            candidates = [c for c in (_candidate(t, d) for t, d in tasks) if c is not None]
            rows: list[dict[str, Any]] = []
            if candidates and not token:
                try:
                    token = ctx.forge_tokens.token_for(tenant)
                except Exception as exc:  # noqa: BLE001 - the code only, never the value
                    log.warning("hand-off branches tenant=%s: no forge token (%s)",
                                tenant_id, _code(exc))
                    report.failed += 1
                    report.failures.append({"workflow_id": workflow_id, "code": _code(exc)})
                    return report
            halted = False
            for candidate in candidates:
                try:
                    with forge_limits.acting_as(forge_limits.tenant_credential(tenant_id)):
                        row = _one(db, writer, tenant_id, candidate, token, defaults)
                except ForgeWriteError as exc:
                    row = {"task_id": candidate.task_id, "branch": candidate.branch,
                           "head": candidate.head, "outcome": "failed", "reason": _code(exc)}
                    halted = isinstance(exc, ForgeRateLimited)
                rows.append(row)
                outcome = row["outcome"]
                if outcome == "deleted":
                    report.deleted += 1
                elif outcome == "already_deleted":
                    report.already_deleted += 1
                elif outcome == "kept":
                    report.kept += 1
                else:
                    report.failed += 1
                    report.failures.append({"workflow_id": workflow_id,
                                            "code": str(row.get("reason"))})
                if halted:
                    break
            done = not halted and all(row["outcome"] != "failed" for row in rows)
            db.collection(BRANCH_CLEANUPS).document(workflow_id).set({
                "tenant_id": tenant_id, "workflow_id": workflow_id,
                "state": workflow.get("state"), "swept_at": now, "done": done,
                "branches": rows,
            })
            log.info("hand-off branches tenant=%s workflow=%s: %d deleted, %d kept, done=%s",
                     tenant_id, workflow_id,
                     sum(1 for r in rows if r["outcome"] in ("deleted", "already_deleted")),
                     sum(1 for r in rows if r["outcome"] == "kept"), done)
            if halted:
                break
    finally:
        token = ""
    return report
