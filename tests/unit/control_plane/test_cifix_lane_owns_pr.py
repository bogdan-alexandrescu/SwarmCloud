"""A CI-fix for a lane's pull request is refused while the lane's merge step is live
(CI-FIXER-GATE, part of #1822).

Measured 2026-10-21 over 72 h: `.github/workflows/ci-fix.yml`'s standalone
fixer ran 91 times and failed 34, 22 of them on a push rejected because the
lane's own merge step had already moved the branch with its CI-fix round
(`mergewake._on_red`). CI-FIXER-ONE (PR 1825) made `scripts/ci-fix.sh` read
the lane's workflow and stand down, but the fixer is the continuation-scoped
`SWARM_CI_FIX_SA`, which reads only what it submitted
(`deps.submission_scope`): its read of a lane task answers 404, so the skip
never fired.

So the API decides, in `cifix.stamp`, with its own access: a `metadata.ci_fix`
submission whose continued task belongs to a workflow with a merge step that
is not terminal is answered 409 `ci_fix_lane_owns_pr` naming that merge step,
and no task and no claim are made. A single task, a workflow with no merge
step, and a merge step that has finished keep today's behaviour. The fixer
gains no read; who may submit does not change.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import cifix, refusals

from swarm_common.states import TERMINAL_STATES, TaskState

from .conftest import seed_task
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    _ci_fix_tasks,
    _integrator,
    _members_hold_grants,
    _original,
    _red_run_payload,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}
MERGE_TASK = "task_" + "e" * 20
SWITCH = refusals.env_name("ci_fix_lane_owns_pr")

LIVE_STATES = sorted(s.value for s in TaskState if s not in TERMINAL_STATES)
FINISHED_STATES = sorted(s.value for s in TERMINAL_STATES)


def _lane(client, db, merge_state: str | None) -> str:
    """A lane: an integrate workflow's integrator, whose `swarm/<id>` pull
    request went red, and -- unless `merge_state` is None -- the workflow's
    merge step in that state, submitted by someone other than the fixer."""
    integrator = _integrator(client)
    if merge_state is not None:
        seed_task(
            db, task_id=MERGE_TASK, tenant_id="eng", state=merge_state,
            runner_profile="merge", workflow_id=db.docs[f"tasks/{integrator}"]["workflow_id"],
        )
    return integrator


def _fix(client, continues: str, **kwargs: Any):
    return client.post("/v1/workflows", headers=FIXER_HEADERS, json=_red_run_payload(continues, **kwargs))


def _claims(db) -> list[str]:
    return [key for key in db.docs if key.startswith(f"{cifix.CLAIMS}/")]


@pytest.fixture(autouse=True)
def _shipped_position(monkeypatch):
    """The switch as it ships: nothing in the environment."""
    monkeypatch.delenv(SWITCH, raising=False)


def test_the_live_states_are_the_ones_the_brief_names():
    """Rule zero: the parametrisation below visits every live state."""
    assert {"QUEUED", "PARKED", "READY", "LEASED", "DISPATCHED", "STARTING", "RUNNING"} <= set(LIVE_STATES)
    assert not set(LIVE_STATES) & set(FINISHED_STATES)


def test_a_fix_for_a_lane_whose_merge_step_is_parked_is_refused_naming_it(fix_client, db):
    """PARKED is the merge step on its CI wait: exactly where its own fix
    round starts, and where the standalone fixer collided with it."""
    integrator = _lane(fix_client, db, "PARKED")

    response = _fix(fix_client, integrator)

    # MUTATION: drop the merge-step read from `stamp` -- this answers 201 and
    # a standalone fix races the lane's own round for the branch.
    assert response.status_code == 409, response.text
    body = response.json()
    # MUTATION: raise CiFixInFlight instead -- the script cannot tell "this
    # failure has a live fix" from "the lane owns this pull request".
    assert body["code"] == "ci_fix_lane_owns_pr"
    # MUTATION: leave the merge step's id out of the detail.
    assert body["detail"]["task_id"] == MERGE_TASK
    assert body["detail"]["workflow_id"] == db.docs[f"tasks/{integrator}"]["workflow_id"]
    # MUTATION: refuse after building the workflow -- a fix task is made.
    assert _ci_fix_tasks(db) == []
    # MUTATION: gate after `_claim` -- the refused request holds the failure's
    # claim, and the fix once the merge step ends answers ci_fix_in_flight.
    assert _claims(db) == []


@pytest.mark.parametrize("merge_state", LIVE_STATES)
def test_every_live_merge_step_state_refuses(fix_client, db, merge_state):
    integrator = _lane(fix_client, db, merge_state)
    response = _fix(fix_client, integrator)
    # MUTATION: count only RUNNING (or only CONCURRENCY_STATES) as live -- a
    # QUEUED, PARKED or READY merge step still runs its rounds.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ci_fix_lane_owns_pr"


@pytest.mark.parametrize("merge_state", FINISHED_STATES)
def test_a_lane_whose_merge_step_has_finished_is_fixed_as_today(fix_client, db, merge_state):
    integrator = _lane(fix_client, db, merge_state)
    response = _fix(fix_client, integrator)
    # MUTATION: refuse whenever the workflow has a merge step -- a merge step
    # that ended checks_failed leaves a red pull request no fixer touches.
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1


def test_a_workflow_with_no_merge_step_is_fixed_as_today(fix_client, db):
    integrator = _lane(fix_client, db, None)
    response = _fix(fix_client, integrator)
    # MUTATION: treat any live step of the workflow as a merge step -- the
    # integrator's own live sibling is not one.
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1


def test_a_live_step_that_is_not_a_merge_step_is_not_one(fix_client, db):
    integrator = _lane(fix_client, db, None)
    seed_task(db, task_id="task_" + "f" * 20, tenant_id="eng", state="RUNNING",
              runner_profile="mock", workflow_id=db.docs[f"tasks/{integrator}"]["workflow_id"])
    # MUTATION: match on state alone, not on the step's runner profile.
    assert _fix(fix_client, integrator).status_code == 201


def test_a_single_task_is_fixed_as_today(fix_client, db):
    original = _original(fix_client)
    response = _fix(fix_client, original)
    # MUTATION: refuse a continuation whose task names no workflow.
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1


def test_another_tenants_live_merge_step_is_never_read_as_this_lanes(fix_client, db):
    integrator = _lane(fix_client, db, None)
    seed_task(db, task_id=MERGE_TASK, tenant_id="research", state="PARKED",
              runner_profile="merge", workflow_id=db.docs[f"tasks/{integrator}"]["workflow_id"])
    # MUTATION: read the workflow's tasks without the tenant filter.
    assert _fix(fix_client, integrator).status_code == 201


def test_the_merge_steps_own_rounds_are_not_gated(fix_client, db):
    """Only `metadata.ci_fix` -- the standalone fixer's -- meets the gate; a
    continuation without it (the merge step's `merge_fix_round`) does not."""
    integrator = _lane(fix_client, db, "PARKED")
    payload = _red_run_payload(integrator)
    payload["metadata"] = {"merge_fix_round": {"pull_request": 77}}
    response = fix_client.post("/v1/workflows", headers=FIXER_HEADERS, json=payload)
    # MUTATION: gate every continuation of a lane task -- the lane's own
    # fix rounds are refused and its pull request stays red.
    assert response.status_code == 201, response.text


def test_the_refusal_ships_on_and_off_lets_the_fix_through(fix_client, db, monkeypatch):
    """It ships ON (refusals.py says why): off, the request goes on as it did
    before the gate, and the log names what would have been refused."""
    # MUTATION: register the switch with default False -- the gate is
    # report-only in production, which is CI-FIXER-ONE's 404 again.
    assert refusals.enforced("ci_fix_lane_owns_pr", environ={})
    integrator = _lane(fix_client, db, "PARKED")
    monkeypatch.setenv(SWITCH, "off")
    response = _fix(fix_client, integrator)
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1
