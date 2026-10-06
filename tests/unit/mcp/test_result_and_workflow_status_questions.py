"""`swarm_result` carries `owner_questions`; `swarm_workflow_status` a per-step `questions` count.

Owner decision 2026-10-06 (observer P15). The worker validates an agent's
`$SWARM_ARTIFACTS_DIR/questions.json` and counts it on the task as
`result_summary.questions`; before this, only the plugin's progress row showed
the questions. Now:

* `swarm_result` carries `owner_questions` -- the validated items, read back
  through the redacted artifacts route by `progress.owner_questions` -- when
  the task asked any, and no such key when it asked none;
* `swarm_workflow_status` carries `questions`, the worker's count, on every
  step whose task was read, so a workflow says which step is waiting on the
  owner without a result read per step;
* a file the worker rejected, or one the API serves as something other than a
  JSON list, is never served as questions.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp import server

from control_plane.conftest import PROJECT
from test_follow_cursor import AUTH, NOW, TENANT, World, swarm, world  # noqa: F401 - fixtures

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
        "context": "The bridge is pinned by tag, so a reader moves with the plugin.",
    },
]


def _finish(world: World, task_id: str, *, body: str | None, count: int,
            rejected: str | None = None) -> None:
    """Mark `task_id` SUCCEEDED with `body` as its questions.json, as the worker leaves it."""
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["state"] = "SUCCEEDED"
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    entries = []
    if body is not None:
        key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_1/artifacts/{NAME}"
        world.objects.put(key, body)
        entries.append({"name": NAME, "bytes": len(body.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"})
    summary: dict[str, Any] = {
        "runner": {"summary": "done"},
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
        "questions": count,
    }
    if rejected is not None:
        summary["questions_rejected"] = rejected
    doc["result_summary"] = summary


def _result(swarm, task_id: str) -> dict[str, Any]:
    return json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))


def _no_read(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("questions.json must not be read for a task the worker counted none in")


# -- swarm_result ---------------------------------------------------------------

def test_swarm_result_carries_the_owner_questions(swarm, world):
    world.task("task_q", state="SUCCEEDED")
    _finish(world, "task_q", body=json.dumps(QUESTIONS), count=1)

    reply = _result(swarm, "task_q")

    assert reply["owner_questions"] == QUESTIONS
    assert "questions_unavailable_because" not in reply
    # Still listed as an artifact, which is where a person reads the raw file.
    assert NAME in json.dumps(reply["outputs"])


def test_swarm_result_of_a_task_without_questions_carries_none(swarm, world, monkeypatch):
    world.task("task_n", state="SUCCEEDED")
    _finish(world, "task_n", body=None, count=0)
    monkeypatch.setattr(swarm, "artifact_content", _no_read)

    reply = _result(swarm, "task_n")

    assert "owner_questions" not in reply
    assert "questions_unavailable_because" not in reply


def test_a_file_the_worker_rejected_is_not_served(swarm, world, monkeypatch):
    # The agent wrote a file the worker refused: uploaded as an artifact, counted 0.
    world.task("task_r", state="SUCCEEDED")
    _finish(
        world, "task_r",
        body=json.dumps([{"question": "no options at all"}]),
        count=0,
        rejected="item 0: options must be a non-empty list",
    )
    monkeypatch.setattr(swarm, "artifact_content", _no_read)

    reply = _result(swarm, "task_r")

    assert "owner_questions" not in reply


def test_a_counted_file_that_is_not_a_json_list_is_not_served(swarm, world):
    world.task("task_bad", state="SUCCEEDED")
    _finish(world, "task_bad", body=json.dumps({"question": "an object, not a list"}), count=1)

    reply = _result(swarm, "task_bad")

    assert "owner_questions" not in reply
    assert "not a JSON list" in reply["questions_unavailable_because"], reply


def test_swarm_result_says_owner_questions_in_its_description():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_result")
    assert "`owner_questions`" in tool["description"]


# -- swarm_workflow_status --------------------------------------------------------

def _workflow(world: World) -> tuple[str, dict[str, str]]:
    created = world.api.post(
        "/v1/workflows",
        json={
            "steps": [
                {"step_id": "asks", "runner_profile": "mock", "input": {"prompt": "a"}},
                {"step_id": "quiet", "runner_profile": "mock", "input": {"prompt": "b"}},
            ]
        },
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    return workflow["workflow_id"], {s["step_id"]: s["task_id"] for s in workflow["steps"]}


def test_swarm_workflow_status_counts_each_steps_questions(swarm, world, monkeypatch):
    workflow_id, task_ids = _workflow(world)
    _finish(world, task_ids["asks"], body=json.dumps(QUESTIONS * 2), count=2)
    _finish(world, task_ids["quiet"], body=None, count=0)
    # A count, never a read: the status costs no artifact read per step.
    monkeypatch.setattr(swarm, "artifact_content", _no_read)

    report = json.loads(server._call(swarm, "swarm_workflow_status", {"workflow_id": workflow_id}))

    rows = {row["step_id"]: row for row in report["steps"]}
    assert rows["asks"]["questions"] == 2, rows["asks"]
    assert rows["quiet"]["questions"] == 0, rows["quiet"]


def test_swarm_workflow_status_says_questions_in_its_description():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_workflow_status")
    assert "`questions`" in tool["description"]
