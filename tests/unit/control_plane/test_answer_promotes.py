"""The last blocking answer returns a parked agent to READY (lane AU-7).

docs/design/agent-autonomy.md §3.9, owner decision Q5. An agent that asked
a blocking question is PARKED(HUMAN_REQUIRED) with a `question_wait` record
in its park's `blocked_by` entry, holding nothing. These tests hold swarm-api
to:

  1. SERVING THE WAIT -- `GET /questions` serves the record's questions (a
     parked task has no result summary), each marked `blocking`, redacted,
     with `waiting` saying it is open and until when;
  2. PROMOTION ON THE LAST BLOCKING ANSWER -- an answer to a non-blocking
     question, or to one of two blocking ones, moves nothing; the last one
     moves PARKED -> READY in the answer's own transaction, with a READY
     event naming `owner_answered`; nothing is leased (invariant 1);
  3. ONE WAIT'S ANSWERS ARE ITS OWN -- a new wait starts with no answers,
     and the earlier wait and its answers are kept beside it;
  4. 72 HOURS -- a wait older than 72 hours is dead-lettered with
     `owner question unanswered`; a younger one, and a hand-off park with
     no question wait, are not.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from swarm_api import agent_questions
from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY

from .conftest import auth_header, seed_task, seed_tenant

TASK = "task_wait"
OWNER = "alice@example.com"


def _question(text: str, *, blocking: bool) -> dict:
    return {
        "question": text,
        "options": [
            {"label": "keep", "description": "the old route stays a release"},
            {"label": "drop", "description": "one route"},
        ],
        "recommended": "keep",
        "context": None,
        "blocking": blocking,
    }


def _wait(*, attempt: str = "att_1", asked: datetime | None = None) -> dict:
    return {
        "questions": [
            _question("Keep the old route?", blocking=True),
            _question("Rename the flag?", blocking=False),
            _question("Ship behind a switch?", blocking=True),
        ],
        "blocking": [0, 2],
        "attempt_id": attempt,
        "asked_at": (asked or datetime.now(timezone.utc)).isoformat(),
    }


def _parked(db, *, wait: dict | None = None, parked_at: datetime | None = None, attempt_count: int = 1) -> dict:
    seed_tenant(db, "eng")
    moment = parked_at or datetime.now(timezone.utc)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=moment)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = attempt_count
    doc["updated_at"] = moment
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait",
                          "question_wait": wait if wait is not None else _wait(asked=moment)}]
    return doc


def _answer(client, index: int, choice: str = "keep"):
    return client.post(f"/v1/tasks/{TASK}/answers", json={"question_index": index, "choice": choice},
                       headers=auth_header("alice"))


def _task(db) -> dict:
    return db.docs[f"tasks/{TASK}"]


def _events(db) -> list[dict]:
    prefix = f"tasks/{TASK}/events/"
    return [doc for path, doc in db.docs.items() if path.startswith(prefix)]


# -- 1. serving the wait ------------------------------------------------------------

def test_a_parked_wait_is_served_with_its_blocking_questions(client, db) -> None:
    _parked(db)

    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()

    # MUTATION: answer `none` for a task with no result summary and the console shows nothing.
    assert body["status"] == "ok", body
    assert body["count"] == 3
    # MUTATION: drop `blocking` from the served question and the card cannot mark it.
    assert [q["blocking"] for q in body["questions"]] == [True, False, True]
    assert body["waiting"]["open"] is True
    assert body["waiting"]["unanswered"] == [0, 2]
    assert body["waiting"]["expires_at"]
    assert body["may_answer"] is True


def test_a_wait_s_text_is_redacted_when_served(client, db) -> None:
    wait = _wait()
    leaked = "ghp_" + secrets.token_hex(18)
    wait["questions"][0]["context"] = f"the token {leaked} is in the log"
    _parked(db, wait=wait)

    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()

    # MUTATION: serve the record's text unredacted and the token is on the page.
    assert leaked not in str(body)
    assert body["redacted"] is True


# -- 2. promotion ---------------------------------------------------------------------

def test_the_last_blocking_answer_promotes_to_ready_and_not_before(client, db) -> None:
    _parked(db)

    assert _answer(client, 1).status_code == 200  # not blocking
    assert _answer(client, 0).status_code == 200  # one of two blocking
    # MUTATION: promote on the first blocking answer and this task runs with question 3 open.
    assert _task(db)["state"] == "PARKED"

    response = _answer(client, 2, "drop")
    assert response.status_code == 200, response.text
    task = _task(db)
    # MUTATION: never call `ready_writes` and the agent waits out its 72 hours.
    assert task["state"] == "READY"
    assert task["park_reason"] is None and task["blocked_by"] == []
    # Invariant 1: nothing is leased by an answer.
    assert task["current_lease_id"] is None
    ready = [e for e in _events(db) if e["type"] == "ready"]
    assert ready and ready[-1]["detail"]["reason"] == agent_questions.OWNER_ANSWERED
    # The wait and its answers stay on the task for the resumed attempt.
    assert task["metadata"][agent_questions.QUESTION_WAIT_KEY]["blocking"] == [0, 2]
    assert len(task["metadata"][OWNER_ANSWERS_METADATA_KEY]) == 3


def test_a_task_with_no_attempt_left_keeps_the_answer_and_is_not_moved(client, db) -> None:
    _parked(db, attempt_count=3)
    assert _answer(client, 0).status_code == 200
    response = _answer(client, 2)

    assert response.status_code == 200, response.text
    # MUTATION: skip `_resumable` and READY leases attempt 4 of 3.
    assert _task(db)["state"] == "PARKED"
    assert len(_task(db)["metadata"][OWNER_ANSWERS_METADATA_KEY]) == 2


# -- 3. one wait's answers are its own -------------------------------------------------

def test_a_new_wait_starts_with_no_answers_and_keeps_the_earlier_one(client, db) -> None:
    doc = _parked(db, wait=_wait(attempt="att_2"))
    doc["metadata"] = {
        agent_questions.QUESTION_WAIT_KEY: _wait(attempt="att_1"),
        OWNER_ANSWERS_METADATA_KEY: [{"question_index": 0, "question": "Keep the old route?",
                                      "choice": "drop", "text": None, "by": OWNER,
                                      "at": "2026-10-22T10:00:00+00:00", "history": []}],
    }

    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()
    # MUTATION: read the stored answers for any wait and question 1 reads answered.
    assert body["questions"][0]["answer"] is None

    assert _answer(client, 1).status_code == 200
    metadata = _task(db)["metadata"]
    assert metadata[agent_questions.QUESTION_WAIT_KEY]["attempt_id"] == "att_2"
    assert [a["question_index"] for a in metadata[OWNER_ANSWERS_METADATA_KEY]] == [1]
    # MUTATION: drop the earlier wait instead of keeping it and its answer is gone.
    (earlier,) = metadata[agent_questions.EARLIER_WAITS_KEY]
    assert earlier["wait"]["attempt_id"] == "att_1"
    assert earlier["answers"][0]["choice"] == "drop"


# -- 4. 72 hours ------------------------------------------------------------------------

def test_a_wait_past_72_hours_is_dead_lettered_and_a_younger_one_is_not(db) -> None:
    now = datetime.now(timezone.utc)
    _parked(db, parked_at=now - timedelta(hours=71))

    # MUTATION: a 24-hour bound and this one is ended a day early.
    assert agent_questions.expire_question_waits(db, now) == 0
    assert _task(db)["state"] == "PARKED"

    # MUTATION: skip the sweep's dead-letter and the agent waits for ever.
    assert agent_questions.expire_question_waits(db, now + timedelta(hours=1, seconds=1)) == 1
    task = _task(db)
    assert task["state"] == "DEAD_LETTERED"
    # MUTATION: write another error and failure-review files the owner's silence as a bug.
    assert task["last_error"] == "owner question unanswered"
    assert task["end_cause"] is None
    dead = [e for e in _events(db) if e["type"] == "dead_lettered"]
    assert dead[-1]["detail"]["unanswered"] == [0, 2]


def test_a_forged_late_asked_at_does_not_hold_the_wait(db) -> None:
    now = datetime.now(timezone.utc)
    wait = _wait(asked=now + timedelta(days=30))
    _parked(db, wait=wait, parked_at=now - timedelta(hours=73))

    # MUTATION: trust `asked_at` over the park instant and the forged date holds it a month.
    assert agent_questions.expire_question_waits(db, now) == 1


def test_a_hand_off_park_with_no_question_wait_is_not_ended(db) -> None:
    now = datetime.now(timezone.utc)
    doc = _parked(db, parked_at=now - timedelta(days=5))
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "human_wait"}]

    # MUTATION: end every HUMAN_REQUIRED park and a hand-off is ended with the wrong cause.
    assert agent_questions.expire_question_waits(db, now) == 0
    assert _task(db)["state"] == "PARKED"


def test_a_wait_behind_a_full_page_of_hand_off_parks_is_still_ended(db) -> None:
    now = datetime.now(timezone.utc)
    wait = _parked(db, parked_at=now - timedelta(hours=73))
    del db.docs[f"tasks/{TASK}"]
    # Three hand-off parks stored, and sorting, before the wait, two to a page.
    for n in range(3):
        db.docs[f"tasks/task_a{n}"] = {
            **wait, "id": f"task_a{n}",
            "blocked_by": [{"reason": "HUMAN_REQUIRED", "park_phase": "human_wait"}]}
    db.docs[f"tasks/{TASK}"] = wait

    # MUTATION: read one unordered `.limit(limit)` page and the wait behind them is never ended.
    assert agent_questions.expire_question_waits(db, now, limit=2) == 1
    assert _task(db)["state"] == "DEAD_LETTERED"


# -- the bell ----------------------------------------------------------------------------

def test_the_bell_lists_an_open_wait_until_its_last_blocking_answer(client, db) -> None:
    from swarm_api import notifications as notif
    from swarm_api.store import Store

    _parked(db)

    def items() -> list[dict]:
        viewer = notif.Viewer(email=OWNER, tenant_id="eng", is_admin=False, is_owner=False)
        reading = notif.Reading(db=db, store=Store(db), viewer=viewer, now=datetime.now(timezone.utc))
        return notif.agent_questions(reading)

    (item,) = items()
    # MUTATION: list only ended tasks and a parked agent's questions never reach the bell.
    assert item["id"] == f"task:questions:{TASK}"
    assert "waiting for your answer to 2 questions" in item["title"]

    assert _answer(client, 0).status_code == 200
    assert "1 question" in items()[0]["title"]
    assert _answer(client, 2).status_code == 200
    # MUTATION: keep listing a resumed agent and the bell asks for an answer already given.
    assert items() == []
