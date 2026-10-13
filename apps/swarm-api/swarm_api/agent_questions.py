"""`GET /v1/tasks/{id}/questions`: what a task's agent asked its owner.

WHY (owner report, 2026-10-13, part of #1206). The bell said "Your agent asked
1 question" and linked to the task, and nothing on the task said what the
question was: the only reader of an agent's `questions.json` was the MCP
bridge's `swarm_result` (`swarm_mcp.progress.owner_questions`). This serves the
questions to the console, so the task's Details can show them where the
notification sends the reader.

THE WORKER IS THE VALIDATOR, AND THE ONLY ONE. `agent_worker.questions.validate`
checks the file's shape when the attempt ends and counts it in
`result_summary.questions` only when it was valid AND uploaded; a file it
rejected is counted 0 with `questions_rejected` beside it. So a count above
zero IS the validation verdict, and this module does not re-implement the
checks (the worker is not installed in the API image). It reads the uploaded
file back, takes exactly the four keys of each question and the two of each
option -- as `owner_questions` does -- and refuses to serve a list whose length
is not the count the worker recorded, because then it is not the file the
worker validated.

THROUGH THE ARTIFACT READ, so the tenant boundary and the redaction are the
ones `/artifacts/content` applies, not a copy: the name is matched against the
task's own manifest, another tenant's task is the same 404 a missing one is,
and a JSON artifact is masked by its tokens so it is still JSON when served
(#327). The response carries that read's `redacted` and `redaction_count`.

DATA, NEVER A CHANNEL. Nothing here answers, dispatches or decides on a
question; there is no in-console answer (the owner is deciding that
separately). An answer goes back through whoever launched the agent.

`[]` IS NEVER AMBIGUOUS. A task that asked none answers `status: "none"` with
no object read; a counted file that could not be read answers
`status: "unavailable"` with `unavailable_because`, so "asked none" and "asked
some we could not read" are different answers -- neither is a 404.
"""

from __future__ import annotations

import json
from typing import Any

from .agent_output import AgentOutputService
from .errors import NotFound
from .store import Store

#: The artifact's name and the `result_summary` key the worker counts it under
#: (`agent_worker.questions.NAME` / `SUMMARY_KEY`). Spelled again: the worker
#: is not installed in the API image.
QUESTIONS_NAME = "questions.json"
QUESTIONS_KEY = "questions"

#: The worker takes no file larger than this (`agent_worker.questions.MAX_BYTES`);
#: one byte more says whether the stored object is larger than it took.
QUESTIONS_MAX_BYTES = 64 * 1024


def question_count(summary: dict[str, Any] | None) -> int:
    """How many questions the worker validated in this task's file, else 0."""
    count = (summary or {}).get(QUESTIONS_KEY)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _question(entry: dict[str, Any]) -> dict[str, Any]:
    """One question with exactly its four keys, and each option with its two."""
    options = [
        {"label": _text(option.get("label")), "description": _text(option.get("description"))}
        for option in entry.get("options") or []
        if isinstance(option, dict)
    ]
    return {
        "question": _text(entry.get("question")),
        "options": options,
        "recommended": _text(entry.get("recommended")),
        "context": _text(entry.get("context")),
    }


def read_questions(
    store: Store,
    service: AgentOutputService,
    tenant_id: str,
    task_id: str,
    *,
    submitted_by: str | None,
) -> dict[str, Any]:
    """The task's questions for its owner, as the worker validated them, redacted."""
    # The task read is the tenant boundary: another tenant's task 404s here,
    # with the message a missing one gets, before anything else is looked at.
    task = store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    count = question_count(task.result_summary)
    out: dict[str, Any] = {
        "task_id": task.id,
        "count": count,
        "questions": [],
        "redacted": False,
        "redaction_count": 0,
    }
    if count == 0:
        return {**out, "status": "none"}

    why: str | None = None
    parsed: Any = None
    try:
        window = service.read_artifact(
            tenant_id,
            task_id,
            submitted_by=submitted_by,
            name=QUESTIONS_NAME,
            limit_bytes=QUESTIONS_MAX_BYTES + 1,
        )
    except NotFound:
        # The task was read above, so this is its manifest not listing the
        # file: counted but not readable, which is not "none".
        window, why = {}, "the task's manifest does not list it"
    out["attempt_id"] = window.get("attempt_id")
    if why is not None:
        pass
    elif window.get("status") != "ok" or not isinstance(window.get("content"), str):
        why = f"the artifact read says {window.get('status')}"
    elif window.get("truncated"):
        why = f"the file is larger than the {QUESTIONS_MAX_BYTES} bytes the worker takes"
    else:
        try:
            parsed = json.loads(window["content"])
        except ValueError:
            why = "the file served is not JSON"
    if why is None and not (isinstance(parsed, list) and all(isinstance(q, dict) for q in parsed)):
        why = "the file served is not a list of questions"
    if why is None and len(parsed) != count:
        why = f"the file served holds {len(parsed)}, not the {count} the worker validated"
    if why is not None:
        return {
            **out,
            "status": "unavailable",
            "unavailable_because": (
                f"the worker counted {count} question(s) in {QUESTIONS_NAME}, and {why}; "
                "it is in the task's artifacts"
            ),
        }
    return {
        **out,
        "status": "ok",
        "questions": [_question(entry) for entry in parsed],
        "redacted": bool(window.get("redacted")),
        "redaction_count": int(window.get("redaction_count") or 0),
    }
