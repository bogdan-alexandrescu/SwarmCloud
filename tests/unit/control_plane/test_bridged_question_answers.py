"""A plugin step's bridged question: its first answer stands, and it waits 24 hours.

docs/design/agent-plugins.md ("The question bridge"), owner decision of
3 November 2026. An unattended gstack step whose agent asked a question it
could not answer itself is PARKED(HUMAN_REQUIRED) on a `question_wait` that
carries a `bridge` block (the session the answer resumes). swarm-api holds
it to the owner questions' rules except two:

  1. THE FIRST ANSWER STANDS -- it is recorded with who answered and where,
     returns the step to READY, and a second answer is refused 409
     `already_answered`, the first left as it was; an ordinary question
     wait may still be re-answered;
  2. 24 HOURS -- unanswered for 24 hours it is dead-lettered with the
     question left open; an ordinary wait still waits 72.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from swarm_api import agent_questions
from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY

from .conftest import auth_header, seed_task, seed_tenant

TASK = "task_bridged"
OWNER = "alice@example.com"


def _wait(*, bridged: bool, asked: datetime) -> dict:
    wait = {
        "questions": [{
            "question": "Which database should the plan use?",
            "options": [
                {"label": "Postgres (Recommended)", "description": "relational"},
                {"label": "Firestore", "description": "documents"},
            ],
            "recommended": "Postgres (Recommended)",
            "context": "Database\n\nPreview of Postgres (Recommended):\nCREATE TABLE t ();",
            "blocking": True,
        }],
        "blocking": [0],
        "attempt_id": "att_1",
        "asked_at": asked.isoformat(),
    }
    if bridged:
        wait["bridge"] = {
            "session_id": str(uuid.uuid4()),
            "tool_use_id": "toolu_" + uuid.uuid4().hex[:12],
            "generation": 3,
            "source_revision": "a" * 40,
            "questions": [],
        }
    return wait


def _parked(db, *, bridged: bool = True, parked_at: datetime | None = None) -> dict:
    seed_tenant(db, "eng")
    moment = parked_at or datetime.now(timezone.utc)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=moment)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = 1
    doc["updated_at"] = moment
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait",
                          "question_wait": _wait(bridged=bridged, asked=moment)}]
    return doc


def _answer(client, choice: str, *, via: str | None = None):
    body = {"question_index": 0, "choice": choice}
    if via is not None:
        body["via"] = via
    return client.post(f"/v1/tasks/{TASK}/answers", json=body, headers=auth_header("alice"))


def _task(db) -> dict:
    return db.docs[f"tasks/{TASK}"]


def _answers(db) -> list[dict]:
    return _task(db)["metadata"][OWNER_ANSWERS_METADATA_KEY]


# -- 1. the first answer stands ---------------------------------------------------

def test_the_first_answer_is_recorded_with_who_and_where_and_resumes_the_step(client, db) -> None:
    _parked(db)

    response = _answer(client, "Firestore", via="console")

    assert response.status_code == 200, response.text
    [stored] = _answers(db)
    assert (stored["choice"], stored["by"], stored["via"]) == ("Firestore", OWNER, "console")
    # Invariant 1: the answer returns the step to READY and leases nothing.
    assert _task(db)["state"] == "READY"
    assert _task(db)["current_lease_id"] is None


def test_a_second_answer_to_a_bridged_question_is_refused_and_the_first_stands(client, db) -> None:
    _parked(db)
    assert _answer(client, "Firestore").status_code == 200

    response = _answer(client, "Postgres (Recommended)")

    # MUTATION: let the re-answer replace the first, and the stored answer is
    # one the resumed session was never given.
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "already_answered"
    [stored] = _answers(db)
    assert stored["choice"] == "Firestore"
    assert stored["history"] == []


def test_an_ordinary_question_wait_may_still_be_re_answered(client, db) -> None:
    _parked(db, bridged=False)
    assert _answer(client, "Firestore").status_code == 200

    # MUTATION: refuse every second answer, and the owner questions' history breaks.
    assert _answer(client, "Postgres (Recommended)").status_code == 200
    [stored] = _answers(db)
    assert stored["choice"] == "Postgres (Recommended)"


# -- 2. 24 hours ------------------------------------------------------------------

def test_a_bridged_question_unanswered_for_24_hours_ends_with_the_question_open(db) -> None:
    now = datetime.now(timezone.utc)
    _parked(db, parked_at=now - timedelta(hours=23))

    assert agent_questions.expire_question_waits(db, now) == 0
    assert _task(db)["state"] == "PARKED"

    # MUTATION: hold a bridged question to the owner questions' 72 hours and
    # the step waits two days past the human steps' default.
    assert agent_questions.expire_question_waits(db, now + timedelta(hours=1, seconds=1)) == 1
    task = _task(db)
    assert task["state"] == "DEAD_LETTERED"
    assert task["last_error"] == "owner question unanswered"
    # Never an answer nobody gave.
    assert OWNER_ANSWERS_METADATA_KEY not in (task.get("metadata") or {})


def test_an_ordinary_wait_still_waits_72_hours(db) -> None:
    now = datetime.now(timezone.utc)
    _parked(db, bridged=False, parked_at=now - timedelta(hours=25))

    assert agent_questions.expire_question_waits(db, now) == 0
    assert _task(db)["state"] == "PARKED"


def test_a_bridged_wait_is_served_with_its_24_hour_deadline(client, db) -> None:
    asked = datetime.now(timezone.utc).replace(microsecond=0)
    _parked(db, parked_at=asked)

    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()

    assert body["status"] == "ok", body
    assert body["waiting"]["open"] is True
    assert datetime.fromisoformat(body["waiting"]["expires_at"]) == asked + timedelta(hours=24)
