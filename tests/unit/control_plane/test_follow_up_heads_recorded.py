"""A `publish_to_pr` follow-up's pushed and started heads are recorded (lane 1).

docs/design/merging-without-a-laptop.md §3.1-§3.2, §11 lane 1. A follow-up
lane (`publish_to_pr: N`, #2183) pushes onto the pull request a branch task
opened, and the merge step then refused the new head `head_moved`: nothing
recorded it. `cifix.stamp` now marks such a workflow's tasks
`pr_fix_head: {kind: "follow_up", state: "pending"}`, as it marks a fix
round, and the merge-wake tick appends a SUCCEEDED follow-up's
`pushed_head` and the `started_head` it fast-forwarded from to the branch
task's `pull_request_fix_heads`. A follow-up that ended FAILED or
CANCELLED records nothing, even if it pushed; so does one whose own publish
record names another pull request or branch, or no `started_head`, and one
of another tenant. The record is an index the merge step checks against the
signed task (lane 2), never the proof.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import cifix

from .conftest import seed_task
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    HEAD,
    NOW,
    PR,
    PUSHED,
    REPO_URL,
    _finish,
    _original,
    _submit,
    _tick,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

REPOSITORY = "example-org/widgets"
STARTED = "b" * 40


def _open_lane_pull(github, original: str, number: int = PR) -> None:
    """Pull request `number`, on `swarm/<original>`, of the spec's own repository."""
    github.pulls[number] = {
        "number": number, "html_url": f"{REPO_URL}/pull/{number}", "title": "lane",
        "state": "open", "merged": False, "body": "",
        "head": {"sha": HEAD, "ref": f"swarm/{original}", "repo": {"full_name": REPOSITORY}},
        "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
    }


def _follow_up(**overrides: Any) -> dict[str, Any]:
    spec = {
        "strategy": "direct-pr", "repository_url": REPO_URL, "publish_to_pr": PR,
        "steps": [{"step_id": "follow-up", "runner_profile": "mock",
                   "input": {"prompt": "apply the review"}}],
    }
    spec.update(overrides)
    return spec


def _tasks_of(db, response) -> list[dict[str, Any]]:
    assert response.status_code == 201, response.text
    return [db.docs[f"tasks/{step['task_id']}"] for step in response.json()["workflow"]["steps"]]


def _git(original: str, *, pushed: str | None = PUSHED, started: str | None = STARTED,
         number: int = PR, branch: str | None = None) -> dict[str, Any]:
    """`result_summary.git` as the worker writes it for a `publish_to_pr` push."""
    record: dict[str, Any] = {"number": number, "branch": branch or f"swarm/{original}"}
    if started is not None:
        record["started_head"] = started
    git: dict[str, Any] = {"published": pushed is not None, "publish_to_pr": record}
    if pushed is not None:
        git["pushed_head"] = pushed
    return {"git": git}


def _heads(db, root: str) -> list[dict[str, Any]]:
    return list((db.docs[f"tasks/{root}"].get("metadata") or {}).get(cifix.FIX_HEADS_FIELD) or [])


def _marker(db, task: dict[str, Any]) -> dict[str, Any]:
    return db.docs[f"tasks/{task['id']}"]["metadata"][cifix.FIX_HEAD_FIELD]


def test_a_follow_up_is_marked_at_submission(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    (task,) = _tasks_of(db, _submit(fix_client, _follow_up()))

    assert task["metadata"][cifix.FIX_HEAD_FIELD] == {
        "kind": cifix.FOLLOW_UP, "pull_request": PR, "state": cifix.PENDING,
    }
    assert task["metadata"]["dispatch"]["publish_to_pr"] == {"number": PR, "task_id": original}


def test_a_succeeded_follow_ups_pushed_and_started_heads_are_recorded(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    (task,) = _tasks_of(db, _submit(fix_client, _follow_up()))

    # Running: nothing recorded yet.
    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []

    _finish(task, result_summary=_git(original))
    tick = _tick(fix_client)
    assert tick.status_code == 200, tick.text
    assert tick.json()["ci_fix"]["heads_recorded"] == 1

    (entry,) = _heads(db, original)
    assert entry["kind"] == cifix.FOLLOW_UP
    assert entry["task_id"] == task["id"]
    assert entry["workflow_id"] == task["workflow_id"]
    assert entry["repository"] == REPOSITORY
    assert entry["pull_request"] == PR
    assert entry["head"] == PUSHED
    assert entry["started_head"] == STARTED
    assert _marker(db, task)["state"] == cifix.RECORDED

    # A second tick records nothing twice.
    assert _tick(fix_client).status_code == 200
    assert len(_heads(db, original)) == 1


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_a_follow_up_that_did_not_succeed_is_not_recorded_even_if_it_pushed(
        fix_client, db, github, state):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    (task,) = _tasks_of(db, _submit(fix_client, _follow_up()))
    _finish(task, state, result_summary=_git(original), last_error="tests failed")

    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []
    assert _marker(db, task)["state"] == cifix.NOT_SUCCEEDED


def test_a_follow_up_that_pushed_nothing_is_not_recorded(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    (task,) = _tasks_of(db, _submit(fix_client, _follow_up()))
    _finish(task, result_summary=_git(original, pushed=None))

    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []
    assert _marker(db, task)["state"] == cifix.NOTHING_PUSHED


@pytest.mark.parametrize("git", [
    pytest.param(lambda o: _git(o, started=None), id="no-started-head"),
    pytest.param(lambda o: _git(o, started="not-a-sha"), id="malformed-started-head"),
    pytest.param(lambda o: _git(o, number=PR + 1), id="another-pull-request"),
    pytest.param(lambda o: _git(o, branch="swarm/task_" + "e" * 20), id="another-branch"),
    pytest.param(lambda o: {"git": {"pushed_head": PUSHED}}, id="no-publish-record"),
])
def test_a_push_not_proven_onto_this_pull_request_is_not_recorded(fix_client, db, github, git):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    (task,) = _tasks_of(db, _submit(fix_client, _follow_up()))
    _finish(task, result_summary=git(original))

    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []
    assert _marker(db, task)["state"] == cifix.UNADDRESSABLE


def test_only_the_integrate_step_that_pushed_is_recorded(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    implement, fix = _tasks_of(db, _submit(fix_client, _follow_up(
        strategy="integrate",
        steps=[
            {"step_id": "implement", "runner_profile": "mock", "input": {"prompt": "a"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input": {"prompt": "b"}},
        ],
    )))
    _finish(implement, result_summary={"git": {"published": False}})
    _finish(fix, result_summary=_git(original))

    assert _tick(fix_client).status_code == 200
    assert [e["task_id"] for e in _heads(db, original)] == [fix["id"]]
    assert _marker(db, implement)["state"] == cifix.NOTHING_PUSHED


def test_only_the_named_tenants_follow_ups_are_recorded(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    (task,) = _tasks_of(db, _submit(fix_client, _follow_up()))
    _finish(task, result_summary=_git(original))

    assert _tick(fix_client, "research").status_code == 200
    assert _heads(db, original) == []
    assert _marker(db, task)["state"] == cifix.PENDING
    assert _tick(fix_client, "eng").status_code == 200
    assert len(_heads(db, original)) == 1


def test_a_follow_up_naming_another_tenants_branch_task_writes_nothing_there(fix_client, db):
    """A document of tenant `research` whose dispatch names eng's branch task
    (submission refuses it; a forged document is still never written across)."""
    original = _original(fix_client)
    other = "task_" + "f" * 20
    doc = seed_task(db, task_id=other, tenant_id="research", state="SUCCEEDED")
    doc.update(
        repository_url=REPO_URL, completed_at=NOW, result_summary=_git(original),
        metadata={"dispatch": {"publish_to_pr": {"number": PR, "task_id": original}},
                  cifix.FIX_HEAD_FIELD: {"kind": cifix.FOLLOW_UP, "pull_request": PR,
                                         "state": cifix.PENDING}},
    )

    assert _tick(fix_client, "research").status_code == 200
    assert _heads(db, original) == []
    assert db.docs[f"tasks/{other}"]["metadata"][cifix.FIX_HEAD_FIELD]["state"] == cifix.UNADDRESSABLE


def test_a_workflow_without_publish_to_pr_is_not_marked(fix_client, db):
    (task,) = _tasks_of(db, _submit(fix_client, {
        "strategy": "direct-pr", "repository_url": REPO_URL,
        "steps": [{"step_id": "work", "runner_profile": "mock"}],
    }))
    assert cifix.FIX_HEAD_FIELD not in task["metadata"]


def test_a_caller_cannot_send_the_marker_beside_publish_to_pr(fix_client, db, github):
    original = _original(fix_client)
    _open_lane_pull(github, original)
    response = _submit(fix_client, _follow_up(metadata={cifix.FIX_HEAD_FIELD: {
        "kind": cifix.FOLLOW_UP, "pull_request": PR, "state": cifix.PENDING}}))
    assert response.status_code == 422, response.text
