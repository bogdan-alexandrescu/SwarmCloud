"""An `auto_merge` issue run merges once CI is green and its keyword block is written.

Review of the merge step (contract request 47): a merge appended INSIDE the
run's compiled workflow ran before the run reached CHECKING -- before the API
wrote `Closes #N` into the pull request, so the merge closed nothing (archived issue 569
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

import copy
from datetime import timedelta

import pytest

import json
import urllib.error

from swarm_api import issueci, issueruns
from swarm_api.validation import REREVIEW_STEP_ID

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
from .test_issue_run_keyword import SAME, _review_writes, _verdict
from .test_issue_runs import FULL_PLAN, _approve, _create, _docs, _finish_planner, _run

#: Holds the one-re-review shape: `fix-loop` switched off (tests/unit/conftest.py).
pytestmark = pytest.mark.usefixtures("fix_loop_off")


def _checking(client, db, objects, writes, *, verdict: str = "MERGE",
              rereview: str | None = None) -> dict:
    """A CHECKING `auto_merge` run whose review wrote `verdict`, and whose
    re-review (archived issue 2217) wrote `rereview` -- by default the review's verdict,
    passed on as a shut gate passes it."""
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
    _review_writes(
        db, objects, running["workflow_id"], json.dumps(_verdict(True, True, verdict=verdict)),
        rereview=SAME if rereview is None else json.dumps(_verdict(True, True, verdict=rereview)),
    )
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
    # The integrator's OWN workflow, signed beside it (archived issue 900): the worker
    # verifies the integrator's spec against the run's workflow, not the merge's.
    assert task["metadata"]["dispatch"]["merge_target"] == {
        "pull_request": integrator["id"], "pull_request_workflow": running["workflow_id"],
    }
    assert integrator["workflow_id"] == running["workflow_id"] != merge_wf["workflow_id"]
    assert task["metadata"]["dispatch"]["continues"] == integrator["id"]
    assert task["repository_url"].startswith("https://github.com/example-org/widgets")

    # Another read while the merge runs submits nothing more.
    again = _read(client, clock, running["id"])
    assert again["state"] == "CHECKING" and len(_merge_workflows(db)) == 1

    # GitHub reports it merged: DONE.
    writes.pulls[PR]["merged"] = True
    writes.pulls[PR]["state"] = "closed"
    done = _read(client, clock, running["id"])
    assert done["state"] == "DONE" and done["green_sha"] == SHA_A


def test_a_green_run_whose_merge_is_in_flight_is_not_failed_by_the_checking_deadline(
    client, db, objects, writes, clock,
):
    # RUNS (2026-10-12): the deadline ends a run nobody can move on. A green
    # `auto_merge` run waits on its merge -- or on a person's merge approval
    # -- and both end it themselves.
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")
    assert _read(client, clock, running["id"])["state"] == "CHECKING"
    assert len(_merge_workflows(db)) == 1

    clock.at += timedelta(seconds=issueci.CHECKING_DEADLINE_SECONDS) * 2
    run = _read(client, clock, running["id"])
    assert run["state"] == "CHECKING" and len(_merge_workflows(db)) == 1


def test_a_green_run_whose_reads_then_fail_transiently_is_not_failed_by_the_deadline(
    client, db, objects, writes, clock,
):
    # WRITEBACK (2026-10-12): checks green on GitHub, the merge in flight, and
    # then GitHub unreachable. What blocks the run is the wire, not its CI: it
    # keeps waiting on its merge, retrying the read, and says why.
    running = _checking(client, db, objects, writes)
    writes.check(SHA_A, "unit", "success")
    assert _read(client, clock, running["id"])["state"] == "CHECKING"
    assert len(_merge_workflows(db)) == 1

    writes.raise_for["GET"] = urllib.error.URLError(TimeoutError("timed out"))
    stuck = _read(client, clock, running["id"])
    assert stuck["pull_request"]["read_error"].startswith("GitHub writeback failing since ")

    clock.at += timedelta(seconds=issueci.CHECKING_DEADLINE_SECONDS) * 2
    run = _read(client, clock, running["id"])
    assert run["state"] == "CHECKING", run.get("error")
    assert run["pull_request"]["checks"] == "green"
    assert run["pull_request"]["read_attempts"] == 2

    del writes.raise_for["GET"]
    writes.pulls[PR]["merged"] = True
    writes.pulls[PR]["state"] = "closed"
    clock.at += timedelta(seconds=issueci.READ_BACKOFF_MAX_SECONDS)
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
    """archived issue 900: the run's record binds the task the merge names AND its workflow.

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


def test_a_not_yet_review_whose_fix_the_rereview_passes_submits_the_merge(
    client, db, objects, writes, clock,
):
    """archived issue 2217: the review said NOT_YET, the fix resolved it, the re-review of
    the fixed head says MERGE. The merge reads the LATEST verdict.

    MUTATION: make `issueci._review_task_id` return the 'review' step's task
    -- the run fails "NOT_YET, not MERGE", as wf_3cc4026eb34a43a885a4 did."""
    running = _checking(client, db, objects, writes, verdict="NOT_YET", rereview="MERGE")
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    (merge_wf,) = _merge_workflows(db)
    assert run["merge"] == {"workflow_id": merge_wf["workflow_id"], "head_sha": SHA_A}


def test_a_rereview_that_still_says_not_yet_fails_the_merge_and_submits_none(
    client, db, objects, writes, clock,
):
    running = _checking(client, db, objects, writes, verdict="NOT_YET", rereview="NOT_YET")
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "NOT_YET, not MERGE" in run["error"]
    assert _merge_workflows(db) == []


def test_a_merge_review_passed_on_by_the_shut_rereview_merges(client, db, objects, writes, clock):
    """On MERGE the re-review's agent does not run; its gate re-publishes the
    review's verdict (`Worker._republish_staged_verdict`), and that is read."""
    running = _checking(client, db, objects, writes, verdict="MERGE")
    compiled = db.docs[f"workflows/{running['workflow_id']}"]
    assert [s["step_id"] for s in compiled["steps"]][-3:] == [
        issueruns.REVIEW_STEP, issueruns.FIX_STEP, REREVIEW_STEP_ID,
    ]
    rereview = _step_task(db, running["workflow_id"], REREVIEW_STEP_ID)
    assert rereview["result_summary"]["artifacts"][0]["name"] == issueruns.VERDICT_FILE
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert len(_merge_workflows(db)) == 1


def test_a_workflow_compiled_without_a_rereview_reads_the_review(
    client, db, objects, writes, clock,
):
    """A run whose workflow was compiled before archived issue 2217 has no re-review step:
    its review's verdict is still the one read, MERGE or NOT_YET."""
    running = _checking(client, db, objects, writes, verdict="NOT_YET", rereview="MERGE")
    compiled = db.docs[f"workflows/{running['workflow_id']}"]
    compiled["steps"] = [s for s in compiled["steps"]
                         if s["step_id"] != REREVIEW_STEP_ID]
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



CONFLICT_FIX_TASK = "task_" + "c9" * 10
CONFLICT_FIX_WORKFLOW = "wf_" + "c9" * 10


def _conflict_fix(db, running: dict, *, run_id: str | None = None,
                  continues: str | None = None, tenant_id: str | None = None,
                  pull_request: int = PR) -> dict:
    """A pr-shepherd conflict fix of this run's pull request that pushed SHA_B,
    as `pr_shepherd.conflict_fix_workflow` submits it: a workflow of one
    `conflict-fix` step continuing the integrator, whose task carries the
    workflow's `metadata.pr_shepherd` (the run, repository, pull request and
    head) -- a submission stores a workflow's metadata on its tasks."""
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    tenant = tenant_id or integrator["tenant_id"]
    mark = {"run_id": run_id or running["id"], "repository": "Example-Org/Widgets",
            "pull_request": pull_request, "head_sha": SHA_A, "base_ref": "main"}
    workflow = copy.deepcopy(db.docs[f"workflows/{running['workflow_id']}"])
    workflow.update({
        "workflow_id": CONFLICT_FIX_WORKFLOW, "id": CONFLICT_FIX_WORKFLOW, "tenant_id": tenant,
        "steps": [{"step_id": "conflict-fix", "task_id": CONFLICT_FIX_TASK,
                   "runner_profile": "claude-code", "depends_on": []}],
    })
    db.docs[f"workflows/{CONFLICT_FIX_WORKFLOW}"] = workflow
    task = copy.deepcopy(integrator)
    task.update({
        "id": CONFLICT_FIX_TASK, "tenant_id": tenant, "workflow_id": CONFLICT_FIX_WORKFLOW,
        "state": "SUCCEEDED", "result_summary": {"git": {"pushed_head": SHA_B}},
        "metadata": {
            "merge": "off",
            "dispatch": {"strategy": "direct-pr",
                         "continues": continues or integrator["id"]},
            "pr_shepherd": mark,
        },
    })
    db.docs[f"tasks/{CONFLICT_FIX_TASK}"] = task
    return task


def test_a_head_a_conflict_fix_of_the_run_pushed_is_merged(client, db, objects, writes, clock):
    """archived issue 2537: pr-shepherd's conflict fix pushed a merge of main onto the run's
    pull request; that head is the run's, and the merge continues the fix.

    MUTATION: drop the conflict-fix candidates from `issueci._pushing_task` --
    the run FAILs "not pushed by any of this run's tasks". MUTATION: drop the
    conflict-fix branch of `merge_target_unbound` -- it FAILs "which is not
    this run's"."""
    running = _checking(client, db, objects, writes)
    fix = _conflict_fix(db, running)
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    (merge_wf,) = _merge_workflows(db)
    task = _merge_task(db, merge_wf)
    # `continues_task` named the fix; the stored `continues` is the branch's
    # root, the integrator, and the signed target is the fix in its own round.
    assert task["metadata"]["dispatch"]["continues"] == fix["metadata"]["dispatch"]["continues"]
    assert task["metadata"]["dispatch"]["merge_target"] == {
        "pull_request": fix["id"], "pull_request_workflow": CONFLICT_FIX_WORKFLOW,
    }
    assert run["merge"]["head_sha"] == SHA_B
    assert _stored(db, running["id"])["merge"]["pushed_by"] == fix["id"]


def _refused(client, db, writes, clock, running: dict) -> None:
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "not pushed by any of this run's tasks" in run["error"]
    assert _merge_workflows(db) == []


def test_a_conflict_fix_of_another_run_is_not_merged(client, db, objects, writes, clock):
    """Another run's conflict fix of the same pull request is not this run's.

    MUTATION: drop the `run_id` check from `issueci._conflict_fix_tasks`."""
    running = _checking(client, db, objects, writes)
    _conflict_fix(db, running, run_id="ir_" + "0" * 20)
    _refused(client, db, writes, clock, running)


def test_a_conflict_fix_of_another_pull_request_is_not_merged(client, db, objects, writes, clock):
    """A fix marked for another pull request of the same branch is not this run's.

    MUTATION: drop the mark's `pull_request` check from `issueci._conflict_mark`
    and the `pull_request` filter from `issueci._conflict_fix_tasks`."""
    running = _checking(client, db, objects, writes)
    _conflict_fix(db, running, pull_request=PR + 1)
    _refused(client, db, writes, clock, running)


def test_a_conflict_fix_that_does_not_continue_the_integrator_is_not_merged(
    client, db, objects, writes, clock,
):
    """A fix of another branch pushed nothing to this run's pull request.

    MUTATION: drop the `metadata.dispatch.continues` filter and its recheck
    from `issueci._conflict_fix_tasks`."""
    running = _checking(client, db, objects, writes)
    _conflict_fix(db, running, continues="task_" + "d" * 20)
    _refused(client, db, writes, clock, running)


def test_a_conflict_fix_of_another_tenant_is_not_merged(client, db, objects, writes, clock):
    """Invariant 9: another tenant's task is never a candidate.

    MUTATION: drop the `tenant_id` filter and its recheck from
    `issueci._conflict_fix_tasks`."""
    running = _checking(client, db, objects, writes)
    _conflict_fix(db, running, tenant_id="research")
    _refused(client, db, writes, clock, running)



# --------------------------------------------------------------------------
# archived issue 474: a person's green head ends the run superseded, not FAILED
# --------------------------------------------------------------------------

PERSON_LANE_TASK = "task_" + "e7" * 10
PERSON_LANE_WORKFLOW = "wf_" + "e7" * 10
PERSON = "alice@example.com"
#: The GitHub login of a person on the pull request, and of one who is not.
PERSON_LOGIN = "person-on-the-pull"
STRANGER_LOGIN = "someone-else"
SUPERSEDED = "superseded_by_person_push"


def _person_lane(db, running: dict, *, via: str = "continues", tenant_id: str | None = None,
                 submitted_by: str = PERSON, root: str | None = None,
                 marks: dict | None = None) -> dict:
    """A plain lane a person dispatched onto the run's branch, ended, that
    pushed SHA_B: a task of its own workflow whose `metadata.dispatch`
    continues the integrator (`via="continues"`) or publishes to its pull
    request (`via="publish_to_pr"`), carrying no writer's mark."""
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    target = root or integrator["id"]
    dispatch: dict = {"strategy": "direct-pr"}
    if via == "continues":
        dispatch["continues"] = target
    else:
        dispatch["publish_to_pr"] = {"task_id": target}
    task = copy.deepcopy(integrator)
    task.update({
        "id": PERSON_LANE_TASK, "tenant_id": tenant_id or integrator["tenant_id"],
        "workflow_id": PERSON_LANE_WORKFLOW, "state": "SUCCEEDED",
        "submitted_by": submitted_by, "runner_profile": "claude-code",
        "result_summary": {"git": {"pushed_head": SHA_B}},
        "metadata": {"dispatch": dispatch, **(marks or {})},
    })
    db.docs[f"tasks/{PERSON_LANE_TASK}"] = task
    return task


def _green_at_b(writes) -> None:
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")


def _superseded(run: dict) -> None:
    assert run["state"] == "DONE", run.get("error")
    assert run["outcome"] == SUPERSEDED
    assert not run.get("error")
    assert run["green_sha"] == SHA_B


def test_a_head_a_persons_continuation_of_the_integrator_pushed_supersedes_the_run(
    client, db, objects, writes, clock,
):
    """archived issue 474: a person continued the run's branch and the head
    their lane pushed is green. That is their pull request now: the run ends
    DONE `superseded_by_person_push` and submits no merge.

    MUTATION: drop `_person_lane_heads` from `issueci._merge` -- the run
    FAILs "not pushed by any of this run's tasks"."""
    running = _checking(client, db, objects, writes)
    _person_lane(db, running)
    _green_at_b(writes)

    run = _read(client, clock, running["id"])

    _superseded(run)
    assert _merge_workflows(db) == []
    assert issueruns.IssueRun.from_firestore(_stored(db, running["id"])).outcome == SUPERSEDED


def test_a_head_a_persons_publish_to_pr_lane_pushed_supersedes_the_run(
    client, db, objects, writes, clock,
):
    """The other plain lane `cifix.live_plain_continuation` reads:
    `metadata.dispatch.publish_to_pr.task_id` names the integrator."""
    running = _checking(client, db, objects, writes)
    _person_lane(db, running, via="publish_to_pr")
    _green_at_b(writes)

    run = _read(client, clock, running["id"])

    _superseded(run)
    assert _merge_workflows(db) == []


def test_a_base_merge_on_a_persons_lane_head_still_supersedes_the_run(
    client, db, objects, writes, clock,
):
    """GitHub's own base merge on top of the lane's head is still the lane's."""
    running = _checking(client, db, objects, writes)
    _person_lane(db, running)
    merged = "9" * 40
    writes.commit_bodies[merged] = {
        "sha": merged, "parents": [{"sha": SHA_B}, {"sha": "8" * 40}],
        "commit": {"message": "Merge branch 'main'",
                   "committer": {"email": "noreply@github.com"},
                   "verification": {"verified": True}},
    }
    writes.on_base.add("8" * 40)
    writes.open_pull(PR, merged)
    writes.check(merged, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE", run.get("error")
    assert run["outcome"] == SUPERSEDED and run["green_sha"] == merged
    assert _merge_workflows(db) == []


@pytest.mark.parametrize("lane", [
    pytest.param({"submitted_by": "ci-bot@example-project.iam.gserviceaccount.com"},
                 id="a-service-account-lane"),
    pytest.param({"tenant_id": "research"}, id="another-tenants-lane"),
    pytest.param({"root": "task_" + "d" * 20}, id="a-lane-of-another-task"),
    pytest.param({"marks": {"issue_run": "ir_" + "0" * 20}}, id="an-issue-run-step"),
    pytest.param({"marks": {"fix_loop_round": {"round": 1}}}, id="a-fix-loop-round"),
    pytest.param({"marks": {"ci_fix": {"pull_request": PR}}}, id="a-ci-fix"),
])
def test_a_head_no_persons_lane_pushed_still_fails(client, db, objects, writes, clock, lane):
    """Invariant 9 and the writers' own gates: another tenant's task, a
    service account's lane, a lane of another branch and a platform writer's
    task name no person, and the head is refused as before.

    MUTATION: drop the tenant, root, submitter or writer-mark check from
    `issueci._person_lane_heads`."""
    running = _checking(client, db, objects, writes)
    _person_lane(db, running, **lane)
    _refused(client, db, writes, clock, running)


def test_a_not_yet_review_still_fails_on_its_verdict_at_a_persons_lane_head(
    client, db, objects, writes, clock,
):
    """The verdict is read first: a person's push does not end a run the
    review refused any other way than FAILED on that verdict."""
    running = _checking(client, db, objects, writes, verdict="NOT_YET")
    _person_lane(db, running)
    _green_at_b(writes)

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "NOT_YET, not MERGE" in run["error"]
    assert not run.get("outcome")
    assert _merge_workflows(db) == []


def _persons_commit(writes, *, login: str = PERSON_LOGIN, message: str = "Tidy the fix",
                    parents: int = 1, email: str = "someone@example.com",
                    kind: str = "User") -> None:
    writes.commit_bodies[SHA_B] = {
        "sha": SHA_B,
        "parents": [{"sha": c * 40} for c in "a76"[:parents]],
        "commit": {"message": message, "committer": {"email": email},
                   "verification": {"verified": False}},
        "committer": {"login": login, "type": kind},
        "author": {"login": login, "type": kind},
    }


@pytest.mark.parametrize("on_pull", ["author", "assignee"])
def test_a_head_a_person_on_the_pull_request_committed_supersedes_the_run(
    client, db, objects, writes, clock, on_pull,
):
    """A person on the pull request -- its author or an assignee -- pushed
    the green head themselves: the run ends superseded, no merge submitted.

    MUTATION: drop the commit read from `issueci._merge` -- the run FAILs."""
    running = _checking(client, db, objects, writes)
    _green_at_b(writes)
    _persons_commit(writes)
    person = {"login": PERSON_LOGIN.upper(), "type": "User"}
    if on_pull == "author":
        writes.pulls[PR]["user"] = person
    else:
        writes.pulls[PR]["user"] = {"login": "swarm-bot", "type": "Bot"}
        writes.pulls[PR]["assignees"] = [person]

    run = _read(client, clock, running["id"])

    _superseded(run)
    assert _merge_workflows(db) == []


def test_a_github_web_edit_by_the_pull_requests_author_supersedes_the_run(
    client, db, objects, writes, clock,
):
    """GitHub commits a web-UI edit itself: the person is the commit's author."""
    running = _checking(client, db, objects, writes)
    _green_at_b(writes)
    _persons_commit(writes, email="noreply@github.com")
    writes.commit_bodies[SHA_B]["committer"] = {"login": "web-flow", "type": "User"}
    writes.pulls[PR]["user"] = {"login": PERSON_LOGIN, "type": "User"}

    run = _read(client, clock, running["id"])

    _superseded(run)


@pytest.mark.parametrize("commit", [
    pytest.param({"login": STRANGER_LOGIN}, id="an-unknown-branch-mate"),
    pytest.param({"message": "Fix it\n\nMade by the worker for task task_" + "1" * 20 + "."},
                 id="a-worker-commit-and-no-lane"),
    pytest.param({"email": "swarmcloud-agent@users.noreply.github.com"},
                 id="the-platforms-committer"),
    pytest.param({"kind": "Bot"}, id="a-bot-account"),
    pytest.param({"parents": 3}, id="an-octopus-merge"),
])
def test_a_head_nobody_on_the_pull_request_committed_still_fails(
    client, db, objects, writes, clock, commit,
):
    """Anyone else is an unknown branch-mate, and a platform commit is a
    defect for failure-review: today's refusal, unchanged."""
    running = _checking(client, db, objects, writes)
    _green_at_b(writes)
    _persons_commit(writes, **commit)
    writes.pulls[PR]["user"] = {"login": PERSON_LOGIN, "type": "User"}

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "not pushed by any of this run's tasks" in run["error"]
    assert not run.get("outcome")
    assert _merge_workflows(db) == []


def test_an_unread_head_commit_is_read_again_not_failed_or_superseded(
    client, db, objects, writes, clock,
):
    """GitHub did not answer the read of who pushed: CHECKING, read next visit."""
    running = _checking(client, db, objects, writes)
    _green_at_b(writes)
    _persons_commit(writes)
    writes.pulls[PR]["user"] = {"login": PERSON_LOGIN, "type": "User"}
    writes.refuse[("GET", f"/repos/example-org/widgets/commits/{SHA_B}")] = 502

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert not run.get("outcome")
    assert _merge_workflows(db) == []


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """archived issue 780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
