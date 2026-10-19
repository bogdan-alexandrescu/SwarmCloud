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

DATA, NEVER A CHANNEL. Nothing here dispatches or decides on a question. The
owner's ANSWER (below) is stored on the task for whoever launched the agent to
read; nothing here acts on it.

`[]` IS NEVER AMBIGUOUS. A task that asked none answers `status: "none"` with
no object read; a counted file that could not be read answers
`status: "unavailable"` with `unavailable_because`, so "asked none" and "asked
some we could not read" are different answers -- neither is a 404.

`POST /v1/tasks/{id}/answers`: THE OWNER ANSWERS ON THE TASK (owner decision
2026-10-13 ~05:25 PDT, "Yes, pick an option on the task"; lane TASK-ANSWERS).
One of the question's option labels, free text ("Other…"), or both. Stored on
the task document as `metadata.owner_answers` -- a reserved platform key, as
`start_problem` is, because the frozen `Task` has no field for it and an
additive metadata key needs no contract change -- one entry per question:
`{question_index, question, choice, text, by, at, history}`. Re-answering
REPLACES the entry and moves the replaced answer, with its own `by` and `at`,
into `history`, so who changed what is on the task for as long as the task
is; the write is one Firestore transaction, so two people answering at once
cannot drop either one's history. `history` is capped at
`MAX_ANSWER_CHANGES`, past which a change is refused rather than an earlier
answer forgotten.

WHO MAY ANSWER: the task's submitter, or an admin. "Admin" is the platform's
`is_admin`. Another member of the tenant reads the questions and is told why
they may not answer (`may_answer_because`); another tenant's task is the 404
a missing one is, from the task read, before anything else.

AND ONLY A `member` OR ABOVE (docs/design/workspace-roles.md §4.8): the route
asks the role first (`routes.tasks.require_member`, `role_member_required`
through its switch), so a submitter a grant narrowed to `viewer` can no
longer steer their own running task's agent. The submitter rule above is
narrower and stays on top of the role. With the switch on, the read says so
too (`role_refusal`), so the console does not offer a form the answer route
would refuse.

VALIDATED AGAINST WHAT IS SERVED: the index must name one of the questions the
worker counted, and a choice must be one of THAT question's option labels as
the read above serves them -- so an answer can only pick something the person
was shown. Free text is at most `MAX_ANSWER_CHARS` and is REDACTED BEFORE IT IS
STORED, as a hand-off's reason is; `task_input.COLLECTED_METADATA_KEYS` masks it
again wherever the task is served.

The notification (`notifications.agent_questions`) stops listing a task once
every question it asked has an answer (`unanswered`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from google.cloud import firestore
from pydantic import BaseModel, ConfigDict

from swarm_common.admission import _snapshot

from . import refusals, roles
from .agent_output import AgentOutputService
from .codec import owner_answer_to_api, owner_answers_by_index
from .errors import Conflict, Forbidden, NotFound, RoleMemberRequired, ValidationFailed
from .redaction import redact
from .store import TASKS, Store
from .task_input import masking_for
from .validation import OWNER_ANSWERS_METADATA_KEY

log = logging.getLogger(__name__)

#: The longest free-text answer, in characters (the lane brief: <= 2000).
MAX_ANSWER_CHARS = 2000

#: How many earlier answers one question keeps in `history`. A change past it
#: is refused (409), never made by forgetting the oldest: the history is the
#: audit. 20 is far more than a person changes their mind, and bounds the
#: task document at 20 x 2,000 characters per question.
MAX_ANSWER_CHANGES = 20

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


def unanswered(count: int, metadata: dict[str, Any] | None) -> int:
    """How many of the `count` questions have no stored answer."""
    answered = owner_answers_by_index(metadata)
    return sum(1 for index in range(count) if index not in answered)


def answer_refusal(submitted_by: str | None, email: str, *, is_admin: bool) -> str | None:
    """Why this caller may not answer this task's questions, or None when they may."""
    if is_admin:
        return None
    if submitted_by and email and submitted_by.strip().lower() == email.strip().lower():
        return None
    who = submitted_by or "the person who launched it"
    return f"only {who}, who launched this task, or an admin may answer its questions"


def role_refusal(role_store: Any, auth: Any) -> str | None:
    """Why the caller's ROLE forbids answering, for the read's
    `may_answer_because`, or None. Only while `role_member_required` is on:
    off, the answer route lets a `viewer` through, and the read must not say
    otherwise. A role that cannot be read says nothing here; the answer route
    decides then (503 with the switch on)."""
    if role_store is None or not refusals.enforced(RoleMemberRequired.code):
        return None
    me = role_store.try_resolve(auth)
    if me is None or roles.at_least(me.role, roles.MEMBER):
        return None
    return f"your role in {auth.tenant_id} is {me.role}; members answer here"


def read_questions(
    store: Store,
    service: AgentOutputService,
    tenant_id: str,
    task_id: str,
    *,
    submitted_by: str | None,
    viewer: str | None = None,
    viewer_is_admin: bool = False,
    viewer_role_refusal: str | None = None,
) -> dict[str, Any]:
    """The task's questions for its owner, as the worker validated them, redacted.

    Each served question carries `answer`: the owner's stored answer, or None.
    `may_answer` says whether `viewer` may answer, and `may_answer_because`
    why not: `viewer_role_refusal` (`role_refusal`) when their role forbids
    it, else the submitter rule's sentence.
    """
    # The task read is the tenant boundary: another tenant's task 404s here,
    # with the message a missing one gets, before anything else is looked at.
    task = store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    count = question_count(task.result_summary)
    refusal = viewer_role_refusal or answer_refusal(
        task.submitted_by, viewer or "", is_admin=viewer_is_admin)
    out: dict[str, Any] = {
        "task_id": task.id,
        "count": count,
        "questions": [],
        "redacted": False,
        "redaction_count": 0,
        "may_answer": refusal is None,
        "may_answer_because": refusal,
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
    answers = owner_answers_by_index(task.metadata)
    masking = masking_for(task)
    return {
        **out,
        "status": "ok",
        "questions": [
            {
                **_question(entry),
                "answer": owner_answer_to_api(answers[i], masking) if i in answers else None,
            }
            for i, entry in enumerate(parsed)
        ],
        "redacted": bool(window.get("redacted")),
        "redaction_count": int(window.get("redaction_count") or 0),
    }


class AnswerBody(BaseModel):
    """`POST /v1/tasks/{id}/answers`: an option label, free text, or both."""

    model_config = ConfigDict(extra="forbid")

    question_index: int
    choice: str | None = None
    text: str | None = None


def answer_question(
    store: Store,
    service: AgentOutputService,
    db: Any,
    tenant_id: str,
    task_id: str,
    body: AnswerBody,
    *,
    submitted_by: str | None,
    email: str,
    is_admin: bool,
    now: datetime,
) -> dict[str, Any]:
    """Store the owner's answer to one question on the task; serve the questions as they now read."""
    # The tenant boundary first, as on the read: another tenant's task is a 404.
    task = store.get_task(tenant_id, task_id, submitted_by=submitted_by)
    refusal = answer_refusal(task.submitted_by, email, is_admin=is_admin)
    if refusal is not None:
        raise Forbidden(refusal)

    read = read_questions(
        store, service, tenant_id, task_id,
        submitted_by=submitted_by, viewer=email, viewer_is_admin=is_admin,
    )
    if read["status"] == "none":
        raise ValidationFailed("this task's agent asked no questions; there is nothing to answer")
    if read["status"] != "ok":
        raise Conflict(
            "this task's questions could not be read, so an answer cannot be checked against them: "
            f"{read.get('unavailable_because')}"
        )
    questions = read["questions"]
    index = body.question_index
    if index < 0 or index >= len(questions):
        raise ValidationFailed(
            f"question_index must be 0 to {len(questions) - 1}: this task's agent asked "
            f"{len(questions)} question(s)"
        )
    question = questions[index]
    choice = body.choice.strip() if isinstance(body.choice, str) else None
    choice = choice or None
    text = body.text.strip() if isinstance(body.text, str) else None
    text = text or None
    if choice is None and text is None:
        raise ValidationFailed("an answer is a choice, free text, or both; this one was empty")
    labels = [o["label"] for o in question["options"] if o.get("label")]
    if choice is not None and choice not in labels:
        raise ValidationFailed(
            f"choice must be one of question {index + 1}'s options: "
            + ", ".join(repr(label) for label in labels),
            detail={"options": labels},
        )
    if text is not None and len(text) > MAX_ANSWER_CHARS:
        raise ValidationFailed(f"text is {len(text)} characters; an answer holds at most {MAX_ANSWER_CHARS}")
    if text is not None:
        # Redacted BEFORE it is stored, as a hand-off's reason is.
        text = redact(text).text

    ref = db.collection(TASKS).document(task_id)
    entry = {
        "question_index": index,
        "question": question.get("question"),
        "choice": choice,
        "text": text,
        "by": email,
        "at": now.isoformat(),
    }

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            raise NotFound(f"task {task_id!r} not found")
        metadata = dict(data.get("metadata") or {})
        current = owner_answers_by_index(metadata)
        previous = current.get(index)
        history: list[dict[str, Any]] = []
        if previous is not None:
            history = [h for h in previous.get("history") or [] if isinstance(h, dict)]
            if len(history) >= MAX_ANSWER_CHANGES:
                raise Conflict(
                    f"question {index + 1}'s answer has been changed {MAX_ANSWER_CHANGES} times; "
                    "it is not changed again, so its history stays whole"
                )
            history.append({k: previous.get(k) for k in ("choice", "text", "by", "at")})
        current[index] = {**entry, "history": history}
        metadata[OWNER_ANSWERS_METADATA_KEY] = [current[i] for i in sorted(current)]
        # `metadata` alone: `updated_at` is the task's own lifecycle clock,
        # and an answer on an ended task is not a change to the task.
        txn.update(ref, {"metadata": metadata})
        return previous is not None

    replaced = _apply(db.transaction())
    # The audit line names who and which question, never the answer's words.
    log.info(
        "owner answer stored",
        extra={
            "task_id": task_id,
            "tenant_id": tenant_id,
            "question_index": index,
            "by": email,
            "replaced": replaced,
            "with_choice": choice is not None,
            "with_text": text is not None,
        },
    )
    return read_questions(
        store, service, tenant_id, task_id,
        submitted_by=submitted_by, viewer=email, viewer_is_admin=is_admin,
    )
