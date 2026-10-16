"""A head a platform fix round pushed for THIS pull request counts, whichever workflow it ran in.

Measured on 2026-10-15 02:16Z (epic #1334): HANG-LEASE's merge step
(wf_605ebc3f) refused PR 1338 `head_moved` at 34ce829e, the commit fix task
task_fcdff76b (wf_87a59d23) pushed to the pull request's branch. That fix ran
as its own workflow, so the merge step's own `merge_fix` record never named it,
and the head was "nor a head the platform's CI fixer recorded for this pull
request". The pull request merged through GitHub's auto-merge instead.

swarm-api's merge-wake tick now records every platform fix round's pushed
head -- a PR-side CI fix, a merge step's CI-fix round, a conflict fix round
-- against the PULL REQUEST (repository + number), on the task whose
`swarm/<id>` branch the pull request is on (`cifix.FIX_HEADS_FIELD`). The
merge step reads them there. What this file holds:

  1. A head recorded for this pull request by a round of ANOTHER workflow --
     no `ci_fix` record, not one of this step's rounds -- is accepted, its
     task verified against its OWN signed workflow.
  2. The record is an index, never the proof: a head nobody recorded is still
     `head_moved`, and so is one recorded for another pull request or
     repository, or whose task fails any single fact (tenant, signature, the
     branch it continues, the repository, the head it pushed, readable).

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import NAME, NUMBER, OPENER, OWNER, REPO_URL, TENANT, MergeWorld

ROUND = "task_fcdff76b00000000aaaa"
ROUND_WORKFLOW = "wf_87a59d23"
FIXED = "34ce829e" + "0" * 32


def _round(*, continues: str = OPENER, pushed: str | None = FIXED, tenant: str = TENANT,
           repository_url: str = REPO_URL, workflow_id: str = ROUND_WORKFLOW) -> dict[str, Any]:
    """A fix round another workflow submitted: no `ci_fix`, no entry in this step's rounds."""
    return {
        "tenant_id": tenant, "workflow_id": workflow_id, "state": "SUCCEEDED",
        "repository_url": repository_url,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": continues},
                     "merge_fix_round": {"merge_task": "task_another_merge", "round": 1,
                                         "pull_request": NUMBER}},
        "result_summary": {"git": {"pushed_head": pushed} if pushed else {}},
    }


def _entry(*, number: int = NUMBER, repository: str = f"{OWNER}/{NAME}",
           task_id: str = ROUND, head: str = FIXED) -> dict[str, Any]:
    return {"repository": repository, "pull_request": number, "task_id": task_id,
            "head": head, "workflow_id": ROUND_WORKFLOW, "kind": "merge_fix_round"}


def _recorded(world: MergeWorld, *entries: dict[str, Any]) -> None:
    """What the tick wrote on the branch's task: the opener here, whose branch it is."""
    world.docs[OPENER]["metadata"][merge.PR_FIX_HEADS_FIELD] = list(entries)


def _at(world: MergeWorld, head: str) -> None:
    world.pr["head"]["sha"] = head
    world.serve_checks(head)


def _world(tmp_path) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.docs[ROUND] = _round()
    _recorded(world, _entry())
    _at(world, FIXED)
    return world


def test_a_head_a_fix_round_of_another_workflow_pushed_for_this_pull_request_is_accepted(tmp_path):
    world = _world(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == FIXED
    assert outcome.summary["head_pushed_by"] == ROUND
    # Verified against the round's OWN signed workflow, never this merge's.
    assert (ROUND, ROUND_WORKFLOW) in world.verified


def test_the_recorded_head_under_githubs_own_base_merge_is_accepted(tmp_path):
    world = _world(tmp_path)
    updated = world.github_merge(FIXED)
    world.pr["head"]["sha"] = updated

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["updates"] == 1


def test_the_record_on_a_continued_root_is_read_from_the_root(tmp_path):
    """An opener that is itself a continuation: the branch, and the record, are its root's."""
    world = _world(tmp_path)
    root = "task_root0000000000000000"
    world.docs[OPENER]["metadata"]["dispatch"]["continues"] = root
    del world.docs[OPENER]["metadata"][merge.PR_FIX_HEADS_FIELD]
    world.docs[root] = {"tenant_id": TENANT, "workflow_id": "wf_0",
                        "metadata": {merge.PR_FIX_HEADS_FIELD: [_entry()]}}
    world.docs[ROUND] = _round(continues=root)
    world.pr["head"]["ref"] = f"swarm/{root}"

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["head_pushed_by"] == ROUND


def _nobody_recorded(world: MergeWorld) -> None:
    del world.docs[OPENER]["metadata"][merge.PR_FIX_HEADS_FIELD]


def _another_pull_request(world: MergeWorld) -> None:
    _recorded(world, _entry(number=NUMBER + 1))


def _another_repository(world: MergeWorld) -> None:
    _recorded(world, _entry(repository=f"{OWNER}/other"))


def _round_continues_another_root(world: MergeWorld) -> None:
    world.docs[ROUND] = _round(continues="task_other")


def _round_in_another_repository(world: MergeWorld) -> None:
    world.docs[ROUND] = _round(repository_url="https://github.com/octo-org/other.git")


def _round_of_another_tenant(world: MergeWorld) -> None:
    world.docs[ROUND] = _round(tenant="research")


def _round_unsigned(world: MergeWorld) -> None:
    world.unverified[ROUND] = "signature_mismatch"


def _round_pushed_another_head(world: MergeWorld) -> None:
    world.docs[ROUND] = _round(pushed="9" * 40)


def _round_unreadable(world: MergeWorld) -> None:
    del world.docs[ROUND]


def _record_not_a_list(world: MergeWorld) -> None:
    world.docs[OPENER]["metadata"][merge.PR_FIX_HEADS_FIELD] = _entry()


REFUSED = [
    ("nobody_recorded_it", _nobody_recorded),
    ("recorded_for_another_pull_request", _another_pull_request),
    ("recorded_for_another_repository", _another_repository),
    ("round_continues_another_root", _round_continues_another_root),
    ("round_in_another_repository", _round_in_another_repository),
    ("round_of_another_tenant", _round_of_another_tenant),
    ("round_spec_unverified", _round_unsigned),
    ("round_pushed_another_head", _round_pushed_another_head),
    ("round_unreadable", _round_unreadable),
    ("record_not_a_list", _record_not_a_list),
]


@pytest.mark.parametrize(("case", "mutate"), REFUSED, ids=[c[0] for c in REFUSED])
def test_a_head_nobody_recorded_for_this_pull_request_is_still_head_moved(tmp_path, case, mutate):
    """The control is the first test of this file: the same head, accepted
    there, refused here for exactly one missing or broken fact."""
    world = _world(tmp_path)
    mutate(world)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, (case, outcome.message)
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "head_moved", (case, outcome.summary)
    assert world.merge_calls() == [] and world.update_calls() == []


def test_the_worker_reads_the_record_swarm_api_writes():
    from swarm_api import cifix

    assert merge.PR_FIX_HEADS_FIELD == cifix.FIX_HEADS_FIELD
    assert merge.PR_FIX_HEADS_READ >= cifix.FIX_HEADS_KEPT
