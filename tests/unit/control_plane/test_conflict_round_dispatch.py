"""A merge step's conflict fix round is marked INSIDE the signed dispatch block (#1321).

The worker checks, before a conflict fix round pushes, that the round's
branch stands on the default branch's head (`gitops.verify_merged_default_branch`).
It may act only on metadata the step-spec signature covers
(`tests/unit/common/test_specsign_covers.py`), and `metadata.merge_conflict_round`
-- the record `mergewake._on_conflict` submits the round with -- is not. So
swarm-api copies the fact into `metadata.dispatch.conflict_round` at
submission, as it does `pr_label`: true on a continuation whose workflow
carries the round record, absent everywhere else.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from swarm_api import cifix, issueci, mergewake
from swarm_api.validation import (
    CONFLICT_ROUND_FIELD,
    CONFLICT_ROUND_METADATA_KEY,
    DispatchOptions,
)

from .conftest import auth_header, seed_task
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    CHECK, HEAD, MOVED, NOW, NUMBER, ROOT, _conflict_workflows, _fix_record, _fix_workflows,
    _tick, _wait, clock, conflicting_merge, fixable_merge, github, tenant_tokens, wake_client,
)

REPO = "https://github.com/example-org/example"
ROUND = {"merge_task": "task_" + "a" * 20, "round": 1, "head_sha": "0" * 40, "pull_request": 7}


def _submit_direct_pr_task(client) -> str:
    response = client.post("/v1/tasks", headers=auth_header("alice"), json={
        "runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO,
    })
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def _round_workflow(*, continues: str | None, metadata: dict) -> dict:
    body: dict = {
        "steps": [{"step_id": "conflict-fix", "runner_profile": "mock",
                   "input": {"prompt": "merge the base in"}}],
        "strategy": "direct-pr",
        "metadata": metadata,
    }
    if continues:
        body["continues_task"] = continues
    else:
        body["repository_url"] = REPO
    return body


def _dispatch(client, db, body: dict) -> dict:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    return db.docs[f"tasks/{step['task_id']}"]["metadata"]["dispatch"]


def test_a_conflict_round_continuation_is_marked_in_its_dispatch_block(client, db):
    original = _submit_direct_pr_task(client)
    dispatch = _dispatch(client, db, _round_workflow(
        continues=original, metadata={CONFLICT_ROUND_METADATA_KEY: dict(ROUND)},
    ))
    assert dispatch == {
        "strategy": "direct-pr",
        "carrier": "checkpoints",
        "continues": original,
        CONFLICT_ROUND_FIELD: True,
    }


def test_a_continuation_that_is_not_a_conflict_round_stores_the_block_it_did_before(client, db):
    original = _submit_direct_pr_task(client)
    dispatch = _dispatch(client, db, _round_workflow(continues=original, metadata={}))
    assert CONFLICT_ROUND_FIELD not in dispatch, dispatch


def test_the_record_on_a_workflow_that_continues_nothing_marks_nothing(client, db):
    """A round is always a continuation of the pull request's branch; the
    record on anything else is a caller's metadata and asks for nothing."""
    dispatch = _dispatch(client, db, _round_workflow(
        continues=None, metadata={CONFLICT_ROUND_METADATA_KEY: dict(ROUND)},
    ))
    assert CONFLICT_ROUND_FIELD not in dispatch, dispatch


def test_the_field_is_absent_unless_set():
    assert CONFLICT_ROUND_FIELD not in DispatchOptions().to_metadata()
    assert DispatchOptions(conflict_round=True).to_metadata()[CONFLICT_ROUND_FIELD] is True


def test_mergewake_submits_the_round_under_the_key_submission_reads():
    source = inspect.getsource(mergewake._on_conflict)
    assert "metadata={CONFLICT_ROUND_METADATA_KEY: {" in source, source


# ONE WRITER PER MERGE STEP'S PULL REQUEST (#2577). Measured: conflict-fix:1
# (task_78214f85) was claimed at a8cb2f5 and still RUNNING when the next tick
# read the pull request CONFLICTING at 40d1cf15. `_hand_off` waited only on a
# round at the current head, so it submitted conflict-fix:2 (task_33065a64) on
# the same branch; round 1 pushed its merge of main, and round 2's own merge
# commit could not be replayed onto it (publish_failed). A round of any kind
# whose task has not ended, at any head, is waited on: no second round, no
# re-run, no wake.

EARLIER = "task_" + "7" * 20


def _moved(github) -> None:
    """The pull request's head is now MOVED (B); the earlier round was at HEAD (A)."""
    github.pulls[NUMBER]["head"]["sha"] = MOVED


def _conflict_at_moved(db, github, **rounds) -> dict:
    """A merge parked conflict_fix_pending at MOVED, CONFLICTING there, with `rounds`."""
    doc = conflicting_merge(db, github, code="conflict_fix_pending", head=MOVED)
    _moved(github)
    doc["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = rounds
    return doc


def _round_task(db, state: str = "RUNNING", task_id: str = EARLIER) -> dict:
    return seed_task(db, task_id=task_id, tenant_id="eng", state=state,
                     runner_profile="claude-code")


def _assert_waited(db, body: dict, *, conflict_rounds: int) -> None:
    assert body["report"]["waiting"] == 1 and body["report"]["fixing"] == 0, body
    assert body["report"]["woken"] == 0, body
    assert _conflict_workflows(db) == [], "a second writer was submitted on a live round"
    assert len(_fix_record(db).get(mergewake.CONFLICT_ROUNDS_KEY) or []) == conflict_rounds
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert "wake_reason" not in _wait(db)


def test_a_conflict_round_live_at_an_earlier_head_is_waited_on_and_no_second_round_is_submitted(
    db, wake_client, github
):
    _round_task(db)
    _conflict_at_moved(db, github, **{mergewake.CONFLICT_ROUNDS_KEY: [
        {"round": 1, "head": HEAD, "claimed_at": NOW - timedelta(seconds=60),
         "task_id": EARLIER}]})

    body = _tick(wake_client).json()

    # MUTATION: wait only on a round at this head (the old rule) and this
    # tick claims and submits conflict-fix:2 beside the running round 1.
    _assert_waited(db, body, conflict_rounds=1)


def test_an_ended_round_at_an_earlier_head_lets_the_next_round_be_submitted(
    db, wake_client, github
):
    _round_task(db, state="SUCCEEDED")
    _conflict_at_moved(db, github, **{mergewake.CONFLICT_ROUNDS_KEY: [
        {"round": 1, "head": HEAD, "claimed_at": NOW - timedelta(seconds=60),
         "task_id": EARLIER}]})

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _conflict_workflows(db)
    first, second = _fix_record(db)[mergewake.CONFLICT_ROUNDS_KEY]
    assert first["task_id"] == EARLIER
    assert second["round"] == 2 and second["head"] == MOVED
    assert second["workflow_id"] == workflow["workflow_id"]


def test_a_live_ci_fix_round_at_another_head_blocks_a_conflict_round(db, wake_client, github):
    _round_task(db)
    _conflict_at_moved(db, github, rounds=[
        {"round": 1, "head": HEAD, "claimed_at": NOW - timedelta(seconds=60),
         "task_id": EARLIER}])

    body = _tick(wake_client).json()

    _assert_waited(db, body, conflict_rounds=0)


@pytest.mark.parametrize("entry, blocks", [
    ({"claimed_at": NOW - timedelta(seconds=issueci.LOST_ROUND_SECONDS - 1)}, True),
    ({"claimed_at": NOW - timedelta(seconds=issueci.LOST_ROUND_SECONDS + 1)}, False),
    ({"claimed_at": NOW - timedelta(seconds=5), "error": "refused"}, False),
], ids=["inside-window", "past-window", "errored"])
def test_an_unrecorded_claim_blocks_only_inside_the_lost_round_window(
    db, wake_client, github, entry, blocks
):
    _conflict_at_moved(db, github, **{mergewake.CONFLICT_ROUNDS_KEY: [
        {"round": 1, "head": HEAD, **entry}]})

    body = _tick(wake_client).json()

    if blocks:
        _assert_waited(db, body, conflict_rounds=1)
    else:
        assert body["report"]["fixing"] == 1, body
        assert len(_conflict_workflows(db)) == 1
        assert [r["round"] for r in _fix_record(db)[mergewake.CONFLICT_ROUNDS_KEY]] == [1, 2]


def test_a_live_pr_side_writer_blocks_a_merge_round(db, wake_client, github):
    writer = _round_task(db)
    writer["metadata"] = {"dispatch": {"continues": ROOT},
                          cifix.CONFLICT_FIX_METADATA_KEY: {"pull_request": NUMBER,
                                                            "head_sha": HEAD}}
    _conflict_at_moved(db, github)

    body = _tick(wake_client).json()

    # MUTATION: read only the merge's own record and the shepherd's conflict
    # fix, live on the same branch, gets a merge conflict round beside it.
    _assert_waited(db, body, conflict_rounds=0)


def test_a_red_reading_with_a_live_round_elsewhere_neither_reruns_nor_submits(
    db, wake_client, github
):
    _round_task(db)
    doc = fixable_merge(db, github, rounds=2, head=MOVED)
    _moved(github)
    github.check(MOVED, CHECK, "failure", output={"title": "1 failed", "summary": "failed"})
    doc["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = {mergewake.CONFLICT_ROUNDS_KEY: [
        {"round": 1, "head": HEAD, "claimed_at": NOW - timedelta(seconds=60),
         "task_id": EARLIER}]}

    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1, body
    assert body["report"]["fixing"] == 0 and body["report"]["rerun"] == 0, body
    assert _fix_workflows(db) == []
    record = _fix_record(db)
    assert mergewake.RERUNS_KEY not in record, "a re-run was asked beside a live round"
    assert "rounds" not in record
    assert "wake_reason" not in _wait(db)
