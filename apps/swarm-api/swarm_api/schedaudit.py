"""`schedule_audit/{id}`: the schedules' append-only audit (docs/schedules.md §4.8, lane S5).

APPEND-ONLY, like `admin_audit`. This module writes an entry and reads
entries; it has no update and no delete path, so nothing that imports it can
rewrite what was recorded. An entry is written IN THE TRANSACTION of the
change it records -- `append` takes the caller's transaction -- so a change
and its audit cannot disagree.

Fields: `{schedule_id, tenant_id, action, by, at, detail}`. `by` is an email,
`schedule-tick` or `admin:<email>`. Private (Firestore): the console shows it
to the tenant's members, the admin view to admins.

THE SHAPE S3 ALREADY WRITES. `routes/schedules.py` (lane S3) wrote its
create, edit, pause and delete entries before this module existed, with the
same fields and the same id; S3's docstring names this module as the owner.
An id sorts newest first -- `{schedule_id}:{inverted ms}:{random}` -- so the
audit is read with an equality filter on `schedule_id` ordered by
`__name__`, the shape the automatic single-field index serves, needing no
composite index.

WHAT LANE S5 WRITES HERE: every approval decision (`approval_approved`,
`approval_rejected`, `approval_expired`), every hard stop that held a
scheduled run (`hard_stop`), and the SD3 merge switch (`gate_merge_auto`,
`gate_merge_approve`) with the before and after of `gate`.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

COLLECTION = "schedule_audit"

#: Ids sort newest first: this minus the entry's Unix milliseconds,
#: zero-padded. 10**13 ms is the year 2286. The same value S3 uses.
_EPOCH_MS = 10**13

#: Actions that change who the schedule's LAST EDITOR of its gate, scope or
#: spec is (§4.6: that person may not approve its merge-tier items or switch
#: its merge to automatic when the tenant has more than one member).
_GATE_ACTIONS = frozenset({"create", "gate_merge_auto", "gate_merge_approve"})
#: The `edit` fields that are "gate, scope or spec": a `custom-prompt`'s spec
#: and every other type's work are its `params`.
_GATE_FIELDS = frozenset({"gate", "scope", "params"})

#: How many entries `last_editor` reads, newest first, before it stops. An
#: audit is a few entries a day; past this many without a gate edit the
#: creator is the last editor, and the create entry is read for that.
EDITOR_SCAN = 200

#: Approval decisions and the merge switch, as `action`.
APPROVED = "approval_approved"
REJECTED = "approval_rejected"
EXPIRED = "approval_expired"
HARD_STOP = "hard_stop"
GATE_MERGE_AUTO = "gate_merge_auto"
GATE_MERGE_APPROVE = "gate_merge_approve"


def entry_id(schedule_id: str, at: datetime) -> str:
    ms = int(at.timestamp() * 1000)
    return f"{schedule_id}:{_EPOCH_MS - ms:013d}:{secrets.token_hex(4)}"


def append(
    txn: Any,
    db: Any,
    *,
    schedule_id: str,
    tenant_id: str,
    action: str,
    by: str,
    at: datetime,
    detail: Mapping[str, Any] | None = None,
) -> str:
    """One entry, inside `txn` (or a write batch). Never updated afterwards."""
    if not schedule_id or not tenant_id:
        raise ValueError("a schedule audit entry names its schedule and its tenant")
    entry = entry_id(schedule_id, at)
    txn.set(db.collection(COLLECTION).document(entry), {
        "schedule_id": schedule_id,
        "tenant_id": tenant_id,
        "action": action,
        "by": by,
        "at": at,
        "detail": dict(detail or {}),
    })
    return entry


def entries(db: Any, tenant_id: str, schedule_id: str, *, limit: int) -> list[dict[str, Any]]:
    """The schedule's newest `limit` entries, newest first, this tenant's only."""
    query = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("schedule_id", "==", schedule_id))
        .order_by("__name__")
        .limit(limit)
    )
    rows = [snap.to_dict() or {} for snap in query.stream()]
    # The filter again, in the application: an index mistake must not move
    # the tenant boundary.
    return [r for r in rows if r.get("tenant_id") == tenant_id and r.get("schedule_id") == schedule_id]


def edits_gate(entry: Mapping[str, Any]) -> bool:
    """Whether an entry records a change of the schedule's gate, scope or spec."""
    action = entry.get("action")
    if action in _GATE_ACTIONS:
        return True
    if action == "edit":
        changed = (entry.get("detail") or {}).get("changed") or {}
        return bool(_GATE_FIELDS & set(changed))
    return False


def last_editor(db: Any, tenant_id: str, schedule: Mapping[str, Any]) -> str | None:
    """Who last changed the schedule's gate, scope or spec (§4.6), or None.

    Read from the audit, which records every such change with its author, so
    the schedule document needs no field of its own for it. With no such
    entry in the newest EDITOR_SCAN, the schedule's creator: a create sets all
    three.
    """
    for entry in entries(db, tenant_id, str(schedule.get("schedule_id") or ""), limit=EDITOR_SCAN):
        if edits_gate(entry):
            return _person(entry.get("by"))
    return _person(schedule.get("created_by"))


def _person(by: Any) -> str | None:
    """The email behind `by`: an admin's action is `admin:<email>`, the tick's is nobody."""
    text = str(by or "").strip().lower()
    if not text or text == "schedule-tick":
        return None
    return text.removeprefix("admin:")
