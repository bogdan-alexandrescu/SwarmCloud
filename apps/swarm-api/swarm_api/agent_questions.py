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
some we could not read" are different answers -- neither is a 404. A file the
worker rejected (`questions_rejected` beside a count of 0) is not "asked
none" either: it answers `unavailable` with the worker's reason, redacted, and
`rejected: {file, reason}`, and an answer to it is a 409 naming the file and
the reason (#3086). `none` is kept for a task that wrote no file at all.

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
cannot drop either one's history, and contention is run again
(`ANSWER_TRANSACTION_ATTEMPTS`) so answers sent at once are all stored or
refused by name (503 `answer_contended`). `history` is capped at
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
session and the sc plugin's `swarm_task_answer` recorded it, or `voice-it`
when they answered by talking in Voice it (the owner, 6 November 2026) -- and that
session's id (`session`), so the console can say "answered in Claude Code".
Both are optional, stored with the answer and inside `history`, and served
beside `choice` and `text`. They are LABELS, NEVER PERMISSIONS: who may answer
is decided above, from the caller's identity and role, before either is
read, so no `via` widens or narrows it. A `session` is a UUID or 8-64 of
`[A-Za-z0-9_-]`, anything else a 422: it is shown to people, so it is held to
the shape of an id and can carry nothing else.

A DESIGN PREVIEW IS SERVED ON ITS OPTION (the owner's answer on the full-page
interview lane, 6 November 2026, 'Serve preview'). A plugin step's agent may
draw each option as HTML (`preview`); the worker keeps it in the wait's bridge
block, and the read serves it on that option, redacted as the question's text
is. One over `PREVIEW_MAX_CHARS`, or past its question's
`PREVIEW_QUESTION_MAX_CHARS`, is dropped whole with a `preview_dropped`
reason, never cut mid-tag. The console draws it only inside an iframe whose
`sandbox` attribute is empty. A preview is not part of `question_digest`.

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

A PLUGIN STEP'S QUESTION (the question bridge, docs/design/agent-plugins.md;
owner decision of 3 November 2026). An unattended gstack step whose agent
asked a question it could not answer itself is parked the same way, its
`question_wait` carrying a `bridge` block (`BRIDGE_KEY`: the session the
answer resumes). Two rules differ for it, and only for it: the FIRST answer
to each question stands -- a second is refused 409 `already_answered`,
because the resumed session has already been given the first -- and it is
dead-lettered unanswered after `BRIDGE_WAIT_MAX_SECONDS` (24 hours, the
human steps' default), the question left open, never with an answer nobody
gave.

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

import hashlib
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator
from typing import Any, Callable, Literal

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
from .errors import Conflict, Forbidden, NotFound, RateLimited, RoleMemberRequired, ValidationFailed
from .handoff import ready_writes
from .redaction import redact
from .store import EVENTS, TASKS, Store, _is_contention
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
#: The worker's short digest of each counted question, in index order
#: (`agent_worker.questions.DIGESTS_KEY`), and the stored answer's copy of
#: the one at its index when it was given (`question_digests`).
QUESTION_DIGESTS_KEY = "question_digests"
#: The worker's reason for not taking the file as questions, beside a count
#: of 0 (`agent_worker.questions.REJECTED_KEY`): a file the platform could
#: not take, which is not a task that asked none (#3086).
QUESTIONS_REJECTED_KEY = "questions_rejected"
ASKED_DIGEST_KEY = "asked_digest"

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
#: The block of a question wait that makes it a plugin step's bridged
#: question (`agent_worker.runners.question_bridge.BRIDGE_KEY`; the worker
#: is not installed here, so it is spelled again), and how long one waits.
BRIDGE_KEY = "bridge"
BRIDGE_WAIT_MAX_SECONDS = 24 * 3600
#: The most characters of design HTML served as one option's `preview`, and
#: as all of one question's previews (the owner, 6 November 2026, 'Serve preview').
#: The largest preview in the tests is a one-line snippet of under 60
#: characters, so the caps come from the worker instead: the bridge takes a
#: preview of at most 8 KiB (`question_bridge.MAX_TEXT`), which one option may
#: show whole, and four such designs per question (a bridged question has up
#: to 8 options) keep one GET under 8 x 32 KiB at the bridge's 8 questions.
#: Past either cap a preview is dropped whole with `preview_dropped`.
PREVIEW_MAX_CHARS = 8 * 1024
PREVIEW_QUESTION_MAX_CHARS = 4 * PREVIEW_MAX_CHARS
#: `detail.reason` of the refusal of a second answer to a bridged question.
ALREADY_ANSWERED = "already_answered"
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
#: ADVISORY QUESTIONS DO NOT HOLD A MERGE (owner decision, 2 November 2026).
#: A merge step goes ahead past an unanswered non-blocking question with a
#: recommended option its branch built, about nothing sensitive, and lists
#: it in its `result_summary` under this key (`agent_worker.merge.
#: ADVISORY_MERGED_PAST_KEY`, held equal by
#: tests/unit/worker/test_merge_advisory_questions.py): `{task_id, step_id,
#: question_index, question, recommended, built}`. The question stays open.
ADVISORY_MERGED_PAST_KEY = "advisory_merged_past"
#: The merge task's metadata record of the pick-fix rounds later answers to
#: those questions started against the default branch: one `{task_id,
#: question_index, picked, text, built, by, at}` per pick, then the round's
#: `round_workflow_id` and `round_task_id`, or its `error`. Claimed in the answer's
#: transaction, so one pick starts one round however often it is answered.
ADVISORY_PICK_FIX_KEY = "advisory_pick_fix"
#: The most rounds one merge's advisory questions start: past this, a pick
#: is stored as an answer and starts nothing, as a record bound.
MAX_ADVISORY_PICK_ROUNDS = 10
#: The runner profile of a merge step (`validation.MERGE_STEP_ID`).
MERGE_PROFILE = "merge"

#: ANSWERS SENT AT ONCE (5 November 2026, part of #2688): four answers to
#: four questions of one task, sent together, contend for the task document;
#: one met contention `firestore.transactional` does not retry (`Aborted` on
#: the transactional read) and answered 500. The answer's transaction is run
#: again, as the cancel's is (`store._is_contention`: only what cannot have
#: committed; never `DeadlineExceeded`), each run re-reading the answers it
#: merges into. How many runs, each itself up to five commit attempts:
ANSWER_TRANSACTION_ATTEMPTS = 4
#: The full-jitter pause before run n+1 is uniform in [0, base * 2**n]: at
#: most 0.1 + 0.2 + 0.4 s, jittered so answers that collided do not again.
ANSWER_RETRY_BASE_SECONDS = 0.1
#: The wall-clock budget for the pauses, well inside a request's time, so the
#: caller hears the named 503 rather than its own client timing out.
ANSWER_RETRY_BUDGET_SECONDS = 2.0
#: The 503's `Retry-After`: contention clears in well under a second.
ANSWER_RETRY_AFTER_SECONDS = 1.0
#: Replaced by the tests, which record the pauses rather than take them.
_contention_sleep = time.sleep


class AnswerContended(RateLimited):
    """The answer's transaction stayed contended through every run.

    503, retryable, and nothing was written. A `RateLimited` only so the
    API's error handler writes its `Retry-After`; its status says it is not
    a rate limit (as `store.CancelContended`).
    """

    status_code = 503
    code = "answer_contended"

    def __init__(self, message: str) -> None:
        super().__init__(message, ANSWER_RETRY_AFTER_SECONDS)


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


def is_bridged(wait: dict[str, Any] | None) -> bool:
    """Whether a question wait is a plugin step's bridged question."""
    return isinstance(wait, dict) and isinstance(wait.get(BRIDGE_KEY), dict)


def asked_by_skill(wait: dict[str, Any], masking: Any) -> str | None:
    """The skill that asked a bridged question, masked: the worker records the
    last `Skill` call before the question in the wait's bridge block
    (`question_bridge.asked_by_skill`, the owner's pick of 4 November 2026).
    None for a wait that is not bridged, a park recorded before the worker
    wrote it, or a value that is not a non-empty text: the page then says
    the skill is not known."""
    bridge = wait.get(BRIDGE_KEY) if isinstance(wait, dict) else None
    skill = bridge.get("skill") if isinstance(bridge, dict) else None
    if not isinstance(skill, str) or not skill.strip():
        return None
    return masking.text(skill.strip())[0]


def wait_max_seconds(wait: dict[str, Any]) -> int:
    """How long a question wait is held before it is dead-lettered unanswered."""
    return BRIDGE_WAIT_MAX_SECONDS if is_bridged(wait) else QUESTION_WAIT_MAX_SECONDS


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
        # The named task's questions are in its artifact, which this
        # transaction does not read, so a stored answer is taken as is here;
        # the woken merge re-reads every answer against the questions before
        # it merges (`agent_worker.merge._unanswered`).
        if index not in owner_answers_by_index(doc.get("metadata")):
            return False
    return True


def _merges_past(txn: Any, db: Any, data: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """The SUCCEEDED merges of the answered task's workflow that went past question `index`.

    Read in the answer's transaction, before any write. The tenant and the
    workflow are checked again on each document, and the merge's record of
    the question must name the answered task and index.
    """
    workflow_id, tenant_id, task_id = data.get("workflow_id"), data.get("tenant_id"), data.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        return []
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("workflow_id", "==", workflow_id))
        .where(filter=FieldFilter("state", "==", TaskState.SUCCEEDED.value))
        .where(filter=FieldFilter("runner_profile", "==", MERGE_PROFILE))
        .limit(MAX_HELD_MERGES)
    )
    merges: list[dict[str, Any]] = []
    for snap in txn.get(query):
        doc = {**(snap.to_dict() or {}), "id": snap.id}
        if (doc.get("tenant_id") == tenant_id and doc.get("workflow_id") == workflow_id
                and doc.get("id") != task_id and merged_past_row(doc, task_id, index) is not None):
            merges.append(doc)
    return merges


def merged_past_row(merge: dict[str, Any], task_id: Any, index: int) -> dict[str, Any] | None:
    """The merge's record that it went past question `index` of `task_id`, else None."""
    summary = merge.get("result_summary")
    rows = summary.get(ADVISORY_MERGED_PAST_KEY) if isinstance(summary, dict) else None
    for row in rows if isinstance(rows, list) else ():
        if (isinstance(row, dict) and row.get("task_id") == task_id
                and row.get("question_index") == index and not isinstance(index, bool)):
            return row
    return None


def advisory_pick_due(merge: dict[str, Any], task_id: str, index: int,
                      choice: str | None, text: str | None) -> dict[str, Any] | None:
    """The pick-fix claim an answer to a merged-past question makes, else None.

    Owner decision, 2 November 2026: an answer whose pick is not what the
    merged branch builds -- its `built`, else its `recommended` option --
    starts a pick-fix round against the default branch; free text with no
    option is such a pick too, as the merge's own check reads it. The same
    pick (the recommended one, or one a round was already started for)
    starts nothing, and a question whose `built` is null changes nothing
    the branch builds. Never an answer: the owner's stays theirs.
    """
    row = merged_past_row(merge, task_id, index)
    if row is None or ("built" in row and row["built"] is None):
        return None
    built = row.get("built") if isinstance(row.get("built"), str) else row.get("recommended")
    if not isinstance(built, str) or (choice is not None and choice == built):
        return None
    metadata = merge.get("metadata") if isinstance(merge.get("metadata"), dict) else {}
    started = [e for e in metadata.get(ADVISORY_PICK_FIX_KEY) or () if isinstance(e, dict)]
    if len(started) >= MAX_ADVISORY_PICK_ROUNDS:
        return None
    for entry in started:
        if (entry.get("task_id") == task_id and entry.get("question_index") == index
                and entry.get("picked") == choice
                and (choice is not None or entry.get("text") == text)):
            return None
    return {"task_id": task_id, "step_id": row.get("step_id"), "question_index": index,
            "question": row.get("question"), "picked": choice,
            "text": text if choice is None else None, "built": built}


#: Starts one pick-fix round against the default branch for a merge's
#: advisory claim: `(merge document, claim) -> {round_workflow_id, round_task_id}`;
#: raises when the round is refused (`mergewake.advisory_pick_fix`).
StartPickFix = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _record_advisory_round(db: Any, merge_id: str, claim: dict[str, Any],
                           patch: dict[str, Any]) -> None:
    """Write a started (or refused) round onto its claim in the merge's metadata."""
    ref = db.collection(TASKS).document(merge_id)

    @firestore.transactional
    def _apply(txn: Any) -> None:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None:
            return
        metadata = dict(data.get("metadata") or {})
        entries = [dict(e) for e in metadata.get(ADVISORY_PICK_FIX_KEY) or () if isinstance(e, dict)]
        for entry in entries:
            if all(entry.get(k) == claim.get(k) for k in ("task_id", "question_index", "picked", "at")):
                entry.update(patch)
        metadata[ADVISORY_PICK_FIX_KEY] = entries
        txn.update(ref, {"metadata": metadata})

    _apply(db.transaction())


def blocking_unanswered(wait: dict[str, Any], metadata: dict[str, Any] | None) -> list[int]:
    """The wait's blocking indices with no stored answer to THAT question (`answers_to`)."""
    answered = answers_to([_redacted_question(q)[0] for q in wait["questions"]], metadata)
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
    # And a design preview, as the text beside it is (the owner, 6 November 2026).
    out["options"] = [{**o, "label": clean(o["label"]), "description": clean(o["description"]),
                       **({"preview": clean(o["preview"])} if "preview" in o else {})}
                      for o in out["options"]]
    return out, masked


def _with_previews(wait: dict[str, Any]) -> list[dict[str, Any]]:
    """The wait's questions, each option with the preview the bridge block
    records for it (`question_bridge.wait_record` keeps them only there),
    matched by question index and option label."""
    bridge = wait.get(BRIDGE_KEY) if isinstance(wait.get(BRIDGE_KEY), dict) else {}
    asked = bridge.get("questions") if isinstance(bridge.get("questions"), list) else []
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(wait["questions"]):
        source = asked[i] if i < len(asked) and isinstance(asked[i], dict) else {}
        previews = {
            o.get("label"): o.get("preview")
            for o in source.get("options") or [] if isinstance(o, dict) and isinstance(o.get("label"), str)
        }
        if not previews:
            out.append(entry)
            continue
        out.append({**entry, "options": [
            {**o, "preview": previews.get(o.get("label"))} if isinstance(o, dict) and "preview" not in o else o
            for o in entry.get("options") or []
        ]})
    return out


def question_count(summary: dict[str, Any] | None) -> int:
    """How many questions the worker validated in this task's file, else 0."""
    count = (summary or {}).get(QUESTIONS_KEY)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def question_digests(summary: dict[str, Any] | None) -> list[str] | None:
    """The worker's digest of each counted question, in index order, else None.

    None for a task summarised before the worker wrote them, and for a list
    that does not hold one non-empty string per counted question: digests
    that cannot be matched to the count are not read, and the task is
    counted by index as it always was.
    """
    raw = (summary or {}).get(QUESTION_DIGESTS_KEY)
    if not isinstance(raw, list) or len(raw) != question_count(summary):
        return None
    if not all(isinstance(d, str) and d for d in raw):
        return None
    return raw


def rejected_questions(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """The worker's rejection of this task's file, redacted, else None.

    `{reason, uploaded}`: the worker's reason, masked as every served text
    is (it can quote the file), and whether the file is in the task's
    manifest. None when the summary holds no reason: a task that wrote no
    file at all, which alone reads as having asked none.
    """
    reason = (summary or {}).get(QUESTIONS_REJECTED_KEY)
    if not isinstance(reason, str) or not reason.strip():
        return None
    artifacts = (summary or {}).get("artifacts")
    uploaded = isinstance(artifacts, list) and any(
        isinstance(a, dict) and a.get("name") == QUESTIONS_NAME for a in artifacts)
    return {"reason": redact(reason).text, "uploaded": uploaded}


def answers_digest(entry: dict[str, Any], index: int, digests: list[str]) -> bool:
    """Whether the stored answer `entry` at `index` answers the question the
    worker's `digests` say is asked there now. An answer stored before it was
    stamped (`ASKED_DIGEST_KEY`) cannot be checked and counts by its index."""
    stamp = entry.get(ASKED_DIGEST_KEY)
    if not isinstance(stamp, str):
        return True
    return index < len(digests) and stamp == digests[index]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _question(entry: dict[str, Any]) -> dict[str, Any]:
    """One question with exactly its four keys, and each option with its two,
    plus `preview` (and `preview_dropped`) on an option the agent drew one for."""
    options = []
    total = 0
    for option in entry.get("options") or []:
        if not isinstance(option, dict):
            continue
        served = {"label": _text(option.get("label")), "description": _text(option.get("description"))}
        preview = _text(option.get("preview"))
        if preview:
            # Dropped whole, never cut: a cut design is HTML broken mid-tag.
            if len(preview) > PREVIEW_MAX_CHARS:
                served.update(preview=None, preview_dropped=(
                    f"the preview is larger than the {PREVIEW_MAX_CHARS} characters one option may show"))
            elif total + len(preview) > PREVIEW_QUESTION_MAX_CHARS:
                served.update(preview=None, preview_dropped=(
                    f"the question's previews pass the {PREVIEW_QUESTION_MAX_CHARS} characters one question may show"))
            else:
                total += len(preview)
                served["preview"] = preview
        options.append(served)
    return {
        "question": _text(entry.get("question")),
        "options": options,
        "recommended": _text(entry.get("recommended")),
        "context": _text(entry.get("context")),
    }


def question_digest(question: dict[str, Any]) -> str:
    """The digest of one question as it is served: its text and its options.

    Stored with each answer (`question_digest`) so an answer is served only
    under the question it answered (#3349). Only the text and the options'
    labels and descriptions are read -- not `recommended`, `context` or a
    served `answer` -- so the store and every read hash the same thing.
    """
    shape = {
        "question": _text(question.get("question")),
        "options": [
            [_text(o.get("label")), _text(o.get("description"))]
            for o in question.get("options") or [] if isinstance(o, dict)
        ],
    }
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def answers_question(entry: dict[str, Any], question: dict[str, Any]) -> bool:
    """Whether the stored answer `entry` answers `question` (served form, redacted).

    THE INDEX IS NOT THE QUESTION (#3349). An answer is stored by
    `question_index`, and a later attempt's file may ask a different
    question at that index; its answer then belongs to the question it was
    given for, never to the new one. An answer records the question it
    answered: `question_digest` (text and options) since #3349, compared
    whole; an answer stored before that has only the `question` text, which
    is compared instead. An answer with neither cannot be checked and is
    served as it always was.
    """
    digest = entry.get("question_digest")
    if isinstance(digest, str):
        return digest == question_digest(question)
    text = entry.get("question")
    if isinstance(text, str):
        return text == _text(question.get("question"))
    return True


def answers_to(questions: list[dict[str, Any]], metadata: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    """The stored answers by index, only those that answer the question asked there now."""
    return {
        index: entry
        for index, entry in owner_answers_by_index(metadata).items()
        if index < len(questions) and answers_question(entry, questions[index])
    }


def unanswered(count: int, metadata: dict[str, Any] | None,
               questions: list[dict[str, Any]] | None = None, *,
               digests: list[str] | None = None) -> int:
    """How many of the `count` questions have no stored answer (`unanswered_indices`)."""
    return len(unanswered_indices(count, metadata, questions, digests=digests))


def unanswered_indices(count: int, metadata: dict[str, Any] | None,
                       questions: list[dict[str, Any]] | None = None, *,
                       digests: list[str] | None = None) -> list[int]:
    """The indices of the `count` questions with no stored answer, in order.

    Given the `questions` as served, an answer to a different question at an
    index leaves it unanswered (`answers_to`). A caller that reads only the
    task document, not its questions file -- `open_question_tasks`,
    `codec.step_needs`'s `questions_unread` and the bell's question item --
    passes the worker's `digests` instead (`question_digests` of the
    task's result summary): an answer stamped with the digest of another
    question at its index leaves it unanswered (`answers_digest`), with no
    read of the questions file per task (the follow-up lane the owner picked
    on #3355, 2026-11-05). A task summarised before the worker wrote digests
    (`digests` None), and an answer stored before answers were stamped, are
    counted by index alone, as they always were.
    """
    if questions is not None:
        answered = answers_to(questions, metadata)
    else:
        answered = owner_answers_by_index(metadata)
        if digests is not None:
            answered = {i: e for i, e in answered.items() if answers_digest(e, i, digests)}
    return [index for index in range(count) if index not in answered]


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
    db: Any, tenant_id: str, submitted_by: str, session: str | None, since: datetime,
) -> tuple[list[Any], bool]:
    """`(tasks, complete)`: `submitted_by`'s tasks in `tenant_id`, tagged with
    the Claude Code `session` (any session or none when it is None: Voice it's
    planner has no Claude Code session), that ended at or after `since` with at least
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
        if session is not None and claude_session_of(task, masking_for(task, warm=False)) != session:
            continue
        # The questions file is not read here: the worker's digests say
        # which question each index asks now (`unanswered_indices`).
        if unanswered(question_count(task.result_summary), task.metadata,
                      digests=question_digests(task.result_summary)) == 0:
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
        if wait is not None:
            return _serve_wait(task, wait, adopted, out)
        rejected = rejected_questions(task.result_summary)
        if rejected is None:
            return {**out, "status": "none"}
        where = "it is in" if rejected["uploaded"] else "it is not in"
        return {
            **out,
            "status": "unavailable",
            "unavailable_because": (
                f"the worker did not take {QUESTIONS_NAME} as questions: {rejected['reason']}; "
                f"{where} the task's artifacts"
            ),
            "rejected": {"file": QUESTIONS_NAME, "reason": rejected["reason"]},
        }

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
    served = [_question(entry) for entry in parsed]
    answers = answers_to(served, task.metadata)
    masking = masking_for(task)
    return {
        **out,
        "status": "ok",
        "questions": [
            {
                **question,
                "answer": owner_answer_to_api(answers[i], masking) if i in answers else None,
            }
            for i, question in enumerate(served)
        ],
        "redacted": bool(window.get("redacted")),
        "redaction_count": int(window.get("redaction_count") or 0),
    }


def _serve_wait(task: Any, wait: dict[str, Any], adopted: bool, out: dict[str, Any]) -> dict[str, Any]:
    """A question wait's questions, as the worker validated them, redacted here.

    `waiting` says whether the agent is parked on them now, which block,
    since when and until when, where on GitHub they were posted, and, for a
    plugin step's bridged question, the skill that asked (`skill`, or null).
    """
    masking = masking_for(task)
    waited = set(wait["blocking"])
    redacted = [_redacted_question(entry) for entry in _with_previews(wait)]
    masked = sum(count for _, count in redacted)
    answers = answers_to([q for q, _ in redacted], task.metadata) if adopted else {}
    served: list[dict[str, Any]] = []
    for i, (question, _) in enumerate(redacted):
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
            "expires_at": (began + timedelta(seconds=wait_max_seconds(wait))).isoformat()
            if began else None,
            "issue_comment_url": comment.get("url") if isinstance(comment.get("url"), str) else None,
            "skill": asked_by_skill(wait, masking),
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
    #: `voice-it` is an answer given by talking in Voice it (the owner, 6 November 2026).
    via: Literal["console", "claude-code", "github-comment", "voice-it"] | None = None
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
    start_pick_fix: StartPickFix | None = None,
) -> dict[str, Any]:
    """Store the owner's answer to one question on the task; serve the questions as they now read.

    An answer to a question a merge went past (`ADVISORY_MERGED_PAST_KEY`)
    whose pick is not what the merged branch builds is claimed on that merge
    in the same transaction (`advisory_pick_due`), and then started through
    `start_pick_fix` as a pick-fix round against the default branch. Without
    `start_pick_fix` the claim is recorded with an error naming why.
    """
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
    if read.get("rejected"):
        # The agent wrote a file the worker did not take: name it and the
        # reason, never "asked no questions" (#3086).
        raise Conflict(f"there is no question here to answer: {read.get('unavailable_because')}")
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
        # The question this answers, so a later attempt asking another
        # question at this index does not read it as answered (#3349).
        "question_digest": question_digest(question),
        "choice": choice,
        "text": text,
        "by": email,
        "at": now.isoformat(),
        "via": body.via,
        "session": body.session,
    }

    # And the worker's digest of it, for the readers that count answers off
    # the task document alone (`unanswered_indices`). The summary is the
    # attempt whose file was just read: the read checked its count.
    digests = question_digests(task.result_summary)
    if digests is not None and index < len(digests):
        entry[ASKED_DIGEST_KEY] = digests[index]

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
        if (previous is not None and is_bridged(recorded_wait(metadata))
                and answers_question(previous, question)):
            # A bridged question's first answer is the one its session
            # resumes with; a second is refused, never recorded.
            raise Conflict(
                f"question {index + 1} was answered already, and that answer is the one the "
                "step resumes with; it is not changed",
                detail={"reason": ALREADY_ANSWERED},
            )
        history: list[dict[str, Any]] = []
        if previous is not None:
            history = [h for h in previous.get("history") or [] if isinstance(h, dict)]
            if len(history) >= MAX_ANSWER_CHANGES:
                raise Conflict(
                    f"question {index + 1}'s answer has been changed {MAX_ANSWER_CHANGES} times; "
                    "it is not changed again, so its history stays whole"
                )
            # With the question it answered: an answer to another question
            # asked at this index stays readable with its own (#3349).
            history.append({
                k: previous.get(k)
                for k in ("question", "question_digest", "choice", "text", "by", "at", "via", "session")
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
        # 2 November 2026: a merge that went past this question while it was
        # advisory; a pick it did not build is claimed here, once.
        claims: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for merge in _merges_past(txn, db, moved, index):
            claim = advisory_pick_due(merge, task_id, index, choice, text)
            if claim is not None:
                claims.append((merge, {**claim, "by": email, "at": now.isoformat()}))
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
        for merge, claim in claims:
            merge_metadata = dict(merge.get("metadata") or {})
            merge_metadata[ADVISORY_PICK_FIX_KEY] = [
                *(e for e in merge_metadata.get(ADVISORY_PICK_FIX_KEY) or () if isinstance(e, dict)),
                claim,
            ]
            txn.update(db.collection(TASKS).document(merge["id"]), {"metadata": merge_metadata})
            promoted.setdefault("claims", []).append((merge, claim))
        return previous is not None

    # CONTENTION IS RUN AGAIN, THEN REFUSED BY NAME. A fresh transaction
    # each run, and each run re-reads the task: an answer another question's
    # answer committed in between is merged, never overwritten, and a second
    # answer to the SAME question meets the history rule as it would alone.
    # Anything that is not contention is raised as is.
    deadline = time.monotonic() + ANSWER_RETRY_BUDGET_SECONDS
    last: BaseException | None = None
    for run in range(ANSWER_TRANSACTION_ATTEMPTS):
        try:
            replaced = _apply(db.transaction())
            break
        except Exception as exc:
            if not _is_contention(exc):
                raise
            last = exc
        pause = random.uniform(0, ANSWER_RETRY_BASE_SECONDS * 2**run)
        if run + 1 == ANSWER_TRANSACTION_ATTEMPTS or time.monotonic() + pause >= deadline:
            log.warning(
                "answer to question %d of task %s stayed contended after %d runs: %s",
                index, task_id, run + 1, type(last).__name__,
            )
            raise AnswerContended(
                f"question {index + 1}'s answer met other writes to task {task_id!r} on every "
                "attempt, often other answers sent at the same moment; nothing was stored, "
                "retry the answer"
            ) from last
        _contention_sleep(pause)
    started: list[str] = []
    for merge, claim in promoted.get("claims", []):
        # After the transaction: a submission is not a Firestore write. A
        # refusal is recorded on the claim, never retried by another answer.
        try:
            if start_pick_fix is None:
                raise RuntimeError("this answer path starts no pick-fix round")
            patch = start_pick_fix(merge, claim)
            started.append(str(patch.get("round_workflow_id")))
        except Exception as exc:  # recorded, and the answer still stands
            reason = exc.message if hasattr(exc, "message") else (
                str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)
            patch = {"error": redact(str(reason)).text[:300]}
            log.warning("advisory pick fix for merge %s not started: %s", merge["id"], patch["error"])
        _record_advisory_round(db, merge["id"], claim, patch)
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
            "pick_fix_rounds_started": started,
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
    """Dead-letter every agent parked on a blocking question past 72 hours (§3.9 step 5),
    or past 24 hours for a bridged one (`wait_max_seconds`).

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
        if began is None or began + timedelta(seconds=wait_max_seconds(wait)) > now:
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
