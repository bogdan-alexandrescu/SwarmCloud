"""Child tasks, the scheduler's half (docs/design/child-tasks.md §3.2, §3.3, §3.4).

Three things, each the scheduler's because the scheduler is the only platform
writer that runs on a clock and can query Firestore:

  * the one-use REGISTRATION NONCE passed to an attempt at dispatch, so its
    worker can register the attempt key it generated (§3.2 step 1);
  * the AWAIT SWEEP: a parent PARKED on CHILDREN_INCOMPLETE is promoted once
    every child is terminal and every SUCCEEDED child's manifest is written,
    and past `child_await_max_seconds` its outstanding children are cancelled
    so that it can be (§3.3 step 6, §5 F7);
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

from .codec import task_from_dict

log = logging.getLogger(__name__)

TASKS = "tasks"
EVENTS = "events"

#: `swarm_api.childkey.NONCE_PURPOSE`, restated: this image does not carry
#: swarm-api. tests/unit/control_plane/test_child_tasks_scheduler.py holds the
#: two derivations equal.
NONCE_PURPOSE = "swarm-child-nonce/v1"

#: `swarm_api.validation.CHILD_CASCADE_METADATA_KEY`, restated for the same
#: reason and held by the same test.
CHILD_CASCADE_METADATA_KEY = "child_cascade"

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


def children_of(db: Any, tenant_id: str, parent_task_id: str) -> list[Task]:
    """A parent's children, in its tenant only. Served by the
    `tasks-tenant-parent-created` index."""
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
    return out


def decide_await(children: list[Task]) -> AwaitDecision:
    """"Terminal with their outputs staged" (OD-B15-3): every child terminal,
    and every SUCCEEDED child's manifest written (`result_summary` set, the
    same test `swarm_api.store.Store.artifact_manifest` calls `complete`)."""
    outstanding = tuple(c.id for c in children if c.state not in TERMINAL_STATES)
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
        txn.update(ref, patch)
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
