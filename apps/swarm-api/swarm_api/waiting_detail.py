"""Why a waiting task waits, what resumes it and when it is checked next (#1782).

WHY THIS EXISTS BESIDE `waiting_for`
------------------------------------
`waiting.waiting_for` answers one question for one state: which pool refuses
a READY task, now (#362). A PARKED task has a reason too, and its park events
already record that reason's facts -- a merge step's pull request, its head,
each pending check by name, the wakes so far, whether the attempt was
refunded, the next check. Before this the console showed "waiting for CI ·
eligible since 19:46:25" for every merge step of the 2026-10-20/21 CI stall,
whether its checks were queued, failing or done. `waiting` is those recorded
facts, gathered in one place and put into one sentence.

WHAT IS NEVER CLAIMED
---------------------
Only RECORDED facts: the task document, its newest events, the `waiting_for`
reading the route already made and the tenant's related tasks it read in one
batch. A fact that was not recorded is null, never inferred. Nothing is read
from GitHub or a provider: the CI detail is what the merge step recorded at
its last check, labelled with that check's time (`last_checked_at`).
`next_check_at` is served only for the parks whose `next_eligible_at` IS a
check time; for AWAITING_INPUT, HUMAN_REQUIRED and CHILDREN_INCOMPLETE the
park writes the park instant there, and serving it as a "next check" would
be a time at which nothing happens.

A CREDENTIAL_MISSING detail is built from an allow-list of NAMES -- the
provider, each workspace-secret slot, each MCP server -- and never copies a
recorded mapping whole, so no value, or any part of one, is served.

PURE. `build_waiting` reads nothing and writes nothing; `read_inputs` is the
route's one place that reads for it, inside the task's own tenant
(invariant 9), and the listing reads nothing for it at all.

PLACE IN LINE (WAIT-LINE, owner decision 2026-10-21)
----------------------------------------------------
For a READY task behind a pool, `place_in_line` is 1 + the READY tasks that
need the same pool and come before it in the scheduler's READY query
(`SchedulerStore.ready_tasks`: priority DESC, then created_at ASC). ONE count
aggregation, on `GET /v1/tasks/{id}` only: the query ends before the task's
own position in that order, so the count is exactly what the scheduler's
slice reads ahead of it. Served by the drain-loop indexes for `global` and
`tenant:` pools and by the `tasks-*-state-priority-created` indexes for the
rest (terraform/modules/firestore/indexes.tf).

What it does not model, and says so in `place_in_line_basis.excludes`: the
aging bonus and the cross-tenant round-robin `scheduler/fairness.py` applies
to the slice, and the tie order of two tasks created at the same instant. The
number is the database's line, not a promise of the pass's order.

A tenant pool counts the task's own tenant only. A shared pool (`global`,
`resource:`, `runner:`, `backend:`, `provider:`) counts across tenants, and
then ONLY the number is served: never which tasks or tenants are ahead
(invariant 9). A failed count -- an index still building, anything else --
serves null with `place_in_line_unavailable` naming why; it never fails the
GET. The masking
(`mask_waiting`) is the task's own masker, applied by the serialiser.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from google.api_core import exceptions as gexc
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Task, TaskEvent
from swarm_common.profiles import RUNNER_PROFILES, resolve_backend
from swarm_common.states import EventType, ParkReason, TaskState, TERMINAL_STATES

from .redaction import redact

log = logging.getLogger(__name__)

READY_CAPACITY = "READY_CAPACITY"
QUEUED = "QUEUED"

#: The newest entries a history keeps. A merge step that waited all night
#: has dozens of parks; the panel shows the recent ones, newest last.
HISTORY_MAX = 20
#: The newest events the GET reads to build the history and the park facts.
EVENTS_READ = 100
#: The children a CHILDREN_INCOMPLETE detail lists at most.
CHILDREN_READ = 100
#: Pending check names a summary names before "...".
SUMMARY_NAMES = 3

#: The `credential` of a park waiting for a workflow's caller to re-store an
#: MCP credential: `mcp_credentials.PARK_CREDENTIAL`, spelled again so this
#: pure module imports nothing that reads Firestore.
MCP_PARK_CREDENTIAL = "mcp"

#: Parks whose `next_eligible_at` is the time the scheduler may next act.
_TIMED = frozenset({
    ParkReason.PROVIDER_QUOTA_EXHAUSTED.value,
    ParkReason.PROVIDER_COOLDOWN.value,
    ParkReason.PROVIDER_OUTAGE.value,
    ParkReason.SCHEDULED_RETRY.value,
    ParkReason.CI_PENDING.value,
    ParkReason.CREDENTIAL_MISSING.value,
})

_PROVIDER_WORDS = {
    ParkReason.PROVIDER_QUOTA_EXHAUSTED.value: "Provider quota exhausted",
    ParkReason.PROVIDER_COOLDOWN.value: "Provider cooling down",
    ParkReason.PROVIDER_OUTAGE.value: "Provider outage",
}

#: Why no account of the pool can take an agent, by the broker's reason
#: (`quota_broker.accounts.Unavailable`), as the waiting line says it (#2222).
#: Restated: swarm-api does not import the broker. A reason not here is shown
#: as the broker spelled it.
_POOL_WORDS = {
    "accounts_full": "every account with room already holds as many agents as it may",
    "no_account_available": "every account is under its floor",
    "no_recent_reading": "no account has a reading recent enough to trust",
    "pool_paused": "every account is paused, draining or needs a sign-in",
}

#: `pool_paused` when an account the park names is withheld as a duplicate.
_POOL_PAUSED_OR_WITHHELD = (
    "every account is paused, draining, needs a sign-in or is withheld as a duplicate"
)

#: The accounts a pool park's line names before "and N more".
POOL_ACCOUNTS_NAMED = 3

#: Window names as the line says them; another provider's is shown as spelled.
_WINDOW_WORDS = {"five_hour": "5-hour", "seven_day": "7-day"}

_HISTORY_KINDS = {
    EventType.QUEUED: "queued",
    EventType.PARKED: "parked",
    EventType.READY: "ready",
    EventType.RUNNING: "running",
    EventType.RETRYING: "retrying",
    EventType.QUOTA_EXHAUSTED: "quota_exhausted",
}

#: Why a place in line is null where nothing counted it: the builder was
#: not handed a count (a listing, a workflow step row).
_PLACE_IN_LINE_WHY = "not counted on this route: only GET /v1/tasks/{id} counts the line"

TASKS = "tasks"
#: The scheduler's READY query order (`SchedulerStore.ready_tasks`), restated
#: as the count's cursor order because swarm-api does not import the
#: scheduler. `test_the_count_follows_the_schedulers_own_ready_query_order`
#: holds the two together.
LINE_ORDER = (("priority", "DESCENDING"), ("created_at", "ASCENDING"))
_LINE_ORDER_WORDS = ("priority descending, then created_at ascending: the scheduler's READY "
                     "query (SchedulerStore.ready_tasks)")
_LINE_EXCLUDES = ("the aging bonus and cross-tenant round-robin scheduler/fairness.py applies "
                  "within a pass, and tasks created at the same instant")


# --------------------------------------------------------------------------
# Small readers
# --------------------------------------------------------------------------

def _hhmm(at: datetime | None, as_of: datetime) -> str:
    if at is None:
        return "unknown"
    if at.date() == as_of.date():
        return at.strftime("%H:%MZ")
    return at.strftime("%Y-%m-%d %H:%MZ")


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _names(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return [str(v) for v in value]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _newest(events: Sequence[TaskEvent] | None, kind: EventType,
            reason: str | None = None) -> TaskEvent | None:
    """The newest event of `kind` (and, for a park, of `reason`)."""
    for ev in sorted(events or (), key=lambda e: e.at, reverse=True):
        if ev.type is not kind:
            continue
        if reason is not None and ev.detail.get("reason") not in (None, reason):
            continue
        return ev
    return None


def _park_facts(task: Task, events: Sequence[TaskEvent] | None) -> tuple[dict[str, Any], TaskEvent | None]:
    """The newest park's recorded detail, and its event.

    From the newest `parked` event of the task's park reason when the events
    were read; otherwise the worker's copy in `blocked_by` (every worker park
    writes `[{"reason", **detail}]` there), when it names the same reason.
    """
    reason = task.park_reason.value if task.park_reason else None
    ev = _newest(events, EventType.PARKED, reason)
    if ev is not None:
        return dict(ev.detail), ev
    for entry in task.blocked_by or ():
        if isinstance(entry, Mapping) and entry.get("reason") == reason:
            return dict(entry), None
    return {}, None


def _other_blocker(task: Task) -> dict[str, Any] | None:
    """The newest `blocked_by` entry that is not the park's own copy."""
    own = task.park_reason.value if task.park_reason else None
    for entry in reversed(task.blocked_by or []):
        if isinstance(entry, Mapping) and entry.get("reason") != own:
            return dict(entry)
    return None


def _blocker_words(entry: Mapping[str, Any]) -> str:
    pool = entry.get("pool")
    reason = entry.get("reason")
    if pool:
        return f"the last scheduler pass found {pool} refusing ({reason})"
    return f"the last scheduler pass recorded {reason}"


def pull_request_url(repository_url: str | None, number: int | None) -> str | None:
    """`https://host/owner/repo/pull/N` off the task's https repository URL.

    The userinfo is dropped, never copied, and anything that is not an https
    URL with an owner and a repository serves null.
    """
    if number is None or not repository_url:
        return None
    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if len([p for p in path.split("/") if p]) != 2:
        return None
    return f"https://{parsed.hostname}{path}/pull/{number}"


# --------------------------------------------------------------------------
# Per reason
# --------------------------------------------------------------------------

def _ci(task: Task, facts: dict[str, Any], park: TaskEvent | None, as_of: datetime) -> dict[str, Any]:
    wait = _mapping(_mapping(task.metadata).get("merge_wait"))
    target = _mapping(_mapping(_mapping(task.metadata).get("dispatch")).get("merge_target"))
    number = _int(facts.get("pull_request"))
    if number is None:
        number = _int(wait.get("pull_request"))
    if number is None:
        number = _int(target.get("number"))
    pending = _names(facts["pending"]) if "pending" in facts else _names(wait.get("pending"))
    failed = _names(facts.get("failed")) if "failed" in facts else _names(wait.get("failed"))
    wakes = _int(facts.get("wakes")) if "wakes" in facts else _int(wait.get("wakes"))
    refunded = facts.get("attempt_refunded")
    last_checked = park.at if park is not None else _as_dt(wait.get("parked_at"))
    head_at = _str(facts.get("head")) or _str(wait.get("head"))
    # THE WAKE TICK'S NEWER READING, when it read the checks at the head this
    # park waits at after the park was recorded (STATUS-TRUE, part of #1914;
    # `mergewake.READING_KEY`): on wf_15160fca1d07438ab594 the line named
    # ci-gate pending for minutes after it had FAILED and a fix round was
    # running, because only the park's own facts were read. The line then
    # says when it was read, and names the round under way.
    reading = _mapping(wait.get("reading"))
    read_at = _as_dt(reading.get("read_at"))
    newer = (
        bool(reading) and head_at is not None and reading.get("head") == head_at
        and read_at is not None and (last_checked is None or read_at > last_checked)
    )
    fix_round = _mapping(reading.get("fix_round")) if newer else {}
    if newer:
        pending = _names(reading.get("pending"))
        failed = _names(reading.get("failed"))
        last_checked = read_at
    detail = {
        "pull_request": (
            {"number": number, "url": pull_request_url(task.repository_url, number)}
            if number is not None else None
        ),
        "head": head_at,
        "code": _str(facts.get("code")) or _str(wait.get("code")),
        "pending": pending,
        "failed": failed,
        "wakes": wakes,
        "attempts_counted": (not refunded) if isinstance(refunded, bool) else None,
        "last_checked_at": last_checked,
        "wake_requested_at": _as_dt(wait.get("wake_requested_at")),
        "fix_round": (
            {"kind": _str(fix_round.get("kind")), "round": _int(fix_round.get("round")),
             "task_id": _str(fix_round.get("task_id")), "state": _str(fix_round.get("state"))}
            if fix_round else None
        ),
    }
    where = f"CI on PR #{number}" if number is not None else "CI on its pull request"
    parts: list[str] = []
    if pending is not None and not (newer and not pending and failed):
        shown = pending[:SUMMARY_NAMES]
        more = ", ..." if len(pending) > SUMMARY_NAMES else ""
        names = f" ({', '.join(shown)}{more})" if shown else ""
        parts.append(f"{len(pending)} checks pending{names}")
    if failed:
        parts.append(f"{len(failed)} failed ({', '.join(failed[:SUMMARY_NAMES])})")
    if not parts:
        parts.append("checks not recorded")
    head = f"{where}: {', '.join(parts)}"
    if fix_round:
        state = _str(fix_round.get("state"))
        head += (f"; fix round {_int(fix_round.get('round'))} "
                 f"{state.lower() if state else 'state not read'}")
    if newer:
        head += f"; read {_hhmm(read_at, as_of)}"
    if wakes is not None:
        head += f"; re-checked {wakes} times"
    if task.next_eligible_at is not None:
        head += f"; next check {_hhmm(task.next_eligible_at, as_of)}"
    resumes = "its pull request's checks settle (swarm-api's wake tick marks the step)"
    if task.next_eligible_at is not None:
        resumes += f", or the fallback re-check at {_hhmm(task.next_eligible_at, as_of)}"
    since = _as_dt(wait.get("first_parked_at"))
    return {"detail": detail, "summary": head, "resumes_when": resumes, "since": since}


def _ready(task: Task, waiting_for: Mapping[str, Any] | None, line: Mapping[str, Any] | None,
           as_of: datetime) -> dict[str, Any]:
    lead = _mapping((waiting_for or {}).get("lead")) or None
    if line is None:
        line = {"place_in_line": None, "place_in_line_unavailable": _PLACE_IN_LINE_WHY,
                "place_in_line_basis": None}
    detail = {
        "waiting_for": dict(waiting_for) if waiting_for is not None else None,
        "pool": lead.get("pool") if lead else None,
        "pool_state": lead.get("state") if lead else None,
        "active": lead.get("active") if lead else None,
        "limit": lead.get("limit") if lead else None,
        "units": lead.get("units") if lead else None,
        "place_in_line": _int(line.get("place_in_line")),
        "place_in_line_unavailable": _str(line.get("place_in_line_unavailable")),
        "place_in_line_basis": (dict(line["place_in_line_basis"])
                                if isinstance(line.get("place_in_line_basis"), Mapping) else None),
        "last_pass": [dict(b) for b in task.blocked_by or [] if isinstance(b, Mapping)],
    }
    if lead:
        state = lead.get("state")
        pool = lead.get("pool")
        if state == "paused":
            summary = f"Ready; pool {pool} is paused"
        elif state == "unknown":
            summary = f"Ready; pool {pool} could not be read"
        elif state in ("zero", "below_units"):
            summary = (f"Ready; pool {pool} admits {lead.get('limit')} units and this task "
                       f"needs {lead.get('units')}")
        else:
            summary = (f"Ready; capacity binds on {pool} "
                       f"({lead.get('active')} of {lead.get('limit')} units in use)")
        resumes = f"{pool} has room for {lead.get('units')} units and the scheduler admits it"
    elif waiting_for is not None and waiting_for.get("admissible_now") is True:
        summary = "Ready; every pool has room: waiting for the scheduler's next pass"
        resumes = "the scheduler's next pass admits it"
    else:
        other = _other_blocker(task)
        if other is not None:
            summary = f"Ready; {_blocker_words(other)}"
        else:
            summary = "Ready; waiting for the scheduler's next pass"
        resumes = "the scheduler's next pass admits it"
    if task.next_eligible_at is not None and task.next_eligible_at > as_of:
        summary += f"; not eligible until {_hhmm(task.next_eligible_at, as_of)}"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _pool_other(label: Any) -> str:
    """The other side of a duplicate pair, as the park recorded it (#2222).

    `scheduler.loop.pool_blockers` writes the label only for the waiting
    agent's own workspace (BGD, Q2: "only within the same tenant"), and null
    for another workspace's account, which the line names without one.
    """
    return _str(label) or "an account in another workspace"


def _pool_account_words(entry: Mapping[str, Any], as_of: datetime) -> str | None:
    """One account of a pool park, in words: what blocks it and when that clears."""
    label = _str(entry.get("account"))
    if label is None:
        return None
    parts: list[str] = []
    state = _str(entry.get("state"))
    if state:
        parts.append(state.lower())
    withheld = entry.get("withheld")
    if isinstance(withheld, Mapping):
        other = _pool_other(withheld.get("same_subscription_as"))
        parts.append(f"withheld: same subscription as {other}")
    for name, w in sorted(_mapping(entry.get("windows")).items()):
        if not isinstance(w, Mapping):
            continue
        used = w.get("used")
        pct = f" at {round(float(used) * 100)}%" if isinstance(used, (int, float)) else ""
        clears = _as_dt(w.get("resets_at"))
        when = f", clears {_hhmm(clears, as_of)}" if clears is not None else ""
        parts.append(f"{_WINDOW_WORDS.get(name, name)}{pct}{when}")
    if _str(entry.get("first_reading")) == "pending":
        parts.append("taking one agent until its first reading")
    full = _str(entry.get("full"))
    if full:
        parts.append(f"full ({full})")
    possible = entry.get("possible_duplicate")
    if isinstance(possible, Mapping):
        other = _pool_other(possible.get("of"))
        parts.append(f"possible duplicate of {other} (identical readings)")
    return f"{label} {'; '.join(parts)}" if parts else label


def _pool(task: Task, provider: str | None, facts: dict[str, Any], as_of: datetime,
          detail: dict[str, Any]) -> dict[str, Any]:
    """A park on the account pool: no account can take a new agent (#2222).

    Before this it read "Provider quota exhausted", the words for a provider
    refusing the tenant, while on 2026-10-24 every account had room by its
    5-hour window and the floor, the cap or a stale reading was what held it.
    The line says which, and names the accounts the park recorded
    (`scheduler.loop.pool_blockers`, or the worker's park, which records
    none), so a reader sees what clears first.
    """
    pool_reason = _str(facts.get("account_pool_reason"))
    words = _POOL_WORDS.get(pool_reason or "", pool_reason or "no account can take it")
    accounts = [a for a in facts.get("accounts") or () if isinstance(a, Mapping)]
    withheld = any(isinstance(a.get("withheld"), Mapping) for a in accounts)
    if pool_reason == "pool_paused" and withheld:
        # The broker answers `pool_paused` when nothing is assignable and
        # nothing is dated, and a duplicate it withholds is one of those: the
        # account is AVAILABLE, so "paused" alone would send a reader to an
        # account nobody paused.
        words = _POOL_PAUSED_OR_WITHHELD
    summary = f"No pool account can take an agent on {provider or 'its provider'}: {words}"
    named = [w for w in (_pool_account_words(a, as_of) for a in accounts) if w]
    if named:
        shown = named[:POOL_ACCOUNTS_NAMED]
        more = len(named) - len(shown)
        summary += " (" + "; ".join(shown) + (f"; and {more} more" if more else "") + ")"
    until = task.next_eligible_at
    if until is not None:
        summary += f"; next check {_hhmm(until, as_of)}"
    detail = {**detail, "accounts": [dict(a) for a in accounts]}
    resumes = ("an account frees a slot or its window clears, and the scheduler re-admits it; "
               "an agent finishing wakes it sooner")
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _provider(task: Task, reason: str, facts: dict[str, Any], account: Mapping[str, Any] | None,
              as_of: datetime) -> dict[str, Any]:
    provider = _str(facts.get("provider")) or task.provider
    label = _str(facts.get("account_id")) or _str(facts.get("account"))
    if label is None and account:
        label = _str(account.get("account_id"))
    until = task.next_eligible_at
    detail = {
        "provider": provider,
        "account": label,
        "provider_state": _str(facts.get("provider_state")),
        "source": _str(facts.get("source")),
        "account_pool_reason": _str(facts.get("account_pool_reason")),
        "wait_seconds": _int(facts.get("wait_seconds")),
        "until": until,
    }
    if reason == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value and (
        facts.get("account_pool_reason") or facts.get("park_phase") == "account_assign"
    ):
        return _pool(task, provider, facts, as_of, detail)
    summary = f"{_PROVIDER_WORDS[reason]} on {provider or 'its provider'}"
    if label:
        summary += f" (account {label})"
    if until is not None:
        summary += f"; until {_hhmm(until, as_of)}"
    resumes = "the provider's window passes and the scheduler re-admits it"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _retry(task: Task, facts: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    nxt = task.next_eligible_at
    cause = _str(facts.get("cause"))
    detail = {
        "last_error": task.last_error,
        "attempt": task.attempt_count,
        "next_attempt": task.attempt_count + 1,
        "max_attempts": task.max_attempts,
        "next_try_at": nxt,
        "cause": cause,
    }
    summary = f"Retry scheduled: attempt {task.attempt_count + 1} of {task.max_attempts}"
    if nxt is not None:
        summary += f" at {_hhmm(nxt, as_of)}"
    if cause:
        summary += f" ({cause})"
    resumes = "the retry time passes and the scheduler re-admits it"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _row_of(task_id: str, related: Mapping[str, Task] | None) -> dict[str, Any]:
    other = (related or {}).get(task_id)
    return {
        "task_id": task_id,
        "step_id": other.step_id if other is not None else None,
        "state": other.state.value if other is not None else None,
    }


def _dependencies(task: Task, facts: dict[str, Any], related: Mapping[str, Task] | None) -> dict[str, Any]:
    ids = _names(facts.get("waiting_on")) or list(task.depends_on or [])
    rows = [_row_of(i, related) for i in ids]
    detail = {"waiting_on": rows, "states_read": related is not None}
    named = ", ".join(
        f"{r['step_id'] or r['task_id']} ({r['state'] or 'state not read'})" for r in rows
    )
    summary = f"Waiting on {len(rows)} upstream steps" + (f": {named}" if named else "")
    resumes = "every upstream step it depends on has SUCCEEDED"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _children(task: Task, related: Mapping[str, Task] | None) -> dict[str, Any]:
    if related is None:
        detail: dict[str, Any] = {"children": None, "unfinished": None, "total": None,
                                  "states_read": False}
        return {"detail": detail, "summary": "Waiting for its children to finish",
                "resumes_when": "every child it submitted has finished", "since": None}
    kids = [t for t in related.values() if t.parent_task_id == task.id]
    kids.sort(key=lambda t: t.id)
    open_ = [t for t in kids if t.state not in TERMINAL_STATES]
    counts = Counter(t.state.value for t in open_)
    detail = {
        "children": [{"task_id": t.id, "state": t.state.value} for t in kids],
        "unfinished": len(open_),
        "total": len(kids),
        "states_read": True,
    }
    tally = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    summary = f"Waiting for {len(open_)} of {len(kids)} children to finish"
    if tally:
        summary += f" ({tally})"
    return {"detail": detail, "summary": summary,
            "resumes_when": "every child it submitted has finished", "since": None}


def _who(task: Task) -> dict[str, Any]:
    return {"tenant_id": task.tenant_id, "text": f"any member of workspace {task.tenant_id}"}


def _awaiting_input(task: Task, facts: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    window = _mapping(_mapping(task.metadata).get("await_input"))
    expires = _as_dt(window.get("expires_at")) or _as_dt(facts.get("expires_at"))
    opened = _as_dt(window.get("opened_at")) or _as_dt(facts.get("opened_at"))
    command = (
        f"/sc answer {task.workflow_id} {task.step_id}"
        if task.workflow_id and task.step_id else None
    )
    detail = {"who_may_answer": _who(task), "opened_at": opened, "expires_at": expires,
              "answer_command": command}
    summary = f"Waiting for a member of {task.tenant_id} to answer"
    if expires is not None:
        summary += f"; expires {_hhmm(expires, as_of)}"
    if command:
        summary += f"; {command}"
    resumes = "a member of the workspace answers, or the window expires and its default applies"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": opened}


#: The merge step's owner-question hold, as its park records it
#: (`agent_worker.lifecycle._park_for_owner_questions`): `park_phase` names
#: it and `owner_questions` holds `{code, questions, unreadable}`. Spelled
#: again: the worker is not installed in this image.
OWNER_QUESTIONS_PHASE = "owner_questions"


def _owner_questions(task: Task, facts: dict[str, Any]) -> dict[str, Any]:
    """A merge step held by the owner's questions (#2070): those questions, not a sign-in (#2313).

    The merge parks HUMAN_REQUIRED because that park holds nothing and no
    timer ends it, but nobody signs in to anything: the answer that clears
    the last named question returns it to READY in the answer's own
    transaction (`agent_questions._hold_cleared`, QUESTION-WAKE), and
    `human-ready` only re-runs it into the same park. So no `answer_route`
    is served -- EXCEPT for a hold that route cannot read whole (an
    `unreadable` count, an entry that is not a task id and an integer
    index): it fails closed and wakes nothing, and only `human-ready`
    returns it, as `agent_questions.owner_questions_hold` rules.

    Each question is copied field by field, never as the recorded mapping.
    Who may answer is the answer route's rule (`answer_refusal`): the
    asking task's submitter, or an admin. A workflow's steps carry its
    submitter, so it is this task's.
    """
    hold = _mapping(facts.get("owner_questions"))
    recorded = _int(hold.get("unreadable"))
    unreadable = recorded if recorded is not None and recorded >= 0 else 1
    questions: list[dict[str, Any]] = []
    raw = hold.get("questions")
    for q in raw if isinstance(raw, list) else ():
        index = q.get("question_index") if isinstance(q, Mapping) else None
        task_id = _str(q.get("task_id")) if isinstance(q, Mapping) else None
        if (task_id is None or isinstance(index, bool) or not isinstance(index, int)
                or index < 0):
            unreadable += 1
            continue
        questions.append({"task_id": task_id, "step_id": _str(q.get("step_id")),
                          "question_index": index, "question": _str(q.get("question"))})
    if not isinstance(raw, list):
        unreadable = max(unreadable, 1)
    by = task.submitted_by
    launched = f"workflow {task.workflow_id}" if task.workflow_id else "it"
    detail: dict[str, Any] = {
        "park_phase": OWNER_QUESTIONS_PHASE,
        "questions": questions,
        "unreadable": unreadable,
        "who_may_answer": {"submitted_by": by, "admin": True,
                           "text": f"{by}, who launched {launched}, or an admin"},
    }
    n = len(questions)
    if n:
        tally = ", ".join(f"{step}: {count}" for step, count in
                          Counter(q["step_id"] or q["task_id"] for q in questions).items())
        summary = (f"Waiting for answers to {n} owner question{'' if n == 1 else 's'} "
                   f"before merging ({tally})")
    else:
        summary = "Waiting for the owner questions the review named before merging"
    if unreadable:
        detail["answer_route"] = f"POST /v1/tasks/{task.id}/human-ready"
        summary += (f"; {unreadable} entr{'y' if unreadable == 1 else 'ies'} the merge cannot "
                    f"read: say it is ready once {'it is' if n <= 1 else 'they are'} answered")
        resumes = ("a member of the workspace calls human-ready once the questions are answered: "
                   "the hold names an entry an answer cannot clear on its own")
    else:
        resumes = "the last named question is answered (it resumes on its own)"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


#: The task's own blocking questions (`agent_worker.lifecycle._park_for_answers`):
#: `park_phase` names it and `question_wait` holds the questions the worker
#: validated, the indices that block, the attempt and when it asked. Spelled
#: again: the worker is not installed in this image.
QUESTION_WAIT_PHASE = "question_wait"
#: How much of the first unanswered question the one line carries.
QUESTION_EXCERPT_CHARS = 80


def _excerpt(text: str | None) -> str | None:
    """`text` on one line, redacted, cut to `QUESTION_EXCERPT_CHARS` with an ellipsis."""
    if not text or not text.strip():
        return None
    words = " ".join(redact(text).text.split())
    if len(words) <= QUESTION_EXCERPT_CHARS:
        return words
    return words[:QUESTION_EXCERPT_CHARS - 1].rstrip() + "\u2026"


def _question_wait(task: Task, facts: dict[str, Any]) -> dict[str, Any]:
    """The task's agent parked on its own blocking questions: an answer, not a sign-in (#2859).

    The worker parks HUMAN_REQUIRED, as a hand-off does, but nobody signs in
    to anything: the answer that leaves no blocking question unanswered
    returns it to READY in the answer's own transaction (`agent_questions`,
    AU-7), and unanswered for `QUESTION_WAIT_MAX_SECONDS` it is
    dead-lettered. So the line counts the blocking questions still
    unanswered, quotes the first (redacted, cut short) and names where it is
    answered: the console page's questions, or `/sc check` in Claude Code,
    which lists them and records the developer's answer through the same
    route. `/sc answer` is a workflow step's await-input form, not this.

    The wait is read the way the answer route reads it (`current_wait`), so
    an answer already stored counts only for the attempt that asked; a
    record that is not a question wait's shape is no wait, never guessed at.
    """
    # Imported here: agent_questions imports codec, which imports this module.
    from .agent_questions import QUESTION_WAIT_MAX_SECONDS, asked_at, blocking_unanswered, current_wait

    wait, adopted = current_wait(task)
    by = task.submitted_by
    launched = f"workflow {task.workflow_id}" if task.workflow_id else "it"
    detail: dict[str, Any] = {
        "park_phase": QUESTION_WAIT_PHASE,
        "questions": [],
        "blocking": [],
        "unanswered": [],
        "who_may_answer": {"submitted_by": by, "admin": True,
                           "text": f"{by}, who launched {launched}, or an admin"},
        "answer_route": f"POST /v1/tasks/{task.id}/answers",
        "questions_route": f"GET /v1/tasks/{task.id}/questions",
        "console_section": "#questions",
        "expires_at": None,
    }
    how = "answer on its console page (Questions) or in Claude Code with /sc check"
    resumes = ("the owner answers the last blocking question (it resumes on its own); "
               f"unanswered for {QUESTION_WAIT_MAX_SECONDS // 3600} hours it is dead-lettered")
    if wait is None:
        summary = f"Waiting for the owner's answers to its questions; {how}"
        return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}
    pending = blocking_unanswered(wait, task.metadata if adopted else None)
    questions = wait["questions"]
    detail["blocking"] = sorted(wait["blocking"])
    detail["unanswered"] = sorted(pending)
    detail["questions"] = [
        {"question_index": i, "question": _excerpt(_str(questions[i].get("question")))}
        for i in sorted(pending)
    ]
    began = asked_at(task, wait)
    detail["expires_at"] = (began + timedelta(seconds=QUESTION_WAIT_MAX_SECONDS)
                            if began is not None else None)
    n = len(pending)
    if n:
        summary = f"Waiting for the owner's answer to {n} question{'' if n == 1 else 's'}"
        first = detail["questions"][0]["question"]
        if first:
            summary += f": \u201c{first}\u201d"
        summary += f"; {how}"
    else:
        summary = ("Every blocking question has the owner's answer and it was not resumed: "
                   "a cancel was asked for, or its last attempt is used")
        resumes = "the scheduler's sweeps end it"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": began}


def _human(task: Task, facts: dict[str, Any]) -> dict[str, Any]:
    if facts.get("park_phase") == OWNER_QUESTIONS_PHASE:
        return _owner_questions(task, facts)
    if facts.get("park_phase") == QUESTION_WAIT_PHASE:
        return _question_wait(task, facts)
    detail = {
        "who_may_answer": _who(task),
        "expires_at": _as_dt(facts.get("expires_at")),
        "answer_route": f"POST /v1/tasks/{task.id}/human-ready",
        "cause": _str(facts.get("cause")),
    }
    summary = "Waiting for a person: sign in on its console page, then say it is ready"
    resumes = "a member of the workspace calls human-ready (the console's Ready button)"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _credential(task: Task, facts: dict[str, Any]) -> dict[str, Any]:
    """NAMES ONLY, from an allow-list. No recorded mapping is copied whole."""
    provider = _str(facts.get("provider")) or task.provider
    slots = [
        {"consumer": _str(s.get("consumer")), "slot": _str(s.get("slot"))}
        for s in (facts.get("workspace_secrets") or []) if isinstance(s, Mapping)
    ]
    servers = [
        {"name": _str(s.get("name")), "code": _str(s.get("code"))}
        for s in (facts.get("mcp_servers") or []) if isinstance(s, Mapping)
    ]
    names: list[str] = []
    if provider:
        names.append(f"{provider} key")
    names += [f"{s['consumer']}/{s['slot']}" if s["slot"] else str(s["consumer"]) for s in slots]
    names += [f"mcp:{s['name']}" for s in servers]
    detail = {
        "provider": provider,
        "secret_names": names,
        "account_pool": _str(facts.get("account_pool")),
        "forge_connection": _str(facts.get("forge_connection")),
        "workspace_secrets": slots,
        "mcp_servers": servers,
    }
    summary = "Credential missing" + (f": {', '.join(names)}" if names else "")
    resumes = "the workspace registers the credential and the scheduler's credential sweep finds it"
    if facts.get("credential") == MCP_PARK_CREDENTIAL:
        # A workflow's per-run MCP credential lapsed while the step waited
        # (registered-mcp-servers.md §4.10, BGD 2026-10-26): its caller
        # re-stores it. What a caller reads, by name and never a value:
        # `credential`, `code`, `server` (the name its re-store keys by) and
        # `expired_at`, as the park's `blocked_by[]` entry holds them.
        server = _str(facts.get("server"))
        detail.update({
            "reason": ParkReason.CREDENTIAL_MISSING.value,
            "credential": MCP_PARK_CREDENTIAL,
            "code": _str(facts.get("code")),
            "server": server,
            "expired_at": _as_dt(facts.get("expired_at")),
            "restore_route": (f"PUT /v1/workflows/{task.workflow_id}/mcp-credentials"
                              if task.workflow_id else None),
        })
        summary = f"MCP credential expired: {server}" if server else "MCP credential expired"
        resumes = ("the workflow's caller re-stores the credential "
                   "(PUT /v1/workflows/{id}/mcp-credentials)")
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": None}


def _budget(facts: dict[str, Any]) -> dict[str, Any]:
    budget = _str(facts.get("budget"))
    summary = "Budget exhausted" + (f": {budget}" if budget else "")
    return {"detail": {"budget": budget}, "summary": summary,
            "resumes_when": "the budget is raised or its period resets", "since": None}


def _pause(facts: dict[str, Any], park: TaskEvent | None) -> dict[str, Any]:
    by = _str(facts.get("paused_by")) or _str(facts.get("by"))
    error = _str(facts.get("error"))
    detail = {"paused_by": by, "paused_at": park.at if park is not None else None, "error": error}
    if by:
        summary = f"Paused by {by}"
    else:
        summary = "Paused" + (f": {error}" if error else "")
    return {"detail": detail, "summary": summary,
            "resumes_when": "what paused it clears and the scheduler's pause sweep returns it",
            "since": None}


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

#: What woke a parked merge step, by the `merge_wait.wake_reason` its marker
#: recorded: `mergewake`'s tick (`_read`'s readings, a fix round's ending),
#: the merge slot (`mergeslot.SLOT_WAKE_REASON`) and the merge train
#: (`train_<state>`). "The checks settled" is said only for a settled
#: reading: on F05 (#2379) a head_moved wake was served as one.
_WAKE_WORDS = {
    "green": "swarm-api's tick read the checks settled",
    "red": "swarm-api's tick read the checks settled",
    "head_moved": "swarm-api's tick read that the head moved",
    "merged": "swarm-api's tick read the pull request merged",
    "closed": "swarm-api's tick read the pull request closed",
    "conflicting": "swarm-api's tick read the pull request conflicting with its base",
    "dequeued": "swarm-api's tick read the pull request out of the merge queue",
    "fix_round_ended": "a fix round ended",
    "fix_round_refused": "a fix round was refused",
    "fix_round_lost": "a fix round was lost",
    "merge_slot_granted": "the repository's merge slot was granted",
}


def _wake_words(wake_reason: Any) -> str:
    """The cause of a wake, in words; a reason not recorded is said so, never guessed."""
    reason = _str(wake_reason)
    if reason is None:
        return "swarm-api's tick marked the step (reason not recorded)"
    if reason in _WAKE_WORDS:
        return _WAKE_WORDS[reason]
    if reason.startswith("train_"):
        return f"the merge train went {reason[len('train_'):]}"
    return f"swarm-api's tick marked the step: {reason}"


def _history_text(ev: TaskEvent, wake_reason: Any = None) -> str:
    """One history row; `wake_reason` is the one recorded for THIS wake, if any."""
    d = ev.detail or {}
    if ev.type is EventType.PARKED:
        text = f"parked: {d.get('reason') or 'reason not recorded'}"
        pending = _names(d.get("pending"))
        if pending is not None:
            text += f", {len(pending)} checks pending"
        if _int(d.get("wakes")) is not None:
            text += f", wake {d['wakes']}"
        if d.get("attempt_refunded") is True:
            text += ", attempt refunded"
        return text
    if ev.type is EventType.READY:
        why = d.get("reason")
        words = {
            "fallback_due": "woke on the fallback re-check",
            "wake_requested": f"woke: {_wake_words(wake_reason)}",
            "retry_due": "retry due",
        }
        text = f"ready: {words.get(why, why or 'promoted')}"
        if d.get("park_reason"):
            text += f" (was {d['park_reason']})"
        return text
    if ev.type is EventType.RUNNING:
        return "running: an attempt started (a re-check, for a merge step)"
    if ev.type is EventType.QUOTA_EXHAUSTED:
        return f"quota exhausted on {d.get('provider') or 'its provider'}"
    return ev.type.value


def _history(task: Task, events: Sequence[TaskEvent] | None, reason: str,
             as_of: datetime) -> list[dict[str, Any]]:
    wait = _mapping(_mapping(task.metadata).get("merge_wait"))
    woke = _as_dt(wait.get("wake_requested_at"))
    # merge_wait holds only the newest mark (a park replaces the record), so
    # its wake_reason names the first wake_requested READY at or after that
    # mark and no other: an older wake's reason went with its record.
    marked = None
    if woke is not None:
        marked = next((ev for ev in sorted(events or (), key=lambda e: e.at)
                       if ev.type is EventType.READY and ev.at >= woke
                       and (ev.detail or {}).get("reason") == "wake_requested"), None)
    rows = [
        {"at": ev.at, "kind": _HISTORY_KINDS[ev.type],
         "text": _history_text(ev, wait.get("wake_reason") if ev is marked else None)}
        for ev in (events or ()) if ev.type in _HISTORY_KINDS
    ]
    if reason == ParkReason.CI_PENDING.value and woke is not None:
        rows.append({"at": woke, "kind": "wake",
                     "text": f"wake requested: {_wake_words(wait.get('wake_reason'))}"})
    stuck = _still_parked(task, reason, as_of)
    if stuck is not None:
        rows.append({"at": task.next_eligible_at, "kind": "eligible",
                     "text": f"eligible, still parked: {stuck}"})
    rows.sort(key=lambda r: r["at"])
    return rows[-HISTORY_MAX:]


def _still_parked(task: Task, reason: str, as_of: datetime) -> str | None:
    """Why a PARKED task past its check time has not resumed; None when it is not past."""
    if task.state is not TaskState.PARKED or reason not in _TIMED:
        return None
    if task.next_eligible_at is None or task.next_eligible_at > as_of:
        return None
    other = _other_blocker(task)
    if other is not None:
        return _blocker_words(other)
    if reason == ParkReason.CREDENTIAL_MISSING.value:
        return "the scheduler's credential sweep has not found the credential"
    return "waiting for the scheduler's next pass"


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------

def build_waiting(
    task: Task,
    *,
    events: Sequence[TaskEvent] | None = None,
    waiting_for: Mapping[str, Any] | None = None,
    related: Mapping[str, Task] | None = None,
    account: Mapping[str, Any] | None = None,
    line: Mapping[str, Any] | None = None,
    as_of: datetime,
) -> dict[str, Any] | None:
    """The `waiting` object for a PARKED, READY or QUEUED task; None otherwise.

    `events` are the task's newest events (None: not read -- the listing);
    `waiting_for` is #362's reading (READY only); `related` maps a task id to
    the tenant's task for a dependency or a child (None: not read); `account`
    is `task_accounts`' reading of the latest attempt; `line` is
    `place_in_line`'s count (READY only; None: not counted). Unread inputs
    serve their facts as null.
    """
    if task.state is TaskState.QUEUED:
        queued = _newest(events, EventType.QUEUED)
        return _finish(
            task, QUEUED, events, as_of,
            {"detail": {}, "summary": "Queued: waiting for the scheduler to read it",
             "resumes_when": "the scheduler's next pass makes it READY or parks it",
             "since": queued.at if queued is not None else task.created_at},
        )
    if task.state is TaskState.READY:
        part = _ready(task, waiting_for, line, as_of)
        ready = _newest(events, EventType.READY)
        part["since"] = ready.at if ready is not None else None
        return _finish(task, READY_CAPACITY, events, as_of, part)
    if task.state is not TaskState.PARKED:
        return None

    reason = task.park_reason.value if task.park_reason else "UNKNOWN"
    facts, park = _park_facts(task, events)
    if reason == ParkReason.CI_PENDING.value:
        part = _ci(task, facts, park, as_of)
    elif reason in _PROVIDER_WORDS:
        part = _provider(task, reason, facts, account, as_of)
    elif reason == ParkReason.SCHEDULED_RETRY.value:
        part = _retry(task, facts, as_of)
    elif reason == ParkReason.DEPENDENCY_INCOMPLETE.value:
        part = _dependencies(task, facts, related)
    elif reason == ParkReason.CHILDREN_INCOMPLETE.value:
        part = _children(task, related)
    elif reason == ParkReason.AWAITING_INPUT.value:
        part = _awaiting_input(task, facts, as_of)
    elif reason == ParkReason.HUMAN_REQUIRED.value:
        part = _human(task, facts)
    elif reason == ParkReason.CREDENTIAL_MISSING.value:
        part = _credential(task, facts)
    elif reason == ParkReason.BUDGET_EXHAUSTED.value:
        part = _budget(facts)
    elif reason == ParkReason.MANUAL_PAUSE.value:
        part = _pause(facts, park)
    else:
        part = {"detail": {}, "summary": f"Parked: {reason}",
                "resumes_when": "the scheduler's sweep for this reason returns it", "since": None}
    if part["since"] is None and park is not None:
        part["since"] = park.at
    stuck = _still_parked(task, reason, as_of)
    if stuck is not None:
        part["summary"] += f"; eligible since {_hhmm(task.next_eligible_at, as_of)}, still parked: {stuck}"
    return _finish(task, reason, events, as_of, part)


def _finish(task: Task, reason: str, events: Sequence[TaskEvent] | None, as_of: datetime,
            part: dict[str, Any]) -> dict[str, Any]:
    timed = task.state is TaskState.PARKED and reason in _TIMED
    return {
        "reason": reason,
        "summary": part["summary"],
        "since": part["since"],
        "next_check_at": task.next_eligible_at if timed else None,
        "resumes_when": part["resumes_when"],
        "detail": part["detail"],
        "history": _history(task, events, reason, as_of),
        # False on a route that read no events: an empty history then means
        # "not read", never "nothing happened".
        "history_read": events is not None,
        "read_at": as_of,
    }


def waiting_row(waiting: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a list row serves: the reason and the one line, nothing else."""
    if waiting is None:
        return None
    return {"reason": waiting["reason"], "summary": waiting["summary"]}


# --------------------------------------------------------------------------
# The route's reads, and the masking
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WaitingReads:
    events: list[TaskEvent] | None
    related: dict[str, Task] | None
    line: dict[str, Any] | None = None


def _line_scope(task: Task, pool: str) -> tuple[list[tuple[str, str, Any]], str] | None:
    """The equality filters naming the READY tasks that need `pool`, and its scope.

    From the TASK's own fields, the way `pool_names_for` names its pools, so a
    tenant pool's filter is always the task's own tenant (invariant 9). None
    for a name that is not one of this task's pools.
    """
    tenant = [("tenant_id", "==", task.tenant_id)]
    scopes: dict[str, tuple[list[tuple[str, str, Any]], str]] = {
        "global": ([], "shared"),
        f"tenant:{task.tenant_id}": (tenant, "tenant"),
        f"resource:{task.resource_class}": ([("resource_class", "==", task.resource_class)], "shared"),
        f"runner:{task.runner_profile}": ([("runner_profile", "==", task.runner_profile)], "shared"),
    }
    profile = RUNNER_PROFILES.get(task.runner_profile)
    if profile is not None:
        backend = resolve_backend(profile).value
        # Every profile that runs on the backend: the pool admits all of them.
        same = sorted(n for n, p in RUNNER_PROFILES.items() if resolve_backend(p).value == backend)
        scopes[f"backend:{backend}"] = ([("runner_profile", "in", same)], "shared")
    if task.provider:
        scopes[f"provider:{task.provider}"] = ([("provider", "==", task.provider)], "shared")
        scopes[f"provider:{task.provider}:tenant:{task.tenant_id}"] = (
            tenant + [("provider", "==", task.provider)], "tenant")
    return scopes.get(pool)


def _no_line(why: str, basis: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"place_in_line": None, "place_in_line_unavailable": why, "place_in_line_basis": basis}


def place_in_line(db: Any, task: Task, waiting_for: Mapping[str, Any] | None) -> dict[str, Any]:
    """1 + the READY tasks ahead of `task` behind its binding pool: ONE count.

    The binding pool is `waiting_for`'s lead. The count is the scheduler's
    READY query, narrowed to the tasks that need that pool and ended before
    the task's own (priority, created_at) position. Never raises: a failed
    count serves null with why, and the basis says what was counted.
    """
    if task.state is not TaskState.READY:
        return _no_line("not READY")
    if waiting_for is None:
        return _no_line("no live pool reading to find the binding pool in")
    lead = _mapping(waiting_for.get("lead"))
    pool = _str(lead.get("pool"))
    if pool is None:
        return _no_line("no pool binds: the scheduler's next pass may admit it")
    if lead.get("state") in ("zero", "below_units"):
        return _no_line(f"{pool}'s limit is below this task's units: no place in line admits it")
    scope = _line_scope(task, pool)
    if scope is None:
        return _no_line(f"{pool} is not one of this task's pools")
    filters, kind = scope
    basis = {"pool": pool, "scope": kind, "order": _LINE_ORDER_WORDS, "excludes": _LINE_EXCLUDES}
    try:
        query = db.collection(TASKS)
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))
        query = query.where(filter=FieldFilter("state", "==", TaskState.READY.value))
        for field, direction in LINE_ORDER:
            query = query.order_by(field, direction=direction)
        query = query.end_before({"priority": task.priority, "created_at": task.created_at})
        ahead = 0
        for row in query.count().get():
            for item in row:
                ahead = int(item.value)
                break
            break
    except gexc.FailedPrecondition as exc:
        # Firestore's answer for an index that is missing or still building.
        log.warning("waiting: place-in-line count refused: %s", type(exc).__name__)
        return _no_line("the count's Firestore index is missing or still building", basis)
    except Exception as exc:  # the task itself was read; only this count failed
        log.warning("waiting: place-in-line count failed: %s", type(exc).__name__)
        return _no_line(f"the count query failed ({type(exc).__name__})", basis)
    return {"place_in_line": ahead + 1, "place_in_line_unavailable": None, "place_in_line_basis": basis}


def read_inputs(store: Any, task: Task, *, waiting_for: Mapping[str, Any] | None = None) -> WaitingReads:
    """What `GET /v1/tasks/{id}` reads for one waiting task, inside its tenant.

    The newest `EVENTS_READ` events, for a dependency or children park ONE
    batched read of the tenant's related tasks (`tasks_by_id` and
    `list_tasks` both drop another tenant's document), and for a READY task
    ONE count of its place in line behind `waiting_for`'s lead pool. A failed
    read serves that input as not read rather than failing the GET it rides on.
    """
    if task.state not in (TaskState.PARKED, TaskState.READY, TaskState.QUEUED):
        return WaitingReads(None, None)
    events: list[TaskEvent] | None
    try:
        page = store.list_events(task.tenant_id, task.id, limit=EVENTS_READ, descending=True)
        events = list(page.items)
    except Exception as exc:  # the task itself was read; only this reading failed
        log.warning("waiting: event read failed: %s", type(exc).__name__)
        events = None
    related: dict[str, Task] | None = None
    reason = task.park_reason if task.state is TaskState.PARKED else None
    try:
        if reason is ParkReason.DEPENDENCY_INCOMPLETE:
            facts, _ = _park_facts(task, events)
            ids = _names(facts.get("waiting_on")) or list(task.depends_on or [])
            related = store.tasks_by_id(task.tenant_id, ids)
        elif reason is ParkReason.CHILDREN_INCOMPLETE:
            page = store.list_tasks(task.tenant_id, parent_task_id=task.id, limit=CHILDREN_READ,
                                    submitted_by=None)
            related = {t.id: t for t in page.items if t.tenant_id == task.tenant_id}
    except Exception as exc:
        log.warning("waiting: related task read failed: %s", type(exc).__name__)
        related = None
    ready = task.state is TaskState.READY
    line = place_in_line(getattr(store, "db", None), task, waiting_for) if ready else None
    return WaitingReads(events, related, line)


def mask_waiting(waiting: Any, text: Any) -> Any:
    """Every string in `waiting` through `text` (the task's masker), shape kept."""
    if isinstance(waiting, str):
        return text(waiting)[0]
    if isinstance(waiting, Mapping):
        return {k: mask_waiting(v, text) for k, v in waiting.items()}
    if isinstance(waiting, list):
        return [mask_waiting(v, text) for v in waiting]
    return waiting
