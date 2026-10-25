"""`swarm_result` says whether a browser task used its browser (BROWSER-USE-FLAG, part of #2322).

From the task read's `browser_used` and `browser_use` (`codec.browser_use_to_api`):
`browser: used (N calls, M images)`, `browser: NOT used`, and no `browser` key
at all for a task that is not a browser runtime. A browser task whose use was
not measured says `not measured` and why, never `NOT used`: an unread count
is not a zero.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp import progress, server

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures


def _finish(world: World, task_id: str, *, profile: str, summary: dict[str, Any]) -> None:
    world.task(task_id, state="SUCCEEDED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["runner_profile"] = profile
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    doc["result_summary"] = {"runner": {"summary": "done"}, "artifacts": [], "logs": {}, **summary}


def _result(swarm, task_id: str) -> dict[str, Any]:
    return json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))


def _counts(calls: int, images: int) -> dict[str, Any]:
    return {"tool_calls": calls, "mcp_calls": calls, "command_calls": 0, "images": images,
            "complete": True, "seconds": 700.0}


def test_a_browser_task_that_used_it_says_how(swarm, world):
    _finish(world, "task_b", profile="claude-code-browser",
            summary={"browser_used": True, "browser_use": _counts(14, 3)})
    # MUTATION: leave the line out of `_result_of` and this is a KeyError.
    assert _result(swarm, "task_b")["browser"] == "used (14 calls, 3 images)"


def test_a_browser_task_that_did_not_says_not_used(swarm, world):
    _finish(world, "task_n", profile="claude-code-browser",
            summary={"browser_used": False, "browser_use": _counts(0, 0)})
    assert _result(swarm, "task_n")["browser"] == "NOT used"


def test_a_non_browser_task_carries_no_browser_line(swarm, world):
    _finish(world, "task_c", profile="claude-code", summary={"browser_used": None})
    # MUTATION: print a line for every task and this key exists.
    assert "browser" not in _result(swarm, "task_c")


def test_an_unmeasured_browser_task_is_never_called_unused():
    task = {"runner_profile": "claude-code-browser", "state": "SUCCEEDED", "browser_used": None,
            "browser_use": {"unknown_because": "stream_unread"}}
    assert progress.browser_words(task) == "not measured (the agent's stream could not be read)"
    assert progress.browser_words({**task, "state": "RUNNING"}) is None
    one = {"runner_profile": "claude-code-browser", "browser_used": True,
           "browser_use": {"tool_calls": 1, "images": 0}}
    assert progress.browser_words(one) == "used (1 call, 0 images)"
