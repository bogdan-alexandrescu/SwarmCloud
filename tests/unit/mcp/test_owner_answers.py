"""Whoever launched the work reads the owner's answers next to the questions.

Owner decision 2026-10-13 ("Yes, pick an option on the task"; lane
TASK-ANSWERS, part of #1206). The console stores an answer on the task
(`metadata.owner_answers`, lifted by the API to the task's `owner_answers`).
The session that launched the agent reads its result through the bridge, so:

* `swarm_result` carries `owner_answers` beside `owner_questions`;
* the follow outcome (`progress.outcome`, what `swarm_follow` and
  `swarm_wait` return for a finished task) carries `owner_answers` beside
  `questions`, and the slimmed step outcome keeps it;
* `sc task` prints each answer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from swarm_mcp import compact, progress, server
from swarm_mcp.render import PLAIN, render_task

from control_plane.conftest import PROJECT
from test_follow_cursor import NOW, TENANT, World, swarm, world  # noqa: F401 - fixtures

BUCKET = f"swarm-artifacts-{PROJECT}"
NAME = "questions.json"

QUESTIONS = [
    {
        "question": "Keep the old key beside the new one for a release?",
        "options": [
            {"label": "keep", "description": "Readers of the old key keep working."},
            {"label": "drop", "description": "One key, one shape."},
        ],
        "recommended": "drop",
        "context": "The bridge is pinned by tag.",
    },
]

ANSWER = {
    "question_index": 0,
    "question": "Keep the old key beside the new one for a release?",
    "choice": "keep",
    "text": "one release, then drop it",
    "by": "alice@saga.xyz",
    "at": "2026-10-13T12:30:00+00:00",
    "history": [],
}


def _finish(world: World, task_id: str, *, answers: list[dict[str, Any]] | None) -> None:
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["state"] = "SUCCEEDED"
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    body = json.dumps(QUESTIONS)
    key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_1/artifacts/{NAME}"
    world.objects.put(key, body)
    entry = {"name": NAME, "bytes": len(body.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"}
    doc["result_summary"] = {
        "runner": {"summary": "done"},
        "artifacts": [entry],
        "artifact_bytes": entry["bytes"],
        "logs": {},
        "questions": 1,
    }
    if answers is not None:
        doc["metadata"] = {**(doc.get("metadata") or {}), "owner_answers": answers}


def test_swarm_result_carries_the_owner_answers_beside_the_questions(swarm, world):
    world.task("task_a", state="SUCCEEDED")
    _finish(world, "task_a", answers=[ANSWER])

    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))

    assert reply["owner_questions"] == QUESTIONS
    assert [a["choice"] for a in reply["owner_answers"]] == ["keep"]
    assert reply["owner_answers"][0]["text"] == "one release, then drop it"
    assert reply["owner_answers"][0]["by"] == "alice@saga.xyz"


def test_swarm_result_of_an_unanswered_task_carries_no_answers_key(swarm, world):
    world.task("task_a", state="SUCCEEDED")
    _finish(world, "task_a", answers=None)

    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))

    assert reply["owner_questions"] == QUESTIONS
    assert "owner_answers" not in reply


def test_the_follow_outcome_carries_the_owner_answers(swarm, world):
    world.task("task_a", state="SUCCEEDED")
    _finish(world, "task_a", answers=[ANSWER])

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert got["questions"] == QUESTIONS
    assert [a["choice"] for a in got["owner_answers"]] == ["keep"]
    # The step row's slimmed outcome keeps them: they are why a person reads it.
    assert [a["choice"] for a in compact._slim_outcome(got)["owner_answers"]] == ["keep"]


def test_the_follow_outcome_of_an_unanswered_task_carries_an_empty_list(swarm, world):
    world.task("task_a", state="SUCCEEDED")
    _finish(world, "task_a", answers=None)

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert got["owner_answers"] == []


def test_sc_task_prints_each_answer():
    task = {
        "id": "task_a",
        "state": "SUCCEEDED",
        "runner_profile": "mock",
        "created_at": "2026-10-13T12:00:00+00:00",
        "result_summary": {"questions": 1},
        "owner_answers": [ANSWER, {**ANSWER, "question_index": 1, "question": "Other?",
                                   "choice": None, "text": "neither"}],
    }

    lines = render_task(task, PLAIN, datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc))

    text = "\n".join(lines)
    assert "answered" in text
    assert "1. keep" in text and "one release, then drop it" in text
    assert "alice@saga.xyz" in text
    assert "2. neither" in text
