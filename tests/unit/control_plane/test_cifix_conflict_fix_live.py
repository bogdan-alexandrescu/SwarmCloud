"""A CI fix for a pull request is refused while a conflict fix on it is live (#2456).

Measured 2026-10-26 on PR #2381: pr-shepherd's conflict fix
(task_2239a84774bf4bceaeb5, wf_eb9eb92c, 01:23Z) and a scripts/ci-fix.sh fix
(task_2f5b70aa, wf_8bed3d10, 01:21Z) both cloned the branch at 15cc7ea. The
CI fix pushed 9c722e47 first, and the conflict fix's push was rejected
non-fast-forward. Both continued the same branch task, so neither fixer saw
the other: the shepherd checked only the issue run's FIXING state, and
`cifix.stamp` only the lane's merge step.

So `stamp` refuses a `metadata.ci_fix` submission while a non-terminal task
continuing the same branch task carries a conflict fix for that pull request
-- pr-shepherd's `metadata.pr_shepherd` or a merge step's
`merge_conflict_round` -- with the existing one-fixer-per-PR refusal, 409
`ci_fix_lane_owns_pr`, which scripts/ci-fix.sh already reads as a skip. No
task and no claim is made. The other direction (the shepherd passes over a
conflict fix while a CI fix is live) is held in
test_schedtypes_pr_shepherd.py.

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
CONFLICT_TASK = "task_" + "c7" * 10
CONFLICT_WORKFLOW = "wf_" + "c7" * 4
SWITCH = refusals.env_name("ci_fix_lane_owns_pr")


def _conflict_fix(db, continues: str, *, kind: str = pr_shepherd.METADATA_KEY,
                  state: str = "RUNNING", tenant_id: str = "eng", pull_request: int = PR) -> None:
    """A conflict fix continuing `continues`, as pr-shepherd (or a merge step's
    conflict round) submits it, in `state`."""
    seed_task(db, task_id=CONFLICT_TASK, tenant_id=tenant_id, state=state,
              runner_profile="claude-code", workflow_id=CONFLICT_WORKFLOW)
    db.docs[f"tasks/{CONFLICT_TASK}"]["metadata"] = {
        "dispatch": {"strategy": "direct-pr", "continues": continues},
        kind: {"pull_request": pull_request, "head_sha": "1" * 40},
    }


def _fix(client, continues: str):
    return client.post("/v1/workflows", headers=FIXER_HEADERS, json=_red_run_payload(continues))


def _claims(db) -> list[str]:
    return [key for key in db.docs if key.startswith(f"{cifix.CLAIMS}/")]


@pytest.fixture(autouse=True)
def _shipped_position(monkeypatch):
    """The switch as it ships: nothing in the environment."""
    monkeypatch.delenv(SWITCH, raising=False)


def test_the_conflict_fix_key_matches_pr_shepherds():
    """cifix restates the key because importing pr_shepherd would be a cycle."""
    # MUTATION: rename pr_shepherd.METADATA_KEY -- stamp stops seeing its fixes.
    assert cifix.CONFLICT_FIX_METADATA_KEY == pr_shepherd.METADATA_KEY


def test_a_ci_fix_beside_a_live_conflict_fix_is_refused_naming_it(fix_client, db):
    integrator = _integrator(fix_client)
    _conflict_fix(db, integrator)

    response = _fix(fix_client, integrator)

    # MUTATION: drop the live-writer read from `stamp` -- 201, and two
    # writers clone one branch, as on PR #2381.
    assert response.status_code == 409, response.text
    body = response.json()
    # MUTATION: a new refusal code -- scripts/ci-fix.sh reads only this one as a skip.
    assert body["code"] == "ci_fix_lane_owns_pr"
    assert body["detail"]["task_id"] == CONFLICT_TASK
    assert body["detail"]["workflow_id"] == CONFLICT_WORKFLOW
    assert body["detail"]["pull_request"] == PR
    assert body["detail"]["conflict_fix"] is True
    assert CONFLICT_TASK in body["message"]
    # MUTATION: refuse after `_claim` -- the refused request holds the
    # failure's claim, and the fix once the conflict fix ends answers in flight.
    assert _ci_fix_tasks(db) == []
    assert _claims(db) == []


def test_a_ci_fix_beside_a_live_merge_conflict_round_is_refused(fix_client, db):
    integrator = _integrator(fix_client)
    _conflict_fix(db, integrator, kind=CONFLICT_ROUND_METADATA_KEY, state="QUEUED")

    response = _fix(fix_client, integrator)

    # MUTATION: look for the pr_shepherd mark alone.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ci_fix_lane_owns_pr"
    assert response.json()["detail"]["task_id"] == CONFLICT_TASK
    assert _ci_fix_tasks(db) == [] and _claims(db) == []


@pytest.mark.parametrize("where", [
    {"state": "SUCCEEDED"}, {"state": "FAILED"}, {"pull_request": PR + 1}, {"tenant_id": "research"},
], ids=["succeeded", "failed", "another_pull_request", "another_tenant"])
def test_a_terminal_or_other_pr_or_other_tenant_conflict_fix_does_not_refuse(fix_client, db, where):
    integrator = _integrator(fix_client)
    _conflict_fix(db, integrator, **where)

    response = _fix(fix_client, integrator)

    # MUTATION: drop the state, pull-request or tenant check (invariant 9).
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1


def test_the_switch_off_lets_the_fix_through(fix_client, db, monkeypatch):
    integrator = _integrator(fix_client)
    _conflict_fix(db, integrator)
    monkeypatch.setenv(SWITCH, "off")

    response = _fix(fix_client, integrator)

    # MUTATION: raise the Conflict directly, not through `refusals.refuse`.
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1
