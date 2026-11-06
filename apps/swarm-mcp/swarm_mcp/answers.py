"""`swarm_task_answer`: the person's answer, given in Claude Code, recorded on the task.

WHY (owner decision 2026-10-19, "Plugin records it, marked as from Claude
Code"; lane ANSWER-SYNC, part of #1644). A question a task's agent asked
(its `questions.json`, served by `swarm_result` as `owner_questions`) and
answered by the person in the orchestrating Claude Code session stayed
unanswered in the console: the bridge had no tool for
`POST /v1/tasks/{id}/answers`, and its one answer tool, `swarm_workflow_input`,
answers an await-input workflow step, not a task's questions.
task_152bcd31dd444aba9916 was answered in session ce05454f at 21:08:55Z and
showed unanswered; six more were backfilled by hand.

WHERE IT WAS ANSWERED IS THE BRIDGE'S TO SAY. The tool takes no `via` and no
`session` (an argument this bridge does not know is refused): it always sends
`via: "claude-code"` and the session id `swarm_mcp.session.current()` reads --
the one every submission is tagged with. The API stores both with the answer
and serves them, so the console can say "answered in Claude Code (session
...)". Neither is a permission: who may answer (the submitter or an admin,
a viewer never) is the API's, from the caller's own identity.

A SESSION THE API WOULD NOT TAKE IS NOT SENT. `session.current()` accepts any
id-shaped value (`.` and `:` included, so a future format is not refused);
the API stores only a UUID or 8-64 of `[A-Za-z0-9_-]`. The answer is the
person's whatever the id looks like, so it is recorded with `session: null`
and the reply says why, rather than refused for a label.

`reply: "short"` (6 November 2026, part of #2688) sends `?reply=short`, and
the API answers only `{status, task_id, question_index, recorded_at, by}`:
the whole task's questions are about 4,000 tokens a reply, and closing about
404 questions on finished tasks one by one cost about 1.6 million. The
default, `full`, sends exactly what it sent before.

`swarm_task_answers` records up to `MAX_BULK` answers in one call: each
entry is sent through the SAME route with `reply=short`, as the same caller,
so each is checked as a single answer is (the submitter or an admin, a
viewer never; no new route and no override). A refusal is that entry's line
and the next entry is sent; a list over `MAX_BULK` is refused before
anything is sent.

THE API'S ANSWER, AS IT CAME. On success, the questions as `GET /questions`
now serves them; on a refusal (a 403 for someone who may not answer, a 422
for a choice that is not one of the options), the API's `SwarmError`
propagates and the host is shown its message word for word
(`server._tool_error_text`).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .client import SwarmError

#: What every answer this tool records says about where it was given.
VIA = "claude-code"

#: A task id is a path segment here; anything that could leave it (`/`,
#: `..`) is refused before a request is built.
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: The session shapes the API stores (`swarm_api.agent_questions.valid_session`).
_API_SESSION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _api_session(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return bool(_API_SESSION.match(value))


#: The most entries one `swarm_task_answers` call takes.
MAX_BULK = 200

#: What one bulk entry may carry: an answer, never where it was given.
_ENTRY_KEYS = frozenset({"task_id", "question_index", "choice", "text"})

#: The reply shapes the API serves (`?reply=`).
REPLIES = ("full", "short")


def _arguments(args: dict[str, Any], tool: str = "swarm_task_answer") -> tuple[str, int, str | None, str | None]:
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID.match(task_id.strip()):
        raise SwarmError(
            f"{tool} needs `task_id`: the task whose agent asked, e.g. "
            "task_152bcd31dd444aba9916. Nothing was sent"
        )
    index = args.get("question_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise SwarmError(
            f"{tool} needs `question_index`: the question's 0-based place in the "
            "task's questions. Nothing was sent"
        )
    picked: dict[str, str | None] = {}
    for name in ("choice", "text"):
        value = args.get(name)
        if value is not None and not isinstance(value, str):
            raise SwarmError(f"{tool} takes `{name}` as a string. Nothing was sent")
        picked[name] = (value.strip() or None) if isinstance(value, str) else None
    if picked["choice"] is None and picked["text"] is None:
        raise SwarmError(
            f"{tool} needs the person's answer: `choice` (one of the question's "
            "option labels, as they picked it), `text` (their words), or both. Nothing was sent"
        )
    return task_id.strip(), index, picked["choice"], picked["text"]


def _session() -> tuple[str | None, str | None]:
    """This session's id as the API stores it (or None), and why none is sent."""
    from . import session

    sid = session.current()
    note: str | None = None
    if sid is None:
        note = (
            f"this bridge does not know which Claude Code session it serves "
            f"({session.SESSION_ENV} is not set), so the answer was recorded without one"
        )
    elif not _api_session(sid):
        note = (
            f"this session's id, {sid!r}, is not one the API stores (a UUID, or 8-64 of "
            "A-Z, a-z, 0-9, _ and -), so the answer was recorded without it"
        )
        sid = None
    return sid, note


def _post(client: Any, task_id: str, index: int, choice: str | None, text: str | None,
          sid: str | None, *, short: bool) -> Any:
    payload = {"question_index": index, "choice": choice, "text": text, "via": VIA, "session": sid}
    path = f"/v1/tasks/{task_id}/answers" + ("?reply=short" if short else "")
    return client.request("POST", path, payload=payload)


def _reply(args: dict[str, Any]) -> str:
    reply = args.get("reply", "full")
    if reply not in REPLIES:
        raise SwarmError("swarm_task_answer takes `reply` as \"short\" or \"full\". Nothing was sent")
    return reply


def record(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Post the person's answer with `via: claude-code` and this session; the API's reply."""
    task_id, index, choice, text = _arguments(args)
    short = _reply(args) == "short"
    sid, note = _session()
    served = _post(client, task_id, index, choice, text, sid, short=short)
    out: dict[str, Any] = dict(served) if isinstance(served, dict) else {"answered": served}
    out["claude_session"] = sid
    if note is not None:
        out["claude_session_note"] = note
    return out


def _refusal_code(exc: SwarmError) -> str:
    if exc.code:
        return exc.code
    return f"http_{exc.status}" if exc.status is not None else "error"


def record_many(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Post each entry through the single-answer route with `reply=short`; counts and one line each.

    A list over `MAX_BULK` is refused before anything is sent. An entry
    that is not an answer is its own line (`invalid_entry`, nothing sent
    for it); a refusal from the API is its own line with the API's code;
    neither stops the entries after it.
    """
    entries = args.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SwarmError(
            "swarm_task_answers needs `entries`: a list of {task_id, question_index, choice?, "
            "text?}. Nothing was sent"
        )
    if len(entries) > MAX_BULK:
        raise SwarmError(
            f"swarm_task_answers takes at most {MAX_BULK} entries; this call has {len(entries)}. "
            "Nothing was sent"
        )
    sid, note = _session()
    lines: list[dict[str, Any]] = []
    recorded = 0
    for entry in entries:
        line: dict[str, Any] = {
            "task_id": entry.get("task_id") if isinstance(entry, dict) else None,
            "question_index": entry.get("question_index") if isinstance(entry, dict) else None,
        }
        try:
            if not isinstance(entry, dict) or set(entry) - _ENTRY_KEYS:
                raise SwarmError("an entry is {task_id, question_index, choice?, text?}, nothing else")
            task_id, index, choice, text = _arguments(entry, "swarm_task_answers")
        except SwarmError as exc:
            lines.append({**line, "refused": "invalid_entry", "why": str(exc)})
            continue
        try:
            served = _post(client, task_id, index, choice, text, sid, short=True)
        except SwarmError as exc:
            lines.append({**line, "refused": _refusal_code(exc)})
            continue
        status = served.get("status") if isinstance(served, dict) else None
        lines.append({**line, "status": status or "recorded"})
        recorded += 1
    out: dict[str, Any] = {
        "entries": len(entries),
        "recorded": recorded,
        "refused": len(entries) - recorded,
        "results": lines,
        "claude_session": sid,
    }
    if note is not None:
        out["claude_session_note"] = note
    return out
