"""Child tasks, the scheduler's half (docs/design/child-tasks.md §3.2, §3.3, §3.4).

Three things, each the scheduler's because the scheduler is the only platform
writer that runs on a clock and can query Firestore:

  * the one-use REGISTRATION NONCE passed to an attempt at dispatch, so its
    worker can register the attempt key it generated (§3.2 step 1);
  * the AWAIT SWEEP: a parent PARKED on CHILDREN_INCOMPLETE is promoted once
    every child is terminal and every SUCCEEDED child's manifest is written,
    and past `child_await_max_seconds` its outstanding children are cancelled
    so that it can be (§3.3 step 6, §5 F7). Its child issue runs and child
    workflows (swarm-api's `child_runs` ledger, agent-autonomy.md §3.6) count
    too, until each has ended;
  * the CASCADE SWEEP: a non-terminal child whose parent is terminal or
    carries `cancel_requested` is cancelled (§3.4 step 2). The API cascades at
    once for responsiveness; this makes it certain.

Nothing here takes or releases capacity. A promotion writes READY, which
costs nothing until admission leases it (invariant 1); a cascade cancels a
child that holds nothing outright and only FLAGS one that holds a lease, which
its worker or the reconciler releases (invariant 1 again). Every query is
tenant-scoped or compares tenants before acting (invariant 9, §5 F11).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.field_path import FieldPath

from swarm_common.admission import _snapshot
from swarm_common.models import EndCause, Task, new_id
from swarm_common.states import (
    PENDING_STATES,
    TERMINAL_STATES,
    EventType,
    TaskState,
    assert_transition,
)
from swarm_rollup import derived_state_write

from .codec import task_from_dict

log = logging.getLogger(__name__)

TASKS = "tasks"
EVENTS = "events"

#: swarm-api's children ledger for the run kinds (`swarm_api.children`), and
#: the documents its rows point at (`swarm_api.issueruns.RUNS_COLLECTION`,
#: `swarm_api.store.WORKFLOWS`), restated: this image does not carry
#: swarm-api. tests/unit/scheduler/test_await_counts_run_children.py holds
#: every name below equal to its original.
CHILD_RUNS = "child_runs"
ISSUE_RUNS = "issue_runs"
WORKFLOWS = "workflows"
#: A ledger row whose run or workflow exists; only then is there a state to read.
STARTED = "STARTED"
#: A ledger row that will never start anything more. HELD and STARTING rows
#: have not ended: a held `after_merged` run is waiting for its predecessor.
LEDGER_ENDED = frozenset({"FAILED", "CANCELLED"})
#: `swarm_api.issueruns.TERMINAL_RUN_STATES`, by value: for an issue run
#: "ended" is one of these (§3.6).
TERMINAL_RUN_STATES = frozenset({"DONE", "FAILED", "REJECTED", "CANCELLED", "NOT_READY"})

#: `swarm_api.childkey.NONCE_PURPOSE`, restated: this image does not carry
#: swarm-api. tests/unit/control_plane/test_child_tasks_scheduler.py holds the
#: two derivations equal.
NONCE_PURPOSE = "swarm-child-nonce/v1"

#: `swarm_api.validation.CHILD_CASCADE_METADATA_KEY`, restated for the same
#: reason and held by the same test.
CHILD_CASCADE_METADATA_KEY = "child_cascade"


def cancel_end_cause(metadata: Any) -> EndCause:
    """Why a cancelled task ended: CHILD_CASCADE when its parent's cancel, end
    or await deadline flagged it (contract request 41), else CANCEL_REQUESTED.

    The rule `agent_worker.control.cancel_end_cause` applies, over the task's
    `metadata` alone so a raw document and a `Task` both answer it. Every
    scheduler writer that ends a task CANCELLED because of `cancel_requested`
    asks this, so a flagged child ends `child_cascade` whichever of them
    reaches it first: the dispatch-failure cancel, admission's cancel and the
    exhausted-retry cancel.
    """
    if isinstance(metadata, dict) and metadata.get(CHILD_CASCADE_METADATA_KEY):
        return EndCause.CHILD_CASCADE
    return EndCause.CANCEL_REQUESTED

#: The cascade's reasons (§3.4), event detail under `EndCause.CHILD_CASCADE`.
PARENT_CANCELLED = "parent_cancelled"
PARENT_ENDED = "parent_ended"
AWAIT_EXPIRED = "await_expired"

#: Children read per parent. The route caps a task at `max_children_per_task`
#: (16); this is the backstop.
_CHILD_CAP = 200

#: The states a child can be cancelled out of: every state that is not
#: terminal. Seven values, inside Firestore's `in` cap of 30.
_LIVE_STATES = tuple(sorted(s.value for s in TaskState if s not in TERMINAL_STATES))


def registration_nonce(
    key: str, *, tenant_id: str, task_id: str, attempt_id: str, lease_id: str, generation: int
) -> str:
    """HMAC-SHA256(swarm-child-key, "swarm-child-nonce/v1\\n" tuple), hex."""
    message = "\n".join((tenant_id, task_id, attempt_id, lease_id, str(generation)))
    return hmac.new(
        key.encode("utf-8"), f"{NONCE_PURPOSE}\n{message}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class AwaitDecision:
    """What the await sweep decided for one parent."""

    #: Children still not terminal.
    outstanding: tuple[str, ...]
    #: SUCCEEDED children whose artifact manifest is not written yet.
    unstaged: tuple[str, ...]

    @property
    def settled(self) -> bool:
        return not self.outstanding and not self.unstaged


class ChildList(list):
    """A parent's agent children, as `list[Task]`, carrying `runs`: the ids of
    its child runs (the `child_runs` ledger) that have not ended.

    One value rather than a second argument so the await sweep's one call,
    `decide_await(children_of(...))`, reads both kinds; a plain list is still
    a valid answer and means "no run children read"."""

    def __init__(self, tasks: Any = (), *, runs: tuple[str, ...] = ()) -> None:
        super().__init__(tasks)
        self.runs = runs


def children_of(db: Any, tenant_id: str, parent_task_id: str) -> ChildList:
    """A parent's children, in its tenant only: its agent children (served by
    the `tasks-tenant-parent-created` index) and, as `runs`, its run children
    that have not ended (`run_children_outstanding`)."""
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("parent_task_id", "==", parent_task_id))
        .limit(_CHILD_CAP)
    )
    out = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        if data.get("tenant_id") == tenant_id and data.get("parent_task_id") == parent_task_id:
            out.append(task_from_dict(data))
    return ChildList(out, runs=run_children_outstanding(db, tenant_id, parent_task_id))


def run_children_outstanding(db: Any, tenant_id: str, parent_task_id: str) -> tuple[str, ...]:
    """The parent's child issue runs and child workflows that have not ended.

    A ledger row is outstanding while it is HELD or STARTING, or STARTED with
    its run (or workflow) not terminal -- the rule `swarm_api.children`'s
    listing applies, so the parent the worker resumes and the sweep that
    resumes it agree on "ended". The query is the ledger's own
    `(tenant_id, parent_task_id)` read, and every row, run and workflow is
    compared to the parent's tenant before it counts (invariant 9): another
    tenant's row never holds this parent, and a run or workflow of another
    tenant, or one that is gone, reads as ended, as the listing's `missing`.
    """
    query = (
        db.collection(CHILD_RUNS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("parent_task_id", "==", parent_task_id))
        .limit(_CHILD_CAP)
    )
    out = []
    for snap in query.stream():
        row = snap.to_dict() or {}
        if row.get("tenant_id") != tenant_id or row.get("parent_task_id") != parent_task_id:
            continue
        if _run_child_live(db, tenant_id, row):
            out.append(str(row.get("child_id") or snap.id))
    return tuple(out)


def _run_child_live(db: Any, tenant_id: str, row: dict[str, Any]) -> bool:
    state = row.get("state")
    if state in LEDGER_ENDED:
        return False
    if state != STARTED:
        return True  # HELD or STARTING: it has not run yet
    if row.get("run_id"):
        collection, key, terminal = ISSUE_RUNS, row["run_id"], TERMINAL_RUN_STATES
    elif row.get("workflow_id"):
        collection, key = WORKFLOWS, row["workflow_id"]
        terminal = frozenset(s.value for s in TERMINAL_STATES)
    else:
        return False  # STARTED with nothing started: nothing to wait for
    snap = db.collection(collection).document(key).get()
    data = snap.to_dict() if snap.exists else None
    if data is None or data.get("tenant_id") != tenant_id:
        return False
    return data.get("state") not in terminal


def decide_await(children: list[Task]) -> AwaitDecision:
    """"Terminal with their outputs staged" (OD-B15-3): every child terminal,
    and every SUCCEEDED child's manifest written (`result_summary` set, the
    same test `swarm_api.store.Store.artifact_manifest` calls `complete`).

    A `ChildList`'s `runs` -- the child runs that have not ended -- are
    outstanding exactly as an unfinished agent child is (§3.6), so a parent
    waiting on run children alone is not promoted early. Past the deadline
    the sweep's cascade finds no task by a run child's id and leaves it be:
    the scheduler does not cancel runs, and the parent waits for them to end.
    """
    outstanding = tuple(c.id for c in children if c.state not in TERMINAL_STATES)
    outstanding += tuple(getattr(children, "runs", ()))
    unstaged = tuple(
        c.id for c in children if c.state is TaskState.SUCCEEDED and not c.result_summary
    )
    return AwaitDecision(outstanding=outstanding, unstaged=unstaged)


def await_deadline(parent: Task, seconds: int) -> datetime:
    """When an awaiting parent stops waiting. The worker's await park writes
    `next_eligible_at` = the instant it parked; `updated_at` stands in for a
    park that carries none."""
    since = parent.next_eligible_at or parent.updated_at
    return since + timedelta(seconds=seconds)


def cascade_reason(parent: Task | None) -> str | None:
    """Why a child of `parent` must be cancelled now, or None to leave it (§3.4)."""
    if parent is None:
        return None
    if parent.state is TaskState.CANCELLED:
        return PARENT_CANCELLED
    if parent.state in TERMINAL_STATES:
        # FAILED or DEAD_LETTERED (F6) -- and a SUCCEEDED parent, which the
        # worker's implicit await should never produce over live children:
        # either way nothing is left to consume their outputs.
        return PARENT_ENDED
    if parent.cancel_requested:
        return PARENT_CANCELLED
    return None


def cancel_child(
    db: Any, child_id: str, *, tenant_id: str, parent_task_id: str, why: str, now: datetime
) -> str | None:
    """One child, one transaction, `swarm_api.children.cascade_cancel_child`'s shape.

    Returns "cancelled" (it held nothing and is CANCELLED with CHILD_CASCADE),
    "flagged" (it holds a lease: `cancel_requested` and the marker are set, and
    its worker or the reconciler ends it) or None (nothing to do: terminal,
    already flagged, gone, or not this parent's child in this tenant).
    """
    ref = db.collection(TASKS).document(child_id)

    @firestore.transactional
    def _apply(txn: Any) -> str | None:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None:
            return None
        if data.get("tenant_id") != tenant_id or data.get("parent_task_id") != parent_task_id:
            log.warning(
                "child cascade skipped task=%s: not a child of parent=%s in its tenant",
                child_id,
                parent_task_id,
            )
            return None
        state = TaskState(data["state"])
        if state in TERMINAL_STATES:
            return None
        metadata = dict(data.get("metadata") or {})
        if data.get("cancel_requested") and metadata.get(CHILD_CASCADE_METADATA_KEY):
            return None
        metadata[CHILD_CASCADE_METADATA_KEY] = {"why": why, "parent_task_id": parent_task_id}
        patch: dict[str, Any] = {"cancel_requested": True, "metadata": metadata, "updated_at": now}
        immediate = state in PENDING_STATES
        if immediate:
            assert_transition(state, TaskState.CANCELLED)
            patch.update(
                {
                    "state": TaskState.CANCELLED.value,
                    "completed_at": now,
                    "end_cause": EndCause.CHILD_CASCADE.value,
                    "park_reason": None,
                    "blocked_by": [],
                    "next_eligible_at": None,
                    "last_error": f"cancelled because of its parent {parent_task_id}: {why}",
                }
            )
        # A child that is a workflow's step moves that workflow's derived
        # state: written in this transaction, read before its first write
        # (#2838). No `workflow_id`, no read.
        workflow = (
            derived_state_write(txn, db, tenant_id=tenant_id, workflow_id=data.get("workflow_id"),
                                now=now, changed={child_id: {**data, **patch}})
            if immediate else None
        )
        txn.update(ref, patch)
        if workflow is not None:
            txn.update(*workflow)
        event_id = new_id("ev")
        txn.set(
            ref.collection(EVENTS).document(event_id),
            {
                "event_id": event_id,
                "task_id": child_id,
                "tenant_id": tenant_id,
                "type": (EventType.CANCELLED if immediate else EventType.CANCEL_REQUESTED).value,
                "at": now,
                "attempt_id": None,
                "lease_id": None,
                "generation": None,
                "detail": {
                    "requested_by": f"cascade:{parent_task_id}",
                    "by": "scheduler",
                    "why": why,
                    "from_state": state.value,
                    "phase": "cancelled" if immediate else "cancel_requested",
                },
            },
        )
        return "cancelled" if immediate else "flagged"

    return _apply(db.transaction())


def live_children_page(db: Any, *, limit: int, after: tuple[str, str] | None) -> list[Task]:
    """One page of every non-terminal child, ordered by parent then id.

    `parent_task_id > ""` excludes every task that is not a child (Firestore
    leaves a document without the field, or with null, out of a range
    filter). Served by the `tasks-state-parent` composite index. Children of
    one parent are adjacent, so a page reads each parent once.
    """
    doc_id = FieldPath.document_id()
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("state", "in", list(_LIVE_STATES)))
        .where(filter=FieldFilter("parent_task_id", ">", ""))
        .order_by("parent_task_id")
        .order_by(doc_id)
    )
    if after is not None:
        query = query.start_after({"parent_task_id": after[0], doc_id: after[1]})
    return [task_from_dict(snap.to_dict()) for snap in query.limit(limit).stream()]
