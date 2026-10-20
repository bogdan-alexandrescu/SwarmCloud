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

from swarm_api import mergewake
from swarm_api.validation import (
    CONFLICT_ROUND_FIELD,
    CONFLICT_ROUND_METADATA_KEY,
    DispatchOptions,
)

from .conftest import auth_header

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
