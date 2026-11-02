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
`{question_index, question, choice, text, by, at, via, session, history}`.
Re-answering REPLACES the entry and moves the replaced answer, with its own
`by`, `at`, `via` and `session`, into `history`, so who changed what is on the task for as long as the task
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

WHERE IT WAS ANSWERED (owner decision 2026-10-19, "Plugin records it, marked
as from Claude Code"; lane ANSWER-SYNC, part of #1644): an answer may say
`via` -- `console`, or `claude-code` when the person answered in a Claude Code
session and the sc plugin's `swarm_task_answer` recorded it -- and that
session's id (`session`), so the console can say "answered in Claude Code".
Both are optional, stored with the answer and inside `history`, and served
beside `choice` and `text`. They are LABELS, NEVER PERMISSIONS: who may answer
is decided above, from the caller's identity and role, before either is
read, so no `via` widens or narrows it. A `session` is a UUID or 8-64 of
`[A-Za-z0-9_-]`, anything else a 422: it is shown to people, so it is held to
the shape of an id and can carry nothing else.

The notification (`notifications.agent_questions`) stops listing a task once
every question it asked has an answer (`unanswered`).

A BLOCKING QUESTION PARKS THE AGENT UNTIL IT IS ANSWERED
(docs/design/agent-autonomy.md §3.9, owner decision Q5; lane AU-7). An agent
that asked with `"blocking": true` and `await-answers` is parked
HUMAN_REQUIRED by its worker, no lease held, with a `question_wait` record
in the park's `blocked_by` entry: the questions the worker validated, the
indices that block, the attempt and when it asked. A parked task has no
result summary, so this module serves THAT record's questions (each text
redacted here, as the artifact read would), and adopts it into
`metadata.question_wait` the first time it writes for it -- an answer, or
the issue comment `ghapprove` posts -- moving any earlier wait and its
answers to `metadata.question_waits_earlier`, so one wait's answers are
never read as another's. When the answer just stored leaves no blocking
question unanswered, the same transaction returns the task to READY through
`handoff.ready_writes`, the transition `human-ready` makes: nothing is
leased here, and the next attempt is admitted all-or-nothing under a new
generation (invariants 1, 2, 5). A task that cannot be resumed (a cancel
asked for, its last attempt used) keeps the answer and is not moved.
Unanswered for `QUESTION_WAIT_MAX_SECONDS` (72 hours), it is dead-lettered
with `last_error: "owner question unanswered"` (`expire_question_waits`),
which failure-review skips by rule. Only HUMAN_REQUIRED is read as a
question wait: an AWAITING_INPUT park is a workflow's await-input step,
answered and expired by `awaitinput` alone, and never promoted or ended here.

AN ANSWER ALSO WAKES THE MERGE ITS QUESTION HELD (owner decision 2026-10-23
~19:23Z, "Resume on answer, no expiry"; lane QUESTION-WAKE, part of #2070).
A merge step whose review named blocking owner questions (`needs_owner`)
parks PARKED(HUMAN_REQUIRED) with an `owner_questions` record in its park's
`blocked_by` entry, naming each question by its asking task and index
(`agent_worker.lifecycle._park_for_owner_questions`). Those questions are
not the merge's own, so its park is not a `question_wait`. In the answer's
transaction, every such merge in the answered task's workflow whose record
names that task is read, and every task the record names is read with it;
when none of the named questions is still unanswered, the merge returns to
READY through `handoff.ready_writes`, as the question wait does, and its
next attempt reads every answer again before it merges. A merge still
waiting on another question stays parked, and so does one whose record
cannot be read whole -- an entry the worker could not read, a task outside
the workflow or the tenant: FAIL CLOSED, it waits for a person's
`human-ready`. The scheduler never expires such a park
(`Scheduler._expire_human_waits`). A parked merge holds no lease and no
capacity (invariant 1); nothing is leased here, and its next attempt is
admitted all-or-nothing under a new generation (invariants 2, 5).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator
from typing import Any, Literal

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, field_validator

from swarm_common.admission import _snapshot
from swarm_common.models import TaskEvent, new_id
from swarm_common.states import TERMINAL_STATES, EventType, ParkReason, TaskState, assert_transition

from . import refusals, roles
from .agent_output import AgentOutputService
from .codec import (
    claude_session_of,
    event_to_firestore,
    owner_answer_to_api,
    owner_answers_by_index,
    task_from_dict,
)
from .errors import Conflict, Forbidden, NotFound, RoleMemberRequired, ValidationFailed
from .handoff import ready_writes
from .redaction import redact
from .store import EVENTS, TASKS, Store
from .task_input import masking_for
from .validation import (
    CONFLICT_ROUND_METADATA_KEY,
    OWNER_ANSWERS_METADATA_KEY,
    QUESTION_WAIT_METADATA_KEY,
    QUESTION_WAITS_EARLIER_METADATA_KEY,
)

log = logging.getLogger(__name__)

#: The longest free-text answer, in characters (the lane brief: <= 2000).
MAX_ANSWER_CHARS = 2000

#: How many earlier answers one question keeps in `history`. A change past it
#: is refused (409), never made by forgetting the oldest: the history is the
#: audit. 20 is far more than a person changes their mind, and bounds the
#: task document at 20 x 2,000 characters per question.
MAX_ANSWER_CHANGES = 20

#: A session id that is not a UUID: 8-64 characters of `[A-Za-z0-9_-]`. A
#: Claude Code session id is a UUID today; the second shape keeps a future
#: id format from being refused while still holding it to an id.
_SESSION_SHAPE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid_session(value: str) -> bool:
    """Whether `value` is a Claude Code session id this API stores."""
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return bool(_SESSION_SHAPE.match(value))


#: The artifact's name and the `result_summary` key the worker counts it under
#: (`agent_worker.questions.NAME` / `SUMMARY_KEY`). Spelled again: the worker
#: is not installed in the API image.
QUESTIONS_NAME = "questions.json"
QUESTIONS_KEY = "questions"

#: The worker takes no file larger than this (`agent_worker.questions.MAX_BYTES`);
#: one byte more says whether the stored object is larger than it took.
QUESTIONS_MAX_BYTES = 64 * 1024


#: The park's record of a blocking question wait (`agent_worker.questions.
#: QUESTION_WAIT_KEY`), in the park's `blocked_by` entry and, once adopted,
#: in `metadata`. Spelled again: the worker is not installed in this image.
#: Both metadata keys are refused at submission (`validation.
#: REFUSED_AGENT_TEXT_METADATA_KEYS`), so only this module writes them.
QUESTION_WAIT_KEY = QUESTION_WAIT_METADATA_KEY
#: Earlier waits of the same task, each with the answers it was given.
EARLIER_WAITS_KEY = QUESTION_WAITS_EARLIER_METADATA_KEY
#: How many earlier waits are kept. A task that waits more often than this
#: is asking one question at a time; the newest are the ones still relevant.
MAX_EARLIER_WAITS = 5
#: How long a blocking question waits before its agent is dead-lettered
#: (§3.9 step 5): three days, a weekend and a day, at no cost (invariant 1).
QUESTION_WAIT_MAX_SECONDS = 72 * 3600
#: The dead-letter's `last_error`, which failure-review skips by rule
#: (docs/design/failure-review.md §2).
UNANSWERED = "owner question unanswered"
#: The READY event's reason when the last blocking answer resumes the agent.
OWNER_ANSWERED = "owner_answered"
#: The key of a merge's owner-question hold in its park's `blocked_by` entry
#: (`agent_worker.lifecycle._park_for_owner_questions`). Spelled again: the
#: worker is not installed in this image.
OWNER_QUESTIONS_PARK_KEY = "owner_questions"
#: The READY event's reason when an answer wakes a merge its questions held.
OWNER_QUESTIONS_ANSWERED = "owner_questions_answered"
#: The most parked merges one answer reads in its workflow. A workflow has
#: one merge step, and a merge-only continuation another; past this the rest
#: wait for a person's `human-ready`, never for a read nobody bounded.
MAX_HELD_MERGES = 10
#: The blocks a merge's fix round carries in its task's metadata, each naming
#: the merge it serves as `merge_task` (`mergewake._on_red` / `_on_pick`:
#: `merge_fix_round`; `_on_conflict`: `validation.CONFLICT_ROUND_METADATA_KEY`).
ROUND_METADATA_KEYS = ("merge_fix_round", CONFLICT_ROUND_METADATA_KEY)
#: The merge's own record of its rounds (`mergewake.MERGE_FIX_METADATA_KEY`)
#: and its round lists: CI, conflict and pick (`mergewake.ROUND_KIND`).
#: Spelled again, as `mergewake` brings the forge clients with it; held
#: equal by tests/unit/control_plane/test_answer_wakes_the_merge_round.py.
MERGE_FIX_METADATA_KEY = "merge_fix"
MERGE_ROUND_LISTS = ("rounds", "conflict_rounds", "pick_rounds")
#: The most distinct asking tasks one hold may name before it is read as
#: unreadable (it waits for a person): every one is read in the answer's
#: transaction, and the document is tenant-writable.
MAX_HOLD_TASKS = 25
#: Parks read per page by `parked_human_snapshots`: the sweep pages through
#: every PARKED(HUMAN_REQUIRED) task, so this bounds one read, not the sweep.
EXPIRY_SCAN = 200


def _field(task: Any, name: str) -> Any:
    """`name` of a Task or of its document: both are read here."""
    if isinstance(task, dict):
        return task.get(name)
    value = getattr(task, name, None)
    return value.value if hasattr(value, "value") and not isinstance(value, str) else value


def _wait_shape(value: Any) -> dict[str, Any] | None:
    """A question wait as the worker writes it, or None for anything else.

    The document is tenant-writable, so a record that is not this shape is
    no wait, never guessed at: a list of question maps, the blocking indices
    inside it, and the attempt that asked.
    """
    if not isinstance(value, dict):
        return None
    questions = value.get("questions")
    if not isinstance(questions, list) or not questions or not all(isinstance(q, dict) for q in questions):
        return None
    waited = value.get("blocking")
    if not isinstance(waited, list) or not waited or not all(
        isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(questions) for i in waited
    ):
        return None
    if not isinstance(value.get("attempt_id"), str):
        return None
    return value


def parked_wait(task: Any) -> dict[str, Any] | None:
    """The question wait this task is PARKED on now, from its park's `blocked_by`, else None."""
    if (_field(task, "state") != TaskState.PARKED.value
            or _field(task, "park_reason") != ParkReason.HUMAN_REQUIRED.value):
        return None
    for entry in _field(task, "blocked_by") or ():
        if isinstance(entry, dict) and entry.get("reason") == ParkReason.HUMAN_REQUIRED.value:
            wait = _wait_shape(entry.get(QUESTION_WAIT_KEY))
            if wait is not None:
                return wait
    return None


def recorded_wait(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """The wait swarm-api adopted into `metadata.question_wait`, else None."""
    return _wait_shape((metadata or {}).get(QUESTION_WAIT_KEY))


def current_wait(task: Any) -> tuple[dict[str, Any] | None, bool]:
    """(the task's question wait, whether `metadata.owner_answers` answers IT).

    The park's record wins while the task is parked on it; the stored
    answers are its own only once it was adopted (the same attempt). With
    no park, the adopted record is the last wait the task had.
    """
    metadata = _field(task, "metadata") or {}
    parked = parked_wait(task)
    recorded = recorded_wait(metadata)
    if parked is not None:
        adopted = recorded is not None and recorded.get("attempt_id") == parked.get("attempt_id")
        return (recorded if adopted else parked), adopted
    return recorded, recorded is not None


def adopt_wait(metadata: dict[str, Any], wait: dict[str, Any]) -> bool:
    """Make `wait` the task's `metadata.question_wait`; True when it was not already.

    Any earlier wait, with the answers it was given, moves to
    `question_waits_earlier` (the newest `MAX_EARLIER_WAITS`), and the
    answers start empty: a new wait's question 1 is not the old one's.
    Mutates `metadata`, which the caller writes in its own transaction.
    """
    recorded = recorded_wait(metadata)
    if recorded is not None and recorded.get("attempt_id") == wait.get("attempt_id"):
        return False
    answers = metadata.get(OWNER_ANSWERS_METADATA_KEY)
    if recorded is not None or answers:
        earlier = [e for e in metadata.get(EARLIER_WAITS_KEY) or [] if isinstance(e, dict)]
        earlier.append({"wait": recorded, "answers": answers if isinstance(answers, list) else []})
        metadata[EARLIER_WAITS_KEY] = earlier[-MAX_EARLIER_WAITS:]
    metadata[QUESTION_WAIT_KEY] = {k: v for k, v in wait.items()}
    metadata[OWNER_ANSWERS_METADATA_KEY] = []
    return True


def owner_questions_hold(data: dict[str, Any]) -> list[tuple[str, int]] | None:
    """The `(task_id, question_index)` pairs a merge's owner-question park names, else None.

    None for a task not PARKED(HUMAN_REQUIRED) on such a record, and for a
    record that is not readable whole: no questions, an entry that is not a
    task id and a non-negative integer index, an `unreadable` count the
    worker recorded, or more asking tasks than `MAX_HOLD_TASKS`. The
    document is tenant-writable, so nothing in it is guessed at, and a hold
    read as None is never woken by an answer.
    """
    if (data.get("state") != TaskState.PARKED.value
            or data.get("park_reason") != ParkReason.HUMAN_REQUIRED.value):
        return None
    for entry in data.get("blocked_by") or ():
        if not isinstance(entry, dict) or entry.get("reason") != ParkReason.HUMAN_REQUIRED.value:
            continue
        hold = entry.get(OWNER_QUESTIONS_PARK_KEY)
        if not isinstance(hold, dict):
            continue
        unreadable = hold.get("unreadable", 0)
        if isinstance(unreadable, bool) or not isinstance(unreadable, int) or unreadable != 0:
            return None
        questions = hold.get("questions")
        if not isinstance(questions, list) or not questions:
            return None
        named: list[tuple[str, int]] = []
        for question in questions:
            task = question.get("task_id") if isinstance(question, dict) else None
            index = question.get("question_index") if isinstance(question, dict) else None
            if (not isinstance(task, str) or not task or isinstance(index, bool)
                    or not isinstance(index, int) or index < 0):
                return None
            named.append((task, index))
        if len({task for task, _ in named}) > MAX_HOLD_TASKS:
            return None
        return named
    return None


def _round_merge(data: dict[str, Any]) -> str | None:
    """The merge task a fix round's task names in its metadata, else None."""
    metadata = data.get("metadata")
    for key in ROUND_METADATA_KEYS:
        block = metadata.get(key) if isinstance(metadata, dict) else None
        merge = block.get("merge_task") if isinstance(block, dict) else None
        if isinstance(merge, str) and merge:
            return merge
    return None


def _is_round_of(doc: dict[str, Any], merge: dict[str, Any]) -> bool:
    """Whether task `doc` is one of `merge`'s own fix rounds (#2913).

    A round runs as a workflow of its own, so it is not a step of the
    merge's workflow. Both documents are tenant-writable, so BOTH sides must
    say it: the same tenant, the round's metadata naming the merge, and the
    merge's `merge_fix` record listing the round's task in the round's own
    workflow -- as `agent_worker.merge._round_questions` reads the rounds
    from that record.
    """
    if doc.get("tenant_id") != merge.get("tenant_id") or _round_merge(doc) != merge.get("id"):
        return False
    task_id, workflow_id = doc.get("id"), doc.get("workflow_id")
    if not isinstance(task_id, str) or not task_id or task_id == merge.get("id"):
        return False
    metadata = merge.get("metadata")
    record = metadata.get(MERGE_FIX_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(record, dict):
        return False
    for key in MERGE_ROUND_LISTS:
        entries = record.get(key)
        for entry in entries if isinstance(entries, list) else ():
            if (isinstance(entry, dict) and entry.get("task_id") == task_id
                    and entry.get("workflow_id") == workflow_id):
                return True
    return False


def _held_merges(txn: Any, db: Any, data: dict[str, Any]) -> list[dict[str, Any]]:
    """The parked merges whose owner-question hold names the answered task.

    Those in the answered task's workflow, and -- when the answered task is a
    merge's fix round, which runs as a workflow of its own (#2913) -- the
    merge its metadata names, when that merge is the same tenant's and its
    `merge_fix` record lists the round (`_is_round_of`). Read in the
    answer's transaction, before any write. Each one's workflow and tenant
    are checked again on the document, as the query alone is not trusted to
    say them.
    """
    workflow_id, tenant_id, task_id = data.get("workflow_id"), data.get("tenant_id"), data.get("id")
    held: list[dict[str, Any]] = []
    if isinstance(workflow_id, str) and workflow_id:
        query = (
            db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("workflow_id", "==", workflow_id))
            .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
            .where(filter=FieldFilter("park_reason", "==", ParkReason.HUMAN_REQUIRED.value))
            .limit(MAX_HELD_MERGES)
        )
        for snap in txn.get(query):
            doc = {"id": snap.id, **(snap.to_dict() or {})}
            if (doc.get("tenant_id") != tenant_id or doc.get("workflow_id") != workflow_id
                    or doc.get("id") == task_id):
                continue
            named = owner_questions_hold(doc)
            if named is not None and any(task == task_id for task, _ in named):
                held.append(doc)
    merge_id = _round_merge(data)
    if merge_id is not None and merge_id != task_id and all(m["id"] != merge_id for m in held):
        snap = _snapshot(txn.get(db.collection(TASKS).document(merge_id)))
        merge = {**(snap.to_dict() or {}), "id": merge_id} if snap.exists else None
        if merge is not None and _is_round_of(data, merge):
            named = owner_questions_hold(merge)
            if named is not None and any(task == task_id for task, _ in named):
                held.append(merge)
    return held


def _hold_cleared(txn: Any, db: Any, merge: dict[str, Any], answered: dict[str, Any],
                  docs: dict[str, dict[str, Any] | None]) -> bool:
    """Whether every question `merge`'s hold names now has an answer.

    `answered` is the task just answered, with the answer it is being
    given; every other named task is read in the transaction (`docs`
    caches the reads, and every read precedes the transaction's writes).
    A named task counts when it is a step of the merge's own workflow and
    tenant, or one of the merge's own fix rounds (`_is_round_of`, #2913).
    FAIL CLOSED: a named task that cannot be read, or is neither, answers
    nothing -- as the merge's own re-read (`agent_worker.merge._unanswered`,
    `_round_questions`) holds it.
    """
    named = owner_questions_hold(merge) or []
    if not named:
        return False
    for task_id, index in named:
        if task_id == answered.get("id"):
            doc: dict[str, Any] | None = answered
        else:
            if task_id not in docs:
                snap = _snapshot(txn.get(db.collection(TASKS).document(task_id)))
                docs[task_id] = {**(snap.to_dict() or {}), "id": task_id} if snap.exists else None
            doc = docs[task_id]
        if doc is None:
            return False
        step = (doc.get("tenant_id") == merge.get("tenant_id")
                and doc.get("workflow_id") == merge.get("workflow_id"))
        if not step and not _is_round_of(doc, merge):
            return False
        if index not in owner_answers_by_index(doc.get("metadata")):
            return False
    return True


def blocking_unanswered(wait: dict[str, Any], metadata: dict[str, Any] | None) -> list[int]:
    """The wait's blocking indices with no stored answer."""
    answered = owner_answers_by_index(metadata)
    return [i for i in wait.get("blocking") or [] if i not in answered]


def waiting_count(task: Any) -> int:
    """How many questions this task is PARKED waiting on, else 0 (the bell's count)."""
    wait = parked_wait(task)
    return len(wait["questions"]) if wait is not None else 0


def asked_at(task: Any, wait: dict[str, Any]) -> datetime | None:
    """When the wait began: the EARLIEST of the park instant and what the
    worker wrote. The document is tenant-writable, and taking the earliest
    means a forged instant can only end the wait sooner, never hold it."""
    moments: list[datetime] = []
    for value in (_field(task, "next_eligible_at"), wait.get("asked_at"), _field(task, "updated_at")):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(value, datetime):
            moments.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    return min(moments) if moments else None


def _redacted_question(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """`_question`, every text redacted, with how many values were masked."""
    out = _question(entry)
    masked = 0

    def clean(value: str | None) -> str | None:
        nonlocal masked
        if value is None:
            return None
        result = redact(value)
        masked += result.count
        return result.text

    out["question"] = clean(out["question"])
    out["context"] = clean(out["context"])
    # Labels too, as the artifact read masks them: an answer's choice is
    # checked against the labels as served, so the two always agree.
    out["options"] = [{"label": clean(o["label"]), "description": clean(o["description"])}
                      for o in out["options"]]
    return out, masked


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


#: `GET /v1/tasks/open-questions` (#2835): how far back `since` may reach, its
#: default, and the most documents one request reads. The sc plugin's check
#: raises a finished task's questions for 7 days (`swarm_mcp.check.
#: OWNER_QUESTIONS_DAYS`); 31 lets a reader ask for a month and no more. The
#: query reads only the caller's tasks that ended in the window AND whose
#: agent asked questions -- answered or not -- so 2,000 is many weeks of a
#: busy person's asking; a read that reaches it says so (`complete: false`).
OPEN_QUESTIONS_DEFAULT_DAYS = 7
OPEN_QUESTIONS_MAX_DAYS = 31
OPEN_QUESTIONS_SCAN = 2000


def open_question_tasks(
    db: Any, tenant_id: str, submitted_by: str, session: str, since: datetime,
) -> tuple[list[Any], bool]:
    """`(tasks, complete)`: `submitted_by`'s tasks in `tenant_id`, tagged with
    the Claude Code `session`, that ended at or after `since` with at least
    one question the worker validated (`result_summary.questions`) and no
    stored answer for it (`metadata.owner_answers`), newest end first.

    ONE QUERY, NOT A WALK OF EVERY FINISHED TASK (#2835). The sc plugin's
    check paged the session's SUCCEEDED tasks to find these and stopped
    after 10 pages, so a busy session's older questions were never listed.
    The worker writes the count only for a file it validated, so
    `result_summary.questions > 0` with `completed_at >= since` (index
    tasks-tenant-submitter-completed-questions) reads exactly the tasks that
    asked; the session tag and the answers, which no index can hold, are
    filtered from those few. `complete` is False only when the read reached
    `OPEN_QUESTIONS_SCAN` documents."""
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("submitted_by", "==", submitted_by))
        .where(filter=FieldFilter("completed_at", ">=", since))
        .where(filter=FieldFilter(f"result_summary.{QUESTIONS_KEY}", ">", 0))
        .limit(OPEN_QUESTIONS_SCAN)
    )
    found: list[Any] = []
    read = 0
    for snap in query.stream():
        read += 1
        task = task_from_dict(snap.to_dict() or {})
        if task.tenant_id != tenant_id or task.submitted_by != submitted_by:
            continue
        if task.state not in TERMINAL_STATES:
            continue
        if claude_session_of(task, masking_for(task, warm=False)) != session:
            continue
        if unanswered(question_count(task.result_summary), task.metadata) == 0:
            continue
        found.append(task)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    found.sort(key=lambda t: (t.completed_at or epoch, t.id), reverse=True)
    return found, read < OPEN_QUESTIONS_SCAN


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
        wait, adopted = current_wait(task)
        if wait is None:
            return {**out, "status": "none"}
        return _serve_wait(task, wait, adopted, out)

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


def _serve_wait(task: Any, wait: dict[str, Any], adopted: bool, out: dict[str, Any]) -> dict[str, Any]:
    """A question wait's questions, as the worker validated them, redacted here.

    `waiting` says whether the agent is parked on them now, which block,
    since when and until when, and where on GitHub they were posted.
    """
    answers = owner_answers_by_index(task.metadata) if adopted else {}
    masking = masking_for(task)
    waited = set(wait["blocking"])
    served: list[dict[str, Any]] = []
    masked = 0
    for i, entry in enumerate(wait["questions"]):
        question, count = _redacted_question(entry)
        masked += count
        served.append({
            **question,
            "blocking": i in waited,
            "answer": owner_answer_to_api(answers[i], masking) if i in answers else None,
        })
    began = asked_at(task, wait)
    comment = wait.get("comment") if isinstance(wait.get("comment"), dict) else {}
    return {
        **out,
        "status": "ok",
        "count": len(served),
        "attempt_id": wait.get("attempt_id"),
        "questions": served,
        "redacted": masked > 0,
        "redaction_count": masked,
        "waiting": {
            "open": parked_wait(task) is not None,
            "blocking": sorted(waited),
            "unanswered": [i for i in sorted(waited) if i not in answers],
            "asked_at": began.isoformat() if began else None,
            "expires_at": (began + timedelta(seconds=QUESTION_WAIT_MAX_SECONDS)).isoformat()
            if began else None,
            "issue_comment_url": comment.get("url") if isinstance(comment.get("url"), str) else None,
        },
    }


class AnswerBody(BaseModel):
    """`POST /v1/tasks/{id}/answers`: an option label, free text, or both.

    `via` and `session` say where it was answered (ANSWER-SYNC); neither
    changes who may answer.
    """

    model_config = ConfigDict(extra="forbid")

    question_index: int
    choice: str | None = None
    text: str | None = None
    #: `github-comment` is the `/answer` reply on the agent's issue
    #: (`ghapprove.poll_answers`), set by swarm-api, never a caller's word for
    #: who may answer.
    via: Literal["console", "claude-code", "github-comment"] | None = None
    session: str | None = None

    @field_validator("session")
    @classmethod
    def _session_is_an_id(cls, value: str | None) -> str | None:
        if value is not None and not valid_session(value):
            raise ValueError(
                "session must be a Claude Code session id: a UUID, or 8-64 characters "
                "of A-Z, a-z, 0-9, _ and -"
            )
        return value


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
        "via": body.via,
        "session": body.session,
    }

    promoted: dict[str, Any] = {}

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        promoted.clear()  # a retried transaction starts again
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            raise NotFound(f"task {task_id!r} not found")
        metadata = dict(data.get("metadata") or {})
        # A wait the task is parked on is adopted before its first answer,
        # so an earlier wait's answers are never read as this one's.
        wait = parked_wait(data)
        if wait is not None:
            adopt_wait(metadata, wait)
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
            history.append({
                k: previous.get(k) for k in ("choice", "text", "by", "at", "via", "session")
            })
        current[index] = {**entry, "history": history}
        metadata[OWNER_ANSWERS_METADATA_KEY] = [current[i] for i in sorted(current)]
        moved = {**data, "metadata": metadata}
        # Every read before the first write, as a Firestore transaction
        # requires: the merges this task's answers hold, and every other
        # task their holds name.
        docs: dict[str, dict[str, Any] | None] = {}
        woken = [merge for merge in _held_merges(txn, db, moved)
                 if _hold_cleared(txn, db, merge, moved, docs) and _resumable(merge)]
        if wait is not None and not blocking_unanswered(wait, metadata) and _resumable(data):
            # The last blocking answer: PARKED(HUMAN_REQUIRED) -> READY in
            # this same transaction, as `human-ready` makes it. Nothing is
            # leased here (invariants 1, 2, 5).
            ready_writes(txn, db, moved, email=email, now=now, reason=OWNER_ANSWERED)
            promoted["by"] = email
        else:
            # `metadata` alone: `updated_at` is the task's own lifecycle clock,
            # and an answer on an ended task is not a change to the task.
            txn.update(ref, {"metadata": metadata})
        for merge in woken:
            # No question its review held it on is open: the merge returns
            # to READY in this same transaction and reads every answer again
            # before it merges. Nothing is leased here (invariants 1, 2, 5).
            ready_writes(txn, db, merge, email=email, now=now, reason=OWNER_QUESTIONS_ANSWERED)
            promoted.setdefault("merges", []).append(merge["id"])
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
            "via": body.via,
            "resumed": "by" in promoted,
            "merges_resumed": promoted.get("merges", []),
        },
    )
    return read_questions(
        store, service, tenant_id, task_id,
        submitted_by=submitted_by, viewer=email, viewer_is_admin=is_admin,
    )


def _resumable(data: dict[str, Any]) -> bool:
    """Whether `ready_writes` would move this task: no cancel asked, an attempt left.

    Asked before, so a task that cannot be resumed keeps the answer it was
    given and is left for the scheduler's sweeps to end, rather than the
    answer being refused with it.
    """
    task = task_from_dict(data)
    return not task.cancel_requested and not task.retries_exhausted()


def parked_human_snapshots(db: Any, *, page_size: int = EXPIRY_SCAN) -> Iterator[Any]:
    """Every PARKED(HUMAN_REQUIRED) task across tenants, in document-id order, page by page.

    An unordered `.limit(n)` answers with the same first page on every tick,
    so once `n` hand-off parks with no question wait exist, a question wait
    after them is never posted or expired (scheduler `parked_page` documents
    the same trap). Two equality filters ordered by `__name__` need no
    composite index. Each document's tenant is the one it names; nothing
    here decides a tenant (invariant 9).
    """
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
        .where(filter=FieldFilter("park_reason", "==", ParkReason.HUMAN_REQUIRED.value))
        .order_by("__name__")
    )
    after: str | None = None
    while True:
        page = query if after is None else query.start_after({"__name__": after})
        snaps = list(page.limit(page_size).stream())
        yield from snaps
        if len(snaps) < page_size:
            return
        after = snaps[-1].id


def expire_question_waits(db: Any, now: datetime, *, limit: int = EXPIRY_SCAN) -> int:
    """Dead-letter every agent parked on a blocking question past 72 hours (§3.9 step 5).

    Every PARKED(HUMAN_REQUIRED) task across tenants, `limit` per page
    (`parked_human_snapshots`); a task
    whose park carries a question wait that began `QUESTION_WAIT_MAX_SECONDS`
    ago is ended DEAD_LETTERED with `last_error: "owner question unanswered"`,
    in a transaction guarded on it still being parked on the SAME wait. A
    parked task holds no lease, so none is released (invariant 1). Returns
    how many it ended.
    """
    ended = 0
    for snap in parked_human_snapshots(db, page_size=limit):
        doc = snap.to_dict() or {}
        wait = parked_wait(doc)
        if wait is None:
            continue
        began = asked_at(doc, wait)
        if began is None or began + timedelta(seconds=QUESTION_WAIT_MAX_SECONDS) > now:
            continue
        if _dead_letter(db, str(doc.get("id") or snap.id), str(doc.get("tenant_id") or ""),
                        wait, now):
            ended += 1
    return ended


def _dead_letter(db: Any, task_id: str, tenant_id: str, wait: dict[str, Any], now: datetime) -> bool:
    ref = db.collection(TASKS).document(task_id)

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != tenant_id:
            return False
        current = parked_wait(data)
        if current is None or current.get("attempt_id") != wait.get("attempt_id"):
            return False
        assert_transition(TaskState.PARKED, TaskState.DEAD_LETTERED)
        txn.update(ref, {
            "state": TaskState.DEAD_LETTERED.value,
            "next_eligible_at": None,
            "blocked_by": [],
            "completed_at": now,
            "last_error": UNANSWERED,
            # No EndCause names this ending; the contract says None then.
            "end_cause": None,
            "updated_at": now,
        })
        event = TaskEvent(
            event_id=new_id("ev"), task_id=task_id, tenant_id=tenant_id,
            type=EventType.DEAD_LETTERED, at=now, attempt_id=None,
            detail={"reason": UNANSWERED, "park_reason": ParkReason.HUMAN_REQUIRED.value,
                    "cause": "question_wait_expired", "attempt_id": wait.get("attempt_id"),
                    "unanswered": blocking_unanswered(wait, data.get("metadata"))},
        )
        txn.set(ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
        return True

    done = _apply(db.transaction())
    if done:
        log.info("question wait expired", extra={"task_id": task_id, "tenant_id": tenant_id})
    return done
