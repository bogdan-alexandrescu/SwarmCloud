"""`POST /v1/tasks/{id}/answers?reply=short`: the answer recorded, no question text.

AI Studio's request on BGD's decision of 6 November 2026 (part of #2688):
about 404 questions on about 189 finished tasks are closed as moot, and
each answer's reply served the whole task's questions (about 4,000 tokens),
so recording them one by one cost about 1.6 million tokens. `reply=short`
answers only `{status, task_id, question_index, recorded_at, by}`; the
default reply is unchanged, and the answer is checked exactly as before.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .conftest import auth_header, seed_task, seed_tenant

TASK = "task_short"
OWNER = "alice@example.com"


def _parked(db) -> None:
    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=now)
    doc["submitted_by"] = OWNER
    doc["updated_at"] = now
    question = {
        "question": "Keep the old route?",
        "options": [{"label": "keep", "description": "a release"}, {"label": "drop", "description": "one"}],
        "recommended": "keep",
        "context": None,
        "blocking": False,
    }
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait", "question_wait": {
        "questions": [question, {**question, "question": "Rename the flag?"}],
        "blocking": [0],
        "attempt_id": "att_1",
        "asked_at": now.isoformat(),
    }}]


def _answer(client, query: str = "", **body):
    return client.post(f"/v1/tasks/{TASK}/answers{query}", json={"question_index": 1, **body},
                       headers=auth_header("alice"))


def test_reply_short_answers_the_record_without_the_questions(client, db) -> None:
    _parked(db)

    response = _answer(client, "?reply=short", choice="drop")

    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: serve the questions as the default reply does and a bulk close costs 4,000 tokens an answer.
    assert set(body) == {"status", "task_id", "question_index", "recorded_at", "by"}
    assert "Keep the old route?" not in response.text and "Rename the flag?" not in response.text
    assert body["status"] == "recorded"
    assert body["task_id"] == TASK and body["question_index"] == 1
    assert body["by"] == OWNER
    assert datetime.fromisoformat(body["recorded_at"])
    # The answer is stored as the full reply would store it.
    stored = db.docs[f"tasks/{TASK}"]["metadata"]["owner_answers"]
    assert [(a["question_index"], a["choice"]) for a in stored] == [(1, "drop")]


def test_a_text_only_answer_is_still_taken_with_reply_short(client, db) -> None:
    _parked(db)

    response = _answer(client, "?reply=short", text="moot: the lane finished")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "recorded"


def test_the_default_reply_is_unchanged(client, db) -> None:
    _parked(db)

    default = _answer(client, choice="drop")
    full = _answer(client, "?reply=full", choice="keep")

    for response in (default, full):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert [q["question"] for q in body["questions"]] == ["Keep the old route?", "Rename the flag?"]
    assert full.json()["questions"][1]["answer"]["choice"] == "keep"


def test_reply_short_is_checked_as_a_single_answer_is(client, db) -> None:
    _parked(db)

    # A choice that is not one of the options is refused, short or not.
    assert _answer(client, "?reply=short", choice="neither").status_code == 422
    # Another member may not answer the submitter's task.
    other = client.post(f"/v1/tasks/{TASK}/answers?reply=short",
                        json={"question_index": 0, "choice": "keep"}, headers=auth_header("bob"))
    assert other.status_code in (403, 404), other.text
    assert "owner_answers" not in (db.docs[f"tasks/{TASK}"].get("metadata") or {})


def test_an_unknown_reply_is_refused(client, db) -> None:
    _parked(db)

    response = _answer(client, "?reply=tiny", choice="drop")

    assert response.status_code == 422, response.text
