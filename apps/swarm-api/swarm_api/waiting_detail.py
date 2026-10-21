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
(invariant 9), and the listing reads nothing for it at all. The masking
(`mask_waiting`) is the task's own masker, applied by the serialiser.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from swarm_common.models import Task, TaskEvent
from swarm_common.states import EventType, ParkReason, TaskState, TERMINAL_STATES

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

_HISTORY_KINDS = {
    EventType.QUEUED: "queued",
    EventType.PARKED: "parked",
    EventType.READY: "ready",
    EventType.RUNNING: "running",
    EventType.RETRYING: "retrying",
    EventType.QUOTA_EXHAUSTED: "quota_exhausted",
}

_PLACE_IN_LINE_WHY = (
    "not computed: it would read every READY task behind the binding pool on each request"
)


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
    detail = {
        "pull_request": (
            {"number": number, "url": pull_request_url(task.repository_url, number)}
            if number is not None else None
        ),
        "head": _str(facts.get("head")) or _str(wait.get("head")),
        "code": _str(facts.get("code")) or _str(wait.get("code")),
        "pending": pending,
        "failed": failed,
        "wakes": wakes,
        "attempts_counted": (not refunded) if isinstance(refunded, bool) else None,
        "last_checked_at": last_checked,
        "wake_requested_at": _as_dt(wait.get("wake_requested_at")),
    }
    where = f"CI on PR #{number}" if number is not None else "CI on its pull request"
    parts: list[str] = []
    if pending is not None:
        shown = pending[:SUMMARY_NAMES]
        more = ", ..." if len(pending) > SUMMARY_NAMES else ""
        names = f" ({', '.join(shown)}{more})" if shown else ""
        parts.append(f"{len(pending)} checks pending{names}")
    if failed:
        parts.append(f"{len(failed)} failed ({', '.join(failed[:SUMMARY_NAMES])})")
    if not parts:
        parts.append("checks not recorded")
    head = f"{where}: {', '.join(parts)}"
    if wakes is not None:
        head += f"; re-checked {wakes} times"
    if task.next_eligible_at is not None:
        head += f"; next check {_hhmm(task.next_eligible_at, as_of)}"
    resumes = "its pull request's checks settle (swarm-api's wake tick marks the step)"
    if task.next_eligible_at is not None:
        resumes += f", or the fallback re-check at {_hhmm(task.next_eligible_at, as_of)}"
    since = _as_dt(wait.get("first_parked_at"))
    return {"detail": detail, "summary": head, "resumes_when": resumes, "since": since}


def _ready(task: Task, waiting_for: Mapping[str, Any] | None, as_of: datetime) -> dict[str, Any]:
    lead = _mapping((waiting_for or {}).get("lead")) or None
    detail = {
        "waiting_for": dict(waiting_for) if waiting_for is not None else None,
        "pool": lead.get("pool") if lead else None,
        "pool_state": lead.get("state") if lead else None,
        "active": lead.get("active") if lead else None,
        "limit": lead.get("limit") if lead else None,
        "units": lead.get("units") if lead else None,
        "place_in_line": None,
        "place_in_line_unavailable": _PLACE_IN_LINE_WHY,
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
    return {"tenant_id": task.tenant_id, "text": f"any member of tenant {task.tenant_id}"}


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
    resumes = "a member of the tenant answers, or the window expires and its default applies"
    return {"detail": detail, "summary": summary, "resumes_when": resumes, "since": opened}


def _human(task: Task, facts: dict[str, Any]) -> dict[str, Any]:
    detail = {
        "who_may_answer": _who(task),
        "expires_at": _as_dt(facts.get("expires_at")),
        "answer_route": f"POST /v1/tasks/{task.id}/human-ready",
        "cause": _str(facts.get("cause")),
    }
    summary = "Waiting for a person: sign in on its console page, then say it is ready"
    resumes = "a member of the tenant calls human-ready (the console's Ready button)"
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
    resumes = "the tenant registers the credential and the scheduler's credential sweep finds it"
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

def _history_text(ev: TaskEvent) -> str:
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
            "wake_requested": "woke: swarm-api's tick read the checks settled",
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
    rows = [
        {"at": ev.at, "kind": _HISTORY_KINDS[ev.type], "text": _history_text(ev)}
        for ev in (events or ()) if ev.type in _HISTORY_KINDS
    ]
    wait = _mapping(_mapping(task.metadata).get("merge_wait"))
    woke = _as_dt(wait.get("wake_requested_at"))
    if reason == ParkReason.CI_PENDING.value and woke is not None:
        rows.append({"at": woke, "kind": "wake",
                     "text": f"wake requested: {wait.get('wake_reason') or 'checks settled'}"})
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
    as_of: datetime,
) -> dict[str, Any] | None:
    """The `waiting` object for a PARKED, READY or QUEUED task; None otherwise.

    `events` are the task's newest events (None: not read -- the listing);
    `waiting_for` is #362's reading (READY only); `related` maps a task id to
    the tenant's task for a dependency or a child (None: not read); `account`
    is `task_accounts`' reading of the latest attempt. Unread inputs serve
    their facts as null.
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
        part = _ready(task, waiting_for, as_of)
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


def read_inputs(store: Any, task: Task) -> WaitingReads:
    """What `GET /v1/tasks/{id}` reads for one waiting task, inside its tenant.

    The newest `EVENTS_READ` events, and for a dependency or children park ONE
    batched read of the tenant's related tasks (`tasks_by_id` and
    `list_tasks` both drop another tenant's document). A failed read serves
    that input as not read rather than failing the GET it rides on.
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
            page = store.list_tasks(task.tenant_id, parent_task_id=task.id, limit=CHILDREN_READ)
            related = {t.id: t for t in page.items if t.tenant_id == task.tenant_id}
    except Exception as exc:
        log.warning("waiting: related task read failed: %s", type(exc).__name__)
        related = None
    return WaitingReads(events, related)


def mask_waiting(waiting: Any, text: Any) -> Any:
    """Every string in `waiting` through `text` (the task's masker), shape kept."""
    if isinstance(waiting, str):
        return text(waiting)[0]
    if isinstance(waiting, Mapping):
        return {k: mask_waiting(v, text) for k, v in waiting.items()}
    if isinstance(waiting, list):
        return [mask_waiting(v, text) for v in waiting]
    return waiting
