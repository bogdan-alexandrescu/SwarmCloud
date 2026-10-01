"""Which subscription account an agent is running on, read off its own events (#379).

WHERE THE FACT IS. The worker leases an account from the quota broker once per
attempt and records it as an event on the task, never on the task document:

  * `running`  with `detail = {cause: "account_assigned", account_id, provider}`
    (`agent_worker.lifecycle.AgentLifecycle._lease_account`);
  * `retrying` with `detail = {cause: "account_unreadable", account_id, ...}`
    when the worker could not read the account it was handed, gave it back and
    asked for another (`_reject_account`).

So "which account" is derived here, at read time, from THAT TASK's events.
`docs/contract-change-requests.md` records why an event is a weaker home than
a field; until a field exists this is the one place the inference is made, so
every screen reads the same answer.

WHAT IS READ, AND WHAT IT COSTS. One collection-group query over `events`:

    tenant_id == <tenant> AND task_id IN <up to 30 ids>
      AND "account_" <= detail.cause < "account`"
    LIMIT 64 * <ids>

served by the `events-tenant-task-cause` composite index
(terraform/modules/firestore/indexes.tf). The cause filter is a RANGE over the
`account_` prefix rather than `IN [assigned, unreadable]` because Firestore
caps a query's disjunctions at 30 -- the PRODUCT of its `in` sizes -- and 30
task ids times two causes would be 60; a range adds none. Anything else the
prefix catches is ignored below. A list page of N rows therefore costs
ceil(N / 30) queries -- two for the phone page of 50, seven for a full page of
200 -- and at most 64 documents per task, never one read per row and never a
whole event history.

Tasks that cannot have an account cost nothing: a profile with no provider
calls no model, a profile that does not take a subscription token never asks
the pool, and a task never admitted (generation 0) has had no worker.

WHAT IS NEVER SERVED. Only `account_id`, `provider` and the swap cause leave
this module -- whitelisted, so a detail key the worker adds later (or a
credential somebody logs into one) is never echoed. A read that failed or was
cut short at the cap says `unread`: never an older attempt's account, never a
guess.

THE CACHE. A FINISHED task's events no longer change, so its answer is kept
per API process, keyed on the task's tenant, id, state, generation and
`completed_at`: a task that runs again moves its generation and is read
afresh. Only a complete read is kept.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Attempt, Task
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TERMINAL_STATES

from .codec import as_datetime

log = logging.getLogger(__name__)

EVENTS = "events"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

ACCOUNT_ASSIGNED = "account_assigned"
ACCOUNT_UNREADABLE = "account_unreadable"

#: The range that selects every `account_*` cause: "`" is the code point after
#: "_", so `[account_, account`)` is exactly the strings starting `account_`.
CAUSE_FROM = "account_"
CAUSE_BEFORE = "account`"

#: Firestore's cap on the values of one `in` filter (measured on PR #196).
IN_LIMIT = 30

#: At most this many account events are read per task. An attempt writes one
#: assignment and at most a few swaps (`MAX_ACCOUNT_TRIES` in the worker) and a
#: task has a handful of attempts, so a task past it is reported `unread`
#: rather than derived from part of its history.
PER_TASK_CAP = 64

#: The env var a runner profile declares when it accepts a subscription token.
#: The worker's `accountlease.ACCOUNT_TOKEN_ENV`: a profile without it never
#: asks the pool for an account (`_lease_account`'s second branch).
ACCOUNT_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

STATUS_ASSIGNED = "assigned"
STATUS_NOT_ASSIGNED_YET = "not_assigned_yet"
STATUS_NOT_ASSIGNED = "not_assigned"
STATUS_NO_MODEL_CALL = "no_model_call"
STATUS_NOT_POOLED = "not_pooled"
STATUS_UNREAD = "unread"

#: The swap causes, as the client names them.
_SWAP_CAUSE = {ACCOUNT_UNREADABLE: "unreadable"}


def _account(
    status: str,
    *,
    account_id: str | None = None,
    provider: str | None = None,
    attempt_id: str | None = None,
    generation: int | None = None,
    swaps: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    return {
        "status": status,
        "account_id": account_id,
        "provider": provider,
        "attempt_id": attempt_id,
        "generation": generation,
        "swapped_from": swaps[-1]["account_id"] if swaps and account_id else None,
        "swaps": [dict(s) for s in swaps],
    }


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------

class FinishedAccounts:
    """A finished task's derived account, per process, bounded LRU."""

    def __init__(self, max_entries: int = 20_000) -> None:
        self._max = max_entries
        self._items: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(task: Task) -> tuple | None:
        if task.state not in TERMINAL_STATES:
            return None
        completed = task.completed_at.isoformat() if task.completed_at else None
        return (task.tenant_id, task.id, task.state.value, task.current_generation, completed)

    def get(self, task: Task) -> dict[str, Any] | None:
        key = self.key(task)
        if key is None:
            return None
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, task: Task, account: dict[str, Any]) -> None:
        key = self.key(task)
        if key is None:
            return
        with self._lock:
            self._items[key] = account
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


#: Per API process. Module level because the answer depends only on documents
#: that no longer change, so every app in the process may share it.
FINISHED = FinishedAccounts()


# --------------------------------------------------------------------------
# What a task's profile already decides
# --------------------------------------------------------------------------

def _static(task: Task) -> dict[str, Any] | None:
    """The answer a task's profile or admission state gives without a read, or None."""
    profile = RUNNER_PROFILES.get(task.runner_profile)
    if profile is not None and profile.provider is None:
        return _account(STATUS_NO_MODEL_CALL)
    if profile is not None and ACCOUNT_TOKEN_ENV not in (profile.secrets or ()):
        return _account(STATUS_NOT_POOLED, provider=profile.provider)
    if task.current_generation <= 0:
        # Never admitted: no worker has run, so no account was ever asked for.
        return _account(STATUS_NOT_ASSIGNED if task.state in TERMINAL_STATES else STATUS_NOT_ASSIGNED_YET)
    return None


# --------------------------------------------------------------------------
# The read
# --------------------------------------------------------------------------

def read_account_events(
    db: Any, tenant_id: str, task_ids: Sequence[str]
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Every account event of the named tasks, and the ids whose read was complete.

    ONE QUERY PER 30 IDS (see the module docstring for the shape and the cost).
    A chunk whose query failed, or came back at its limit, is incomplete for
    every task in it: which task's rows the limit cut off is not knowable from
    the rows that did arrive. A task past `PER_TASK_CAP` is incomplete alone.
    """
    wanted = list(dict.fromkeys(t for t in task_ids if t))
    events: dict[str, list[dict[str, Any]]] = {t: [] for t in wanted}
    complete: set[str] = set()
    for start in range(0, len(wanted), IN_LIMIT):
        chunk = wanted[start : start + IN_LIMIT]
        limit = PER_TASK_CAP * len(chunk)
        query = (
            db.collection_group(EVENTS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("task_id", "in", chunk))
            .where(filter=FieldFilter("detail.cause", ">=", CAUSE_FROM))
            .where(filter=FieldFilter("detail.cause", "<", CAUSE_BEFORE))
            .limit(limit)
        )
        try:
            rows = [snap.to_dict() or {} for snap in query.stream()]
        except Exception as exc:  # the tasks were read; only this reading failed
            log.warning("task account read failed: %s", type(exc).__name__)
            continue
        members = set(chunk)
        for row in rows:
            # The query already filters both; this is the boundary restated
            # where a fake, an index mistake or a future refactor cannot move it.
            if row.get("tenant_id") != tenant_id or row.get("task_id") not in members:
                continue
            events[row["task_id"]].append(row)
        if len(rows) >= limit:
            continue
        complete.update(t for t in chunk if len(events[t]) <= PER_TASK_CAP)
    return events, complete


# --------------------------------------------------------------------------
# The derivation
# --------------------------------------------------------------------------

def _moment(value: Any) -> datetime:
    try:
        return as_datetime(value) or _EPOCH
    except (TypeError, ValueError):
        return _EPOCH


def _ordered(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Oldest first, the event id breaking a tie as `Store.list_events` does."""
    return sorted(rows, key=lambda r: (_moment(r.get("at")), str(r.get("event_id") or "")))


def _walk(rows: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, list[dict[str, str]]]:
    """The assignment an attempt ended up holding, and every account it gave back.

    "The last `account_assigned` not followed by an `account_unreadable` for
    the same id" -- the inference contract-change-requests.md warns every
    reader would otherwise re-derive.
    """
    held: Mapping[str, Any] | None = None
    swaps: list[dict[str, str]] = []
    for row in _ordered(rows):
        detail = row.get("detail") or {}
        cause = detail.get("cause")
        account_id = detail.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            continue
        if cause == ACCOUNT_ASSIGNED:
            held = row
        elif cause in _SWAP_CAUSE:
            swaps.append({"account_id": account_id, "cause": _SWAP_CAUSE[cause]})
            if held is not None and (held.get("detail") or {}).get("account_id") == account_id:
                held = None
    return held, swaps


def _from_rows(rows: Sequence[Mapping[str, Any]], *, live: bool) -> dict[str, Any]:
    held, swaps = _walk(rows)
    if held is None:
        first = _ordered(rows)[0] if rows else {}
        return _account(
            STATUS_NOT_ASSIGNED_YET if live else STATUS_NOT_ASSIGNED,
            attempt_id=first.get("attempt_id"),
            generation=first.get("generation"),
            swaps=swaps,
        )
    detail = held.get("detail") or {}
    provider = detail.get("provider")
    return _account(
        STATUS_ASSIGNED,
        account_id=detail["account_id"],
        provider=provider if isinstance(provider, str) else None,
        attempt_id=held.get("attempt_id"),
        generation=held.get("generation"),
        swaps=swaps,
    )


def derive(task: Task, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The task's LATEST attempt's account, from a complete read of its account events.

    The latest attempt is the task's `current_generation`. An earlier attempt's
    account is never served as this one's: a retry may be on another account,
    and until it is assigned one it says `not_assigned_yet`.
    """
    static = _static(task)
    if static is not None:
        return static
    current = [r for r in rows if r.get("generation") == task.current_generation]
    return _from_rows(current, live=task.state not in TERMINAL_STATES)


def accounts_for(
    db: Any, tenant_id: str, tasks: Sequence[Task], *, cache: FinishedAccounts | None = None
) -> dict[str, dict[str, Any]]:
    """`task.account` for every task of one page, in at most ceil(n / 30) queries."""
    cache = FINISHED if cache is None else cache
    out: dict[str, dict[str, Any]] = {}
    pending: list[Task] = []
    for task in tasks:
        if task.tenant_id != tenant_id:
            # Never read across tenants; the store already refused such a row.
            continue
        static = _static(task)
        if static is not None:
            out[task.id] = static
            continue
        hit = cache.get(task)
        if hit is not None:
            out[task.id] = hit
            continue
        pending.append(task)
    if not pending:
        return out
    events, complete = read_account_events(db, tenant_id, [t.id for t in pending])
    for task in pending:
        if task.id not in complete:
            out[task.id] = _account(STATUS_UNREAD)
            continue
        account = derive(task, events.get(task.id, []))
        out[task.id] = account
        cache.put(task, account)
    return out


def accounts_for_attempts(
    db: Any, task: Task, attempts: Sequence[Attempt]
) -> dict[str, dict[str, Any]]:
    """Each attempt's own account and swaps, keyed by `attempt_id`, from one bounded read."""
    static = _static(task)
    if static is not None:
        return {a.attempt_id: static for a in attempts}
    events, complete = read_account_events(db, task.tenant_id, [task.id])
    if task.id not in complete:
        return {a.attempt_id: _account(STATUS_UNREAD) for a in attempts}
    rows = events.get(task.id, [])
    terminal = task.state in TERMINAL_STATES
    out: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        own = [
            r
            for r in rows
            if r.get("attempt_id") == attempt.attempt_id
            or (r.get("attempt_id") is None and r.get("generation") == attempt.generation)
        ]
        live = (
            not terminal
            and attempt.completed_at is None
            and attempt.generation == task.current_generation
        )
        account = _from_rows(own, live=live)
        if account["attempt_id"] is None:
            account["attempt_id"] = attempt.attempt_id
            account["generation"] = attempt.generation
        out[attempt.attempt_id] = account
    return out
