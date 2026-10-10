"""The per-repository merge slot: one merge step at a time per base (merge race, #295).

WHY. A base that requires an up-to-date branch (GitHub's ruleset
`strict_required_status_checks_policy`) lets exactly one pull request land
per CI run: every merge puts every other open pull request behind. Measured
2026-10-10 across the eng tenant: 16 merge steps ran against one `main` at
once, each landing merge put the rest behind, and 7 of them refused
`behind_too_often` after `MERGE_MAX_BRANCH_UPDATES` updates that each cost a
full CI run. There is no GitHub merge queue on a user-owned repository
(docs/ci.md "Merging through the merge queue"), so an operator ran a laptop
driver that submitted one `merge_pr` at a time. This module is that driver,
inside the platform.

WHAT. One Firestore document per (tenant, repository, base branch),
`merge_slots/{slot_id}`, held by at most one merge step at a time. A step
takes the slot only once its own checks are green (`merge._with_forge`), so
every pull request's first CI run still runs in parallel; only the
update-and-merge tail is serial, which the strict policy makes serial anyway.

  * ACQUIRE, RENEW AND RELEASE ARE TRANSACTIONS, each fenced on the calling
    attempt's own task generation and lease (`ControlPlane._fenced_task`,
    invariant 5): a superseded worker can neither take, keep nor give away the
    slot. Every grant bumps the slot's own `generation`, and the holder
    re-reads it (`SlotUse.confirm`) immediately before each forge write, as
    the merge re-reads its task fence -- a holder whose slot expired and was
    taken over makes no update and no merge.
  * THE SLOT IS A LEASE. `holder.expires_at` is the grant plus
    `MERGE_SLOT_LEASE_SECONDS`, renewed by every attempt of the holder (its
    heartbeat: a parked holder is woken at least every
    `MERGE_CI_FALLBACK_SECONDS`, well inside the lease). A crashed holder's
    slot frees itself when the lease runs out, or AT ONCE when its task is
    read terminal -- a holder whose task ended holds nothing.
  * A STEP THAT CANNOT HAVE THE SLOT DOES NOT SPIN. It joins the slot's
    `waiters` and parks CI_PENDING with the code `MERGE_SLOT_WAIT`, holding
    no lease and no pool count (invariant 1). No new park reason: CI_PENDING
    is "a merge step waits on a fact that changes by itself", which this is;
    contract request 62 (docs/contract-change-requests.md) asks for a
    reason of its own.
  * FIFO BY SUBMISSION TIME. Waiters are ordered by their task's
    `created_at`, then id. A release HANDS the slot to the first waiter whose
    task is not ended -- it becomes the holder while still parked -- and
    marks that task's `merge_wait.wake_requested_at` so the scheduler
    promotes it on its next drain (`SlotUse.settle`). A waiter that finds the
    slot free with an earlier waiter queued hands it on the same way and
    parks again behind it.
  * THE LIST IS BOUNDED AND SELF-HEALING. An entry not refreshed for
    `MERGE_SLOT_WAITER_STALE_SECONDS` is dropped; since order is by
    submission time, not by when an entry was added, a live waiter that was
    dropped takes its old place back on its next wake.

The document is tenant-writable, like every document the worker reads. A
forged slot can at worst make this tenant's own merge steps wait (bounded
by `MERGE_SLOT_MAX_WAITS`) or take the slot early, after which every forge
fact is still read again and the head still pinned: nothing merges that the
merge step would not have merged without the slot.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, TypeVar

from swarm_common.models import utcnow
from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES, ParkReason, TaskState

#: The collection the slots live in. Worker role: get, create and update by
#: id, no list and no delete (`worker_firestore`,
#: terraform/bootstrap/platform_roles.tf) -- which is all a slot needs.
MERGE_SLOTS_COLLECTION = "merge_slots"

#: The CI_PENDING park code of a step waiting for the slot. swarm-api's wake
#: tick leaves these parks alone (`swarm_api.mergewake.MERGE_SLOT_WAIT`, held
#: equal by tests/unit/worker/test_merge_slot.py): their checks are green,
#: so reading them would wake the step for nothing every tick. The release's
#: handoff marks them, and the fallback instant covers a lost mark.
MERGE_SLOT_WAIT = "merge_slot_wait"

#: The wake reason a handoff writes beside the marker.
SLOT_WAKE_REASON = "merge_slot_granted"

#: How long a grant or a renewal holds the slot. 45 min = 3 x
#: `merge.MERGE_CI_FALLBACK_SECONDS` (900): a holder parked for CI is woken
#: at the latest at its fallback instant and renews then, so a live holder
#: renews well inside the lease even when admission is slow to lease it
#: again; and a crashed holder whose task did NOT end blocks its repository
#: for at most this long. A holder whose task ended frees it at once.
MERGE_SLOT_LEASE_SECONDS = 45 * 60

#: A waiter entry not refreshed for this long is dropped. Twice the lease: a
#: parked waiter refreshes it on every wake, at least every fallback (15 min).
MERGE_SLOT_WAITER_STALE_SECONDS = 2 * MERGE_SLOT_LEASE_SECONDS

#: The most waiters one slot document lists. Far above any burst seen (16 on
#: 2026-10-10) and far below Firestore's 1 MiB document limit; a step past it
#: parks without joining and joins on a later wake.
MERGE_SLOT_MAX_WAITERS = 500

#: How many slot-wait parks give their attempt back, counted apart from the
#: CI wakes (`merge_wait.slot_waits`, `ControlPlane.park_ci_pending`). 200 x
#: the 15 min fallback = 50 h: a step waiting behind a long queue is not
#: failing, so it must not spend `merge.MERGE_CI_MAX_WAKES` or its attempts
#: on it; past the bound a wait counts like any attempt, so a slot that never
#: frees still ends the step at `max_attempts`.
MERGE_SLOT_MAX_WAITS = 200

#: How many waiters a release reads, oldest first, looking for one whose
#: task has not ended before it hands the slot on. A dead waiter it does not
#: reach can still be handed the slot; it then frees at once on the next
#: reader that finds its task ended, or when its lease runs out.
HANDOFF_READS = 8

_MERGE_WAIT_KEY = "merge_wait"
_WAKE_MARKER = "wake_requested_at"

T = TypeVar("T")


def slot_id(tenant_id: str, repository: str, base: str) -> str:
    """The slot document's id: the tenant, then a digest of `owner/repo` and the base.

    A digest because a repository's name holds a `/`, which a document id
    cannot; lower-cased because GitHub's owner and repository names are
    case-insensitive, and two spellings of one repository must share a slot.
    """
    digest = hashlib.sha256(f"{repository.lower()}\n{base}".encode()).hexdigest()[:40]
    return f"{tenant_id}--{digest}"


# ---------------------------------------------------------------------------
# What the transitions return
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claimant:
    """The merge step asking for a slot: its task, attempt and submission time."""

    task_id: str
    attempt_id: str
    submitted_at: datetime


@dataclass(frozen=True)
class Held:
    """This step holds the slot at `generation`.

    `updates_at_acquire` is how many of GitHub's base merges were already on
    the pushed head when the slot was taken: the updates the merge counts
    against `merge.MERGE_MAX_BRANCH_UPDATES` are the ones after it.
    """

    generation: int
    updates_at_acquire: int


@dataclass(frozen=True)
class Waiting:
    """Another step holds the slot, or is owed it first. `position` is 1-based."""

    holder: str | None
    position: int
    waiters: int
    #: A waiter this call handed the slot to, whose park is to be woken.
    wake: str | None = None


class SlotLost(Exception):
    """The slot this attempt held is another step's now (lease expiry, takeover)."""


# ---------------------------------------------------------------------------
# The transitions: pure functions of the document as one transaction read it
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _instant(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _holder(doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    holder = doc.get("holder")
    return holder if isinstance(holder, Mapping) and isinstance(holder.get("task_id"), str) else None


def _waiters(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = doc.get("waiters")
    if not isinstance(raw, list):
        return []
    return [dict(w) for w in raw
            if isinstance(w, Mapping) and isinstance(w.get("task_id"), str)
            and isinstance(w.get("submitted_at"), datetime)]


def _order(entry: Mapping[str, Any]) -> tuple[datetime, str]:
    return entry["submitted_at"], entry["task_id"]


def holder_expired(holder: Mapping[str, Any], now: datetime) -> bool:
    expires = _instant(holder.get("expires_at"))
    return expires is None or expires <= now


def _grant(entry: Mapping[str, Any], *, generation: int, now: datetime, how: str,
           attempt_id: str | None = None, updates: int | None = None) -> dict[str, Any]:
    return {
        "task_id": entry["task_id"],
        "attempt_id": attempt_id,
        "submitted_at": entry["submitted_at"],
        "generation": generation,
        "granted_at": now,
        "granted_by": how,
        "renewed_at": now,
        "expires_at": now + timedelta(seconds=MERGE_SLOT_LEASE_SECONDS),
        # None for a handoff: the new holder records it on its first renewal,
        # when it has read its head.
        "updates_at_acquire": updates,
    }


def _base_doc(doc: Mapping[str, Any] | None, *, tenant_id: str, repository: str,
              base: str) -> dict[str, Any]:
    out = dict(doc or {})
    out.update(tenant_id=tenant_id, repository=repository, base=base)
    out["generation"] = _int(out.get("generation"))
    return out


def _live_waiters(doc: Mapping[str, Any], *, now: datetime, ended: set[str],
                  drop: str | None = None) -> list[dict[str, Any]]:
    stale = now - timedelta(seconds=MERGE_SLOT_WAITER_STALE_SECONDS)
    kept = []
    for entry in _waiters(doc):
        if entry["task_id"] == drop or entry["task_id"] in ended:
            continue
        seen = _instant(entry.get("seen_at"))
        if seen is not None and seen < stale:
            continue
        kept.append(entry)
    return sorted(kept, key=_order)


def acquire(doc: Mapping[str, Any] | None, me: Claimant, *, tenant_id: str, repository: str,
            base: str, now: datetime, updates: int,
            ended: set[str]) -> tuple[dict[str, Any], Held | Waiting]:
    """Take the slot, keep it, or queue for it. `ended`: task ids read terminal."""
    out = _base_doc(doc, tenant_id=tenant_id, repository=repository, base=base)
    holder = _holder(out)
    if holder is not None and holder["task_id"] == me.task_id:
        held, out = _renewed(out, holder, me, now=now, updates=updates)
        out["waiters"] = _live_waiters(out, now=now, ended=ended, drop=me.task_id)
        return out, held
    waiters = _live_waiters(out, now=now, ended=ended, drop=me.task_id)
    mine = {"task_id": me.task_id, "submitted_at": me.submitted_at, "seen_at": now}
    previous = next((w for w in _waiters(out) if w["task_id"] == me.task_id), None)
    mine["queued_at"] = _instant((previous or {}).get("queued_at")) or now
    free = (holder is None or holder_expired(holder, now) or holder["task_id"] in ended)
    if not free:
        queue = sorted([*waiters, mine], key=_order)[:MERGE_SLOT_MAX_WAITERS]
        out["waiters"] = queue
        position = next((i for i, w in enumerate(queue, 1) if w["task_id"] == me.task_id),
                        len(queue) + 1)
        return out, Waiting(holder=holder["task_id"], position=position, waiters=len(queue))
    if holder is not None:
        out["last_release"] = {"task_id": holder["task_id"], "generation": holder.get("generation"),
                               "reason": "ended" if holder["task_id"] in ended else "expired",
                               "at": now, "by": me.task_id}
    queue = sorted([*waiters, mine], key=_order)
    first = queue[0]
    out["generation"] += 1
    if first["task_id"] == me.task_id:
        out["holder"] = _grant(mine, generation=out["generation"], now=now, how="acquire",
                               attempt_id=me.attempt_id, updates=updates)
        out["waiters"] = queue[1:MERGE_SLOT_MAX_WAITERS + 1]
        out["updated_at"] = now
        return out, Held(generation=out["generation"], updates_at_acquire=updates)
    # An earlier submission is queued: the slot is its, and this step waits
    # behind it. The earlier one is parked; the caller wakes it.
    out["holder"] = _grant(first, generation=out["generation"], now=now, how="handoff")
    rest = queue[1:MERGE_SLOT_MAX_WAITERS + 1]
    out["waiters"] = rest
    out["updated_at"] = now
    position = next((i for i, w in enumerate(rest, 1) if w["task_id"] == me.task_id), len(rest))
    return out, Waiting(holder=first["task_id"], position=position, waiters=len(rest),
                        wake=first["task_id"])


def _renewed(out: dict[str, Any], holder: Mapping[str, Any], me: Claimant, *, now: datetime,
             updates: int | None) -> tuple[Held, dict[str, Any]]:
    renewed = dict(holder)
    renewed["attempt_id"] = me.attempt_id
    renewed["renewed_at"] = now
    renewed["expires_at"] = now + timedelta(seconds=MERGE_SLOT_LEASE_SECONDS)
    if not isinstance(renewed.get("updates_at_acquire"), int) and updates is not None:
        renewed["updates_at_acquire"] = updates
    out["holder"] = renewed
    out["updated_at"] = now
    return Held(generation=_int(renewed.get("generation")),
                updates_at_acquire=_int(renewed.get("updates_at_acquire"))), out


def renew(doc: Mapping[str, Any] | None, me: Claimant, *, now: datetime,
          updates: int | None) -> tuple[dict[str, Any] | None, Held | None]:
    """Extend the slot's lease if this step holds it; nothing written otherwise."""
    if doc is None:
        return None, None
    out = dict(doc)
    holder = _holder(out)
    if holder is None or holder["task_id"] != me.task_id:
        return None, None
    held, out = _renewed(out, holder, me, now=now, updates=updates)
    return out, held


def release(doc: Mapping[str, Any] | None, me: Claimant, *, now: datetime, reason: str,
            ended: set[str], generation: int | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Give the slot up and leave the queue. Returns the task the slot was handed to.

    Only a holder that is this task -- at `generation` when given -- releases;
    any caller leaves the waiters. Nothing is written when there is nothing
    of this step's to remove.
    """
    if doc is None:
        return None, None
    out = dict(doc)
    holder = _holder(out)
    listed = any(w["task_id"] == me.task_id for w in _waiters(out))
    mine = holder is not None and holder["task_id"] == me.task_id and (
        generation is None or _int(holder.get("generation")) == generation)
    if not mine and not listed:
        return None, None
    waiters = _live_waiters(out, now=now, ended=ended, drop=me.task_id)
    out["waiters"] = waiters
    out["updated_at"] = now
    if not mine:
        return out, None
    assert holder is not None
    out["last_release"] = {"task_id": me.task_id, "generation": holder.get("generation"),
                           "reason": reason, "at": now, "by": me.task_id}
    if not waiters:
        out["holder"] = None
        return out, None
    out["generation"] = _int(out.get("generation")) + 1
    first = waiters[0]
    out["holder"] = _grant(first, generation=out["generation"], now=now, how="handoff")
    out["waiters"] = waiters[1:]
    out["last_release"]["handed_to"] = first["task_id"]
    return out, first["task_id"]


# ---------------------------------------------------------------------------
# Where the documents live
# ---------------------------------------------------------------------------


class MergeSlots(Protocol):
    """The slot documents and the one cross-task write a handoff makes."""

    tenant_id: str

    def now(self) -> datetime: ...

    def peek(self, slot: str) -> Mapping[str, Any] | None: ...

    def transact(self, slot: str,
                 fn: Callable[[Mapping[str, Any] | None], tuple[dict[str, Any] | None, T]]) -> T: ...

    def wake(self, task_id: str) -> bool: ...


class SlotTenantMismatch(Exception):
    """A slot document of another tenant: never read, never written."""


class FirestoreMergeSlots:
    """`MergeSlots` over Firestore, every transaction fenced on this attempt.

    `fence(txn)` is `ControlPlane._fenced_task`: it reads this attempt's task
    and lease through the same transaction and raises `FencedWriteRefused`
    when the attempt is superseded, so the slot write is refused with it.
    """

    def __init__(self, db: Any, *, tenant_id: str,
                 run_transaction: Callable[[Callable[[Any], Any]], Any],
                 fence: Callable[[Any], Any],
                 call_options: Callable[[], Mapping[str, Any]] = dict,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self.tenant_id = tenant_id
        self._run = run_transaction
        self._fence = fence
        self._options = call_options
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    def _ref(self, slot: str) -> Any:
        return self._db.collection(MERGE_SLOTS_COLLECTION).document(slot)

    def _mine(self, data: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if data is None:
            return None
        if data.get("tenant_id") != self.tenant_id:
            raise SlotTenantMismatch(f"merge slot of tenant {data.get('tenant_id')!r}")
        return data

    def peek(self, slot: str) -> Mapping[str, Any] | None:
        snap = self._ref(slot).get(**dict(self._options()))
        return self._mine(snap.to_dict() if snap.exists else None)

    def transact(self, slot: str, fn: Callable[[Mapping[str, Any] | None],
                                                tuple[dict[str, Any] | None, T]]) -> T:
        ref = self._ref(slot)

        def body(txn: Any) -> T:
            # Every read before any write: Firestore forbids a read after one.
            snap = _snapshot(txn.get(ref, **dict(self._options())))
            self._fence(txn)
            data = self._mine(snap.to_dict() if snap.exists else None)
            new, result = fn(data)
            if new is not None:
                txn.set(ref, new)
            return result

        return self._run(body)

    def wake(self, task_id: str) -> bool:
        """Mark a waiter's slot-wait park for the scheduler, guarded; True if marked.

        Only a task of this tenant, PARKED on CI_PENDING with the code
        `MERGE_SLOT_WAIT` and no marker yet: the marker swarm-api's wake tick
        writes (`swarm_api.mergewake._record`), the scheduler's
        `_promote_ci_waits` wakes on, and a forged one of costs one early
        wake. Fenced like every slot write.
        """
        ref = self._db.collection("tasks").document(task_id)
        now = self.now()

        def body(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref, **dict(self._options())))
            self._fence(txn)
            data = snap.to_dict() if snap.exists else None
            if not data or data.get("tenant_id") != self.tenant_id:
                return False
            if (data.get("state") != TaskState.PARKED.value
                    or data.get("park_reason") != ParkReason.CI_PENDING.value):
                return False
            metadata = dict(_mapping(data.get("metadata")))
            wait = dict(_mapping(metadata.get(_MERGE_WAIT_KEY)))
            if wait.get("code") != MERGE_SLOT_WAIT or wait.get(_WAKE_MARKER):
                return False
            wait[_WAKE_MARKER] = now
            wait["wake_reason"] = SLOT_WAKE_REASON
            metadata[_MERGE_WAIT_KEY] = wait
            txn.update(ref, {"metadata": metadata, "updated_at": now})
            return True

        return bool(self._run(body))


# ---------------------------------------------------------------------------
# One merge attempt's use of its slot
# ---------------------------------------------------------------------------


@dataclass
class SlotUse:
    """The slot as one merge attempt sees it: what it holds, and how it ends.

    `task_state` reads a task's state through the tenant-gated upstream read
    (None when it cannot be read: never taken as ended, so a read that fails
    never takes a live holder's slot). `base` is set by the merge once it
    knows the pull request's base; nothing is read or written before.
    """

    slots: MergeSlots
    me: Claimant
    repository: str
    task_state: Callable[[str], TaskState | None]
    log: Any = None
    base: str | None = None
    held: Held | None = None
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def slot(self) -> str | None:
        if self.base is None:
            return None
        return slot_id(self.slots.tenant_id, self.repository, self.base)

    def _ended(self, task_id: str) -> bool:
        state = self.task_state(task_id)
        return state is not None and state in _ENDED

    def renew(self, *, updates: int) -> Held | None:
        """Keep the slot if this step holds it (a handoff, or an earlier attempt's)."""
        slot = self.slot
        assert slot is not None
        now = self.slots.now()
        self.held = self.slots.transact(
            slot, lambda doc: renew(doc, self.me, now=now, updates=updates))
        if self.held is not None:
            self.record.update(slot=slot, held=True, generation=self.held.generation,
                               updates_at_acquire=self.held.updates_at_acquire)
        return self.held

    def acquire(self, *, updates: int) -> Held | Waiting:
        slot = self.slot
        assert slot is not None
        peek = self.slots.peek(slot) or {}
        holder = _holder(peek)
        ended: set[str] = set()
        now = self.slots.now()
        if (holder is not None and holder["task_id"] != self.me.task_id
                and not holder_expired(holder, now) and self._ended(holder["task_id"])):
            ended.add(holder["task_id"])
        result = self.slots.transact(slot, lambda doc: acquire(
            doc, self.me, tenant_id=self.slots.tenant_id, repository=self.repository,
            base=self.base or "", now=now, updates=updates, ended=ended))
        self.record["slot"] = slot
        if isinstance(result, Held):
            self.held = result
            self.record.update(held=True, generation=result.generation,
                               updates_at_acquire=result.updates_at_acquire)
            return result
        self.record.update(held=False, holder=result.holder, position=result.position,
                           waiters=result.waiters)
        if result.wake is not None:
            self._wake(result.wake)
        return result

    def confirm(self) -> None:
        """Raise `SlotLost` unless this attempt still holds the slot at its generation.

        Called after the fencing recheck, immediately before a forge write.
        """
        slot = self.slot
        if self.held is None or slot is None:
            raise SlotLost("this attempt does not hold the merge slot")
        holder = _holder(self.slots.peek(slot) or {})
        if (holder is None or holder["task_id"] != self.me.task_id
                or _int(holder.get("generation")) != self.held.generation):
            self.held = None
            self.record["held"] = False
            raise SlotLost(f"the merge slot is {holder['task_id'] if holder else 'free'} now")

    def release(self, reason: str) -> str | None:
        """Give up the slot (handing it on) and leave the queue. Never raises a forge error."""
        slot = self.slot
        if slot is None:
            return None
        peek = self.slots.peek(slot) or {}
        holder = _holder(peek)
        if (self.held is None and (holder is None or holder["task_id"] != self.me.task_id)
                and not any(w["task_id"] == self.me.task_id for w in _waiters(peek))):
            return None  # nothing of this step's to give back: no transaction
        ended: set[str] = set()
        for entry in _live_waiters(peek, now=self.slots.now(), ended=set(),
                                   drop=self.me.task_id)[:HANDOFF_READS]:
            if not self._ended(entry["task_id"]):
                break
            ended.add(entry["task_id"])
        now = self.slots.now()
        generation = self.held.generation if self.held is not None else None
        handed = self.slots.transact(slot, lambda doc: release(
            doc, self.me, now=now, reason=reason, ended=ended, generation=generation))
        if self.held is not None or handed is not None:
            self.record.update(released=reason)
        self.held = None
        if handed is not None:
            self.record["handed_to"] = handed
            self._wake(handed)
        return handed

    def _wake(self, task_id: str) -> None:
        try:
            woken = self.slots.wake(task_id)
        except Exception as exc:  # noqa: BLE001 - the fallback instant wakes it anyway
            if _is_fence(exc):
                raise
            woken = False
            if self.log is not None:
                self.log.warning("merge slot handoff wake not written", task=task_id,
                                 error=type(exc).__name__)
        self.record.setdefault("woke", []).append({"task_id": task_id, "marked": bool(woken)})


_ENDED = TERMINAL_STATES


def _is_fence(exc: BaseException) -> bool:
    from .errors import FencedError

    return isinstance(exc, FencedError)
