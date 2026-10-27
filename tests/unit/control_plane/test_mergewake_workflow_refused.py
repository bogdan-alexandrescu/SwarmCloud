"""The wake tick sends no CI-fix round to a merge parked for a workflow patch (#2046).

A merge step whose head was written by a publish swarm-api refused
`workflow_scope_missing` parks `workflow_patch_pending`
(`agent_worker.merge.WORKFLOW_PATCH_PENDING`): CI is red because the branch
lacks the workflow lines left in `workflow-lines.patch`, and only an operator
can push them. The tick must therefore not read that red reading as one a
round may fix -- every round so far was refused the same push (task_58815d23,
task_d91ca0c9, task_0519f290) -- and must wake the step once the head moves,
which is the operator's push landing. Until then the park is a wait whose
checks are not read, as a pending branch update's is.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import timedelta

from agent_worker import merge
from swarm_api import issueci, mergewake
from swarm_common.states import ParkReason

from .test_merge_wake import (  # noqa: F401 - the fixtures are used by name
    CHECK, HEAD, MOVED, NOW, NUMBER, _fix_record, _fix_workflows, _reads, _tick, _wait,
    clock, fixable_merge, github, tenant_tokens, wake_client,
)

PATCH_PENDING = "workflow_patch_pending"


def test_the_park_code_is_the_workers():
    assert mergewake.WORKFLOW_PATCH_PENDING == merge.WORKFLOW_PATCH_PENDING == PATCH_PENDING


def test_red_on_a_workflow_patch_park_submits_no_round(db, wake_client, github):
    """Rounds left and CI red: without the code, `_on_red` claims round 1."""
    fixable_merge(db, github, rounds=2, code=PATCH_PENDING, pending=[])

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 0, body
    assert _fix_workflows(db) == []
    assert _fix_record(db).get("rounds") is None
    assert body["report"]["waiting"] == 1 and body["report"]["woken"] == 0, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    task = db.docs["tasks/task_merge"]
    assert task["state"] == "PARKED" and task["park_reason"] == ParkReason.CI_PENDING.value
    # Nothing the park waits on is a check: none is read, none is re-run.
    assert not [u for u in _reads(github) if "/check-runs" in u or "/status" in u]


def test_the_operators_push_wakes_the_step(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=2, code=PATCH_PENDING, pending=[])
    _tick(wake_client)

    github.pulls[NUMBER]["head"]["sha"] = MOVED
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "head_moved"
    assert _fix_workflows(db) == []


def test_a_red_ci_park_still_gets_its_round(db, wake_client, github):
    """The control: the same red merge on the ordinary CI code."""
    fixable_merge(db, github, rounds=2, code=merge.CI_FIX_PENDING, pending=[CHECK])

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    assert len(_fix_workflows(db)) == 1
    assert HEAD == _fix_record(db)["rounds"][0]["head"]
