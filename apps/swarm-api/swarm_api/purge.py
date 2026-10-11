"""The audited purge of failed history: `POST /v1/admin/history:purge`.

Owner request, 2026-10-11: delete every FAILED job and workflow from history.
Nothing in the API deleted a workflow or a task before this -- events had a
TTL and that was all -- so the history of a platform that has been failing on
purpose for weeks (acceptance runs, red lanes) is still every row of the
console's Workflows page.

WHAT IT DELETES, per terminal workflow and per standalone terminal task:

    workflows/{id}                         the workflow document (last)
    tasks/{id}                             each step task, or the task itself
    tasks/{id}/events/*                    its events
    attempts/* where task_id == id         its attempt records
    leases/*   where task_id == id         its RELEASED leases
    tenants/<t>/tasks/<id>/...             its artifacts, logs and checkpoints

and nothing else. Never `pools/`, `quota/`, `accounts/`, `account_holds/` or
`tenants/`: invariants 1-3 are kept by those documents, and a purge that
touched capacity accounting would leak or invent slots. A lease is deleted
only once it is RELEASED; a task holding an unreleased one is not purged at
all (an unreleased lease on a terminal task is a leak for the reconciler to
release, and deleting it here would hide it, not release it).

WHAT IT NEVER DELETES, each refused with its reason in `skipped`:

* a workflow with any step not terminal (`live_step`), or a task not
  terminal -- the query asks for the requested states, and the steps are
  checked one by one, because a workflow's stored state is a rollup cache;
* a task with an unreleased lease (`live_lease`);
* a task with a live child (`live_child`): the child's parent link would dangle;
* anything a LIVE issue run still references -- its `workflow_id`, its merge's
  `workflow_id`, its `planner_task_id` or `pr_task_id` (`issue_run`). An
  ended run keeps its record and may point at a purged workflow; that is the
  history the owner asked to drop;
* anything named in `exclude_ids`, a workflow included when one of its steps
  is named (a workflow cannot be half purged);
* a step task whose tenant is not its workflow's (`tenant_mismatch`): no
  shipped path writes one, and an anomaly is reported rather than acted on.

THE AUDIT COMES FIRST. One `admin_audit` entry, action `history_purged`, per
workflow or standalone task, written BEFORE anything of it is deleted, in the
house shape `{action, target_*, by, at, detail}`. It is append-only like every
other entry (admins.py): a purge that then fails is a SECOND entry,
`history_purge_failed`, never an edit of the first.

TENANT ISOLATION (invariant 9). The bucket prefix is built from the tenant
STORED on the task document, never from the request's `tenant_id` (which only
narrows the selection), and every segment passes `objects.safe_segment`. A
listing that returns a key outside that prefix fails the item before anything
is deleted.

ORDER, AND WHY A RE-RUN IS SAFE. Per item: classify, audit, delete the bucket
objects, then the Firestore documents -- subcollections and attempt/lease
records first, the task documents next, the workflow document last. Every
step deletes what is there and skips what is not, so a purge interrupted at
any point is finished by running it again: the workflow document is what the
next run finds, and it goes only once everything under it is gone. The bucket
goes before the documents because the documents are the only index of the
bucket; the other way round, a failure between the two would orphan objects
nothing can find again.

BOUNDED. One call selects at most `limit` items and scans at most
`MAX_SCANNED` documents, and returns `next_page_token` when there is more.
Firestore batches stay under its 500-operation cap (`_BATCH`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import utcnow
from swarm_common.states import TERMINAL_STATES, TaskState

from .admins import AUDIT_COLLECTION
from .errors import ValidationFailed
from .issueruns import RUNS_COLLECTION, RunState
from .objects import UnsafeKeySegment, safe_segment
from .redaction import redact_detail
from .store import ATTEMPTS, EVENTS, LEASES, TASKS, WORKFLOWS

log = logging.getLogger(__name__)

#: The only states a purge may be asked for. SUCCEEDED is terminal too and is
#: left out on purpose: the owner asked for FAILED history, and a succeeded
#: workflow is the record of work that landed.
PURGEABLE_STATES: tuple[str, ...] = (
    TaskState.FAILED.value,
    TaskState.CANCELLED.value,
    TaskState.DEAD_LETTERED.value,
)

#: The word a destructive call must carry in `confirm`.
CONFIRM_WORD = "purge"

AUDIT_PURGED = "history_purged"
AUDIT_PURGE_FAILED = "history_purge_failed"

#: Items one call selects when the caller names no limit, and the most it may name.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: Documents one call reads while selecting, whatever the limit: a history of
#: protected or excluded rows must not turn one call into a full scan.
MAX_SCANNED = 1000
_SCAN_PAGE = 200
#: Deletes per Firestore batch, under its 500-operation cap.
_BATCH = 400
#: The issue-run states that are over. A run in any other state is live, and
#: what it references is not purged.
_ENDED_RUNS = frozenset({
    RunState.DONE.value, RunState.FAILED.value, RunState.REJECTED.value,
    RunState.CANCELLED.value, RunState.NOT_READY.value,
})
_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)
_TOKEN = re.compile(r"^(w|t):([A-Za-z0-9_.-]*)$")


class PurgeFailed(Exception):
    """The bucket could not be listed or an object could not be deleted."""


class ArtifactPurger(Protocol):
    """The artifact bucket, for the purge: list one prefix, delete keys.

    Separate from `objects.ObjectReader`, which is read-only by design and
    stays so: every inspection route holds a reader, and only this route
    holds a purger.
    """

    bucket: str

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete_keys(self, keys: list[str]) -> None: ...


class GcsArtifactPurger:
    """Google Cloud Storage. Holds no client until first used, so
    `create_app()` still needs no credentials (see objects.py)."""

    def __init__(self, bucket: str, project_id: str | None = None, client: Any = None) -> None:
        if not bucket:
            raise ValueError("an artifact bucket is required to purge objects")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # lazy: create_app() must need no credentials

            self._client = storage.Client(project=self._project_id)
        return self._client

    def list_keys(self, prefix: str) -> list[str]:
        try:
            return [b.name for b in self._get_client().list_blobs(self.bucket, prefix=prefix)]
        except Exception as exc:  # noqa: BLE001 - a failed listing is never an empty one
            raise PurgeFailed(f"could not list {prefix}: {type(exc).__name__}: {exc}") from None

    def delete_keys(self, keys: list[str]) -> None:
        if not keys:
            return
        try:
            bucket = self._get_client().bucket(self.bucket)
            # `on_error` is called for a 404 only: an object already gone is
            # what a re-run finds. Every other failure (a 403 above all)
            # raises, and the item's documents are kept.
            bucket.delete_blobs([bucket.blob(k) for k in keys], on_error=lambda _blob: None)
        except Exception as exc:  # noqa: BLE001
            raise PurgeFailed(f"could not delete objects: {type(exc).__name__}: {exc}") from None


def build_artifact_purger(*, bucket: str, project_id: str | None = None) -> ArtifactPurger | None:
    """The production purger, or None when no bucket is configured -- in which
    case there are no artifacts to delete and every item counts 0 objects."""
    if not bucket:
        return None
    return GcsArtifactPurger(bucket=bucket, project_id=project_id)


def task_prefix(tenant_id: str, task_id: str) -> str:
    """`tenants/<t>/tasks/<id>/`, from the STORED tenant (invariant 9).

    The trailing slash matters: without it `task_1` would also match `task_10`.
    """
    tenant = safe_segment(tenant_id, what="tenant id")
    task = safe_segment(task_id, what="task id")
    return f"tenants/{tenant}/tasks/{task}/"


@dataclass
class PurgeQuery:
    states: tuple[str, ...]
    before: datetime | None = None
    tenant_id: str | None = None
    exclude_ids: frozenset[str] = frozenset()
    dry_run: bool = True
    limit: int = DEFAULT_LIMIT
    page_token: str | None = None


@dataclass
class _TaskPlan:
    task_id: str
    tenant_id: str
    state: str
    attempt_ids: list[str] = field(default_factory=list)
    lease_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    artifact_keys: list[str] | None = None
    artifact_error: str | None = None


@dataclass
class _Item:
    kind: str           # "workflow" | "task"
    item_id: str
    tenant_id: str
    state: str
    tasks: list[_TaskPlan]

    def artifact_count(self) -> int | None:
        if any(t.artifact_keys is None for t in self.tasks):
            return None
        return sum(len(t.artifact_keys or ()) for t in self.tasks)

    def to_api(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.item_id,
            "tenant_id": self.tenant_id,
            "state": self.state,
            "task_ids": [t.task_id for t in self.tasks],
            "attempts": sum(len(t.attempt_ids) for t in self.tasks),
            "leases": sum(len(t.lease_ids) for t in self.tasks),
            "events": sum(len(t.event_ids) for t in self.tasks),
            "artifact_objects": self.artifact_count(),
        }
        errors = [t.artifact_error for t in self.tasks if t.artifact_error]
        if errors:
            body["artifact_error"] = errors[0]
        return body


class _Skip(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def parse_query(
    *, states: list[str], before: datetime | None, tenant_id: str | None,
    exclude_ids: list[str], dry_run: bool, confirm: str | None,
    limit: int | None, page_token: str | None,
) -> PurgeQuery:
    """Validate a request body. Every refusal here is a 422 and deletes nothing."""
    wanted = tuple(dict.fromkeys(s.strip().upper() for s in states))
    if not wanted:
        raise ValidationFailed("states must name at least one of " + ", ".join(PURGEABLE_STATES))
    refused = [s for s in wanted if s not in PURGEABLE_STATES]
    if refused:
        raise ValidationFailed(
            f"states {refused} cannot be purged; only {list(PURGEABLE_STATES)} can",
            detail={"purgeable_states": list(PURGEABLE_STATES)},
        )
    if not dry_run and confirm != CONFIRM_WORD:
        raise ValidationFailed(
            f"a purge that deletes (dry_run false) must carry confirm: {CONFIRM_WORD!r}"
        )
    if before is not None and before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    if tenant_id is not None:
        try:
            safe_segment(tenant_id, what="tenant_id")
        except UnsafeKeySegment as exc:
            raise ValidationFailed(str(exc)) from None
    size = DEFAULT_LIMIT if limit is None else limit
    if size < 1:
        raise ValidationFailed("limit must be at least 1")
    if page_token is not None and page_token != "" and not _TOKEN.match(page_token):
        raise ValidationFailed("page_token is not one this route issued")
    return PurgeQuery(
        states=wanted, before=before, tenant_id=tenant_id,
        exclude_ids=frozenset(i.strip() for i in exclude_ids if i and i.strip()),
        dry_run=dry_run, limit=min(size, MAX_LIMIT), page_token=page_token or None,
    )


class HistoryPurge:
    """Selects, audits and deletes. Holds no client of its own: `db` is the
    context's Firestore (or a test's fake) and `objects` its purger."""

    def __init__(
        self, db: Any, objects: ArtifactPurger | None, *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._objects = objects
        self._now = now

    # -- the call ----------------------------------------------------------

    def run(self, query: PurgeQuery, *, by: str) -> dict[str, Any]:
        selected, skipped, scanned, next_token = self._select(query)
        if self._objects is not None:
            for item in selected:
                for plan in item.tasks:
                    self._list_artifacts(plan)

        done: list[_Item] = []
        failed: list[dict[str, Any]] = []
        if query.dry_run:
            done = selected
        else:
            for item in selected:
                try:
                    self._purge(item, by=by)
                    done.append(item)
                except PurgeFailed as exc:
                    reason = redact_detail(str(exc))
                    self._audit(AUDIT_PURGE_FAILED, item, by=by, extra={"error": reason})
                    failed.append({"kind": item.kind, "id": item.item_id, "reason": reason})
                    log.warning("history purge failed kind=%s id=%s", item.kind, item.item_id)

        rows = [i.to_api() for i in done]
        workflows = [r for r, i in zip(rows, done) if i.kind == "workflow"]
        tasks = [r for r, i in zip(rows, done) if i.kind == "task"]
        return {
            "dry_run": query.dry_run,
            "states": list(query.states),
            "before": query.before.isoformat() if query.before else None,
            "tenant_id": query.tenant_id,
            # Dry run: what WOULD be deleted. Otherwise: what WAS.
            "workflows": workflows,
            "tasks": tasks,
            "skipped": skipped,
            "failed": failed,
            "counts": {
                "workflows": len(workflows),
                "standalone_tasks": len(tasks),
                "task_documents": sum(len(r["task_ids"]) for r in rows),
                "attempts": sum(r["attempts"] for r in rows),
                "leases": sum(r["leases"] for r in rows),
                "events": sum(r["events"] for r in rows),
                "artifact_objects": sum(r["artifact_objects"] or 0 for r in rows),
                "skipped": len(skipped),
                "failed": len(failed),
            },
            "scanned": scanned,
            "next_page_token": next_token,
        }

    def _select(
        self, query: PurgeQuery,
    ) -> tuple[list[_Item], list[dict[str, Any]], int, str | None]:
        """Workflows first, then standalone tasks, each in document-id order,
        until `limit` items are chosen or `MAX_SCANNED` documents are read."""
        selected: list[_Item] = []
        skipped: list[dict[str, Any]] = []
        scanned = 0
        start, after = "w", None
        if query.page_token:
            match = _TOKEN.match(query.page_token)
            assert match is not None  # parse_query checked it
            start, after = match.group(1), (match.group(2) or None)

        def full() -> bool:
            return len(selected) >= query.limit or scanned >= MAX_SCANNED

        for phase in ("w", "t")[("w", "t").index(start):]:
            kind = "workflow" if phase == "w" else "task"
            cursor = after if phase == start else None
            exhausted = False
            while not full():
                page = self._page(WORKFLOWS if phase == "w" else TASKS, query, cursor)
                for snap in page:
                    scanned += 1
                    cursor = snap.id
                    try:
                        item = (self._plan_workflow(snap.id, snap.to_dict() or {}, query)
                                if phase == "w"
                                else self._plan_task(snap.id, snap.to_dict() or {}, query))
                    except _Skip as skip:
                        skipped.append({"kind": kind, "id": snap.id,
                                        "reason": skip.reason, "detail": skip.detail})
                        item = None
                    if item is not None:
                        selected.append(item)
                    if full():
                        break
                if len(page) < _SCAN_PAGE and (not page or cursor == page[-1].id):
                    exhausted = True
                    break
            if not exhausted:
                # Stopped inside this collection: the next call resumes after
                # the last document read, whether or not it was selected.
                return selected, skipped, scanned, f"{phase}:{cursor or ''}"
        return selected, skipped, scanned, None

    # -- selection ---------------------------------------------------------

    def _page(self, collection: str, query: PurgeQuery, after: str | None) -> list[Any]:
        q: Any = self._db.collection(collection).where(
            filter=FieldFilter("state", "in", list(query.states)))
        if query.tenant_id is not None:
            q = q.where(filter=FieldFilter("tenant_id", "==", query.tenant_id))
        q = q.order_by("__name__")
        if after is not None:
            q = q.start_after({"__name__": after})
        return list(q.limit(_SCAN_PAGE).stream())

    def _old_enough(self, data: dict[str, Any], query: PurgeQuery) -> bool:
        if query.before is None:
            return True
        moment = data.get("updated_at") or data.get("created_at")
        if not isinstance(moment, datetime):
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment < query.before

    def _plan_workflow(self, workflow_id: str, data: dict[str, Any],
                       query: PurgeQuery) -> _Item | None:
        if not self._old_enough(data, query):
            return None
        tenant = str(data.get("tenant_id") or "")
        if workflow_id in query.exclude_ids:
            raise _Skip("excluded", "named in exclude_ids")
        self._issue_run_guard("workflow_id", workflow_id)
        self._issue_run_guard("merge.workflow_id", workflow_id)
        task_ids: list[str] = []
        for step in data.get("steps") or []:
            if isinstance(step, dict) and step.get("task_id"):
                task_ids.append(str(step["task_id"]))
        for snap in (self._db.collection(TASKS)
                     .where(filter=FieldFilter("workflow_id", "==", workflow_id)).stream()):
            task_ids.append(snap.id)
        plans: list[_TaskPlan] = []
        for task_id in dict.fromkeys(task_ids):
            if task_id in query.exclude_ids:
                raise _Skip("excluded", f"its step task {task_id} is named in exclude_ids")
            snap = self._db.collection(TASKS).document(task_id).get()
            if not snap.exists:
                continue  # a re-run after a partial purge: already gone
            task = snap.to_dict() or {}
            if task.get("state") not in _TERMINAL:
                raise _Skip("live_step", f"step task {task_id} is {task.get('state')}")
            if str(task.get("tenant_id") or "") != tenant:
                raise _Skip("tenant_mismatch",
                            f"step task {task_id} is stored under another tenant")
            plans.append(self._plan_one(task_id, task))
        return _Item("workflow", workflow_id, tenant, str(data.get("state")), plans)

    def _plan_task(self, task_id: str, data: dict[str, Any], query: PurgeQuery) -> _Item | None:
        workflow_id = data.get("workflow_id")
        if workflow_id and self._db.collection(WORKFLOWS).document(str(workflow_id)).get().exists:
            # A step goes with its workflow, never on its own.
            return None
        if not self._old_enough(data, query):
            return None
        if task_id in query.exclude_ids:
            raise _Skip("excluded", "named in exclude_ids")
        plan = self._plan_one(task_id, data)
        return _Item("task", task_id, str(data.get("tenant_id") or ""),
                     str(data.get("state")), [plan])

    def _plan_one(self, task_id: str, data: dict[str, Any]) -> _TaskPlan:
        """Everything of one terminal task, or a _Skip saying why not."""
        tenant = str(data.get("tenant_id") or "")
        try:
            task_prefix(tenant, task_id)
        except UnsafeKeySegment as exc:
            raise _Skip("unsafe_id", str(exc)) from None
        self._issue_run_guard("planner_task_id", task_id)
        self._issue_run_guard("pr_task_id", task_id)
        for child in (self._db.collection(TASKS)
                      .where(filter=FieldFilter("parent_task_id", "==", task_id)).stream()):
            if (child.to_dict() or {}).get("state") not in _TERMINAL:
                raise _Skip("live_child", f"child task {child.id} of {task_id} is not terminal")
        plan = _TaskPlan(task_id=task_id, tenant_id=tenant, state=str(data.get("state")))
        for lease in (self._db.collection(LEASES)
                      .where(filter=FieldFilter("task_id", "==", task_id)).stream()):
            body = lease.to_dict() or {}
            if body.get("released_at") is None:
                raise _Skip("live_lease", f"task {task_id} holds unreleased lease {lease.id}")
            plan.lease_ids.append(lease.id)
        current = data.get("current_lease_id")
        if current and current not in plan.lease_ids:
            snap = self._db.collection(LEASES).document(str(current)).get()
            if snap.exists and (snap.to_dict() or {}).get("released_at") is None:
                raise _Skip("live_lease", f"task {task_id} holds unreleased lease {current}")
        for attempt in (self._db.collection(ATTEMPTS)
                        .where(filter=FieldFilter("task_id", "==", task_id)).stream()):
            plan.attempt_ids.append(attempt.id)
        plan.event_ids = [e.id for e in
                          self._db.collection(TASKS).document(task_id).collection(EVENTS).stream()]
        return plan

    def _issue_run_guard(self, field_path: str, value: str) -> None:
        for run in (self._db.collection(RUNS_COLLECTION)
                    .where(filter=FieldFilter(field_path, "==", value)).stream()):
            state = (run.to_dict() or {}).get("state")
            if state not in _ENDED_RUNS:
                raise _Skip("issue_run", f"issue run {run.id} is {state} and references {value}")

    def _list_artifacts(self, plan: _TaskPlan) -> None:
        assert self._objects is not None
        prefix = task_prefix(plan.tenant_id, plan.task_id)
        try:
            keys = self._objects.list_keys(prefix)
        except PurgeFailed as exc:
            plan.artifact_keys = None
            plan.artifact_error = redact_detail(str(exc))
            return
        stray = [k for k in keys if not k.startswith(prefix)]
        if stray:
            # The isolation boundary is the prefix. A store that answers with
            # anything outside it is not one this route deletes from.
            plan.artifact_keys = None
            plan.artifact_error = f"the listing of {prefix} returned keys outside it"
            return
        plan.artifact_keys = keys

    # -- the delete --------------------------------------------------------

    def _purge(self, item: _Item, *, by: str) -> None:
        unlisted = [t for t in item.tasks if t.artifact_keys is None and self._objects is not None]
        if unlisted:
            raise PurgeFailed(unlisted[0].artifact_error or "the artifacts could not be listed")
        self._audit(AUDIT_PURGED, item, by=by)
        for plan in item.tasks:
            self._recheck(plan)
            if self._objects is not None and plan.artifact_keys:
                self._objects.delete_keys(list(plan.artifact_keys))
        refs: list[Any] = []
        for plan in item.tasks:
            task_ref = self._db.collection(TASKS).document(plan.task_id)
            refs += [task_ref.collection(EVENTS).document(e) for e in plan.event_ids]
            refs += [self._db.collection(ATTEMPTS).document(a) for a in plan.attempt_ids]
            refs += [self._db.collection(LEASES).document(lid) for lid in plan.lease_ids]
        self._delete(refs)
        for plan in item.tasks:
            self._delete_task(plan)
        if item.kind == "workflow":
            self._delete([self._db.collection(WORKFLOWS).document(item.item_id)])
        log.info("history purged kind=%s id=%s tenant=%s tasks=%d",
                 item.kind, item.item_id, item.tenant_id, len(item.tasks))

    def _recheck(self, plan: _TaskPlan) -> None:
        """Read the task again just before its objects go: a task that has
        left its terminal state since it was selected keeps everything."""
        snap = self._db.collection(TASKS).document(plan.task_id).get()
        if snap.exists and (snap.to_dict() or {}).get("state") not in _TERMINAL:
            raise PurgeFailed(f"task {plan.task_id} is no longer terminal; nothing of it was deleted")

    def _delete_task(self, plan: _TaskPlan) -> None:
        """The task document, in a transaction that checks it is still
        terminal and holds no live lease -- the last line before it is gone."""
        ref = self._db.collection(TASKS).document(plan.task_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return
            data = snap.to_dict() or {}
            if data.get("state") not in _TERMINAL:
                raise PurgeFailed(f"task {plan.task_id} is no longer terminal; its document was kept")
            lease_id = data.get("current_lease_id")
            if lease_id:
                lease = _snapshot(txn.get(self._db.collection(LEASES).document(str(lease_id))))
                if lease.exists and (lease.to_dict() or {}).get("released_at") is None:
                    raise PurgeFailed(f"task {plan.task_id} holds a live lease; its document was kept")
            txn.delete(ref)

        _apply(transaction)

    def _delete(self, refs: list[Any]) -> None:
        for start in range(0, len(refs), _BATCH):
            batch = self._db.batch()
            for ref in refs[start:start + _BATCH]:
                batch.delete(ref)
            batch.commit()

    def _audit(self, action: str, item: _Item, *, by: str,
               extra: dict[str, Any] | None = None) -> None:
        """One `admin_audit` entry. Created, never updated (admins.py)."""
        detail: dict[str, Any] = {
            "kind": item.kind,
            "tenant_id": item.tenant_id,
            "state": item.state,
            "task_ids": [t.task_id for t in item.tasks],
            "attempt_ids": [a for t in item.tasks for a in t.attempt_ids],
            "lease_ids": [lid for t in item.tasks for lid in t.lease_ids],
            "event_count": sum(len(t.event_ids) for t in item.tasks),
            "artifact_objects": item.artifact_count(),
        }
        detail.update(extra or {})
        self._db.collection(AUDIT_COLLECTION).document().set({
            "action": action,
            "target_kind": item.kind,
            "target_id": item.item_id,
            "by": by,
            "at": self._now(),
            "detail": detail,
        })
