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


def _arguments(args: dict[str, Any]) -> tuple[str, int, str | None, str | None]:
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID.match(task_id.strip()):
        raise SwarmError(
            "swarm_task_answer needs `task_id`: the task whose agent asked, e.g. "
            "task_152bcd31dd444aba9916. Nothing was sent"
        )
    index = args.get("question_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise SwarmError(
            "swarm_task_answer needs `question_index`: the question's 0-based place in the "
            "task's questions. Nothing was sent"
        )
    picked: dict[str, str | None] = {}
    for name in ("choice", "text"):
        value = args.get(name)
        if value is not None and not isinstance(value, str):
            raise SwarmError(f"swarm_task_answer takes `{name}` as a string. Nothing was sent")
        picked[name] = (value.strip() or None) if isinstance(value, str) else None
    if picked["choice"] is None and picked["text"] is None:
        raise SwarmError(
            "swarm_task_answer needs the person's answer: `choice` (one of the question's "
            "option labels, as they picked it), `text` (their words), or both. Nothing was sent"
        )
    return task_id.strip(), index, picked["choice"], picked["text"]


def record(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Post the person's answer with `via: claude-code` and this session; the API's reply."""
    from . import session

    task_id, index, choice, text = _arguments(args)
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
    payload = {"question_index": index, "choice": choice, "text": text, "via": VIA, "session": sid}
    served = client.request("POST", f"/v1/tasks/{task_id}/answers", payload=payload)
    out: dict[str, Any] = dict(served) if isinstance(served, dict) else {"answered": served}
    out["claude_session"] = sid
    if note is not None:
        out["claude_session_note"] = note
    return out
