"""A CI fix for a pull request waits while a plain continuation of its lane is live (#2583).

Measured 2026-10-27 on PR #2570: the CI fixer's attempt 2 (task_7d270954,
wf_68cfa3f0) was admitted at 16:26:01Z, two minutes after the `implement`
step task_8017fba2 (wf_1a460a31) had started at 16:24:06Z on the same red
head 7e55a8d. Both fixed the same catalogue.tftest.hcl count; the fixer
pushed f4f93dc first, and the implement step's 88-minute publish was refused
`head_moved`. #2554 (`_refuse_if_fix_live`) holds only the other order: a
plain continuation submitted while a fix is live. `cifix.stamp`'s ci_fix
branch read a live merge step, issue run, conflict fix and claim, never a
plain continuation, which carries no writer mark for `live_pr_writer` to see.

So `stamp` refuses a `metadata.ci_fix` submission while a non-terminal task
of the tenant continues the same branch task (`metadata.dispatch.continues`)
or pushes onto it with `publish_to_pr` (`metadata.dispatch.publish_to_pr.task_id`),
is not a merge step, and carries no writer mark of its own. The refusal is
the one scripts/ci-fix.sh already reads as a skip, 409 `ci_fix_lane_owns_pr`,
with `plain_continuation: true`. No task and no claim is made, and the
refused request does not count against the cap.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import pytest

from swarm_api import cifix, refusals
from swarm_api.schedtypes import pr_shepherd
from swarm_api.validation import CONFLICT_ROUND_METADATA_KEY

from .conftest import seed_task
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    PR,
    _ci_fix_tasks,
    _integrator,
    _members_hold_grants,
    _red_run_payload,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}
LANE_TASK = "task_" + "e5" * 10
LANE_WORKFLOW = "wf_" + "e5" * 4
SWITCH = refusals.env_name("ci_fix_lane_owns_pr")


def _continuation(db, root: str, *, state: str = "RUNNING", tenant_id: str = "eng",
                  runner_profile: str = "claude-code", publish_to_pr: bool = False,
                  **marks) -> dict:
    """A lane's step pushing to `swarm/<root>`: a plain `continues_task` one,
    or (`publish_to_pr`) one pushing onto the pull request that branch opened."""
    doc = seed_task(db, task_id=LANE_TASK, tenant_id=tenant_id, state=state,
                    runner_profile=runner_profile, workflow_id=LANE_WORKFLOW)
    dispatch = {"strategy": "direct-pr"}
    if publish_to_pr:
        dispatch["publish_to_pr"] = {"number": PR, "task_id": root}
    else:
        dispatch["continues"] = root
    db.docs[f"tasks/{LANE_TASK}"]["metadata"] = {"dispatch": dispatch, **marks}
    return doc


def _fix(client, continues: str, **record):
    return client.post("/v1/workflows", headers=FIXER_HEADERS,
                       json=_red_run_payload(continues, **record))


def _claims(db) -> list[str]:
    return [key for key in db.docs if key.startswith(f"{cifix.CLAIMS}/")]


@pytest.fixture(autouse=True)
def _shipped_position(monkeypatch):
    """The switch as it ships: nothing in the environment."""
    monkeypatch.delenv(SWITCH, raising=False)


def _assert_refused_naming_the_lane(response) -> None:
    # MUTATION: drop the plain-continuation read from `stamp` -- 201, and the
    # fixer and the lane clone one head, as on PR #2570.
    assert response.status_code == 409, response.text
    body = response.json()
    # MUTATION: a new refusal code -- scripts/ci-fix.sh reads only this one as a skip.
    assert body["code"] == "ci_fix_lane_owns_pr"
    assert body["detail"]["task_id"] == LANE_TASK
    assert body["detail"]["workflow_id"] == LANE_WORKFLOW
    assert body["detail"]["pull_request"] == PR
    assert body["detail"]["plain_continuation"] is True
    assert LANE_TASK in body["message"]


@pytest.mark.parametrize("state", ["RUNNING", "QUEUED"])
def test_a_ci_fix_is_refused_while_a_plain_continuation_of_the_root_is_live(fix_client, db, state):
    integrator = _integrator(fix_client)
    _continuation(db, integrator, state=state)

    _assert_refused_naming_the_lane(_fix(fix_client, integrator))


def test_a_ci_fix_is_refused_while_a_publish_to_pr_lane_on_the_root_is_live(fix_client, db):
    integrator = _integrator(fix_client)
    _continuation(db, integrator, publish_to_pr=True)

    # MUTATION: read `metadata.dispatch.continues` alone -- wf_1a460a31's path is missed.
    _assert_refused_naming_the_lane(_fix(fix_client, integrator))


@pytest.mark.parametrize("publish_to_pr", [False, True], ids=["continues", "publish_to_pr"])
def test_a_refused_ci_fix_writes_no_task_and_no_claim(fix_client, db, publish_to_pr):
    integrator = _integrator(fix_client)
    _continuation(db, integrator, publish_to_pr=publish_to_pr)

    response = _fix(fix_client, integrator)

    assert response.status_code == 409, response.text
    # MUTATION: refuse after `_claim` -- the refused request holds the
    # failure's claim, and the fix once the lane ends answers in flight.
    assert _ci_fix_tasks(db) == []
    assert _claims(db) == []


@pytest.mark.parametrize("where", [
    {"state": "SUCCEEDED"},
    {"state": "FAILED"},
    {"state": "CANCELLED"},
    {"runner_profile": "merge"},
    # Each writer's own mark, on another pull request so its own gate (left
    # unchanged) does not fire either: the new read must pass over the mark.
    {"ci_fix": {"pull_request": PR + 1, "attempt": 1, "max_attempts": 2}},
    {"merge_fix_round": {"pull_request": PR + 1}},
    {CONFLICT_ROUND_METADATA_KEY: {"pull_request": PR + 1}},
    {pr_shepherd.METADATA_KEY: {"pull_request": PR + 1}},
    {"issue_run": {"run_id": "run_" + "e5" * 10}},
    {"tenant_id": "research"},
    {"root": "task_" + "f6" * 10},
], ids=["succeeded", "failed", "cancelled", "merge_step", "ci_fix", "merge_fix_round",
        "merge_conflict_round", "pr_shepherd", "issue_run", "another_tenant", "another_root"])
def test_a_terminal_merge_only_marked_or_other_tenants_continuation_does_not_refuse(
    fix_client, db, where
):
    integrator = _integrator(fix_client)
    where = dict(where)
    root = where.pop("root", integrator)
    plain = {key: where.pop(key) for key in ("state", "runner_profile", "tenant_id") if key in where}
    _continuation(db, root, **plain, **where)

    response = _fix(fix_client, integrator)

    # MUTATION: drop the state, merge-step, mark, tenant (invariant 9) or root check.
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1 + ("ci_fix" in where)


def test_after_the_continuation_ends_the_ci_fix_is_admitted_and_claims(fix_client, db):
    integrator = _integrator(fix_client)
    lane = _continuation(db, integrator)

    refused = _fix(fix_client, integrator, max_attempts=1)
    assert refused.status_code == 409, refused.text

    lane["state"] = "SUCCEEDED"
    response = _fix(fix_client, integrator, max_attempts=1)

    # MUTATION: count the refused request as an attempt -- the cap of one
    # refuses the fix the lane's end was waiting for.
    assert response.status_code == 201, response.text
    (task,) = _ci_fix_tasks(db)
    assert task["metadata"]["ci_fix"].get(cifix.CLAIM_FIELD)
    assert len(_claims(db)) == 1


def test_the_switch_off_lets_the_fix_through(fix_client, db, monkeypatch):
    integrator = _integrator(fix_client)
    _continuation(db, integrator)
    monkeypatch.setenv(SWITCH, "off")

    response = _fix(fix_client, integrator)

    # MUTATION: raise the Conflict directly, not through `refusals.refuse`.
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1
