"""The Workflows list's Needs-you bar draws from the list read (PR #3147).

The owner's pick on PR #3147, "Widen view=summary": the Needs-you bar
(apps/swarm-ui/src/work/NeedsYou.tsx) is mounted on the Workflows list, and
what it draws for each step -- a browser hand-off and its clock, a merge held
by the owner's questions, the open questions of a question wait, the indices
of the questions only the agent's artifact holds, the pull request -- is in
each step's `task_summary.needs`, off the task document the rollup already
read. Every text an agent or a worker wrote is masked by the task's masker.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from swarm_api.codec import workflow_to_firestore
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
NEEDS_KEYS = {
    "park_reason", "next_eligible_at", "updated_at", "human_wait", "owner_questions", "question_wait",
    "questions", "questions_unread", "pull_request",
}


def _one_step(db, workflow_id: str, **task: object) -> str:
    task_id = f"task_{workflow_id}"
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state=str(task.pop("state", "SUCCEEDED")),
                    created_at=T0, workflow_id=workflow_id)
    doc.update({"step_id": "s0", "metadata": {"title": "The step"}, **task})
    workflow = Workflow(
        workflow_id=workflow_id, tenant_id="eng", created_at=T0, updated_at=T0,
        state=TaskState.QUEUED, submitted_by="alice@example.com",
        steps=[WorkflowStep(step_id="s0", runner_profile="mock", input={"prompt": "p"},
                            depends_on=[], task_id=task_id)],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)
    return task_id


def _needs(client, workflow_id: str) -> dict:
    response = client.get("/v1/workflows", headers=auth_header("alice"), params={"view": "summary"})
    assert response.status_code == 200, response.text
    row = next(w for w in response.json()["workflows"] if w["workflow_id"] == workflow_id)
    needs = row["steps"][0]["task_summary"]["needs"]
    assert set(needs) == NEEDS_KEYS
    return needs


def test_a_plain_step_needs_nothing(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_plain")

    needs = _needs(client, "wf_plain")

    assert needs["human_wait"] is None
    assert needs["owner_questions"] is None
    assert needs["questions"] == []
    assert needs["questions_unread"] == []
    assert needs["pull_request"] is None


def test_a_running_hand_off_serves_its_wait_masked(client, db):
    seed_tenant(db, "eng")
    secret = "gh" + "p_" + secrets.token_hex(18)
    deadline = T0 + timedelta(minutes=30)
    _one_step(db, "wf_hand", state="RUNNING",
              human_wait={"reason": f"sign in with {secret}", "expect": "a login", "deadline": deadline})

    response = client.get("/v1/workflows", headers=auth_header("alice"), params={"view": "summary"})
    needs = _needs(client, "wf_hand")

    wait = needs["human_wait"]
    assert wait["reason"].startswith("sign in with ")
    assert secret not in response.text
    assert wait["expect"] == "a login"
    assert wait["deadline"].startswith("2026-11-03T12:30:00")


def test_a_merge_held_by_owner_questions_serves_the_hold(client, db):
    seed_tenant(db, "eng")
    secret = "gh" + "p_" + secrets.token_hex(18)
    hold = {
        "reason": "HUMAN_REQUIRED", "park_phase": "owner_questions",
        "owner_questions": {
            "code": "owner_questions_open", "unreadable": 0,
            "questions": [
                {"task_id": "task_impl", "step_id": "implement", "question_index": 0,
                 "question": f"Use {secret}?"},
                "not a question record",
            ],
        },
    }
    _one_step(db, "wf_merge", state="PARKED", park_reason="HUMAN_REQUIRED", blocked_by=[hold],
              next_eligible_at=T0 + timedelta(minutes=5))

    needs = _needs(client, "wf_merge")

    assert needs["park_reason"] == "HUMAN_REQUIRED"
    assert needs["question_wait"] is False
    assert needs["next_eligible_at"].startswith("2026-11-03T12:05:00")
    held = needs["owner_questions"]
    assert held["unreadable"] == 0
    first, second = held["questions"]
    assert first["task_id"] == "task_impl" and first["step_id"] == "implement"
    assert first["question_index"] == 0
    assert first["question"].startswith("Use ") and secret not in first["question"]
    # A record that is not a question is served as null, never as stored.
    assert second is None
    assert "not a question record" not in json.dumps(needs)


def test_a_question_wait_serves_its_open_questions_and_not_the_answered(client, db):
    seed_tenant(db, "eng")
    wait = {
        "attempt_id": "att_1",
        "blocking": [0, 1],
        "questions": [
            {"question": "Which database?", "options": [{"label": "Postgres", "description": "the usual"}],
             "recommended": "Postgres"},
            {"question": "Which region?", "options": []},
        ],
    }
    _one_step(db, "wf_ask", state="PARKED", park_reason="HUMAN_REQUIRED",
              blocked_by=[{"reason": "HUMAN_REQUIRED", "question_wait": wait}],
              metadata={"title": "The step", "question_wait": wait,
                        "owner_answers": [{"question_index": 1, "text": "eu", "by": "alice@example.com",
                                           "at": T0.isoformat()}]})

    needs = _needs(client, "wf_ask")

    assert needs["owner_questions"] is None
    assert needs["question_wait"] is True
    assert needs["questions"] == [{
        "index": 0, "question": "Which database?",
        "options": [{"label": "Postgres", "description": "the usual"}], "recommended": "Postgres",
    }]


def test_artifact_questions_are_served_by_index_only(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_file", state="SUCCEEDED",
              result_summary={"questions": 3, "git": {"pull_request": {"number": 42, "url": "https://github.com/o/r/pull/42"}}},
              metadata={"title": "The step",
                        "owner_answers": [{"question_index": 1, "text": "yes", "by": "alice@example.com",
                                           "at": T0.isoformat()}]})

    needs = _needs(client, "wf_file")

    assert needs["questions"] == []
    assert needs["questions_unread"] == [0, 2]
    assert needs["pull_request"] == {"number": 42, "url": "https://github.com/o/r/pull/42"}


def _answer(index: int) -> dict:
    return {"question_index": index, "text": "earlier", "by": "alice@example.com", "at": T0.isoformat()}


def test_answers_to_an_earlier_wait_never_hide_a_new_waits_questions(client, db):
    # The park is attempt att_2's wait; metadata still holds att_1's, with
    # att_1's answer at index 0. Until swarm-api adopts att_2's wait those
    # answers are not its own (notifications._asked_item reads them the same way).
    seed_tenant(db, "eng")
    earlier = {"attempt_id": "att_1", "blocking": [0], "questions": [{"question": "Old?", "options": []}]}
    new = {"attempt_id": "att_2", "blocking": [0], "questions": [{"question": "Which bucket?", "options": []}]}
    _one_step(db, "wf_new_wait", state="PARKED", park_reason="HUMAN_REQUIRED",
              blocked_by=[{"reason": "HUMAN_REQUIRED", "question_wait": new}],
              metadata={"title": "The step", "question_wait": earlier, "owner_answers": [_answer(0)]})

    needs = _needs(client, "wf_new_wait")

    assert needs["question_wait"] is True
    assert needs["questions"] == [{"index": 0, "question": "Which bucket?", "options": [], "recommended": None}]


def test_questions_unread_is_bounded_by_the_workers_cap(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_many", state="SUCCEEDED", result_summary={"questions": 200_000})

    needs = _needs(client, "wf_many")

    assert needs["questions_unread"] == list(range(20))


def test_the_served_question_cap_is_the_workers():
    from agent_worker.questions import MAX_QUESTIONS
    from swarm_api.codec import MAX_ARTIFACT_QUESTIONS

    assert MAX_ARTIFACT_QUESTIONS == MAX_QUESTIONS


def test_a_step_not_parked_on_a_wait_serves_no_wait_questions(client, db):
    seed_tenant(db, "eng")
    wait = {"attempt_id": "att_1", "blocking": [0], "questions": [{"question": "Old?", "options": []}]}
    _one_step(db, "wf_dead", state="DEAD_LETTERED", metadata={"title": "The step", "question_wait": wait})

    needs = _needs(client, "wf_dead")

    assert needs["question_wait"] is False
    assert needs["questions"] == []


def test_a_running_step_serves_no_unread_artifact_questions(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_running", state="RUNNING", result_summary={"questions": 3})

    needs = _needs(client, "wf_running")

    assert needs["questions_unread"] == []
