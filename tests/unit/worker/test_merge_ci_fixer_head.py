"""The merge step and the head the pull request's own CI fixer pushed (#1073).

The PR-side CI fixer (`ci-fix.yml` -> scripts/ci-fix.sh) submits a one-step
`continues_task` workflow of the task whose branch the pull request is on,
which swarm-api stamps `metadata.ci_fix = {pull_request, run_id, head_sha,
...}` (`swarm_api.cifix`). It is never one of the merge task's
`metadata.merge_fix` rounds, so before #1073 the head it pushed was refused
`head_moved`: wf_ca1fa9e2701642db890e, PR 1071, fixer
task_b87630a30d7b43428d8f at ed1b9056. An operator had to resubmit a
`merge_pr` at the new head.

What the WORKER holds here:

  1. The fixer's head is accepted -- by itself, or under GitHub's own base
     merges -- only from facts a tenant-written record cannot forge: the
     fixer task is this tenant's, its signed spec verifies against ITS OWN
     signed workflow, its SIGNED dispatch block `continues` the root whose
     branch the pull request is on, and the head is its own
     `result_summary`. `metadata.ci_fix` is unsigned: it narrows the query
     and decides waiting, and is never evidence of a head. Every other head
     is still `head_moved`.
  2. Red at a head a still-running fixer was submitted for parks
     `ci_fix_running`; once that fixer has ended it is `checks_failed`.
  3. Finding fixers is a task-store read that needs no credential, so it
     happens before the token read, and an unreadable query has no
     candidates.

No credentials, no network. The fake forge is `merge_world.MergeWorld`;
its fake query applies `inputs.find_ci_fix_tasks`' three filters.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, PINNED, TASK, TENANT, MergeWorld

FIXER = "task_cifix1"
FIXED = "f" * 40


def _fixer(world: MergeWorld, *, task_id: str = FIXER, continues: str = OPENER,
           pushed: str | None = FIXED, state: str = "SUCCEEDED", tenant: str = TENANT,
           red_at: str = PINNED, pull_request: int = NUMBER) -> dict[str, Any]:
    """A CI-fixer task as swarm-api stores it: its own workflow, signed
    `continues`, the unsigned `ci_fix` record, and the head it pushed."""
    git = {"pushed_head": pushed} if pushed else {}
    doc = {
        "tenant_id": tenant, "workflow_id": "wf_cifix", "state": state,
        "metadata": {
            "dispatch": {"strategy": "direct-pr", "continues": continues},
            "ci_fix": {"pull_request": pull_request, "run_id": 9001, "head_sha": red_at,
                       "attempt": 1, "max_attempts": 2},
        },
        "result_summary": {"git": git},
    }
    world.docs[task_id] = doc
    return doc


def _at(world: MergeWorld, head: str) -> None:
    world.pr["head"]["sha"] = head
    world.serve_checks(head)


def _red(world: MergeWorld) -> None:
    world.runs[0].update(conclusion="failure")


def _parked(outcome, code: str, head: str) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None and outcome.retryable is False
    assert outcome.ci_wait["code"] == code, outcome.ci_wait
    assert outcome.ci_wait["head"] == head
    assert "refusal" not in outcome.summary


# ---------------------------------------------------------------------------
# 1. the head the CI fixer pushed
# ---------------------------------------------------------------------------

def test_the_head_the_prs_ci_fixer_pushed_is_merged_there(tmp_path):
    """The incident: pinned at PINNED, the fixer pushed FIXED, green there."""
    world = MergeWorld(tmp_path)
    _fixer(world)
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == FIXED
    assert outcome.summary["head"] == FIXED
    assert outcome.summary["head_pushed_by"] == FIXER
    assert outcome.summary["updates"] == 0


def test_the_ci_fixers_head_under_githubs_base_merge_is_accepted(tmp_path):
    world = MergeWorld(tmp_path)
    _fixer(world)
    updated = world.github_merge(FIXED)
    world.pr["head"]["sha"] = updated

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == updated
    assert outcome.summary["updates"] == 1
    assert outcome.summary["head_pushed_by"] == FIXER


def test_pending_checks_at_the_ci_fixers_head_park_there(tmp_path):
    world = MergeWorld(tmp_path)
    _fixer(world)
    _at(world, FIXED)
    world.runs[0].update(status="in_progress", conclusion=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, "checks_pending", FIXED)
    assert outcome.summary["head_pushed_by"] == FIXER
    assert world.merge_calls() == []


def _leaky(world: MergeWorld):
    """A query that answers every task carrying `ci_fix`, whatever its tenant
    or root: the worker's own checks, not the query's filters, refuse it."""
    def find(_root: str, _pull_request: int) -> list[dict[str, Any]]:
        return [{**doc, "id": task_id} for task_id, doc in world.docs.items()
                if "ci_fix" in (doc.get("metadata") or {})]
    return find


def _another_tenant(world: MergeWorld) -> None:
    _fixer(world, tenant="research")


def _unsigned(world: MergeWorld) -> None:
    _fixer(world)
    world.unverified[FIXER] = "unsigned"


def _signature_invalid(world: MergeWorld) -> None:
    _fixer(world)
    world.unverified[FIXER] = "signature_mismatch"


def _other_root(world: MergeWorld) -> None:
    _fixer(world, continues="task_other")


def _not_a_continuation(world: MergeWorld) -> None:
    _fixer(world)["metadata"]["dispatch"].pop("continues")


def _no_pushed_head(world: MergeWorld) -> None:
    _fixer(world, pushed=None)


def _other_sha(world: MergeWorld) -> None:
    _fixer(world, pushed="9" * 40)


def _another_pull_request(world: MergeWorld) -> None:
    _fixer(world, pull_request=NUMBER + 1)


def _no_task(_world: MergeWorld) -> None:
    pass


HEAD_MOVED = [
    ("fixer_of_another_tenant", _another_tenant),
    ("fixer_unsigned", _unsigned),
    ("fixer_signature_invalid", _signature_invalid),
    ("fixer_continues_another_root", _other_root),
    ("fixer_is_not_a_continuation", _not_a_continuation),
    ("fixer_pushed_no_head", _no_pushed_head),
    ("fixer_pushed_another_sha", _other_sha),
    ("fixer_of_another_pull_request", _another_pull_request),
    ("no_task_pushed_it", _no_task),
]


@pytest.mark.parametrize("leaky", [False, True], ids=["query", "leaky_query"])
@pytest.mark.parametrize(("case", "mutate"), HEAD_MOVED, ids=[c[0] for c in HEAD_MOVED])
def test_any_other_head_is_still_head_moved(tmp_path, case, mutate, leaky):
    """The control is the first test of this file: the same head, accepted
    there, refused here for exactly one broken fact about the fixer. Run
    once through the query's own filters and once through a query that
    returns everything, so the worker's checks are proven by themselves."""
    world = MergeWorld(tmp_path)
    mutate(world)
    _at(world, FIXED)
    ctx = world.context()
    if leaky:
        ctx = dataclasses.replace(ctx, find_ci_fixes=_leaky(world))

    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.FAILED, (case, outcome.message)
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "head_moved", (case, outcome.summary)
    assert "nor the CI fixer's" in outcome.summary["refusal"]["message"]
    assert "head_pushed_by" not in outcome.summary
    assert world.merge_calls() == [] and world.update_calls() == []


# ---------------------------------------------------------------------------
# 2. red, and the fixer
# ---------------------------------------------------------------------------

def test_red_with_the_ci_fixer_running_at_this_head_parks(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _fixer(world, state="RUNNING", pushed=None, red_at=PINNED)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_RUNNING, PINNED)
    assert FIXER in outcome.summary["wait"]["message"]
    assert world.merge_calls() == []


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_red_after_the_ci_fixer_ended_at_this_head_is_checks_failed(tmp_path, state):
    world = MergeWorld(tmp_path)
    _red(world)
    _fixer(world, state=state, pushed=None, red_at=PINNED)

    outcome = merge.run_merge(world.context())

    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_red_with_an_unsigned_running_fixer_is_checks_failed(tmp_path):
    """`ci_fix` is unsigned: a forged running fixer makes nothing wait."""
    world = MergeWorld(tmp_path)
    _red(world)
    _fixer(world, state="RUNNING", pushed=None, red_at=PINNED)
    world.unverified[FIXER] = "unsigned"

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_red_with_a_fixer_running_at_another_head_is_checks_failed(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _fixer(world, state="RUNNING", pushed=None, red_at="9" * 40)

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_failed"


# ---------------------------------------------------------------------------
# 3. the query
# ---------------------------------------------------------------------------

def test_the_fixers_are_found_before_the_token_is_read(tmp_path):
    world = MergeWorld(tmp_path)
    _fixer(world)
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert world.fixer_queries == [0]
    assert world.token_reads == 1


def test_an_unreadable_fixer_query_has_no_candidates(tmp_path):
    world = MergeWorld(tmp_path)
    _fixer(world)
    _at(world, FIXED)
    world.fixer_query_error = RuntimeError("the task store did not answer")

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert world.fixer_queries == [0]


def test_a_fixer_already_a_merge_fix_round_is_one_candidate(tmp_path):
    """The same task named by both: accepted once, named once."""
    world = MergeWorld(tmp_path)
    _fixer(world)
    world.docs[TASK]["metadata"] = {
        "merge_fix_rounds": 1,
        merge.MERGE_FIX_METADATA_KEY: {"rounds": [{"round": 1, "head": PINNED,
                                                   "task_id": FIXER}]},
    }
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["head_pushed_by"] == FIXER


def test_the_worker_restates_the_apis_ci_fix_keys():
    from swarm_api import cifix

    assert merge.CI_FIX_METADATA_KEY == cifix.CI_FIX_METADATA_FIELD == "ci_fix"
    assert merge.CI_FIX_MAX_ATTEMPTS == cifix.MAX_ATTEMPTS_CEILING
    assert {"pull_request", "head_sha"} <= cifix.CALLER_FIELDS
    assert merge.CI_FIX_PULL_REQUEST == "pull_request"
    assert merge.CI_FIX_HEAD == "head_sha"
