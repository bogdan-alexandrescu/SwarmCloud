"""The bell's task questions: only what still holds work (the owner's pick, 6 November 2026).

the owner asked "why do I have so many needs me in the console and a ton of
question marks even beside completed workflows?". An ended step's unanswered
questions ring only while its workflow has not finished, or while a merge in it
is held on the owner's questions; a task in no workflow rings only while it is
parked on them; the same words asked by two steps of one workflow ring once.
The console's Workflows list and Needs you count by the same rule
(apps/swarm-ui/src/__tests__/workflows.holdswork.test.tsx).

MUTATIONS: ring a finished workflow's questions; ring an ended task in no
workflow; drop a held merge's questions because the stored state says the
workflow ended; match a repeated question on its options; let a question left
to its twin ring again from the held merge.
"""

from __future__ import annotations

from typing import Any

from swarm_api import agent_questions
from swarm_api import notifications as notif

from .test_notifications import _ago, _feed, _held_merge, _reading, _task, _viewer, _workflow


def _questions(db) -> list[str]:
    return [i["id"] for i in notif.agent_questions(_reading(db, _viewer()))]


def _wait(*texts: str, options: tuple[str, ...] = ("yes", "no")) -> dict[str, Any]:
    return {
        "attempt_id": "att_1",
        "blocking": list(range(len(texts))),
        "questions": [{"question": t, "options": [{"label": o} for o in options]} for t in texts],
    }


def _parked(db, task_id: str, wait: dict[str, Any], *, workflow_id: str | None = "wf_open",
            minutes: float = 5) -> None:
    _task(db, task_id, state="PARKED", park_reason="HUMAN_REQUIRED", workflow_id=workflow_id,
          step_id=task_id.removeprefix("task_"), created_at=_ago(minutes=minutes),
          blocked_by=[{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait",
                       agent_questions.QUESTION_WAIT_KEY: wait}])


def test_a_finished_workflows_unanswered_questions_ring_nobody(db):
    for state in ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"):
        _workflow(db, f"wf_{state.lower()}", state=state)
        _task(db, f"task_{state.lower()}", result_summary={"questions": 2},
              workflow_id=f"wf_{state.lower()}")
    assert _questions(db) == []


def test_a_running_workflows_unanswered_questions_still_ring(db):
    _workflow(db, "wf_run", state="RUNNING")
    _task(db, "task_impl", result_summary={"questions": 1}, workflow_id="wf_run")
    assert _questions(db) == ["task:questions:task_impl"]


def test_a_held_merge_keeps_its_workflows_questions_ringing(db):
    # The stored state lags the hold: the merge is the work still held.
    _workflow(db, "wf_lane", state="SUCCEEDED")
    _task(db, "task_implement", workflow_id="wf_lane", step_id="implement",
          result_summary={"questions": 1})
    _held_merge(db, [("task_implement", 0)])
    assert _questions(db) == ["task:questions:task_implement"]
    reading = _reading(db, _viewer())
    # The merge leaves the question to the item that rings it: no second item.
    assert [i["id"] for i in notif.handoffs(reading)] == []


def test_an_ended_task_in_no_workflow_rings_nobody_and_a_parked_one_still_does(db):
    _task(db, "task_lone", result_summary={"questions": 1})
    _parked(db, "task_waits", _wait("Which region?"), workflow_id=None)
    assert _questions(db) == ["task:questions:task_waits"]


def test_the_same_question_asked_by_implement_and_fix_rings_once(db):
    _workflow(db)
    _parked(db, "task_implement", _wait("Which database  should it use?", "Which region?"),
            minutes=10)
    _parked(db, "task_fix", _wait("which database should it use?",
                                  options=("Postgres (keep)", "SQLite (small)")), minutes=2)
    items = notif.agent_questions(_reading(db, _viewer()))
    assert [i["id"] for i in items] == ["task:questions:task_implement"]
    assert items[0]["title"] == "Your agent is waiting for your answer to 2 questions"


def test_a_repeat_left_to_its_twin_is_not_rung_again_by_a_held_merge(db):
    _workflow(db, "wf_lane")
    _parked(db, "task_implement", _wait("Ship it?"), workflow_id="wf_lane", minutes=10)
    _parked(db, "task_fix", _wait("Ship it?"), workflow_id="wf_lane", minutes=2)
    _held_merge(db, [("task_implement", 0), ("task_fix", 0)])
    reading = _reading(db, _viewer())
    assert [i["id"] for i in notif.agent_questions(reading)] == ["task:questions:task_implement"]
    assert not any(i["id"].startswith("task:handoff:task_merge:") for i in notif.handoffs(reading))


def test_the_unread_count_drops_once_the_workflow_finishes(client, db):
    _workflow(db, "wf_ship", state="RUNNING")
    _task(db, "task_impl", result_summary={"questions": 1}, workflow_id="wf_ship")
    assert _feed(client)["unread_needs_you"] == 1
    _workflow(db, "wf_ship", state="SUCCEEDED")
    after = _feed(client)
    assert after["unread_needs_you"] == 0
    assert not any(i["id"] == "task:questions:task_impl" for i in after["items"])
