"""A SINGLE task with `publish_to_pr` is a follow-up too, and its heads are recorded.

docs/design/merging-without-a-laptop.md §3.1-§3.2. The owner, 13 November:
"workflows now, single tasks next". `cifix.stamp` marked only a
`publish_to_pr` WORKFLOW `pr_fix_head: {kind: "follow_up"}`; a single task
sent through `POST /v1/tasks` (`swarm_dispatch`) with `publish_to_pr` went
unmarked, so the merge-wake tick never recorded what it pushed. That night
two merge steps refused such heads `head_moved`. `cifix.stamp_task` now
marks the single task with the same marker, and the tick records its
`pushed_head` and `started_head` against the branch task exactly as it
records a workflow follow-up's (test_follow_up_heads_recorded.py). A single
task without `publish_to_pr` is not marked, and a caller may not send the
marker itself.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

from swarm_api import cifix

from .conftest import auth_header
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    PR,
    PUSHED,
    REPO_URL,
    _finish,
    _original,
    _tick,
    clock,
    fix_client,
    github,
    tenant_tokens,
)
from .test_follow_up_heads_recorded import STARTED, _git, _heads, _open_lane_pull


def _single(**overrides: Any) -> dict[str, Any]:
    """What `swarm_dispatch` sends for one task publishing onto pull request PR."""
    spec = {"runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO_URL,
            "publish_to_pr": PR, "input": {"prompt": "apply the review"}}
    spec.update(overrides)
    return spec


def _submit_task(client, payload: dict[str, Any], user: str = "alice"):
    return client.post("/v1/tasks", headers=auth_header(user), json=payload)


def _task_of(db, response) -> dict[str, Any]:
    assert response.status_code == 201, response.text
    return db.docs[f"tasks/{response.json()['task']['id']}"]


def test_a_single_task_publishing_onto_a_pull_request_is_marked_a_follow_up(
        fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    task = _task_of(db, _submit_task(fix_client, _single()))

    assert task.get("workflow_id") is None
    assert task["metadata"][cifix.FIX_HEAD_FIELD] == {
        "kind": cifix.FOLLOW_UP, "pull_request": PR, "state": cifix.PENDING,
    }
    assert task["metadata"]["dispatch"]["publish_to_pr"] == {"number": PR, "task_id": original}


def test_a_succeeded_single_follow_ups_heads_are_recorded(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    task = _task_of(db, _submit_task(fix_client, _single()))

    _finish(task, result_summary=_git(original))
    tick = _tick(fix_client)
    assert tick.status_code == 200, tick.text
    assert tick.json()["ci_fix"]["heads_recorded"] == 1

    (entry,) = _heads(db, original)
    assert entry["kind"] == cifix.FOLLOW_UP
    assert entry["task_id"] == task["id"]
    assert entry["workflow_id"] is None
    assert entry["pull_request"] == PR
    assert entry["head"] == PUSHED
    assert entry["started_head"] == STARTED
    assert task["metadata"][cifix.FIX_HEAD_FIELD]["state"] == cifix.RECORDED


def test_a_failed_single_follow_up_is_not_recorded(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    task = _task_of(db, _submit_task(fix_client, _single()))
    _finish(task, "FAILED", result_summary=_git(original), last_error="tests failed")

    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []
    assert task["metadata"][cifix.FIX_HEAD_FIELD]["state"] == cifix.NOT_SUCCEEDED


def test_a_single_task_without_publish_to_pr_is_not_marked(fix_client, db):
    task = _task_of(db, _submit_task(fix_client, {
        "runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO_URL}))
    assert cifix.FIX_HEAD_FIELD not in task["metadata"]


def test_a_caller_cannot_send_the_marker_on_a_single_task(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    for key in cifix.API_KEYS:
        response = _submit_task(fix_client, _single(metadata={key: {
            "kind": cifix.FOLLOW_UP, "pull_request": PR, "state": cifix.PENDING}}))
        assert response.status_code == 422, (key, response.text)
