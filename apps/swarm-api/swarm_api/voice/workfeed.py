"""Voice it's work feed: what the working card and the roster are told
(docs/design/voice-it.md §3.12.5, §3.12.6, §3.12.8).

THE STEP. Every call a session grant makes writes one `work.step` event on
the session's stream (`grant.write_audit`, after the audit row), so the
working card can say what the agent is doing ("Reading running agents ·
0:42"). The step is built here from the call's (method, route template) and
the path parameters the template names, and nothing else:

    {type: work.step, delegation_id, phrase, method, class, ids}

  * `phrase` is the route's entry in `PHRASES`, a fixed table: the template's
    `{param}` placeholders are left for the console to fill from `ids`, so no
    value is ever spliced into text here. A route missing from the table is
    `FALLBACK` ("Reading SwarmCloud"); tests/unit/voice/test_workfeed.py walks
    the grant's lists and fails on any route without its own phrase.
  * `class` is the call's class on the grant's lists: `read` for
    `ALLOWED_ROUTES`, `write` for `WRITE_ROUTES`. The step is written before
    the route runs, so it says what was asked, never how it ended.
  * `ids` are the path parameters the template names, each one an id
    (`ID_RE`) that the shared redaction rules leave alone; any other value is
    dropped, never shortened or masked in place.
  * `delegation_id` is the oldest delegation the session holds open
    (`grant.resolve` reads it from the session document it already reads),
    or None.

WHAT IS MASKED (§3.12.5). A step carries no query string, no request body, no
response, no transcript and no tool argument the model wrote: none of them is
an input to `step_event`. The planner never writes a step of its own, so the
feed cannot claim work the grant did not do.

THE DELEGATION'S LIFECYCLE. swarm-voice reports each delegation through the
session grant (`grant.record_work`): `started`, then any number of
`progress`, then one of `done`, `failed` or `refused`. A `work.delegation`
event carries the delegation id and the state, nothing else.

THE ROSTER (§3.12.6). The follower writes the rows that changed as one
`roster.update` event through the same write: each row is a kind, an id, a
state and, for a workflow or a run, its progress as two counts. No title:
the page reads a row's title by id through the reads the person already has.

`stream.frame` passes these events, and `paused`, through the whitelists below
and nothing else, so a field a writer added never reaches the browser.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..errors import ValidationFailed
from ..redaction import redact

WORK_STEP = "work.step"
WORK_DELEGATION = "work.delegation"
ROSTER_UPDATE = "roster.update"
PAUSED = "paused"

#: swarm-voice's write route for a delegation's lifecycle and the roster's
#: changed rows (§3.12.5, §3.12.6), handled by `grant.record_work`.
WORK_ROUTE = ("POST", "/v1/voice/sessions/{session_id}/work")

#: A route the table does not name (§3.12.5).
FALLBACK = "Reading SwarmCloud"

#: Every route a grant reaches, as (method, template) -> phrase. A `{param}`
#: is one of the template's own path parameters, filled by the console from
#: the step's `ids`. The work route is absent: its own event is the lifecycle.
PHRASES: Mapping[tuple[str, str], str] = {
    ("GET", "/v1/tenants/me"): "Reading your workspace",
    ("GET", "/v1/tasks"): "Reading running agents",
    ("GET", "/v1/tasks/open-questions"): "Reading agents' open questions",
    ("GET", "/v1/tasks/{task_id}"): "Reading agent {task_id}",
    ("GET", "/v1/tasks/{task_id}/attempts"): "Reading agent {task_id}'s attempts",
    ("GET", "/v1/tasks/{task_id}/events"): "Reading agent {task_id}'s events",
    ("GET", "/v1/tasks/{task_id}/questions"): "Reading agent {task_id}'s questions",
    ("GET", "/v1/workflows"): "Reading workflows",
    ("GET", "/v1/workflows/{workflow_id}"): "Reading workflow {workflow_id}",
    ("GET", "/v1/runs"): "Reading runs",
    ("GET", "/v1/runs/{run_id}"): "Reading run {run_id}",
    ("GET", "/v1/schedules"): "Reading schedules",
    ("GET", "/v1/schedules/{schedule_id}"): "Reading schedule {schedule_id}",
    ("GET", "/v1/schedules/{schedule_id}/firings"): "Reading schedule {schedule_id}'s firings",
    ("GET", "/v1/repositories"): "Reading repositories",
    ("GET", "/v1/repositories/{repo_id}"): "Reading repository {repo_id}",
    ("GET", "/v1/repositories/{repo_id}/languages"): "Reading repository {repo_id}'s languages",
    ("GET", "/v1/approvals"): "Reading approvals",
    ("GET", "/v1/approvals/{item_id}"): "Reading approval {item_id}",
    ("GET", "/v1/failure-findings"): "Reading failure findings",
    ("GET", "/v1/failure-findings/{finding_id}"): "Reading failure finding {finding_id}",
    ("GET", "/v1/runtimes"): "Reading runtimes",
    ("GET", "/v1/voice/cards/schema"): "Reading the card catalogue",
    ("POST", "/v1/voice/sessions/{session_id}/cards"): "Drawing a card",
    ("POST", "/v1/voice/sessions/{session_id}/readbacks/answer"): "Hearing your answer",
    ("POST", "/v1/voice/sessions/{session_id}/visuals"): "Drawing a visual",
    ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"):
        "Pinning a note on visual {visual_id}",
    ("POST", "/v1/tasks/{task_id}/answers"): "Answering agent {task_id}'s question",
    ("POST", "/v1/voice/sessions/{session_id}/trace"): "Tracing the turn",
    ("POST", "/v1/voice/sessions/{session_id}/reopen"): "Reporting the voice's state",
}

#: A step's `class`.
STEP_CLASSES = ("read", "write")
#: A step's `method`: the methods the grant's lists use.
STEP_METHODS = ("GET", "POST", "DELETE")

#: A delegation's states, in lifecycle order; the last three end it.
STARTED, PROGRESS, DONE, FAILED, REFUSED = "started", "progress", "done", "failed", "refused"
DELEGATION_STATES = (STARTED, PROGRESS, DONE, FAILED, REFUSED)
DELEGATION_ENDS = (DONE, FAILED, REFUSED)

#: A roster row's kind and state (§3.12.6).
ROSTER_KINDS = ("delegation", "agent", "workflow", "run")
ROSTER_STATES = ("queued", "running", "waiting", "done", "failed", "cancelled")
#: The most rows one `roster.update` carries: one follower pass's changes.
ROSTER_MAX = 50
#: A progress count's ceiling (a workflow's steps, a run's lanes).
COUNT_MAX = 10_000

#: An id as a path names one: a task, a workflow, a run, a repository, a
#: visual, a delegation. Nothing with a space, a slash, a query or a dot run.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def params_of(template: str) -> tuple[str, ...]:
    """The path parameters a route template names, in order."""
    return tuple(_PARAM_RE.findall(template))


def phrase_for(method: str, route: str) -> str:
    return PHRASES.get((method.upper(), route), FALLBACK)


def clean_id(value: Any) -> str | None:
    """`value` when it is an id the redaction rules leave alone; else None."""
    if not isinstance(value, str) or not ID_RE.match(value):
        return None
    if redact(value, decoded=True).count:
        return None
    return value


def step_event(*, method: str, route: str, path_params: Mapping[str, Any],
               write: bool, delegation_id: str | None) -> dict[str, Any]:
    """The `work.step` event for one granted call: the route's phrase, the
    method, the class and the ids its template names. The query, the body and
    the response are not inputs, so none of them can be in it."""
    ids: dict[str, str] = {}
    for name in params_of(route):
        value = clean_id(path_params.get(name))
        if value is not None:
            ids[name] = value
    return {"type": WORK_STEP, "delegation_id": clean_id(delegation_id),
            "phrase": phrase_for(method, route), "method": method.upper(),
            "class": "write" if write else "read", "ids": ids}


def _refuse(field: str, says: str) -> ValidationFailed:
    return ValidationFailed(f"{field} {says}", detail={"field": field})


def delegation_event(body: Any) -> dict[str, Any]:
    """The `work.delegation` event swarm-voice asked for, or a 422."""
    if not isinstance(body, dict) or set(body) - {"delegation_id", "state"}:
        raise _refuse("delegation", "is {delegation_id, state} and nothing else")
    delegation_id = clean_id(body.get("delegation_id"))
    if delegation_id is None:
        raise _refuse("delegation_id", "is a delegation id")
    if body.get("state") not in DELEGATION_STATES:
        raise _refuse("state", f"is one of {', '.join(DELEGATION_STATES)}")
    return {"type": WORK_DELEGATION, "delegation_id": delegation_id, "state": body["state"]}


def _count(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool)
                             and 0 <= value <= COUNT_MAX)


def roster_row(row: Any) -> dict[str, Any] | None:
    """A roster row as it is stored and framed, or None when it is not one."""
    if not isinstance(row, dict) or set(row) - {"kind", "id", "state", "done", "total"}:
        return None
    row_id = clean_id(row.get("id"))
    if row.get("kind") not in ROSTER_KINDS or row_id is None \
            or row.get("state") not in ROSTER_STATES \
            or not _count(row.get("done")) or not _count(row.get("total")):
        return None
    return {"kind": row["kind"], "id": row_id, "state": row["state"],
            "done": row.get("done"), "total": row.get("total")}


def roster_event(rows: Any) -> dict[str, Any]:
    """The `roster.update` event the follower asked for, or a 422."""
    if not isinstance(rows, list) or not 0 < len(rows) <= ROSTER_MAX:
        raise _refuse("roster", f"is a list of 1 to {ROSTER_MAX} rows")
    out = []
    for index, row in enumerate(rows):
        clean = roster_row(row)
        if clean is None:
            raise _refuse(f"roster[{index}]",
                          "is {kind, id, state, done, total} with a known kind and state")
        out.append(clean)
    return {"type": ROSTER_UPDATE, "rows": out}


# ---- what a frame may carry (stream.frame) ----------------------------------


def step_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """A stored `work.step` cut to its whitelist, or None when it is not one
    `step_event` writes. The phrase must be one of the table's own."""
    phrase = event.get("phrase")
    ids = event.get("ids")
    if phrase not in _KNOWN_PHRASES or event.get("method") not in STEP_METHODS \
            or event.get("class") not in STEP_CLASSES or not isinstance(ids, dict):
        return None
    clean_ids = {str(k): v for k, v in ids.items()
                 if isinstance(k, str) and _PARAM_NAME.match(k) and clean_id(v) is not None}
    if len(clean_ids) != len(ids):
        return None
    return {"delegation_id": clean_id(event.get("delegation_id")), "phrase": phrase,
            "method": event["method"], "class": event["class"], "ids": clean_ids}


def delegation_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    delegation_id = clean_id(event.get("delegation_id"))
    if delegation_id is None or event.get("state") not in DELEGATION_STATES:
        return None
    return {"delegation_id": delegation_id, "state": event["state"]}


def roster_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = event.get("rows")
    if not isinstance(rows, list) or not 0 < len(rows) <= ROSTER_MAX:
        return None
    clean = [roster_row(row) for row in rows]
    if any(row is None for row in clean):
        return None
    return {"rows": clean}


def paused_payload(event: Mapping[str, Any], *, states: tuple[str, ...],
                   causes: tuple[str, ...]) -> dict[str, Any] | None:
    """A `paused` event: the voice state swarm-voice reported and, for a
    wake, its cause, both from the session module's closed lists."""
    cause = event.get("cause")
    if event.get("voice_state") not in states or not (cause is None or cause in causes):
        return None
    return {"voice_state": event["voice_state"], "cause": cause}


_KNOWN_PHRASES = frozenset((*PHRASES.values(), FALLBACK))
_PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
