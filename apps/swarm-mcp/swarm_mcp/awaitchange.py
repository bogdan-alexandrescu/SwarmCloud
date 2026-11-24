"""`sc await`: wait for the FIRST change a session should hear of, print it, exit.

WHY (plan wave 2, "finished work is noticed in a minute"). A session that
attaches no follower rows learnt of a finished task only at its scheduled
`/sc check`, every thirty minutes. Claude Code re-invokes a session when a
background shell command it started exits, so a BOUNDED command that exits on
the first change is a push path that costs no model tokens while it waits:
the session runs it in the background and hears of the change when it exits.

WHAT IT WAITS ON. The held follow call the rows use (`compact.watch_progress`,
`format: "progress"`), ONE call at a time, re-issued with its `since` token
until something the session should act on happens -- so it reads exactly as
often as a row's follow does, and never faster: a call that came back sooner
than `compact.POLL_SECONDS` is followed by a pause that makes up the rest.
The follow ends a hold on any change of state; most are not news (QUEUED to
RUNNING), and this command holds again on those. What IS news (`_attention`):

  * the task finished, in any end state (a refused merge is a FAILED merge
    step; its refusal code and the failing checks it names are said);
  * the task is PARKED waiting for a person (a browser hand-off, an
    owner-question hold, a review-and-fix loop stopped for a person);
  * the task is PARKED waiting for an answer to its own agent's questions;
  * the follow gave the task up (it can no longer be read).

A task already in one of those when the command starts is the baseline, not a
change: it is said once, at the start, only when EVERY item is already there
-- otherwise the command would wait its whole deadline for nothing.

WHAT IT NEVER DOES. It dispatches, cancels and retries nothing: it reads.
It prints no remote log (the follow's `progress` format carries none) and
nothing credential-shaped: a line holds ids, a title, a state, a pull request
link, an error CODE (an identifier, never free text) and the names of failed
checks. An id it cannot read is one line saying so, never a traceback.

ITS BOUNDS. A deadline always: `DEFAULT_TIMEOUT_SECONDS`, at most
`MAX_TIMEOUT_SECONDS`; when it passes with nothing changed the command exits
`EXIT_DEADLINE`, its own code. And a bound on tries: at most `MAX_HOLDS`
follow calls, whatever the clock says.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import compact, session, workflows
from .client import SwarmClient, SwarmError, task_id_of
from .progress import clip
from .render import task_label

#: The command exited because something changed (the line(s) say what).
EXIT_CHANGED = 0
#: Nothing could be waited on: no id given was readable, or `--mine` found
#: no running work of this session's in this project.
EXIT_NOTHING = 1
#: The deadline passed (or the tries ran out) with nothing changed. Distinct
#: from every other `sc` exit code (0, 1, 2 usage, 3 trouble), so a session
#: reads it as "nothing yet; run it again" without parsing the line.
EXIT_DEADLINE = 4

#: Thirty minutes, the scheduled `/sc check`'s own default interval: a wait
#: that ends with nothing changed costs one wake, no more often than the check.
DEFAULT_TIMEOUT_SECONDS = 1800
#: Never more than an hour (the plan's hard rule).
MAX_TIMEOUT_SECONDS = 3600

#: The bound on follow calls in one run. A hold lasts at least
#: `compact.POLL_SECONDS` from start to start (see `_pace`), so an hour holds
#: fewer than this; the bound is there for a clock that does not move.
MAX_HOLDS = int(MAX_TIMEOUT_SECONDS // compact.POLL_SECONDS)

#: The park a task's own agent waits in for an answer (contract request 92).
_AWAITING_INPUT = "AWAITING_INPUT"

#: An error code is an identifier; anything else in `last_error` is not printed.
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

#: The merge refusal that names failed checks (`agent_worker.merge`): its
#: message is `at <head>: <check>, <check>[; ...]`.
_CHECKS_FAILED = "checks_failed"
_AT_HEAD = re.compile(r"^at [0-9a-fA-F]{7,40}: ")


def timeout_seconds(value: Any) -> int:
    """The deadline in seconds: a positive whole number, at most an hour.

    Refused, not clamped, past the hour: a caller who asked for two hours
    must not believe they got them."""
    try:
        seconds = int(str(value).strip())
    except ValueError:
        raise SwarmError(f"--timeout takes whole seconds, 1 to {MAX_TIMEOUT_SECONDS}; got {value!r}") from None
    if not 1 <= seconds <= MAX_TIMEOUT_SECONDS:
        raise SwarmError(f"--timeout takes whole seconds, 1 to {MAX_TIMEOUT_SECONDS}; got {seconds}")
    return seconds


@dataclass
class Targets:
    """What one run waits on: task ids with a title each, and the lines for
    ids that could not be read (said once, at the start)."""

    task_ids: list[str] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)


def _why(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    head = f"HTTP {status}" if isinstance(status, int) else "read failed"
    return f"{head} -- {clip(str(exc), 160)}"


def resolve(client: SwarmClient, given: list[str]) -> Targets:
    """Task ids for the ids given: a workflow id (`wf_...`) stands for every
    step of it that has a task. A workflow that cannot be read is one line."""
    out = Targets()
    for item in given:
        ident = str(item).strip()
        if not ident or ident in out.task_ids:
            continue
        if not ident.startswith("wf_"):
            out.task_ids.append(ident)
            continue
        try:
            envelope = workflows.fetch(client, ident)
        except SwarmError as exc:
            out.unreadable.append(f"{ident} · cannot be read: {_why(exc)} · not waited on")
            continue
        workflow = envelope.get("workflow") if isinstance(envelope.get("workflow"), dict) else {}
        title = workflows.stored_names(envelope).get("title") or ident
        steps = [s for s in workflow.get("steps") or [] if isinstance(s, dict) and s.get("task_id")]
        if not steps:
            out.unreadable.append(f"{ident} · no step has a task yet · not waited on")
            continue
        for step in steps:
            task_id = str(step["task_id"])
            if task_id not in out.task_ids:
                out.task_ids.append(task_id)
                out.titles[task_id] = f"{title} / {step.get('step_id') or task_id}"
    return out


def mine(client: SwarmClient) -> tuple[list[str], str | None]:
    """`(ids, why_none)`: this session's running work in this project --
    its unfinished workflows and single tasks, as `/sc check` scopes them
    (`check.server_scope`, `session.ownership`)."""
    from . import check
    from .sc import walk_running_workflows

    sid = session.current()
    if not sid:
        return [], session.UNKNOWN_NOTE
    scope = check.resolve_scope(None)
    record = session.project_record()
    # The API applied the repository filter only while the record is empty
    # (`check.server_scope`), so only then does the repository decide.
    by_repository = not (record.ids or record.submitted)
    walk = walk_running_workflows(client, **check.server_scope(scope["key"], sid, record))
    ids: list[str] = []
    for row in walk["workflows"]:
        wid = row.get("workflow_id")
        if wid and session.ownership(session.owner_of(row), sid, record=record, ident=wid,
                                     name=row.get("name"), in_repository=by_repository) == "own":
            ids.append(str(wid))
    tasks, _, _ = check._single_tasks(client, sid)
    for task in tasks:
        tid = task_id_of(task)
        if tid and session.ownership(session.owner_of(task), sid, record=record, ident=tid,
                                     in_repository=by_repository) == "own":
            ids.append(str(tid))
    if not ids:
        return [], "nothing of this session's is running in this project"
    return ids, None


def _attention(row: dict[str, Any]) -> str | None:
    """What about a follow row the session should hear of, as one key; None
    when nothing (it waits, or it runs)."""
    if row.get("abandoned"):
        return "unreadable"
    if row.get("terminal"):
        return f"finished:{row.get('state')}"
    wait = row.get("waiting_on_person")
    if isinstance(wait, dict):
        return f"person:{wait.get('kind')}:{wait.get('park_phase')}"
    if row.get("state") == "PARKED" and row.get("park_reason") == _AWAITING_INPUT:
        return "questions"
    return None


def _failing(outcome: dict[str, Any] | None, last_error: Any) -> tuple[str | None, str | None]:
    """`(code, failing checks)` from a finished task, each None when not known.

    The code is the refusal's (a merge) or the head of `last_error`, and only
    when it is an identifier: free text is never printed."""
    found = outcome or {}
    error = str(found.get("last_error") or last_error or "")
    head, _, rest = error.partition(":")
    code = head.strip() if _CODE.match(head.strip() or "-") else None
    if code is None and found.get("end_cause") and _CODE.match(str(found["end_cause"])):
        code = str(found["end_cause"])
    checks = None
    if code == _CHECKS_FAILED and rest.strip():
        named = _AT_HEAD.sub("", rest.strip(), count=1)
        checks = clip(named.split(";", 1)[0].split(", after", 1)[0].strip(), 160) or None
    return code, checks


def line(row: dict[str, Any], title: str | None) -> str:
    """ONE compact line for a changed task: id, title, state, pull request,
    and the error code and failing checks when known -- from the per-task
    answer the follow already returns (`compact.step_result`,
    `compact.person_wait_result`)."""
    task_id = str(row.get("task_id"))
    name = title or task_label(task_id, row.get("step_id"))
    if row.get("abandoned"):
        return f"{task_id} · {name} · UNREADABLE · {clip(str(row.get('abandoned_because') or ''), 200)}"
    wait = row.get("waiting_on_person")
    if isinstance(wait, dict):
        parts = [task_id, name, "PARKED waiting on a person"]
        if wait.get("kind") == "fix_loop":
            parts.append(f"fix loop {wait.get('fix_loop') or ''}".strip())
            parts.append(f"add a round: {wait.get('add_round')}")
        elif wait.get("kind") == "owner_questions":
            n = len(wait.get("questions") or [])
            parts.append(f"{n} owner question{'' if n == 1 else 's'}")
        else:
            parts.append("hand-off: sign in on its console page, then say it is ready")
        if wait.get("console"):
            parts.append(f"console: {wait['console']}")
        return " · ".join(parts)
    if not row.get("terminal"):
        parts = [task_id, name, f"PARKED {row.get('park_reason') or ''}".strip()]
        if row.get("park_reason") == _AWAITING_INPUT:
            parts.append("its agent asked questions: answer them on its console page or with /sc check")
        if row.get("console"):
            parts.append(f"console: {row['console']}")
        return " · ".join(parts)
    result = compact.step_result(row)
    parts = [task_id, name, result["state"], f"PR {result['pr_url'] or 'none'}"]
    code, checks = _failing(row.get("outcome"), row.get("last_error"))
    if result["state"] != "SUCCEEDED" and code:
        parts.append(f"refused {code}" if code in check_codes() else f"error {code}")
    if checks:
        parts.append(f"failing check: {checks}")
    asked = len(result.get("questions") or [])
    if asked:
        parts.append(f"{asked} owner question{'' if asked == 1 else 's'}")
    if result.get("console"):
        parts.append(f"console: {result['console']}")
    return " · ".join(parts)


def check_codes() -> tuple[str, ...]:
    """The merge refusals `/sc check` flags (`check.MERGE_CODES`)."""
    from .check import MERGE_CODES

    return MERGE_CODES


def _pace(held: float, sleep: Callable[[float], None], remaining: float) -> None:
    """Never read faster than the follow does: a call that came back within
    `compact.POLL_SECONDS` is followed by the rest of that interval."""
    gap = compact.POLL_SECONDS - max(0.0, held)
    if gap > 0 and remaining > 0:
        sleep(min(gap, remaining))


def wait(
    client: SwarmClient,
    targets: Targets,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    hold: Callable[..., dict[str, Any]] = compact.watch_progress,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    max_holds: int = MAX_HOLDS,
) -> tuple[int, list[str]]:
    """`(exit code, lines)` once the first item changes, or the deadline passes.

    `hold` is the follow call (`compact.watch_progress`); it is called with
    one `since` token at a time and never twice at once."""
    lines = list(targets.unreadable)
    if not targets.task_ids:
        return EXIT_NOTHING, lines or ["nothing to wait on"]
    timeout = min(float(timeout), float(MAX_TIMEOUT_SECONDS))
    deadline = clock() + timeout
    following = list(targets.task_ids)
    seen: dict[str, str | None] = {}
    since: Any = None
    holds = 0
    while holds < max_holds:
        remaining = deadline - clock()
        if since is not None and remaining <= 0:
            break
        began = clock()
        reply = hold(client, following, since=since, wait_seconds=max(0.0, remaining),
                     max_wait=compact.MAX_WAIT_SECONDS, rows=True, sleep=sleep, clock=clock, now=now)
        holds += 1
        rows = {str(r.get("task_id")): r for r in reply.get("tasks") or [] if isinstance(r, dict)}
        if since is None:
            # The baseline. A task already finished or waiting on a person is
            # not a change; an id that cannot be read is said now, once.
            for task_id in following:
                row = rows.get(task_id) or {}
                seen[task_id] = _attention(row)
                if row.get("read") == "failed" and row.get("abandoned"):
                    lines.append(f"{task_id} · cannot be read · {clip(str(row.get('abandoned_because') or ''), 200)}")
            live = [t for t in following if seen[t] is None]
            if not live:
                said = [line(rows[t], targets.titles.get(t)) for t in following
                        if t in rows and not (rows[t].get("read") == "failed" and rows[t].get("abandoned"))]
                if said:
                    return EXIT_CHANGED, lines + said
                return EXIT_NOTHING, lines
            if len(live) != len(following):
                # A settled task is not followed again: its outcome would be
                # re-read on every call for nothing. One immediate call (no
                # `since` answers at once) is the baseline for the rest.
                following = live
                _pace(clock() - began, sleep, deadline - clock())
                began = clock()
                rebased = hold(client, following, since=None, wait_seconds=0,
                               max_wait=compact.MAX_WAIT_SECONDS, rows=True, sleep=sleep, clock=clock, now=now)
                holds += 1
                since = rebased.get("since")
            else:
                since = reply.get("since")
            _pace(clock() - began, sleep, deadline - clock())
            continue
        changed = []
        for task_id in following:
            row = rows.get(task_id)
            if row is None:
                continue
            now_key = _attention(row)
            if now_key is not None and now_key != seen.get(task_id):
                changed.append(line(row, targets.titles.get(task_id)))
            seen[task_id] = now_key
        if changed:
            return EXIT_CHANGED, lines + changed
        since = reply.get("since") or since
        _pace(clock() - began, sleep, deadline - clock())
    minutes = timeout / 60
    tried = "" if holds < max_holds else f" (stopped after {holds} follow calls)"
    lines.append(
        f"nothing changed in {minutes:g} minute{'' if minutes == 1 else 's'}{tried} for "
        f"{len(targets.task_ids)} task{'' if len(targets.task_ids) == 1 else 's'}; run it again to keep waiting"
    )
    return EXIT_DEADLINE, lines



#: The reply field that names the command (`await_command`): dispatch, the
#: workflow submissions and `/sc check` each carry it, once.
FIELD = "await_in_background"


def await_command(ids: list[str] | None = None, *, mine: bool = False) -> str | None:
    """The exact `sc await` for the work a reply just started (its ids, no
    launch argument), or `--mine` for the check; None when there is none."""
    from .invocation import terminal_command

    if mine:
        return terminal_command("sc await --mine")
    named = [str(i) for i in ids or [] if i]
    return terminal_command("sc await " + " ".join(named)) if named else None
