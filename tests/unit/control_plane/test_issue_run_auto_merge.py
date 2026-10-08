"""An `auto_merge` issue run merges once CI is green and its keyword block is written.

Review of the merge step (contract request 47): a merge appended INSIDE the
run's compiled workflow ran before the run reached CHECKING -- before the API
wrote `Closes #N` into the pull request, so the merge closed nothing (#569
again), and before the CI loop could fix a red check, so a red or slow CI
failed the workflow and the run with it. What these tests hold:

  1. The compiled workflow and every CI fix round say `metadata.merge` "off",
     whatever the run or the platform default says.
  2. The merge is ONE merge-only continuation, submitted only once CI is
     green at the head AND the keyword block is recorded written, and only
     when the review's verdict is MERGE. It continues the run's own task that
     pushed the green head: the integrator, or the newest fix round's.
  3. Red CI on an `auto_merge` run goes to FIXING, never FAILED.
  4. A merge that GitHub reports done is DONE; one the merge step refused is
     FAILED with the step's own reason, and never resubmitted for that head.

GitHub is the fake transport the CI-loop tests use. No credentials, no
network, no emulator.
"""

from __future__ import annotations

import pytest

import json

from swarm_api import issueci, issueruns

from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    PR,
    SHA_A,
    SHA_B,
    _read,
    _round_ends,
    _rounds,
    _step_task,
    _stored,
    _workflow_ends,
    api_context,
    clock,
    forge_tokens,
    writes,
)
from .test_issue_run_keyword import _review_writes, _verdict
from .test_issue_runs import FULL_PLAN, _approve, _create, _docs, _finish_planner, _run


def _checking(client, db, objects, writes, *, verdict: str = "MERGE") -> dict:
    created = _create(client, auto_merge=True)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["auto_merge"] is True
    _finish_planner(db, objects, run, FULL_PLAN)
    planned = _run(client, run["id"]).json()["run"]
    running = _approve(client, run["id"], planned["plan_digest"]).json()["run"]
    writes.open_pull(PR, SHA_A)
    integrator = _workflow_ends(db, running["workflow_id"])
    integrator["result_summary"]["git"]["pushed_head"] = SHA_A
    _review_writes(db, objects, running["workflow_id"],
                   json.dumps(_verdict(True, True, verdict=verdict)))
    return running


def _merge_workflows(db) -> list[dict]:
    return [
        w for w in _docs(db, "workflows").values()
        if any(s.get("step_id") == "merge" for s in w.get("steps") or [])
    ]


def _merge_task(db, workflow: dict) -> dict:
    (step,) = workflow["steps"]
    return db.docs[f"tasks/{step['task_id']}"]


def test_the_compiled_workflow_and_a_fix_round_never_carry_a_merge(client, db, objects, writes, clock):
    client.put("/v1/admin/settings", headers={"Authorization": "Bearer token-root"},
               json={"merge_by_default": True})
    running = _checking(client, db, objects, writes)
    compiled = db.docs[f"workflows/{running['workflow_id']}"]
    assert not [s for s in compiled["steps"] if s["step_id"] == "merge"]
    for doc in _docs(db, "tasks").values():
        if doc.get("workflow_id") == running["workflow_id"]:
            assert doc["runner_profile"] != "merge"
    stored = issueruns.IssueRun.from_firestore(_stored(db, running["id"]))
    stored.pr_task_id = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)["id"]
    spec = issueci.ci_fix_workflow(stored, 1, "x", SHA_A)
    assert spec.metadata["merge"] == "off"


def test_green_with_the_keyword_written_submits_one_merge_continuing_the_integrator(
    client, db, objects, writes, clock,
):
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING"
    # The keyword block went on before the merge was asked for.
    assert "Closes #42" in writes.pulls[PR]["body"]
    (merge_wf,) = _merge_workflows(db)
    assert run["merge"] == {"workflow_id": merge_wf["workflow_id"], "head_sha": SHA_A}
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    task = _merge_task(db, merge_wf)
    assert task["runner_profile"] == "merge" and task["provider"] == "git"
    # The integrator's OWN workflow, signed beside it (#900): the worker
    # verifies the integrator's spec against the run's workflow, not the merge's.
    assert task["metadata"]["dispatch"]["merge_target"] == {
        "pull_request": integrator["id"], "pull_request_workflow": running["workflow_id"],
    }
    assert integrator["workflow_id"] == running["workflow_id"] != merge_wf["workflow_id"]
    assert task["metadata"]["dispatch"]["continues"] == integrator["id"]
    assert task["repository_url"].startswith("https://github.com/saga-xyz/widgets")

    # Another read while the merge runs submits nothing more.
    again = _read(client, clock, running["id"])
    assert again["state"] == "CHECKING" and len(_merge_workflows(db)) == 1

    # GitHub reports it merged: DONE.
    writes.pulls[PR]["merged"] = True
    writes.pulls[PR]["state"] = "closed"
    done = _read(client, clock, running["id"])
    assert done["state"] == "DONE" and done["green_sha"] == SHA_A


def test_no_merge_while_the_keyword_block_is_not_written(client, db, objects, writes, clock):
    writes.status["PATCH"] = 500
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING"
    assert _merge_workflows(db) == []


def test_red_ci_on_an_auto_merge_run_is_a_fix_round_then_the_merge_continues_it(
    client, db, objects, writes, clock,
):
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "failure", output={"summary": "test_a"})

    run = _read(client, clock, running["id"])
    assert run["state"] == "FIXING", run.get("error")
    assert _merge_workflows(db) == []
    (round_one,) = _rounds(db, running["id"])
    _round_ends(db, round_one)
    fixer = _step_task(db, round_one, issueci.CI_FIX_STEP)
    fixer["result_summary"] = {"git": {"pushed_head": SHA_B}}
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")

    run = _read(client, clock, running["id"])
    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    (merge_wf,) = _merge_workflows(db)
    assert _merge_task(db, merge_wf)["metadata"]["dispatch"]["merge_target"] == {
        "pull_request": fixer["id"], "pull_request_workflow": round_one,
    }
    assert run["merge"]["head_sha"] == SHA_B


def test_a_pushing_task_of_a_workflow_not_the_runs_is_not_merged(client, db, objects, writes, clock):
    """#900: the run's record binds the task the merge names AND its workflow.

    MUTATION: drop `merge_target_unbound` from `issueci._merge` -- the merge
    is submitted for an integrator whose workflow is not the run's."""
    running = _checking(client, db, objects, writes)
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    integrator["workflow_id"] = "wf_" + "f" * 20
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "which is not this run's" in run["error"]
    assert _merge_workflows(db) == []


def test_a_review_verdict_of_not_yet_fails_the_merge_and_submits_none(
    client, db, objects, writes, clock,
):
    running = _checking(client, db, objects, writes, verdict="NOT_YET")
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "NOT_YET, not MERGE" in run["error"]
    assert _merge_workflows(db) == []


def test_a_head_no_task_of_the_run_pushed_is_not_merged(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes)
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "not pushed by any of this run's tasks" in run["error"]
    assert _merge_workflows(db) == []


def test_a_refused_merge_fails_the_run_with_the_steps_reason_and_is_not_resubmitted(
    client, db, objects, writes, clock,
):
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")
    _read(client, clock, running["id"])
    (merge_wf,) = _merge_workflows(db)
    task = _merge_task(db, merge_wf)
    task["state"] = "FAILED"
    task["result_summary"] = {"merge": {"refusal": {
        "code": "token_lacks_rights", "message": "the tenant's token cannot merge"}}}

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "token_lacks_rights" in run["error"]
    assert len(_merge_workflows(db)) == 1



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
