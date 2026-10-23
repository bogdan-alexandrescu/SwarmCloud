"""An answer to a review's blocking owner question wakes the merge it held (QUESTION-WAKE, part of #2070).

The owner decided on 2026-10-23 (~19:23Z, "Resume on answer, no expiry").
A merge step whose review named blocking owner questions (`needs_owner`)
parks PARKED(HUMAN_REQUIRED) with an `owner_questions` record in its park's
`blocked_by` entry, naming each question's asking task and index
(`agent_worker.lifecycle._park_for_owner_questions`, LANE-QUESTIONS #2133).
These tests hold `POST /v1/tasks/{id}/answers` to:

  1. THE LAST ANSWER WAKES IT -- once no question the park names is still
     unanswered (every named task's `owner_answers` read), the answer's own
     transaction returns the merge to READY through `handoff.ready_writes`;
  2. ONE OF TWO DOES NOT -- a merge still waiting on another unanswered
     question stays parked;
  3. AN UNRELATED ANSWER DOES NOT -- an answer on a task the park does not
     name leaves it alone.

Nothing is leased by an answer (invariant 1). No credentials, no network,
no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"
OWNER = "alice@example.com"
WORKFLOW = "wf_lane"
MERGE = "task_merge"
IMPLEMENT = "task_implement"
FIX = "task_fix"
OTHER = "task_other"


def _key(task: str) -> str:
    return f"tenants/eng/tasks/{task}/attempts/att_1/artifacts/questions.json"


def _questions() -> list[dict]:
    return [
        {
            "question": "Does the new route join UNCAPTURED_ROUTES?",
            "options": [
                {"label": "join", "description": "it is captured nowhere"},
                {"label": "stay", "description": "it is captured"},
            ],
            "recommended": "join",
            "context": None,
        },
        {
            "question": "Keep the old flag?",
            "options": [
                {"label": "keep", "description": "one more release"},
                {"label": "drop", "description": "one flag"},
            ],
            "recommended": "drop",
            "context": None,
        },
    ]


def _asked(db, objects, task: str, *, workflow: str | None = WORKFLOW) -> dict:
    doc = seed_task(db, task_id=task, tenant_id="eng", state="SUCCEEDED", workflow_id=workflow)
    doc["submitted_by"] = OWNER
    raw = json.dumps(_questions()).encode()
    objects.put(_key(task), raw)
    doc["result_summary"] = {
        "artifacts": [{"name": "questions.json", "bytes": len(raw),
                       "uri": f"gs://{BUCKET}/{_key(task)}"}],
        "artifact_bytes": len(raw),
        "logs": {},
        "questions": 2,
    }
    doc["completed_at"] = datetime.now(timezone.utc)
    return doc


def _merge_parked_on(db, held: list[tuple[str, int]]) -> dict:
    """The merge's owner-question park, exactly as the worker writes it."""
    moment = datetime.now(timezone.utc)
    doc = seed_task(db, task_id=MERGE, tenant_id="eng", state="PARKED",
                    park_reason="HUMAN_REQUIRED", next_eligible_at=moment,
                    workflow_id=WORKFLOW)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = 1
    doc["blocked_by"] = [{
        "reason": "HUMAN_REQUIRED",
        "park_phase": "owner_questions",
        "owner_questions": {
            "code": "owner_questions_open",
            "questions": [{"task_id": task, "step_id": task.removeprefix("task_"),
                           "question_index": index, "question": "q"} for task, index in held],
            "unreadable": 0,
        },
        "expires_at": moment.isoformat(),
    }]
    return doc


def _answer(client, task: str, index: int, choice: str):
    return client.post(f"/v1/tasks/{task}/answers",
                       json={"question_index": index, "choice": choice},
                       headers=auth_header("alice"))


def _merge(db) -> dict:
    return db.docs[f"tasks/{MERGE}"]


def _merge_events(db) -> list[dict]:
    prefix = f"tasks/{MERGE}/events/"
    return [doc for path, doc in db.docs.items() if path.startswith(prefix)]


def test_answering_the_last_of_two_blocking_questions_returns_the_merge_to_ready(
        client, db, objects) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects, IMPLEMENT)
    _asked(db, objects, FIX)
    _merge_parked_on(db, [(IMPLEMENT, 0), (FIX, 1)])

    assert _answer(client, IMPLEMENT, 0, "join").status_code == 200
    response = _answer(client, FIX, 1, "drop")

    assert response.status_code == 200, response.text
    merge = _merge(db)
    # MUTATION: never look for the merges a park names and the merge waits
    # for a person's `human-ready` although every question is answered.
    assert merge["state"] == "READY"
    # MUTATION: write the state alone, not through `ready_writes`, and the
    # stale park still reads as a wait on the console.
    assert merge["park_reason"] is None and merge["blocked_by"] == []
    # Invariant 1: an answer leases nothing.
    assert merge["current_lease_id"] is None
    ready = [e for e in _merge_events(db) if e["type"] == "ready"]
    # MUTATION: promote outside the answer's transaction (no event) and the
    # Attempts tab cannot say who woke it.
    assert len(ready) == 1 and ready[0]["detail"]["by"] == OWNER
    # The answer itself is stored on the asking task, as before.
    stored = db.docs[f"tasks/{FIX}"]["metadata"]["owner_answers"]
    assert [a["question_index"] for a in stored] == [1]


def test_answering_one_of_two_leaves_the_merge_parked(client, db, objects) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects, IMPLEMENT)
    _asked(db, objects, FIX)
    _merge_parked_on(db, [(IMPLEMENT, 0), (FIX, 1)])

    # Question 1 of the implement task is not one the park names.
    assert _answer(client, IMPLEMENT, 1, "keep").status_code == 200
    response = _answer(client, IMPLEMENT, 0, "join")

    assert response.status_code == 200, response.text
    merge = _merge(db)
    # MUTATION: wake on the answering task's questions alone and the merge
    # merges past the fix step's open question.
    assert merge["state"] == "PARKED" and merge["park_reason"] == "HUMAN_REQUIRED"
    assert merge["blocked_by"][0]["park_phase"] == "owner_questions"
    assert [e for e in _merge_events(db) if e["type"] == "ready"] == []


def test_an_answer_on_an_unrelated_task_does_not_touch_the_merge(client, db, objects) -> None:
    seed_tenant(db, "eng")
    _asked(db, objects, IMPLEMENT)
    _asked(db, objects, OTHER)
    before = _merge_parked_on(db, [(IMPLEMENT, 0)])
    snapshot = json.loads(json.dumps(before, default=str))

    response = _answer(client, OTHER, 0, "join")

    assert response.status_code == 200, response.text
    # MUTATION: wake every owner-questions park in the workflow on any answer
    # and a merge whose question is still open goes ahead.
    assert json.loads(json.dumps(_merge(db), default=str)) == snapshot
    assert _merge_events(db) == []
