"""A workflow held until other pull requests MERGE (`after_merge`).

docs/design/merging-without-a-laptop.md §4, lane 4 (D3, D4). A spec says
what it waits for:

    "after_merge": [{"workflow_id": "wf_..."}, {"pull_request": 41}]

A `workflow_id` is a workflow of the SAME tenant (or a held one, `hwf_...`),
satisfied when the pull request it opened is merged; a `pull_request` is a
number of the spec's own repository, satisfied when GitHub reports it merged.
A finish is not a merge: a predecessor that ended green with its pull request
still open is not enough, because its code is not on the base until it merges.

WHERE THE WAIT LIVES: a `held_workflows/{id}` row (§4.2 A). `hold` validates
the spec and writes the row -- the tenant, the submitter, the spec,
`after_merge`, `state: HELD` -- and NO workflow, NO task and NO signature. A
HELD or BLOCKED row is a Firestore document, never QUEUED, LEASED or RUNNING:
it holds no pool count, no lease, no merge slot and no pending pod
(invariant 1, §4.3). Only the tick reads it.

THE TICK (`release_tenant`) rides the per-minute merge-wake route
(`POST /v1/admin/merges/wake`), after the merge trains and the held child
runs. For ONE tenant (invariant 9: every query filters on the tenant and
every document read is checked again), each HELD row is read in two passes:

  1. Firestore. Each `workflow_id` predecessor is concluded when its merge
     step is SUCCEEDED with a merge recorded, or its train entry is LANDED.
     A merge step that ended any other way, or a predecessor that is missing
     or another tenant's, BLOCKS the row. A predecessor workflow with no merge
     step is concluded when one of its tasks recorded the pull request it
     opened; that pull request is then read as a `pull_request` entry is.
  2. GitHub, only once every Firestore predecessor concluded: each pull
     request read at most once per tick with the tenant's own `-git` token,
     under the tenant's forge quota. Merged satisfies; closed unmerged BLOCKS.

Every entry satisfied: the row is claimed HELD -> STARTING in one
transaction and submitted through the ordinary submission path AS ITS
SUBMITTER (`routes.runs.run_owner_auth`: membership asked of the directory
again), so the spec is validated again, signed and written only then, from
the base's head at release, which holds every merge it waited for. A
submitter removed in the meantime starts nothing: the row is FAILED with the
refusal's code. Nothing is cancelled in a cascade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import new_id
from swarm_common.states import TERMINAL_STATES, TaskState

from . import forge_limits
from .errors import ApiError, NotFound, ValidationFailed
from .forgewrite import ForgeRateLimited, ForgeWriteNotFound
from .mergetrain import ENTRIES_COLLECTION, LANDED, entry_id_for
from .schemas import HeldWorkflowCreate, WorkflowCreate
from .store import TASKS, WORKFLOWS
from .validation import IssueRef, is_merge_step, merge_repository

log = logging.getLogger(__name__)

HELD_WORKFLOWS = "held_workflows"
#: A row's states. HELD and BLOCKED hold nothing (§4.3); STARTING is a
#: claimed row the tick is submitting; RELEASED names its workflow; FAILED
#: is a release the submission path refused.
HELD = "HELD"
STARTING = "STARTING"
RELEASED = "RELEASED"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
#: A STARTING row older than this was left by a tick that died mid-submit.
START_STALE_SECONDS = 600
#: Tasks of one predecessor workflow read for its merge step or its pull request.
PREDECESSOR_TASKS_READ = 50
#: The forge-quota sweep name (`forge_limits.sweep_key`).
HELD_SWEEP = "held_workflows"

_MET = "met"
_WAIT = "wait"


class AfterMergeRefused(ValidationFailed):
    """An `after_merge` the hold refuses: a cycle, a duplicate, a pull request
    of a workflow with no GitHub repository, or a spec field a row must not keep."""

    code = "after_merge_refused"


class AfterMergeUnknown(NotFound):
    """A `workflow_id` naming no workflow of this tenant. Another tenant's is
    refused in the same words as a missing one (invariant 9)."""

    code = "after_merge_unknown"


@dataclass
class _Submitter:
    """The fields `routes.runs.run_owner_auth` reads of what it submits for."""

    id: str
    tenant_id: str
    created_by: str
    on_behalf_of: str | None = None


# --------------------------------------------------------------------------
# Hold
# --------------------------------------------------------------------------

def _doc(db: Any, collection: str, doc_id: str) -> dict[str, Any] | None:
    snap = db.collection(collection).document(doc_id).get()
    return (snap.to_dict() or {}) if snap.exists else None


def _workflow_ids(entries: Any) -> list[str]:
    return [str(e.get("workflow_id")) for e in entries or ()
            if isinstance(e, Mapping) and isinstance(e.get("workflow_id"), str)]


def _refuse_unknown(db: Any, tenant_id: str, workflow_id: str) -> None:
    collection = HELD_WORKFLOWS if workflow_id.startswith("hwf_") else WORKFLOWS
    doc = _doc(db, collection, workflow_id)
    if doc is None or doc.get("tenant_id") != tenant_id:
        raise AfterMergeUnknown(
            f"after_merge names workflow {workflow_id!r}, which is not a workflow of "
            "this workspace",
            detail={"field": "after_merge", "workflow_id": workflow_id},
        )


def _refuse_cycle(db: Any, tenant_id: str, own_id: str, named: list[str]) -> None:
    """Walk the held rows the new row waits on; reaching the new row or a row
    already on the path is a cycle, which would hold every row on it forever."""

    def walk(node: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
        if node == own_id or node in path:
            return path + (node,)
        if not node.startswith("hwf_"):
            return None
        row = _doc(db, HELD_WORKFLOWS, node)
        if row is None or row.get("tenant_id") != tenant_id:
            return None
        for nxt in _workflow_ids(row.get("after_merge")):
            found = walk(nxt, path + (node,))
            if found is not None:
                return found
        return None

    for start in named:
        found = walk(start, (own_id,))
        if found is not None:
            raise AfterMergeRefused(
                "after_merge would wait in a cycle (" + " -> ".join(found) + "), so no "
                "workflow on it could ever start",
                detail={"field": "after_merge", "cycle": list(found)},
            )


def hold(ctx: Any, auth: Any, body: HeldWorkflowCreate) -> dict[str, Any]:
    """Write `body` as a HELD row of the caller's tenant; no workflow, no task.

    The caller's role floor and tenant are the submission path's own
    (`require_member`, `tenant_for`). Refused before anything is written:
    another tenant's or a missing workflow (in the same words), a cycle, a
    duplicate entry, a `pull_request` beside a repository not on github.com,
    and per-run MCP credentials, which a ledger row must not keep.
    """
    from .routes.tasks import require_member

    require_member(ctx, auth)
    tenant = ctx.submissions.tenant_for(auth)
    tenant_id = tenant.tenant_id
    db = ctx.store.db
    if body.mcp_credentials:
        raise AfterMergeRefused(
            "a held workflow cannot carry mcp_credentials: the held row is stored until "
            "release, and a credential is never kept in it; submit them when it starts",
            detail={"field": "mcp_credentials"},
        )
    keys = [(e.workflow_id, e.pull_request) for e in body.after_merge]
    if len(set(keys)) != len(keys):
        raise AfterMergeRefused("after_merge names the same predecessor twice",
                                detail={"field": "after_merge"})
    numbers = [e.pull_request for e in body.after_merge if e.pull_request is not None]
    if numbers and merge_repository(body.repository_url) is None:
        raise AfterMergeRefused(
            "after_merge names pull requests of the workflow's own repository, and this "
            "workflow's repository_url is not a github.com repository",
            detail={"field": "after_merge", "pull_requests": numbers},
        )
    named = [e.workflow_id for e in body.after_merge if e.workflow_id is not None]
    for workflow_id in named:
        _refuse_unknown(db, tenant_id, workflow_id)
    row_id = new_id("hwf")
    _refuse_cycle(db, tenant_id, row_id, named)
    now = ctx.now()
    row = {
        "id": row_id,
        "tenant_id": tenant_id,
        "submitted_by": auth.email,
        "spec": body.model_dump(mode="json", exclude={"after_merge"}),
        "after_merge": [e.model_dump(exclude_none=True) for e in body.after_merge],
        "state": HELD,
        "workflow_id": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    db.collection(HELD_WORKFLOWS).document(row_id).set(row)
    log.info("held workflow %s tenant=%s waits on %d merge(s)", row_id, tenant_id,
             len(body.after_merge))
    return held_to_api(row)


def held_to_api(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as served: never the spec, which is the submitter's input."""
    return {key: row.get(key) for key in
            ("id", "tenant_id", "submitted_by", "after_merge", "state", "workflow_id",
             "error", "created_at", "updated_at")}


# --------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------

@dataclass
class HeldReport:
    visited: int = 0
    released: int = 0
    blocked: int = 0
    waiting: int = 0
    failed: int = 0
    deferred: int = 0
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {"visited": self.visited, "released": self.released, "blocked": self.blocked,
                "waiting": self.waiting, "failed": self.failed, "deferred": self.deferred,
                "truncated": self.truncated}

    def fail(self, row_id: str, code: str) -> None:
        self.failures.append({"id": row_id, "code": code})


def _merge_recorded(summary: Any) -> bool:
    """Whether a SUCCEEDED merge step's `result_summary` records a merge:
    its own, the train's, the queue's, an earlier attempt's or one outside
    the workflow (`agent_worker.merge`)."""
    if not isinstance(summary, Mapping):
        return False
    return any(summary.get(key) is True for key in
               ("merged_by_this_task", "already_merged", "merged_outside_workflow"))


def _pull_number(doc: Mapping[str, Any]) -> int | None:
    summary = doc.get("result_summary")
    git = summary.get("git") if isinstance(summary, Mapping) else None
    pull = git.get("pull_request") if isinstance(git, Mapping) else None
    number = pull.get("number") if isinstance(pull, Mapping) else None
    return number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None


def _state(value: Any) -> TaskState | None:
    try:
        return TaskState(value)
    except ValueError:
        return None


class _Tick:
    def __init__(self, ctx: Any, tenant: Any, report: HeldReport) -> None:
        self.ctx = ctx
        self.tenant = tenant
        self.tenant_id = tenant.tenant_id
        self.db = ctx.store.db
        self.now = ctx.now()
        self.report = report
        self._token = ""
        self._token_error = ""
        self._readings: dict[tuple[str, str, int], tuple[bool, str]] = {}
        self._credential = forge_limits.tenant_credential(self.tenant_id)
        self._sweep = forge_limits.sweep_key(HELD_SWEEP, self.tenant_id)
        self.halted = forge_limits.QUOTAS.held(self._sweep, now=self.now)
        self.deferral = forge_limits.QUOTAS.defer(
            self._credential, forge_limits.defer_percent(ctx), now=self.now)

    def close(self) -> None:
        self._token = ""

    # -- Firestore ----------------------------------------------------------

    def predecessor(self, workflow_id: str) -> tuple[str, Any]:
        """(`met` | `wait` | why it never merges, a pull request to read or None)."""
        if workflow_id.startswith("hwf_"):
            row = _doc(self.db, HELD_WORKFLOWS, workflow_id)
            if row is None or row.get("tenant_id") != self.tenant_id:
                return "predecessor_missing", None
            if row.get("state") in (BLOCKED, FAILED):
                return "predecessor_blocked", None
            if row.get("state") != RELEASED or not isinstance(row.get("workflow_id"), str):
                return _WAIT, None
            workflow_id = row["workflow_id"]
        workflow = _doc(self.db, WORKFLOWS, workflow_id)
        if workflow is None or workflow.get("tenant_id") != self.tenant_id:
            return "predecessor_missing", None
        query = (
            self.db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", self.tenant_id))
            .where(filter=FieldFilter("workflow_id", "==", workflow_id))
            .limit(PREDECESSOR_TASKS_READ)
        )
        tasks = [doc for doc in (snap.to_dict() or {} for snap in query.stream())
                 if doc.get("tenant_id") == self.tenant_id
                 and doc.get("workflow_id") == workflow_id]
        merges = [doc for doc in tasks if is_merge_step(doc.get("runner_profile"))]
        if merges:
            pending = False
            for doc in merges:
                entry = _doc(self.db, ENTRIES_COLLECTION,
                             entry_id_for(self.tenant_id, str(doc.get("id") or "")))
                if entry is not None and entry.get("tenant_id") == self.tenant_id \
                        and entry.get("state") == LANDED:
                    return _MET, None
                state = _state(doc.get("state"))
                if state is TaskState.SUCCEEDED and _merge_recorded(doc.get("result_summary")):
                    return _MET, None
                if state is None or state not in TERMINAL_STATES:
                    pending = True
            return (_WAIT if pending else "predecessor_unmerged"), None
        # No merge step: the pull request its branch agent opened, once one
        # of its tasks recorded it, is read as a `pull_request` entry is.
        for doc in tasks:
            number = _pull_number(doc)
            if number is not None:
                return _MET, (doc.get("repository_url") or workflow.get("repository_url"), number)
        ended = bool(tasks) and all(_state(d.get("state")) in TERMINAL_STATES for d in tasks)
        return ("predecessor_no_pull_request" if ended else _WAIT), None

    # -- GitHub -------------------------------------------------------------

    def read(self, repository_url: Any, number: int) -> tuple[str, str]:
        """(`met` | `wait` | `closed_unmerged`, an error code or "")."""
        repository = merge_repository(repository_url if isinstance(repository_url, str) else None)
        if repository is None:
            return _WAIT, "repository_unreadable"
        owner, repo = repository
        key = (owner.lower(), repo.lower(), number)
        if key not in self._readings:
            if self._token_error:
                return _WAIT, self._token_error
            if not self._token:
                try:
                    self._token = self.ctx.forge_tokens.token_for(self.tenant)
                except Exception as exc:
                    self._token_error = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                    return _WAIT, self._token_error
            try:
                with forge_limits.acting_as(self._credential):
                    pull = self.ctx.forge_writer.read_pull(
                        IssueRef(owner=owner, repo=repo, number=number), number, self._token)
            except ForgeWriteNotFound:
                # Not read as closed: a token that lost the repository answers
                # 404 too, and blocking every row on a rotated token is worse.
                return _WAIT, "pull_request_not_found"
            except Exception as exc:
                if isinstance(exc, ForgeRateLimited):
                    self.halted = exc.reset or self.now + forge_limits.DEFAULT_HOLD
                    forge_limits.QUOTAS.hold(self._sweep, self.halted)
                return _WAIT, exc.code if isinstance(exc, ApiError) else type(exc).__name__
            self._readings[key] = (bool(pull.merged), str(pull.state or ""))
        merged, state = self._readings[key]
        if merged:
            return _MET, ""
        return ("closed_unmerged" if state == "closed" else _WAIT), ""

    # -- one row ------------------------------------------------------------

    def visit(self, row: dict[str, Any]) -> None:
        row_id = str(row.get("id") or "")
        spec_url = (row.get("spec") or {}).get("repository_url")
        reads: list[tuple[Any, int]] = []
        waiting = False
        for entry in row.get("after_merge") or ():
            if not isinstance(entry, Mapping):
                continue
            if isinstance(entry.get("workflow_id"), str):
                verdict, read = self.predecessor(entry["workflow_id"])
                if verdict not in (_MET, _WAIT):
                    self._settle(row_id, HELD, BLOCKED, verdict)
                    self.report.blocked += 1
                    return
                waiting = waiting or verdict == _WAIT
                if read is not None:
                    reads.append(read)
            elif isinstance(entry.get("pull_request"), int):
                reads.append((spec_url, entry["pull_request"]))
        if waiting:
            # Not every predecessor concluded in Firestore: no GitHub read.
            self.report.waiting += 1
            return
        if reads and (self.halted is not None or self.deferral is not None):
            self.report.deferred += 1
            return
        for repository_url, number in reads:
            verdict, error = self.read(repository_url, number)
            if error:
                self.report.fail(row_id, error)
                self.report.waiting += 1
                return
            if verdict == "closed_unmerged":
                self._settle(row_id, HELD, BLOCKED, f"pull_request_{number}_closed_unmerged")
                self.report.blocked += 1
                return
            if verdict == _WAIT:
                self.report.waiting += 1
                return
        self._release(row)

    def _release(self, row: dict[str, Any]) -> None:
        from .routes.runs import run_owner_auth

        row_id = str(row["id"])
        if not self._claim(row_id):
            self.report.waiting += 1
            return
        try:
            owner = run_owner_auth(self.ctx, _Submitter(
                id=row_id, tenant_id=self.tenant_id, created_by=str(row.get("submitted_by") or "")))
            spec = WorkflowCreate.model_validate(row.get("spec") or {})
            submission = self.ctx.submissions.submit_workflow(owner, spec)
        except Exception as exc:  # one row's failure is that row's
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.warning("held workflow %s tenant=%s not released (%s)", row_id, self.tenant_id, code)
            self._settle(row_id, STARTING, FAILED, code)
            self.report.failed += 1
            self.report.fail(row_id, code)
            return
        workflow_id = submission.workflow.workflow_id
        self.db.collection(HELD_WORKFLOWS).document(row_id).update(
            {"state": RELEASED, "workflow_id": workflow_id, "released_at": self.now,
             "updated_at": self.now, "error": None})
        self.report.released += 1
        log.info("held workflow %s tenant=%s released as %s", row_id, self.tenant_id, workflow_id)

    def _claim(self, row_id: str) -> bool:
        """HELD -> STARTING in one transaction: exactly one tick submits a row."""
        ref = self.db.collection(HELD_WORKFLOWS).document(row_id)
        tenant_id, now = self.tenant_id, self.now

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("state") != HELD or data.get("tenant_id") != tenant_id:
                return False
            txn.update(ref, {"state": STARTING, "updated_at": now})
            return True

        return bool(_apply(self.db.transaction()))

    def _settle(self, row_id: str, from_state: str, state: str, why: str) -> None:
        """Move a row from `from_state` only, so a row another tick moved is left alone."""
        ref = self.db.collection(HELD_WORKFLOWS).document(row_id)
        tenant_id, now = self.tenant_id, self.now

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("state") != from_state or data.get("tenant_id") != tenant_id:
                return
            txn.update(ref, {"state": state, "error": why, "updated_at": now})

        _apply(self.db.transaction())
        log.info("held workflow %s tenant=%s %s (%s)", row_id, tenant_id, state.lower(), why)


def release_tenant(ctx: Any, tenant_id: str, *, limit: int) -> HeldReport:
    """One tick over `tenant_id`'s held workflows; see the module docstring."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    report = HeldReport()
    tick = _Tick(ctx, tenant, report)
    ledger = ctx.store.db.collection(HELD_WORKFLOWS)
    try:
        for state in (STARTING, HELD):
            query = (
                ledger.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("state", "==", state))
                .limit(limit + 1)
            )
            rows = [snap.to_dict() or {} for snap in query.stream()]
            report.truncated = report.truncated or len(rows) > limit
            for row in rows[:limit]:
                # The query filtered on both; asked again (invariant 9).
                if row.get("tenant_id") != tenant_id or row.get("state") != state:
                    continue
                report.visited += 1
                if state == STARTING:
                    since = row.get("updated_at") or row.get("created_at")
                    if since is not None and (tick.now - since).total_seconds() > START_STALE_SECONDS:
                        tick._settle(str(row.get("id")), STARTING, FAILED, "start_interrupted")
                        report.failed += 1
                    continue
                tick.visit(row)
    finally:
        tick.close()
    return report
