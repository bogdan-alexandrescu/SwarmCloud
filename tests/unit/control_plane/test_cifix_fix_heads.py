"""Every platform fix round's pushed head is recorded against its PULL REQUEST (epic #1334).

Measured 2026-10-15 02:16Z: HANG-LEASE's merge step refused PR 1338
`head_moved` at 34ce829e, pushed by fix task task_fcdff76b of ANOTHER
workflow (wf_87a59d23). A merge step knew only its own rounds' heads
(`merge_fix`) and a query keyed on the `ci_fix` record; a head any other
round pushed to the same pull request was nobody's.

So `stamp` marks every fix round as it is submitted -- a PR-side CI fix
(`metadata.ci_fix`), a merge step's CI-fix round (`merge_fix_round`), a
conflict fix round (`merge_conflict_round`) -- and the merge-wake tick,
once that task is terminal, appends what it pushed to `FIX_HEADS_FIELD` on
the task whose `swarm/<id>` branch the pull request is on, keyed by the
repository and the pull request's number, never by a workflow. The merge
step reads it there (tests/unit/worker/test_merge_recorded_fix_heads.py).

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import cifix

from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    HEAD,
    PR,
    PUSHED,
    _finish,
    _fix_task,
    _members_hold_grants,
    _original,
    _red_run_payload,
    _submit,
    _tick,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

REPOSITORY = "example-org/widgets"


def _round_payload(continues: str, key: str) -> dict[str, Any]:
    """A merge step's fix round, in the shape mergewake submits it."""
    return {
        "steps": [{"step_id": "fix", "runner_profile": "mock", "input": {"prompt": "fix it"}}],
        "strategy": "direct-pr",
        "continues_task": continues,
        "metadata": {key: {"merge_task": "task_" + "a" * 20, "round": 1,
                           "head_sha": HEAD, "pull_request": PR}},
    }


def _heads(db, root: str) -> list[dict[str, Any]]:
    return list((db.docs[f"tasks/{root}"].get("metadata") or {}).get(cifix.FIX_HEADS_FIELD) or [])


@pytest.mark.parametrize("kind", ["ci_fix", "merge_fix_round", "merge_conflict_round"])
def test_a_fix_rounds_pushed_head_is_recorded_against_the_pull_request(fix_client, db, github, kind):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")
    payload = (_red_run_payload(original) if kind == "ci_fix"
               else _round_payload(original, kind))
    response = _submit(fix_client, payload)
    assert response.status_code == 201, response.text
    task = _fix_task(db, response)
    assert task["metadata"][cifix.FIX_HEAD_FIELD]["state"] == cifix.PENDING

    # Running: nothing pushed yet, nothing recorded.
    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []

    _finish(task, result_summary={"git": {"pushed_head": PUSHED}})
    tick = _tick(fix_client)
    assert tick.status_code == 200, tick.text
    assert tick.json()["ci_fix"]["heads_recorded"] == 1

    (entry,) = _heads(db, original)
    assert entry["repository"] == REPOSITORY
    assert entry["pull_request"] == PR
    assert entry["task_id"] == task["id"]
    assert entry["workflow_id"] == task["workflow_id"]
    assert entry["head"] == PUSHED
    assert entry["kind"] == kind
    assert db.docs[f"tasks/{task['id']}"]["metadata"][cifix.FIX_HEAD_FIELD]["state"] == cifix.RECORDED

    # A second tick records nothing twice.
    assert _tick(fix_client).status_code == 200
    assert len(_heads(db, original)) == 1


def test_a_round_that_pushed_nothing_records_nothing(fix_client, db, github):
    original = _original(fix_client)
    task = _fix_task(db, _submit(fix_client, _round_payload(original, "merge_fix_round")))
    _finish(task, "FAILED", last_error="evicted")

    assert _tick(fix_client).status_code == 200
    assert _heads(db, original) == []
    marker = db.docs[f"tasks/{task['id']}"]["metadata"][cifix.FIX_HEAD_FIELD]
    assert marker["state"] == cifix.NOTHING_PUSHED


def test_only_the_named_tenants_rounds_are_recorded(fix_client, db, github):
    original = _original(fix_client)
    task = _fix_task(db, _submit(fix_client, _round_payload(original, "merge_fix_round")))
    _finish(task, result_summary={"git": {"pushed_head": PUSHED}})

    assert _tick(fix_client, "research").status_code == 200
    assert _heads(db, original) == []
    assert _tick(fix_client, "eng").status_code == 200
    assert len(_heads(db, original)) == 1


def test_a_workflow_with_no_fix_round_is_not_marked(fix_client, db):
    original = _original(fix_client)
    response = _submit(fix_client, {
        "steps": [{"step_id": "more", "runner_profile": "mock"}],
        "strategy": "direct-pr", "continues_task": original,
    })
    assert response.status_code == 201, response.text
    assert cifix.FIX_HEAD_FIELD not in _fix_task(db, response)["metadata"]


@pytest.mark.parametrize("where", ["workflow", "step"])
def test_a_caller_cannot_send_the_marker_or_the_record(fix_client, db, where):
    original = _original(fix_client)
    for key in (cifix.FIX_HEAD_FIELD, cifix.FIX_HEADS_FIELD):
        payload = _round_payload(original, "merge_fix_round")
        forged = {key: {"state": cifix.PENDING}}
        if where == "workflow":
            payload["metadata"].update(forged)
        else:
            payload["steps"][0]["metadata"] = forged
        response = _submit(fix_client, payload)
        assert response.status_code == 422, (key, response.text)
        assert response.json()["code"] == "invalid_dispatch"


def test_the_record_keeps_the_newest_entries_only(fix_client, db, github):
    original = _original(fix_client)
    root = db.docs[f"tasks/{original}"]
    root.setdefault("metadata", {})[cifix.FIX_HEADS_FIELD] = [
        {"repository": REPOSITORY, "pull_request": PR, "task_id": f"task_{i:020x}",
         "head": f"{i:040x}", "workflow_id": "wf_old", "kind": "ci_fix"}
        for i in range(cifix.FIX_HEADS_KEPT)
    ]
    task = _fix_task(db, _submit(fix_client, _round_payload(original, "merge_fix_round")))
    _finish(task, result_summary={"git": {"pushed_head": PUSHED}})

    assert _tick(fix_client).status_code == 200
    heads = _heads(db, original)
    assert len(heads) == cifix.FIX_HEADS_KEPT
    assert heads[-1]["task_id"] == task["id"], "the newest is last"
    assert heads[0]["task_id"] == f"task_{1:020x}", "the oldest was dropped"
