"""An issue run compiles the fix loop with its `fix_rounds`, and its CI merge reads the loop's final review.

docs/design/fix-loop.md §5.3 (lane FL-ISSUE), and the owner's rule for every
workflow that merges on a review (§9 Q8 (b), 9 November 2026): a loop when
it is on, the one re-review until then. What these tests hold:

  1. WITH THE `fix-loop` RUNNER TYPE AVAILABLE, both compile paths (a chain
     and stages) end review -> fix -> loop: the step
     `validation.verdict_fix_loop_step` builds, with `max_rounds` the run's
     `fix_rounds`, and no re-review. Submission checks and signs it as a
     declared loop (`resolve_fix_loop`, the loop task's signed
     `metadata.dispatch.fix_loop`). A run that asked for a security review
     keeps the re-review: a loop's fix must be the integrator nothing else
     depends on.
  2. WITHOUT IT the plan compiles as before: review -> fix -> re-review, one
     compiled round.
  3. THE CI LOOP'S MERGE AND `Closes #N` FOLLOW THE LOOP'S FINAL REVIEW
     (`result_summary.fix_loop.final_review`), never round 0's: a NOT_YET
     round 0 whose re-review said MERGE merges and closes; a MERGE round 0
     behind a loop that did not succeed, or a final review that is not one
     of the loop's, neither merges nor closes. A head a `review-fix` round
     pushed is the run's, and the merge continues that round.

The runner type is available since contract request 151; every test here
sets it on or off itself with `monkeypatch`, never depending on the
catalogue's value. No credentials, no network, no emulator.
"""

from __future__ import annotations

import copy
import json
import secrets
from dataclasses import replace

import pytest

from swarm_api import forge, forgewrite, issueruns
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import compile_plan
from swarm_api.metrics import ApiMetrics
from swarm_api.service import SubmissionService
from swarm_api.validation import (
    FIX_LOOP_PROFILE,
    REREVIEW_STEP_ID,
    resolve_fix_loop,
    verdict_fix_loop_step,
)
from swarm_api.waker import NullWaker
from swarm_common.profiles import RUNNER_PROFILES

from . import forge_fakes
from .conftest import api_settings
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    PR,
    SHA_A,
    SHA_B,
    _read,
    _step_task,
    _workflow_ends,
    clock,
    forge_tokens,
    writes,
)
from .test_issue_run_keyword import STAGED_FULL_PLAN, _step_writes, _verdict
from .test_issue_runs import FULL_PLAN, _approve, _create, _docs, _finish_planner, _run
from .test_issue_writeback import _bare_run

SIGNER = LocalSpecSigner()
LOOP_STEP = "loop"


def _profile(monkeypatch, available: bool) -> None:
    reason = "" if available else "fix-loop is not available (patched off for this test)"
    monkeypatch.setitem(RUNNER_PROFILES, FIX_LOOP_PROFILE,
                        replace(RUNNER_PROFILES[FIX_LOOP_PROFILE], available=available,
                                disabled_reason=reason))


@pytest.fixture
def loop_on(monkeypatch):
    """`fix-loop` available, as it is since contract request 151."""
    _profile(monkeypatch, True)


@pytest.fixture
def loop_off(monkeypatch):
    """`fix-loop` unavailable for this test, as it shipped until contract request 151."""
    _profile(monkeypatch, False)


def _compiled(plan: dict, **fields):
    return compile_plan(_bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan), **fields))


# --------------------------------------------------------------------------
# 1-2. the compiled plan
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fix_rounds", [1, 3, 5])
@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_the_compiled_plan_carries_the_loop_with_max_rounds_the_runs_fix_rounds(
    loop_on, plan, fix_rounds,
):
    """MUTATION: compile `_with_rereview` alone -- the last step is the re-review."""
    workflow = _compiled(plan, fix_rounds=fix_rounds)
    ids = [s.step_id for s in workflow.steps]

    assert ids[-3:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, LOOP_STEP]
    assert REREVIEW_STEP_ID not in ids
    loop = workflow.steps[-1]
    assert loop.runner_profile == FIX_LOOP_PROFILE
    assert list(loop.depends_on) == [issueruns.FIX_STEP]
    assert loop.fix_loop == {"review": issueruns.REVIEW_STEP, "fix": issueruns.FIX_STEP,
                             "max_rounds": fix_rounds}
    assert workflow.metadata["issue_run"]["fix_rounds"] == fix_rounds
    assert workflow.metadata["issue_run"]["review_rounds_compiled"] == fix_rounds
    assert workflow.metadata["merge"] == "off"

    # The same step the submission path appends to a workflow that merges on
    # its review's verdict, but for its rounds; and the one it checks.
    specs = SubmissionService._step_specs(workflow)
    derived = verdict_fix_loop_step([s for s in specs if s.step_id != LOOP_STEP], "integrate")
    assert derived is not None
    assert {**derived, "fix_loop": {**derived["fix_loop"], "max_rounds": fix_rounds}} == {
        "step_id": loop.step_id, "runner_profile": loop.runner_profile,
        "depends_on": list(loop.depends_on), "fix_loop": loop.fix_loop,
    }
    checked = resolve_fix_loop(workflow.steps, strategy="integrate")
    assert checked is not None
    assert (checked.review, checked.fix, checked.max_rounds) == (
        issueruns.REVIEW_STEP, issueruns.FIX_STEP, fix_rounds)


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_without_the_runner_type_the_plan_compiles_the_one_rereview(loop_off, plan):
    workflow = _compiled(plan, fix_rounds=4)
    ids = [s.step_id for s in workflow.steps]

    assert ids[-3:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, REREVIEW_STEP_ID]
    assert not [s for s in workflow.steps if s.runner_profile == FIX_LOOP_PROFILE]
    assert workflow.metadata["issue_run"]["fix_rounds"] == 4
    assert workflow.metadata["issue_run"]["review_rounds_compiled"] == 1


def test_a_run_with_a_security_review_keeps_the_rereview_with_the_loop_on(loop_on, monkeypatch):
    """A loop's fix is the integrator nothing else depends on, and the security
    review builds on it: the pair would be refused at submission."""
    monkeypatch.setenv("SWARM_WORK_CONTRACTS", "on")
    workflow = _compiled(FULL_PLAN, security_review=True)
    ids = [s.step_id for s in workflow.steps]

    assert REREVIEW_STEP_ID in ids
    assert issueruns.SECURITY_REVIEW_STEP in ids
    assert not [s for s in workflow.steps if s.runner_profile == FIX_LOOP_PROFILE]


# --------------------------------------------------------------------------
# 3. the CI loop reads the loop's final review
# --------------------------------------------------------------------------

@pytest.fixture
def api_context(db, tokens, group_map, objects, forge_tokens, writes, clock, loop_on):
    """`test_issue_run_ci`'s context, signing step specs, so the loop's and its
    rounds' signed records verify as they do in a deployment."""
    return build_context(
        settings=api_settings(
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
        ),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=forge_fakes.GitHub(issues=[forge_fakes.issue(42)])),
        forge_writer=forgewrite.GitHubWriter(
            send=writes, locate=writes.locate, fetch=writes.fetch
        ),
    )


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


def _task_id() -> str:
    return "task_" + secrets.token_hex(10)


def _loop_running(client, db, objects, writes, *, review: str) -> dict:
    """An `auto_merge` run with `fix_rounds` 2, compiled and submitted with
    its loop; every step ended, the integrator opened PR at SHA_A, and round
    0's review wrote `review` with every requirement met."""
    created = _create(client, auto_merge=True, fix_rounds=2)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, FULL_PLAN)
    planned = _run(client, run["id"]).json()["run"]
    running = _approve(client, run["id"], planned["plan_digest"]).json()["run"]
    assert running["state"] == "RUNNING", running
    writes.open_pull(PR, SHA_A)
    integrator = _workflow_ends(db, running["workflow_id"])
    integrator["result_summary"]["git"]["pushed_head"] = SHA_A
    _step_writes(db, objects, running["workflow_id"], issueruns.REVIEW_STEP,
                 json.dumps(_verdict(True, True, verdict=review)))
    return running


def _loop(db, running: dict) -> dict:
    return _step_task(db, running["workflow_id"], LOOP_STEP)


def _round(db, objects, running: dict, *, kind: str, round_no: int, verdict: str | None = None,
           pushed: str | None = None, loop_task: str | None = None) -> dict:
    """A loop round's agent as `fixloop` submits it: its own one-step workflow,
    its signed dispatch carrying `fix_loop_round`, SUCCEEDED; a re-review
    wrote `verdict` with every requirement met, a fix pushed `pushed`."""
    loop = _loop(db, running)
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    base = _step_task(db, running["workflow_id"],
                      issueruns.REVIEW_STEP if kind == "review" else issueruns.FIX_STEP)
    task_id, workflow_id = _task_id(), "wf_" + secrets.token_hex(10)
    step_id = "re-review" if kind == "review" else "review-fix"
    db.docs[f"workflows/{workflow_id}"] = {
        **copy.deepcopy(db.docs[f"workflows/{running['workflow_id']}"]),
        "workflow_id": workflow_id, "id": workflow_id,
        "steps": [{"step_id": step_id, "task_id": task_id,
                   "runner_profile": base["runner_profile"], "depends_on": []}],
    }
    record = {"loop_task": loop_task or loop["id"], "round": round_no, "kind": kind,
              "pull_request": PR}
    dispatch: dict = {"strategy": "collect" if kind == "review" else "direct-pr"}
    if kind == "fix":
        dispatch["continues"] = integrator["id"]
        record["start_head"] = SHA_A
    else:
        record["head"] = SHA_A
    dispatch["fix_loop_round"] = record
    task = copy.deepcopy(base)
    task.update({
        "id": task_id, "workflow_id": workflow_id, "step_id": step_id, "state": "SUCCEEDED",
        "metadata": {"merge": "off", "dispatch": dispatch, "fix_loop_round": dict(record)},
    })
    SIGNER.sign_document(task, task_id)
    db.docs[f"tasks/{task_id}"] = task
    if verdict is not None:
        _step_writes(db, objects, workflow_id, step_id,
                     json.dumps(_verdict(True, True, verdict=verdict)))
    else:
        task["result_summary"] = {"git": {"pushed_head": pushed}}
    return task


def _loop_ended(db, running: dict, *, state: str = "SUCCEEDED", final_review: str | None,
                rounds: list[dict] | None = None) -> None:
    """The loop's own ending, as `fixloop._finish` writes it."""
    loop = _loop(db, running)
    loop["state"] = state
    loop["result_summary"] = {"fix_loop": {
        "rounds": [], "final_review": final_review, "final_head": SHA_A,
        "verdict": "MERGE" if final_review else "NOT_YET", "findings_count": 0, "phase": None,
    }}
    loop.setdefault("metadata", {})["fix_loop_state"] = {"rounds": rounds or []}


def _merge_workflows(db) -> list[dict]:
    return [w for w in _docs(db, "workflows").values()
            if any(s.get("step_id") == "merge" for s in w.get("steps") or [])]


def _merge_task(db, workflow: dict) -> dict:
    (step,) = workflow["steps"]
    return db.docs[f"tasks/{step['task_id']}"]


def test_the_submitted_loop_signs_max_rounds_the_runs_fix_rounds(client, db, objects, writes):
    running = _loop_running(client, db, objects, writes, review="NOT_YET")
    compiled = db.docs[f"workflows/{running['workflow_id']}"]
    assert [s["step_id"] for s in compiled["steps"]][-3:] == [
        issueruns.REVIEW_STEP, issueruns.FIX_STEP, LOOP_STEP]
    loop = _loop(db, running)
    review = _step_task(db, running["workflow_id"], issueruns.REVIEW_STEP)
    fix = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    assert loop["runner_profile"] == FIX_LOOP_PROFILE
    assert loop["metadata"]["dispatch"]["fix_loop"] == {
        "review": issueruns.REVIEW_STEP, "review_task_id": review["id"],
        "fix": issueruns.FIX_STEP, "fix_task_id": fix["id"], "max_rounds": 2,
    }
    assert SIGNER.verifies(loop, loop["id"])


def test_a_not_yet_round_zero_whose_final_rereview_says_merge_merges_and_closes(
    client, db, objects, writes, clock,
):
    """MUTATION: read round 0's review in `issueci._review_task_id` -- the run
    FAILs "NOT_YET, not MERGE" and the block says `part of #42`."""
    running = _loop_running(client, db, objects, writes, review="NOT_YET")
    final = _round(db, objects, running, kind="review", round_no=1, verdict="MERGE")
    _loop_ended(db, running, final_review=final["id"])
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert "Closes #42" in writes.pulls[PR]["body"]
    (merge_wf,) = _merge_workflows(db)
    assert run["merge"] == {"workflow_id": merge_wf["workflow_id"], "head_sha": SHA_A}


def test_a_merge_round_zero_behind_a_loop_that_did_not_succeed_neither_merges_nor_closes(
    client, db, objects, writes, clock,
):
    """The loop parked for a person and was stopped: FAILED, `fix_loop_exhausted`.
    Its workflow fails, and round 0's MERGE closes and merges nothing."""
    running = _loop_running(client, db, objects, writes, review="MERGE")
    _loop_ended(db, running, state="FAILED", final_review=None)
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "Closes #42" not in writes.pulls[PR]["body"]
    assert _merge_workflows(db) == []


def test_a_loop_that_recorded_no_final_review_is_no_verdict_whatever_round_zero_said(
    client, db, objects, writes, clock,
):
    """A SUCCEEDED loop whose summary names no final review is read as no
    verdict, never as round 0's.

    MUTATION: fall back to round 0's review when the loop names none -- the
    merge is submitted and the block says `Closes #42`."""
    running = _loop_running(client, db, objects, writes, review="MERGE")
    _loop_ended(db, running, final_review=None)
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    body = writes.pulls[PR]["body"]
    assert "Closes #42" not in body and "part of #42" in body.lower()
    assert _merge_workflows(db) == []
    assert run["state"] == "FAILED"
    assert "recorded no final review" in run["error"]


def test_a_loop_whose_final_review_is_round_zero_merges_on_it(client, db, objects, writes, clock):
    """Round 0 said MERGE: the loop ends SUCCEEDED with no round, its
    `final_review` the review the signed block names (§4.6, step 2)."""
    running = _loop_running(client, db, objects, writes, review="MERGE")
    review = _step_task(db, running["workflow_id"], issueruns.REVIEW_STEP)
    _loop_ended(db, running, final_review=review["id"])
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert "Closes #42" in writes.pulls[PR]["body"]
    assert len(_merge_workflows(db)) == 1


@pytest.mark.parametrize("forgery", ["another_loop", "unsigned", "a_fix_round"])
def test_a_final_review_that_is_not_one_of_the_loops_reviews_is_not_read(
    client, db, objects, writes, clock, forgery,
):
    """`result_summary` is tenant-writable: a final review the loop did not run
    is no verdict, however it says MERGE."""
    running = _loop_running(client, db, objects, writes, review="NOT_YET")
    if forgery == "another_loop":
        forged = _round(db, objects, running, kind="review", round_no=1, verdict="MERGE",
                        loop_task=_task_id())
    elif forgery == "unsigned":
        forged = _round(db, objects, running, kind="review", round_no=1, verdict="MERGE")
        forged["spec_signature"] = None
    else:
        forged = _round(db, objects, running, kind="fix", round_no=2, pushed=SHA_A)
        _step_writes(db, objects, forged["workflow_id"], "review-fix",
                     json.dumps(_verdict(True, True, verdict="MERGE")))
    _loop_ended(db, running, final_review=forged["id"])
    writes.check(SHA_A, "unit", "success")

    run = _read(client, clock, running["id"])

    assert "Closes #42" not in writes.pulls[PR]["body"]
    assert _merge_workflows(db) == []
    assert run["state"] == "FAILED"
    assert "not one of that loop's reviews" in run["error"]


def test_a_head_a_review_fix_round_pushed_is_merged_continuing_that_round(
    client, db, objects, writes, clock,
):
    """Round 2's fix pushed SHA_B and its re-review said MERGE: the pull
    request's head is the round's, and the merge continues it.

    MUTATION: drop `_loop_fix_heads` from `issueci._pushing_task` -- the run
    FAILs "not pushed by any of this run's tasks". MUTATION: drop its branch
    of `merge_target_unbound` -- it FAILs "which is not this run's"."""
    running = _loop_running(client, db, objects, writes, review="NOT_YET")
    first = _round(db, objects, running, kind="review", round_no=1, verdict="NOT_YET")
    fix = _round(db, objects, running, kind="fix", round_no=2, pushed=SHA_B)
    final = _round(db, objects, running, kind="review", round_no=2, verdict="MERGE")
    _loop_ended(db, running, final_review=final["id"], rounds=[
        {"round": 1, "kind": "review", "task_id": first["id"]},
        {"round": 2, "kind": "fix", "task_id": fix["id"]},
        {"round": 2, "kind": "review", "task_id": final["id"]},
    ])
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    assert "Closes #42" in writes.pulls[PR]["body"]
    (merge_wf,) = _merge_workflows(db)
    assert run["merge"]["head_sha"] == SHA_B
    task = _merge_task(db, merge_wf)
    assert task["metadata"]["dispatch"]["merge_target"] == {
        "pull_request": fix["id"], "pull_request_workflow": fix["workflow_id"],
    }


def test_a_fix_round_of_another_loop_does_not_bind_its_head(client, db, objects, writes, clock):
    running = _loop_running(client, db, objects, writes, review="NOT_YET")
    fix = _round(db, objects, running, kind="fix", round_no=2, pushed=SHA_B,
                 loop_task=_task_id())
    final = _round(db, objects, running, kind="review", round_no=2, verdict="MERGE")
    _loop_ended(db, running, final_review=final["id"], rounds=[
        {"round": 2, "kind": "fix", "task_id": fix["id"]},
    ])
    writes.open_pull(PR, SHA_B)
    writes.check(SHA_B, "unit", "success")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED"
    assert "not pushed by any of this run's tasks" in run["error"]
    assert _merge_workflows(db) == []
