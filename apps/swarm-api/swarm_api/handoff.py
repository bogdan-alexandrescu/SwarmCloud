"""The live-browser hand-off, swarm-api's half (docs/design/live-browser.md §4).

Lane LB-L10. Read §4.1 (the agent asks), §4.3 (the bounded wait) and §5.4.1
(the attempt key) before changing anything here. In short:

  * `request` is §4.1 step 3: the WORKER, having paused the agent's CDP,
    records that its agent is waiting for a person. One transaction re-reads
    the task and its lease (invariant 5) and sets `Task.human_wait` -- the
    field, not a state (the owner's Q2) -- so the task stays RUNNING and keeps
    exactly the capacity it already holds (invariants 1 and 3).
  * `resolve` clears it: the worker calls it before it parks on an expired
    hold (§4.3), on a cancel (§4.4), or when its live path is lost (§5.4.2).
    Control returned by a person is lane L9's to report, through
    `resolution_writes`, so both writers produce one shape.
  * `human_ready` is §4.3 "Promotion is a person's": the ONLY path from
    PARKED(HUMAN_REQUIRED) to READY. No sweep promotes it; the scheduler only
    dead-letters one parked past `human_park_max_seconds`
    (`scheduler.loop.Scheduler._expire_human_waits`). A person's Return
    control on the attempt that parked is the same promotion
    (`LiveService.return_control`, `ready_writes`).

A PERSON WHO CAME IS NEVER PARKED AWAY (LB-RETURN, part of #1030). MEASURED by
the live-browser acceptance group on every run from 2026-10-14 00:24Z: the
hand-off, the take-over and the typing passed, and the task sat
PARKED(HUMAN_REQUIRED) at the 900 s deadline. The worker judged the unwatched
minute over on its poll, and the person's view, take and return landed while it
was parking. Three rules close that window, each in a transaction:

  * the worker's expiry names the deadline it judged (`seen_deadline`), and
    is answered `REVIVED`, writing nothing, when a viewer or a controller has
    moved it since, so whichever commits first decides and the worker keeps
    holding. An answer, not a refusal: the worker is told the hold goes on,
    and a worker from before this reads it as any 200, as it always did;
  * a return while the attempt is still RUNNING is written onto the task
    (`HUMAN_RETURNED_METADATA_KEY`, naming the attempt and generation), and
    the worker's park reads it in its own fenced transaction and writes READY
    instead of PARKED (`agent_worker.handoff.park_human_required`);
  * a return after the park committed is that person's "I'm ready".

WHO MAY CALL THE WORKER ROUTES. Exactly as the child routes (`children.py`):
the ID token proves the TENANT, and a signature by the attempt key the worker
registered before its agent existed proves the WORKER. The signature is over a
message domain-separated as `swarm-handoff/v1` (§5.4.1), so a child-request
signature is never a hand-off signature, nor the reverse. The registration is
the child path's (`child_keys`), so a deployment without `swarm-child-key`
offers no hand-off, and the worker's `swarm-handoff` exits 5 (§4.1).

WHO MAY SAY "I'M READY". The person who may take control (§5.1, Q1): the
submitter; for a task a service account submitted, its workflow's or
schedule's human owner, and when there is none any member of the tenant. A
member of another tenant is told the task does not exist (§5.2 step 2).

Nothing here logs, stores or serves a token, a signature or a key; the reason
is redacted before it is stored (§4.1 step 1).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Mapping

from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.models import Task, TaskEvent, new_id, utcnow
from swarm_common.profiles import url_refusal
from swarm_common.states import EventType, ParkReason, TaskState, assert_transition

from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _AttemptFields, _lease_is_live, parse_body
from .codec import event_to_firestore, task_from_dict
from .errors import ApiError, NotFound, ValidationFailed
from .gitidentity import is_service_account
from .redaction import redact
from .schedules import COLLECTION as SCHEDULES
from .store import EVENTS, LEASES, TASKS, WORKFLOWS, Store
from .validation import (  # noqa: F401 - re-exported: the keys this module writes
    HUMAN_HANDOFFS_METADATA_KEY,
    HUMAN_READY_METADATA_KEY,
    HUMAN_RETURNED_METADATA_KEY,
    HUMAN_WAIT_RESUMES_METADATA_KEY,
)

#: The hand-off's signing purpose (§5.4.1). The worker restates it (lane L4).
HANDOFF_PURPOSE = "swarm-handoff/v1"

log = logging.getLogger(__name__)

#: The append-only audit (§5.8). Lane L9 owns the collection and writes the
#: control entries; this lane writes `handoff_requested` and `handoff_resolved`
#: in the same transaction as the change each records.
LIVE_AUDIT = "live_audit"

#: §4.3, HANDOFF-POLICY (owner decision 2026-10-13, "notify, park fast, extend
#: while watched"): how long a hold lasts with NOBODY watching -- from the
#: request, and from the last moment a viewer was seen. Was a flat 10 minutes
#: (Q3); MEASURED over 26 browser tasks, 25 of 42 hand-offs expired, 13 of
#: them with nobody present, each holding a 2-unit browser lease and its pod
#: for the full 10 minutes (~4.2 pod-hours). A person who is coming opens the
#: live view within a minute of the bell; one who is not costs a minute.
HUMAN_UNWATCHED_SECONDS = 60
#: The ceiling on a hold that viewers and a controller keep extending, from
#: the request. 7 of the 42 expired AFTER the person pressed "I'm ready" --
#: they were there, on a slow or frozen view -- so a watched hold outlives the
#: old 10 minutes; bounded so a tab left open does not hold an 8-vCPU pod for
#: an afternoon (invariant 4).
HUMAN_HOLD_MAX_SECONDS = 30 * 60
#: §5.10: hand-off requests per attempt, enforced by the worker AND here, from
#: the task document, so a worker that miscounts cannot pester people.
MAX_HANDOFFS_PER_ATTEMPT = 3
#: §4.1 step 1.
MAX_REASON_CHARS = 200

#: The end the scheduler's sweep writes past `human_park_max_seconds` (§4.3,
#: Q4). Not an `EndCause` member: the frozen enum has none, so it is the
#: dead-letter's `last_error` prefix and its event's `cause`, and the task's
#: `end_cause` is None as for every other dead-lettered park. The scheduler
#: restates it; tests/unit/control_plane/test_handoff_api.py holds them equal.
HUMAN_WAIT_EXPIRED = "human_wait_expired"

#: LB-RETURN: HUMAN_RETURNED_METADATA_KEY (validation.py) is a person's return
#: while the attempt was RUNNING: `{attempt_id, generation, by, at}`, written by
#: `LiveService.return_control`. The worker's park reads it, for its own
#: attempt and generation only, and re-queues instead of parking; a new request
#: and `human_ready` drop it, so a return answers only the hand-off it came
#: during. The worker restates it.
#: LB-RETURN: `resolve`'s answer to an expiry whose deadline moved after the
#: worker read it (`{"resolved": "revived"}`). The worker restates it.
REVIVED = "revived"


#: The agent's request id: also its spool file's name (§4.1).
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
#: `expect` is a bare host, nothing else: no scheme, path, port or userinfo.
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")

#: How a hand-off ended, as the worker reports it.
OUTCOMES = ("returned", "expired", "cancelled", "unavailable")

#: THE HAND-OFF FUNNEL (LIVE-MEASURE; part of #1030). Owner decision
#: 2026-10-13: Phase 1 is "measure". Of 42 hand-offs 25 expired, and which
#: step lost the person -- opening the view, taking control, typing -- was
#: recorded nowhere. The first time each happened is written onto the wait
#: (`Task.human_wait` is a free-form dict in the frozen model), by
#: `LiveService.watched`, `take` and `record_input`, under these keys; the
#: wait's end (`resolution_writes`) turns them into seconds from the request
#: on the resolved event's `detail.funnel`, and logs it once committed.
FUNNEL_MILESTONES = (("first_viewer_at", "first_viewer_s"),
                     ("control_taken_at", "control_taken_s"),
                     ("first_input_at", "first_input_s"))
#: `jsonPayload.event` of the funnel's log line, and of "I'm ready" -> the
#: resumed attempt's first frame (`LiveService.record_report`):
#: scripts/live-report.sh filters on them.
FUNNEL_EVENT = "live_handoff_funnel"
READY_FRAME_EVENT = "live_ready_first_frame"


def first_time(wait: Mapping[str, Any], key: str, now: datetime) -> dict[str, Any]:
    """`wait` with `key` set to `now` unless it is already set: a milestone is
    the FIRST time it happened, never moved later by a repeat."""
    return dict(wait) if isinstance(wait.get(key), datetime) else {**wait, key: now}


def handoff_funnel(wait: Mapping[str, Any], *, outcome: str, now: datetime) -> dict[str, Any]:
    """The wait's milestones as whole seconds from its request; None for a step
    that never happened. `person_present`: a viewer joined at some point."""
    requested = wait.get("requested_at")

    def since(at: Any) -> int | None:
        if not isinstance(requested, datetime) or not isinstance(at, datetime):
            return None
        return max(0, int((at - requested).total_seconds()))

    funnel: dict[str, Any] = {"outcome": outcome}
    for key, name in FUNNEL_MILESTONES:
        funnel[name] = since(wait.get(key))
    funnel["resolved_s"] = since(now)
    funnel["person_present"] = isinstance(wait.get("first_viewer_at"), datetime)
    return funnel


def log_funnel(task: Mapping[str, Any], detail: Mapping[str, Any]) -> None:
    """The funnel as one structured line, after its transaction committed:
    names and numbers only, never the reason or a person's address."""
    funnel = detail.get("funnel") if isinstance(detail.get("funnel"), Mapping) else {}
    log.info(
        "live hand-off funnel task=%s outcome=%s", task.get("id"), funnel.get("outcome"),
        extra={"event": FUNNEL_EVENT, "task_id": task.get("id"), "tenant_id": task.get("tenant_id"),
               "attempt_id": detail.get("attempt_id"), "request_id": detail.get("request_id"),
               **funnel},
    )


class HandoffUnavailable(ApiError):
    status_code = 503
    code = "handoff_unavailable"


class HandoffUnauthenticated(ApiError):
    status_code = 403
    code = "handoff_unauthenticated"


class HandoffUnproven(ApiError):
    status_code = 403
    code = "handoff_unproven"


class HandoffFenced(ApiError):
    status_code = 409
    code = "handoff_fenced"


class HandoffPending(ApiError):
    status_code = 409
    code = "handoff_pending"


class HandoffLimit(ApiError):
    status_code = 409
    code = "handoff_limit"


class HumanReadyNotEligible(ApiError):
    status_code = 403
    code = "human_ready_not_eligible"


class NotWaitingForAPerson(ApiError):
    status_code = 409
    code = "not_waiting_for_a_person"


class HandoffRequest(_AttemptFields):
    request_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(max_length=MAX_REASON_CHARS)
    expect: str | None = Field(default=None, max_length=253)
    #: Who asked (CAPTCHA-HANDOFF, BGD 2026-10-23; part of #2058): the agent
    #: (`swarm-handoff request`), or the live gate, which raises the hand-off
    #: itself when it sees a CAPTCHA on the agent's page. Stored on the wait
    #: and the event so the console and the event log tell them apart. The
    #: default is "agent": a worker from before the field sends none.
    origin: Literal["agent", "gate"] = "agent"


class HandoffResolve(_AttemptFields):
    request_id: str = Field(min_length=1, max_length=64)
    outcome: Literal["returned", "expired", "cancelled", "unavailable"]
    #: The deadline the worker judged expired (LB-RETURN). An expiry naming
    #: one that has since moved into the future is answered `REVIVED`.
    seen_deadline: datetime | None = None


def handoff_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs for one hand-off request. The child path's shape
    under its own purpose, so neither signature verifies as the other."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{HANDOFF_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def expect_refusal(value: str) -> str:
    """Why `value` is not a host a hand-off may name, or "" when it is."""
    if not _HOST.fullmatch(value):
        return "expect is a bare host name: lower-case letters, digits, dots and hyphens"
    return url_refusal(f"https://{value}/")


def control_owner(task: Task, db: Any) -> str | None:
    """Who may control `task` (§5.1), or None when any tenant member may.

    The submitter when a person submitted it; for a service account's task,
    its workflow's human submitter, then its schedule's human owner. Each is
    read from the task's OWN tenant only: a document of another tenant names
    nobody here.
    """
    if task.submitted_by and not is_service_account(task.submitted_by):
        return task.submitted_by.strip().lower()
    candidates: list[tuple[str, str, str]] = []
    if task.workflow_id:
        candidates.append((WORKFLOWS, task.workflow_id, "submitted_by"))
    mark = (task.metadata or {}).get("schedule")
    if isinstance(mark, dict) and isinstance(mark.get("schedule_id"), str):
        candidates.append((SCHEDULES, mark["schedule_id"], "owner"))
    for collection, doc_id, field in candidates:
        snap = db.collection(collection).document(doc_id).get()
        if not snap.exists:
            continue
        doc = snap.to_dict() or {}
        if doc.get("tenant_id") != task.tenant_id:
            continue
        who = doc.get(field)
        if isinstance(who, str) and who and not is_service_account(who):
            return who.strip().lower()
    return None


def may_control(task: Task, email: str, db: Any, *, opened_to_members: bool = False) -> bool:
    """§5.1 and Q1: the owner, or any tenant member when there is no human
    owner or the owner opened control to the tenant (lane L9's toggle).

    The caller is already known to be a member of the task's tenant:
    `tenant_scope` and the store's tenant filter decided that first.
    """
    if opened_to_members:
        return True
    owner = control_owner(task, db)
    return owner is None or owner == (email or "").strip().lower()


def resolution_writes(
    txn: Any,
    db: Any,
    task: dict[str, Any],
    *,
    outcome: str,
    now: datetime,
    by: str | None = None,
    lease_id: str | None = None,
    recorded: list[dict[str, Any]] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Clear `human_wait` and record why, inside the caller's transaction.

    One shape for both writers: the worker's `resolve` and lane L9's return of
    control. False, and nothing written, when nothing is waiting. `lease_id` is
    the caller's, for the event: the wait does not store one. The detail is
    appended to `recorded`, for the caller to `log_funnel` once the
    transaction has committed: logged here, a retried transaction would log
    one hand-off twice. `extra` is more of the caller's own task fields, in
    the same write.
    """
    wait = task.get("human_wait")
    if not isinstance(wait, dict):
        return False
    task_ref = db.collection(TASKS).document(task["id"])
    requested = wait.get("requested_at")
    held = (now - requested).total_seconds() if isinstance(requested, datetime) else None
    detail = {
        "outcome": outcome,
        "request_id": wait.get("request_id"),
        "attempt_id": wait.get("attempt_id"),
        "generation": wait.get("generation"),
        "held_seconds": int(held) if held is not None else None,
        "funnel": handoff_funnel(wait, outcome=outcome, now=now),
    }
    if recorded is not None:
        recorded.append(detail)
    event = TaskEvent(
        event_id=new_id("ev"),
        task_id=task["id"],
        tenant_id=task["tenant_id"],
        type=EventType.HUMAN_HANDOFF_RESOLVED,
        at=now,
        # The wait's attempt, on the event's own fields too (LB-V6): the
        # Attempts tab groups on them, and `detail` alone filed this under
        # "Before any attempt".
        attempt_id=wait.get("attempt_id"),
        lease_id=lease_id,
        generation=wait.get("generation"),
        detail=detail,
    )
    txn.update(task_ref, {**(extra or {}), "human_wait": None, "updated_at": now})
    txn.set(task_ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
    _audit(txn, db, task, "handoff_resolved", now=now, email=by, detail=detail)
    return True


def returned_mark(task: Mapping[str, Any], session: Mapping[str, Any], *, by: str,
                  now: datetime) -> dict[str, Any]:
    """The task's metadata with this return recorded (LB-RETURN): the attempt
    and generation of the session it was returned on, so the park of exactly
    that attempt reads it."""
    metadata = dict(task.get("metadata") or {})
    metadata[HUMAN_RETURNED_METADATA_KEY] = {
        "attempt_id": session.get("attempt_id"),
        "generation": session.get("generation"),
        "by": by,
        "at": now.isoformat(),
    }
    return metadata


def ready_writes(txn: Any, db: Any, data: dict[str, Any], *, email: str, now: datetime,
                 reason: str = "human_ready") -> dict[str, Any]:
    """PARKED(HUMAN_REQUIRED) -> READY inside the caller's transaction (§4.3).

    One shape for "I'm ready" (`HandoffService.human_ready`) and a Return
    control that arrives after the park (`LiveService.return_control`, LB-RETURN).
    Raises NotWaitingForAPerson, writing nothing, for any other task. Nothing
    is leased here: the next attempt is admitted all-or-nothing, under a new
    generation, like any READY task (invariants 1, 2, 5). Returns the task as
    written.
    """
    task_id = data["id"]
    if (
        data.get("state") != TaskState.PARKED.value
        or data.get("park_reason") != ParkReason.HUMAN_REQUIRED.value
    ):
        raise NotWaitingForAPerson(
            "this task is not parked waiting for a person; nothing was changed"
        )
    current = task_from_dict(data)
    if current.cancel_requested:
        raise NotWaitingForAPerson("a cancel was requested for this task")
    if current.retries_exhausted():
        # Admission does not check the cap: READY would lease attempt
        # N+1 of N. The scheduler's sweep dead-letters it instead.
        raise NotWaitingForAPerson(
            f"this task has used its last attempt ({current.attempt_count} of "
            f"{current.max_attempts}); it cannot be resumed"
        )
    assert_transition(TaskState.PARKED, TaskState.READY)
    metadata = dict(data.get("metadata") or {})
    # The attempt whose hand-off parked this task, which this `ready`
    # answers (LB-V6): the Attempts tab files it there. None when no
    # hand-off was counted -- named as nothing, never guessed.
    counted = metadata.get(HUMAN_HANDOFFS_METADATA_KEY)
    answered = counted.get("attempt_id") if isinstance(counted, dict) else None
    metadata[HUMAN_READY_METADATA_KEY] = {"by": email, "at": now.isoformat()}
    metadata.pop(HUMAN_RETURNED_METADATA_KEY, None)
    update = {
        "state": TaskState.READY.value,
        "park_reason": None,
        "next_eligible_at": None,
        "blocked_by": [],
        "metadata": metadata,
        # Nothing is held while parked; a stale wait must not show.
        "human_wait": None,
        "updated_at": now,
    }
    event = TaskEvent(
        event_id=new_id("ev"),
        task_id=task_id,
        tenant_id=data["tenant_id"],
        type=EventType.READY,
        at=now,
        attempt_id=answered if isinstance(answered, str) else None,
        detail={
            "reason": reason,
            "park_reason": ParkReason.HUMAN_REQUIRED.value,
            "by": email,
        },
    )
    task_ref = db.collection(TASKS).document(task_id)
    txn.update(task_ref, update)
    txn.set(task_ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
    return {**data, **update}


def _audit(
    txn: Any,
    db: Any,
    task: dict[str, Any],
    entry: str,
    *,
    now: datetime,
    email: str | None,
    detail: dict[str, Any],
) -> None:
    wait = task.get("human_wait") if isinstance(task.get("human_wait"), dict) else {}
    entry_id = new_id("la")
    txn.set(
        db.collection(LIVE_AUDIT).document(entry_id),
        {
            "id": entry_id,
            "entry": entry,
            "task_id": task["id"],
            "tenant_id": task["tenant_id"],
            "attempt_id": detail.get("attempt_id", wait.get("attempt_id")),
            "generation": detail.get("generation", wait.get("generation")),
            "email": email,
            "at": now,
            "detail": detail,
        },
    )


def attempt_deadline(task: Mapping[str, Any]) -> datetime | None:
    """When the attempt itself times out (`started_at + timeout_seconds`), or
    None when the task does not say. No hold outlives it (§4.3)."""
    started = task.get("started_at")
    timeout = task.get("timeout_seconds")
    if isinstance(started, datetime) and isinstance(timeout, int) and not isinstance(timeout, bool):
        return started + timedelta(seconds=timeout)
    return None


def opening_deadline(now: datetime, task: Mapping[str, Any], *,
                     unwatched_seconds: int = HUMAN_UNWATCHED_SECONDS) -> datetime:
    """The deadline a new hold opens with: `unwatched_seconds` from now, never
    past the attempt's own deadline and never before now. Nobody is watching
    yet; a viewer or a controller moves it (`extended_deadline`)."""
    deadline = now + timedelta(seconds=unwatched_seconds)
    end = attempt_deadline(task)
    if end is not None:
        deadline = max(now, min(deadline, end))
    return deadline


def extended_deadline(now: datetime, task: Mapping[str, Any], wait: Mapping[str, Any], *,
                      seconds: int, max_seconds: int = HUMAN_HOLD_MAX_SECONDS,
                      revive: bool = True) -> datetime | None:
    """§4.3: someone is watching or in control, so the hold's deadline moves to
    `now + seconds` -- capped at `requested_at + max_seconds` and the attempt's
    own deadline, and never earlier than it already was.

    None when there is nothing to move, or when `revive` is False and the
    deadline has already passed: the worker may be parking on it, and a viewer
    arriving a second late must not race that (a controller's input keeps the
    rule it always had, since `_settle` ends control on an expired hold first).
    """
    current = wait.get("deadline")
    if not revive and isinstance(current, datetime) and current < now:
        return None
    target = now + timedelta(seconds=seconds)
    requested = wait.get("requested_at")
    if isinstance(requested, datetime):
        target = min(target, requested + timedelta(seconds=max_seconds))
    end = attempt_deadline(task)
    if end is not None:
        target = min(target, end)
    if isinstance(current, datetime):
        target = max(current, target)
    return target


def _wait_to_api(wait: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v)
        for k, v in wait.items()
    }


class HandoffService:
    """The hand-off routes' logic. Built per request from the app context."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        store: Store,
        children: ChildService,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._store = store
        self._children = children
        self._now = now
        self._clock = clock or children._clock

    # -- the worker's proof ------------------------------------------------

    def _prove(
        self, authorization: str | None, attempt: AttemptTuple, *, path: str, body: bytes,
        proof: str | None, timestamp: str | None,
    ) -> None:
        """§5.4.1. Every failure of the signature is the same 403."""
        if not self._children._keys.configured:
            raise HandoffUnavailable(
                "this deployment has no swarm-child-key, so it offers no hand-off path"
            )
        try:
            self._children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise HandoffUnauthenticated(
                "only this tenant's worker service account may call this route; a person "
                "has no attempt"
            ) from None
        unproven = HandoffUnproven("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self._settings, "child_proof_skew_seconds", 120))
        if abs(self._clock() - int(timestamp)) > skew:
            raise unproven
        registration = self._children._registration(attempt)
        if registration is None:
            raise unproven
        if not verify_proof(
            registration["public_key"], proof, handoff_message("POST", path, body, timestamp)
        ):
            raise unproven

    @staticmethod
    def _attempt(task_id: str, body: _AttemptFields) -> AttemptTuple:
        return AttemptTuple(
            tenant_id=body.tenant_id,
            task_id=task_id,
            attempt_id=body.attempt_id,
            lease_id=body.lease_id,
            generation=body.generation,
        )

    # -- §4.1 step 3: the request -------------------------------------------

    def request(
        self,
        authorization: str | None,
        task_id: str,
        raw: bytes,
        *,
        path: str,
        proof: str | None,
        timestamp: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if len(raw) > 4096:
            raise ValidationFailed("a hand-off request is at most 4096 bytes")
        body: HandoffRequest = parse_body(HandoffRequest, raw)
        attempt = self._attempt(task_id, body)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof, timestamp=timestamp)

        if not REQUEST_ID.fullmatch(body.request_id):
            raise ValidationFailed("request_id must match [a-z0-9-]{1,64}")
        reason = body.reason.strip()
        if not reason:
            raise ValidationFailed("reason is required: say what the person is asked to do")
        # Redacted BEFORE it is stored, shown or put in an event (§4.1 step 1).
        reason = redact(reason).text[:MAX_REASON_CHARS]
        expect = body.expect.strip().lower() if body.expect else None
        if expect:
            why = expect_refusal(expect)
            if why:
                raise ValidationFailed(f"expect is not a host a hand-off may name: {why}")

        unwatched = int(getattr(self._settings, "human_unwatched_seconds", HUMAN_UNWATCHED_SECONDS))
        cap = int(getattr(self._settings, "max_handoffs_per_attempt", MAX_HANDOFFS_PER_ATTEMPT))
        task_ref = self._db.collection(TASKS).document(task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[dict[str, Any], bool]:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            if (
                task is None
                or task.get("state") != TaskState.RUNNING.value
                or task.get("cancel_requested")
                or not _lease_is_live(task, lease, attempt, now)
            ):
                raise HandoffFenced(
                    "the task is not RUNNING under this attempt's live lease, or a cancel "
                    "was requested; nothing was recorded"
                )
            existing = task.get("human_wait")
            if isinstance(existing, dict):
                if (
                    existing.get("request_id") == body.request_id
                    and existing.get("attempt_id") == attempt.attempt_id
                ):
                    return existing, False
                raise HandoffPending("this task is already waiting for a person")
            metadata = dict(task.get("metadata") or {})
            counted = metadata.get(HUMAN_HANDOFFS_METADATA_KEY)
            used = 0
            if isinstance(counted, dict) and counted.get("attempt_id") == attempt.attempt_id:
                value = counted.get("count")
                used = value if isinstance(value, int) and not isinstance(value, bool) else cap
            if used >= cap:
                raise HandoffLimit(f"this attempt has asked for help {used} times; the cap is {cap}")

            # Park fast (HANDOFF-POLICY): a minute unless someone opens the
            # live view (`LiveService.watched`) or takes control.
            deadline = opening_deadline(now, task, unwatched_seconds=unwatched)
            wait = {
                "reason": reason,
                "expect": expect,
                "requested_at": now,
                "deadline": deadline,
                "controller": None,
                "origin": body.origin,
                # The last moment a viewer was seen on the live view; None
                # until one is. The bell and the console read it.
                "watched_at": None,
                "request_id": body.request_id,
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            }
            metadata[HUMAN_HANDOFFS_METADATA_KEY] = {
                "attempt_id": attempt.attempt_id,
                "count": used + 1,
            }
            # A person already said they are ready: consumed by the request it
            # was waiting for, so a later one does not claim it too.
            person_waiting = metadata.pop(HUMAN_READY_METADATA_KEY, None) is not None
            # A return from before this request answers nothing it asks (LB-RETURN).
            metadata.pop(HUMAN_RETURNED_METADATA_KEY, None)
            detail = {
                "reason": reason,
                "expect": expect,
                "deadline": deadline.isoformat(),
                "request_id": body.request_id,
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "person_waiting": person_waiting,
                "origin": body.origin,
            }
            event = TaskEvent(
                event_id=new_id("ev"),
                task_id=task_id,
                tenant_id=attempt.tenant_id,
                type=EventType.HUMAN_HANDOFF_REQUESTED,
                at=now,
                attempt_id=attempt.attempt_id,
                lease_id=attempt.lease_id,
                generation=attempt.generation,
                detail=detail,
            )
            txn.update(task_ref, {"human_wait": wait, "metadata": metadata, "updated_at": now})
            txn.set(task_ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
            _audit(txn, self._db, task, "handoff_requested", now=now, email=None, detail=detail)
            return wait, True

        wait, created = _apply(self._db.transaction())
        return _wait_to_api(wait), created

    # -- the end of a wait, as the worker reports it ------------------------

    def resolve(
        self,
        authorization: str | None,
        task_id: str,
        raw: bytes,
        *,
        path: str,
        proof: str | None,
        timestamp: str | None,
    ) -> bool | str:
        """Whether the wait was resolved; `REVIVED`, writing nothing, for an
        expiry someone overtook (LB-RETURN)."""
        if len(raw) > 4096:
            raise ValidationFailed("a hand-off resolution is at most 4096 bytes")
        body: HandoffResolve = parse_body(HandoffResolve, raw)
        attempt = self._attempt(task_id, body)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof, timestamp=timestamp)
        task_ref = self._db.collection(TASKS).document(task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        now = self._now()
        recorded: list[dict[str, Any]] = []
        resolved: dict[str, Any] = {}

        @firestore.transactional
        def _apply(txn: Any) -> bool | str:
            recorded.clear()  # a retried transaction starts again
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            resolved.update(task or {})
            # A cancel does not fence this: the cancel path resolves (§4.4).
            if (
                task is None
                or task.get("state") != TaskState.RUNNING.value
                or not _lease_is_live(task, lease, attempt, now)
            ):
                raise HandoffFenced("the task is not RUNNING under this attempt's live lease")
            wait = task.get("human_wait")
            if not isinstance(wait, dict) or (
                wait.get("request_id") != body.request_id
                or wait.get("attempt_id") != attempt.attempt_id
            ):
                return False
            deadline = wait.get("deadline")
            if (
                body.outcome == "expired"
                and body.seen_deadline is not None
                and body.seen_deadline.tzinfo is not None
                and isinstance(deadline, datetime)
                and deadline > body.seen_deadline
                and deadline > now
            ):
                # LB-RETURN: a viewer or a controller moved the deadline after
                # the worker read it. They came first; the hold goes on.
                return REVIVED
            return resolution_writes(txn, self._db, task, outcome=body.outcome, now=now,
                                     lease_id=attempt.lease_id, recorded=recorded)

        written = _apply(self._db.transaction())
        for detail in recorded:
            log_funnel(resolved, detail)
        return written

    # -- §4.3: a person says they are ready ---------------------------------

    def human_ready(
        self,
        tenant_id: str,
        task_id: str,
        *,
        email: str,
        submitted_by: str | None,
        opened_to_members: bool = False,
    ) -> Task:
        # The tenant read first: another tenant's task is a 404 (§5.2 step 2).
        task = self._store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        if not may_control(task, email, self._db, opened_to_members=opened_to_members):
            raise HumanReadyNotEligible(
                "only the person who may take control of this task may say they are ready: "
                "its submitter, unless they opened control to the tenant"
            )
        task_ref = self._db.collection(TASKS).document(task_id)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(task_ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != tenant_id:
                raise NotFound(f"task {task_id!r} not found")
            return ready_writes(txn, self._db, {**data, "id": task_id}, email=email, now=now)

        return task_from_dict(_apply(self._db.transaction()))
