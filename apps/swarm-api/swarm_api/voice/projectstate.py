"""Voice it's project states and who moves them (docs/design/voice-it.md
§3.13.2; the owner's answers of 11 and 12 November 2026).

THE FIVE STATES, stored in the project document's `status`:

    in_progress  In progress    work is going on; resume it to continue
    waiting      Waiting on you nothing moves until the owner decides
    completed    Completed      finished; Reopen to do more
    cancelled    Cancelled      stopped on purpose, with its running work
    archived     Archived       put away, kept whole

Old rows are read without a migration (`store.read_state`): `active` reads
`in_progress`, `shipped` `completed`, a missing value `in_progress`. The new
value is written by the project's next move, recorded like any other.

WHO MOVES WHAT.

* The planner (`planner_move`, its `set_project_state` tool) moves between
  `in_progress`, `waiting` and `completed`. `completed` is refused (409) while
  a start of the project is queued or running (read as the roster reads
  them: `voice_starts` by id, follow-ups by the project's tag) or the brief's
  `open` list is not empty; any move of a `cancelled` or `archived` project is
  refused (409): those are the owner's.
* A person (`person_move`, `POST /v1/voice/projects/{id}/state`): Mark
  completed, Cancel, Archive, Unarchive (back to the state it had) and Reopen.
* A merge (`settle`, on read, no new job): a project in progress or waiting
  whose work has all ended, with something done that no earlier move saw,
  whose brief has nothing open and none of whose cards waits on the owner
  moves to `completed` with `by: merge`. The move is a transaction that holds
  only while the state is still the one read, so two readers move it once.
  Each move out of `completed` records the done work it saw (`merge_seen`),
  so a Reopen is not undone by the next read. Every reader §3.13.2 names
  settles it (the owner's pick, 12 November 2026): Projects' list and get
  (`VoiceStore._view`), and through `settle_on_read` a session start on the
  project (`POST /v1/voice/sessions`), the follower's tick while a session
  is live (its `{roster}` report on `POST .../sessions/{id}/work`), and, as
  they land, a session's switch (P4) and the catch-up (P6).

A PERSON'S CANCEL moves the project to `cancelled` first, then cancels each
of its queued or running starts through the platform's own cancel for its
kind, as the person (`Cancel`, given by the route): an agent by
`POST /v1/tasks/{id}/cancel`, a workflow by `POST /v1/workflows/{id}/cancel`,
an issue run through its running agent (its CI fix workflow, its workflow or
its planner task: a run whose agent is cancelled ends cancelled, and there
is no run cancel route). A start that ended between the list and the cancel
is skipped; one whose cancel fails stays listed on the project
(`still_running`) and its failure is in the move's `voice_audit` row. The
platform's cancel is the only path, so a cancelled attempt's lease, fencing
generation and capacity are released as for any other cancel.

Every move appends `{from, to, by, why, at}` to `state_history` (the last
50); a person's move writes a `voice_audit` row in the same transaction.

INVARIANTS. A state is a field of a document: it admits, reserves and runs
nothing. Every read and write is the caller's own project in the caller's
tenant (`VoiceStore._owned` and the transaction's own check: another person's
project, another tenant's and a missing one are the same 404).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore

from swarm_common.admission import _snapshot

from ..errors import Conflict, NotFound, ValidationFailed
from ..redaction import redact_detail
from . import roster as vroster
from . import store as vstore

log = logging.getLogger(__name__)

IN_PROGRESS = vstore.IN_PROGRESS
WAITING = vstore.WAITING
COMPLETED = vstore.COMPLETED
CANCELLED = vstore.CANCELLED
ARCHIVED = vstore.ARCHIVED
STATES = vstore.STATUSES

#: What Projects shows, and its tooltip's one line (§3.13.2).
SHOWN: Mapping[str, str] = {
    IN_PROGRESS: "In progress", WAITING: "Waiting on you", COMPLETED: "Completed",
    CANCELLED: "Cancelled", ARCHIVED: "Archived",
}
TOOLTIPS: Mapping[str, str] = {
    IN_PROGRESS: "Work on this project is going on; resume it to continue.",
    WAITING: "Nothing moves until you decide something; resume it to see what.",
    COMPLETED: "Finished: what it set out to do is done. Reopen it to do more.",
    CANCELLED: "Stopped on purpose before it was finished, with the work it had running; "
               "its record is kept.",
    ARCHIVED: "Put away: hidden from the default list, kept whole.",
}

#: Who moved a project, as `state_history` records it.
BY_PLANNER = "planner"
BY_PERSON = "person"
BY_SESSION_END = "session_end"
BY_MERGE = "merge"

#: The planner's states; the other two are the owner's alone.
PLANNER_STATES = (IN_PROGRESS, WAITING, COMPLETED)
OWNERS_STATES = (CANCELLED, ARCHIVED)

#: A person's actions: the states each moves from, and the state it moves to
#: (Unarchive's is the state the project had when it was archived).
COMPLETE = "complete"
CANCEL = "cancel"
ARCHIVE = "archive"
UNARCHIVE = "unarchive"
REOPEN = "reopen"
PERSON_ACTIONS: Mapping[str, tuple[tuple[str, ...], str | None]] = {
    COMPLETE: ((IN_PROGRESS, WAITING), COMPLETED),
    CANCEL: ((IN_PROGRESS, WAITING), CANCELLED),
    ARCHIVE: ((IN_PROGRESS, WAITING, COMPLETED, CANCELLED), ARCHIVED),
    UNARCHIVE: ((ARCHIVED,), None),
    REOPEN: ((COMPLETED, CANCELLED), IN_PROGRESS),
}

AUDIT_STATE = "voice_project_state"
#: The roster states a Cancel stops and a completion waits for.
LIVE_ROWS = (vroster.QUEUED, vroster.RUNNING)
#: The roster states that keep a merge from completing the project.
UNENDED_ROWS = (vroster.QUEUED, vroster.RUNNING, vroster.WAITING)
WHY_MAX = 300
REASON_MAX = 300
#: The done work a project remembers having seen (`merge_seen`).
MERGE_SEEN_MAX = 200

#: The platform's cancel, as the person: (kind, id), kind "agent" or
#: "workflow"; it raises when the cancel did not happen.
Cancel = Callable[[str, str], None]


class StateRefused(Conflict):
    """A move its mover may not make, or not now (409)."""


def _why(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationFailed("why is text", detail={"field": "why"})
    text = " ".join(value.split())
    return text[:WHY_MAX] or None


def _key(row: Mapping[str, Any]) -> str:
    return f"{row.get('kind')}:{row.get('id')}"


def _done(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({_key(r) for r in rows if r.get("state") == vroster.DONE})[-MERGE_SEEN_MAX:]


def _brief_open(data: Mapping[str, Any]) -> list[Any]:
    brief = data.get("brief")
    items = brief.get("open") if isinstance(brief, Mapping) else None
    return list(items) if isinstance(items, list) else []


def _row_name(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": row.get("kind"), "id": row.get("id"), "title": row.get("title")}


class ProjectStates:
    """One person's project states in one tenant."""

    def __init__(self, db: Any, store: vstore.VoiceStore, *, now: Callable[[], Any]) -> None:
        self._db = db
        self._store = store
        self._now = now

    # ---- reads -----------------------------------------------------------

    def rows(self, *, tenant_id: str, person: str, project_id: str) -> list[dict[str, Any]]:
        """The project's starts and follow-ups, as the roster reads them."""
        return vroster.Roster(self._db, self._store).rows(
            tenant_id=tenant_id, person=person, project_id=project_id)

    def live(self, *, tenant_id: str, person: str, project_id: str) -> list[dict[str, Any]]:
        """What a Cancel would stop: the queued or running starts."""
        return [r for r in self.rows(tenant_id=tenant_id, person=person, project_id=project_id)
                if r.get("state") in LIVE_ROWS]

    # ---- the one move ----------------------------------------------------

    def _move(self, *, tenant_id: str, person: str, project_id: str,
              allowed: tuple[str, ...], to: str | None, by: str, why: str | None,
              expect: str | None = None, extra: Mapping[str, Any] | None = None,
              audit: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], Any] | None:
        """Move the project in one transaction; (its data, the audit row's
        reference or None). None when `expect` is given and the state is no
        longer it (another reader moved it). `to` None is Unarchive's: the
        state the project had."""
        vstore.check_id(project_id, "vp_", "project_id")
        ref = self._db.collection(vstore.PROJECTS).document(project_id)
        audit_ref = self._db.collection(vstore.AUDIT).document() if by == BY_PERSON else None

        @firestore.transactional
        def _apply(txn: Any) -> tuple[dict[str, Any], Any] | None:
            snap = _snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not (data.get("tenant_id") == tenant_id and data.get("person") == person):
                raise NotFound(f"voice project {project_id!r} not found")
            current = vstore.read_state(data.get("status"))
            if expect is not None and current != expect:
                return None
            if current not in allowed:
                raise StateRefused(f"a {SHOWN[current]} project cannot be moved that way",
                                   detail={"state": current})
            target = to
            if target is None:
                had = vstore.read_state(data.get("archived_from"))
                target = had if had != ARCHIVED else IN_PROGRESS
            now = self._now()
            history = [h for h in (data.get("state_history") or []) if isinstance(h, dict)]
            history = (history + [{"from": current, "to": target, "by": by, "why": why,
                                   "at": now}])[-vstore.STATE_HISTORY_MAX:]
            changes: dict[str, Any] = {"status": target, "state_history": history,
                                       "updated_at": now, **(extra or {})}
            if target == ARCHIVED:
                changes["archived_from"] = current
            txn.update(ref, changes)
            if audit_ref is not None:
                txn.set(audit_ref, {
                    "action": AUDIT_STATE, "tenant_id": tenant_id, "by": person, "at": now,
                    "project_id": project_id, "session_id": None,
                    "detail": {"from": current, "to": target, **(audit or {})}})
            return {**data, **changes}, audit_ref

        return _apply(self._db.transaction())

    # ---- the planner -----------------------------------------------------

    def planner_move(self, *, tenant_id: str, person: str, project_id: str,
                     state: Any, why: Any = None) -> dict[str, Any]:
        """The planner's `set_project_state(state, why)`: the project's view."""
        if state not in PLANNER_STATES:
            raise ValidationFailed(f"the planner's state is one of {', '.join(PLANNER_STATES)}",
                                   detail={"field": "state"})
        reason = _why(why)
        data = self._store._owned(tenant_id, person, project_id)
        current = vstore.read_state(data.get("status"))
        if current in OWNERS_STATES:
            raise StateRefused(f"a {SHOWN[current]} project is moved by its owner only",
                               detail={"state": current})
        if current == state:
            return vstore.project_view(project_id, data)
        rows = self.rows(tenant_id=tenant_id, person=person, project_id=project_id)
        if state == COMPLETED:
            live = [_row_name(r) for r in rows if r.get("state") in LIVE_ROWS]
            if live:
                raise StateRefused("the project still has work queued or running",
                                   detail={"running": live})
            if _brief_open(data):
                raise StateRefused("the project's brief still has open items",
                                   detail={"open": len(_brief_open(data))})
        moved = self._move(tenant_id=tenant_id, person=person, project_id=project_id,
                           allowed=PLANNER_STATES, to=state, by=BY_PLANNER, why=reason,
                           expect=current, extra={"merge_seen": _done(rows)})
        if moved is None:
            raise StateRefused("the project's state changed; read it again")
        return vstore.project_view(project_id, moved[0])

    # ---- a person --------------------------------------------------------

    def person_move(self, *, tenant_id: str, person: str, project_id: str, action: Any,
                    why: Any = None, cancel: Cancel | None = None) -> dict[str, Any]:
        """A person's action: {project, cancelled, skipped, still_running}
        (the last three empty for every action but Cancel)."""
        if action not in PERSON_ACTIONS:
            raise ValidationFailed(f"action is one of {', '.join(PERSON_ACTIONS)}",
                                   detail={"field": "action"})
        reason = _why(why)
        if action == CANCEL:
            if cancel is None:
                raise ValidationFailed("a cancel needs the platform's cancel",
                                       detail={"field": "action"})
            return self._cancel(tenant_id=tenant_id, person=person, project_id=project_id,
                                why=reason, cancel=cancel)
        allowed, to = PERSON_ACTIONS[action]
        extra: dict[str, Any] = {}
        if action == REOPEN:
            # What is already done stays seen, so the next read's merge
            # check does not complete the project again.
            extra["merge_seen"] = _done(self.rows(
                tenant_id=tenant_id, person=person, project_id=project_id))
        moved = self._move(tenant_id=tenant_id, person=person, project_id=project_id,
                           allowed=allowed, to=to, by=BY_PERSON, why=reason, extra=extra)
        if moved is None:  # only a move with `expect` reads None
            raise StateRefused("the project's state changed; read it again")
        return {"project": vstore.project_view(project_id, moved[0]),
                "cancelled": [], "skipped": [], "still_running": []}

    def _cancel(self, *, tenant_id: str, person: str, project_id: str, why: str | None,
                cancel: Cancel) -> dict[str, Any]:
        live = self.live(tenant_id=tenant_id, person=person, project_id=project_id)
        moved = self._move(tenant_id=tenant_id, person=person, project_id=project_id,
                           allowed=PERSON_ACTIONS[CANCEL][0], to=CANCELLED, by=BY_PERSON,
                           why=why, audit={"stopping": [_row_name(r) for r in live]})
        if moved is None:
            raise StateRefused("the project's state changed; read it again")
        data, audit_ref = moved
        cancelled: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for row in live:
            name = _row_name(row)
            try:
                target = self._cancel_target(tenant_id, row)
                if target is None:
                    skipped.append(name)
                    continue
                cancel(*target)
            except Exception as exc:  # noqa: BLE001 -- any failure is listed, never raised
                reason = redact_detail(f"{type(exc).__name__}: {exc}", limit=REASON_MAX)
                failed.append({**name, "state": vroster.RUNNING, "reason": reason})
                log.warning("voice project %s cancel of %s %s failed: %s",
                            project_id, name["kind"], name["id"], type(exc).__name__)
                continue
            cancelled.append(name)
        ref = self._db.collection(vstore.PROJECTS).document(project_id)
        ref.update({"still_running": failed})
        data = {**data, "still_running": failed}
        if audit_ref is not None:
            audit_ref.update({"detail": {
                "from": data["state_history"][-1]["from"], "to": CANCELLED,
                "stopping": [_row_name(r) for r in live], "cancelled": cancelled,
                "skipped": skipped, "failed": failed}})
        return {"project": vstore.project_view(project_id, data), "cancelled": cancelled,
                "skipped": skipped, "still_running": failed}

    def _cancel_target(self, tenant_id: str, row: Mapping[str, Any]) -> tuple[str, str] | None:
        """The platform cancel for one listed start, read again now: None
        when it ended since the list (or is not this tenant's)."""
        kind, target_id = str(row.get("kind")), str(row.get("id"))
        collection = vroster.COLLECTIONS.get(kind)
        if collection is None:
            raise ValidationFailed(f"no cancel for a {kind!r}")
        snap = self._db.collection(collection).document(target_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
        if doc.get("tenant_id") != tenant_id or vroster.row_state(doc.get("state")) not in LIVE_ROWS:
            return None
        if kind in ("agent", "workflow"):
            return kind, target_id
        # An issue run: its running agent, by what the run is doing.
        fixes = [str(w) for w in (doc.get("ci_fix_workflows") or []) if w]
        if str(doc.get("state") or "").upper() == "FIXING" and fixes:
            return "workflow", fixes[-1]
        if doc.get("workflow_id"):
            return "workflow", str(doc["workflow_id"])
        if doc.get("planner_task_id"):
            return "agent", str(doc["planner_task_id"])
        raise Conflict(f"issue run {target_id!r} names no running agent to cancel")

    # ---- a merge, settled on read ---------------------------------------

    def settle(self, *, tenant_id: str, person: str, project_id: str,
               data: dict[str, Any], counts: Mapping[str, Any]) -> dict[str, Any]:
        """The project's data after the merge check (§3.13.2, question 13):
        moved to `completed` by `merge` when all its work has ended with
        something done that no earlier move saw, nothing in its brief is
        open and no card waits on the owner. `counts` is
        `VoiceStore.project_counts`. Cheap checks first; the roster read only
        when they pass."""
        current = vstore.read_state(data.get("status"))
        if current not in (IN_PROGRESS, WAITING):
            return data
        if counts.get("waiting_on_owner") or _brief_open(data):
            return data
        rows = self.rows(tenant_id=tenant_id, person=person, project_id=project_id)
        if not rows or any(r.get("state") in UNENDED_ROWS for r in rows):
            return data
        done = _done(rows)
        if not set(done) - set(data.get("merge_seen") or []):
            return data
        moved = self._move(tenant_id=tenant_id, person=person, project_id=project_id,
                           allowed=(current,), to=COMPLETED, by=BY_MERGE,
                           why="the last of its work merged", expect=current,
                           extra={"merge_seen": done})
        if moved is None:
            # Another reader settled it first: read what it wrote.
            return self._store._owned(tenant_id, person, project_id)
        return moved[0]


def settle_on_read(db: Any, *, tenant_id: str, person: str, project_id: str,
                   now: Callable[[], Any]) -> None:
    """The merge check of a reader that is not Projects (§3.13.2): a session
    start, the follower's tick, a switch, the catch-up. The read's own answer
    does not wait on it: a project that is not this person's in this tenant
    is left to the reader's own check (the same 404 there), and a check that
    fails is logged by class name and leaves the state for the next read."""
    try:
        vstore.VoiceStore(db, now=now).get_project(
            tenant_id=tenant_id, person=person, project_id=project_id)
    except NotFound:
        return
    except Exception as exc:  # noqa: BLE001 - the next read settles it
        log.warning("voice project merge check failed: %s", type(exc).__name__)
