"""#295's three profiles are refused on every submission path until the owner enables them.

The owner accepted contract requests 33, 35 and 36 on 2026-10-01 and decided
in the same breath that the merge chain stays disabled for every tenant until
signed step specs (#342) are enforced and the review and merge GitHub Apps
exist. A catalogue entry is dispatchable the moment it merges unless
submission refuses it, and a workflow step is a second way in that a test of
`POST /v1/tasks` alone would not see.

Nothing else holds the line meanwhile: the lifecycle does not branch on
`worker_action` or read `never_restore_checkpoint` yet (merge-step.md §10
items 1 and 4a), and Terraform creates no Job for any of the three.

FAILS WITHOUT THE CHANGE: before it, the three names are unknown, and the
refusal says "unknown runner_profile" rather than that the profile is
disabled and why, which is what is asserted here.
"""

from __future__ import annotations

import pytest

from .conftest import auth_header

#: `merge` left this list on 2026-10-04: contract request 47 enabled it on the
#: tenant's `-git` token (test_merge_step_submission.py holds where it may run).
MERGE_CHAIN = ("post-verdict", "claude-code-review")


def test_merge_is_no_longer_refused_as_disabled(client):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "merge", "input": {}}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"].get("disabled") is not True


def _assert_refused_as_disabled(response, name):
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["runner_profile"] == name, detail
    assert detail["disabled"] is True, detail
    assert "#342" in detail["reason"], detail
    assert name not in detail["known_runner_profiles"], detail


@pytest.mark.parametrize("name", MERGE_CHAIN)
def test_a_task_naming_a_merge_chain_profile_is_refused_as_disabled(client, name):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": name, "input": {}}
    )
    _assert_refused_as_disabled(response, name)


@pytest.mark.parametrize("name", MERGE_CHAIN)
def test_a_workflow_step_naming_a_merge_chain_profile_is_refused_as_disabled(client, name):
    body = {
        "steps": [
            {"step_id": "implement", "runner_profile": "mock", "input": {}, "depends_on": []},
            {"step_id": "chain", "runner_profile": name, "input": {}, "depends_on": ["implement"]},
        ],
    }
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    _assert_refused_as_disabled(response, name)
