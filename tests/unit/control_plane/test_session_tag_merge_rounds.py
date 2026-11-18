"""The merge step's fix rounds count as their workflow's session's own
(SC-SESSION-SCOPE, owner decision 2026-10-14).

swarm-api's wake tick (`mergewake`) submits the merge step's CI-fix and
conflict-fix rounds as workflows of their own. Each copies the Claude Code
session its merge task was tagged with (`metadata.claude_session`, written
onto every step task from the workflow's metadata), so the sc plugin's
`/sc check`, `/sc attach --all` and watch treat them as that session's work.
The control: an untagged merge starts untagged rounds.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import uuid

import pytest

from swarm_api import sessiontag

from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    _conflict_workflows,
    _fix_workflows,
    _tick,
    clock,
    conflicting_merge,
    fixable_merge,
    github,
    tenant_tokens,
    wake_client,
)


def _round_task(db, workflow: dict) -> dict:
    (step,) = workflow["steps"]
    return db.docs[f"tasks/{step['task_id']}"]


@pytest.mark.parametrize("tagged", [True, False], ids=["tagged", "control-untagged"])
def test_a_ci_fix_round_inherits_its_merge_tasks_session(db, wake_client, github, tagged):
    sid = str(uuid.uuid4()) if tagged else None
    doc = fixable_merge(db, github, rounds=2)
    if sid:
        doc["metadata"][sessiontag.KEY] = sid

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _fix_workflows(db)
    assert _round_task(db, workflow)["metadata"].get(sessiontag.KEY) == sid


@pytest.mark.parametrize("tagged", [True, False], ids=["tagged", "control-untagged"])
def test_a_conflict_fix_round_inherits_its_merge_tasks_session(db, wake_client, github, tagged):
    sid = str(uuid.uuid4()) if tagged else None
    doc = conflicting_merge(db, github)
    if sid:
        doc["metadata"][sessiontag.KEY] = sid

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _conflict_workflows(db)
    assert _round_task(db, workflow)["metadata"].get(sessiontag.KEY) == sid
