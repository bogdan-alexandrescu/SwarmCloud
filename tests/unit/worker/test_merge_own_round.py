"""A merge step's own conflict-fix and CI-fix rounds, in the shape #2373 measured.

On 2026-10-25 four MERGE-verdict workflows (PRs 2341, 2343, 2349, 2352) ended
`head_moved` on the head their merge step's own round had pushed moments
earlier. Each was strategy `integrate`, its pull request opened by the fix
step from `swarm/<fix task>`, and its round submitted by swarm-api's merge
wake tick (`mergewake._on_conflict` / `_on_red`).

The issue suspected `_fix_round_heads`' filter: that the round `continues` a
task other than the root `_branch_task(opener, target)` compares against.
Section 1 holds that suspicion REFUTED from the code: the tick submits the
round `continues_task=<the signed target>`, swarm-api's
`continuation.resolve_continuation` signs the round's `continues` as that
target's own `continues` or, absent one, the target -- the same rule as
`_branch_task` and `mergewake._target_and_root` -- and with the PR on
`swarm/<fix task>` that is the fix task in all three. Read as a direct
Firestore worker reads, those rounds' heads are accepted.

What did fail is the read. Tenant `eng` is on the state routes
(terraform/environments/dev/dev.tfvars `state_via_api_tenants`, cutover b1),
and `GET state/upstream` serves only a task the merge DECLARES: neither its
own task -- which holds `merge_fix` -- nor a round's task, which is another
workflow's. Both reads raise, so no round is a candidate. Section 3 holds
that the refusal now says which records it could not read, rather than
reading as a foreign push; the read itself is swarm-api's to widen
(questions.json of lane MERGE-OWN-ROUND).

Section 2 holds the guard: a round of another branch, another tenant, or a
`merge_fix` entry naming an unsigned task is still `head_moved`.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_worker import merge
from agent_worker.errors import InputUnavailable
from swarm_common.states import TaskState

from merge_world import OPENER, PINNED, TASK, TENANT, MergeWorld

#: The fix step that opened the pull request from its own branch, as
#: task_7cdf691e03c344cabfcb did for PR 2341: an `integrate` workflow's
#: integrator, whose dispatch block continues nothing.
FIX_STEP = OPENER
CONFLICT_ROUND = "task_9c2cea64"
CONFLICT_WORKFLOW = "wf_c291de19"
CI_ROUND = "task_7131dcf3"
CI_WORKFLOW = "wf_ci_round"
#: The round's merge of main into the branch (6a52d2b2 on PR 2341).
MERGED_MAIN = "6" * 40
#: The round's one-line guard fix (8d6e2761 on PR 2343).
GUARD_FIX = "8" * 40


def _record(world: MergeWorld, key: str, task_id: str, workflow_id: str) -> None:
    """The tick's `merge_fix` entry for round 1 at the pinned head, as `_hand_off` writes it."""
    world.docs[TASK]["metadata"] = {
        "merge_fix_rounds": 1,
        merge.MERGE_FIX_METADATA_KEY: {key: [
            {"round": 1, "head": PINNED, "task_id": task_id, "workflow_id": workflow_id},
        ]},
    }


def _round_task(world: MergeWorld, task_id: str, workflow_id: str, pushed: str, *,
                continues: str = FIX_STEP, tenant: str = TENANT) -> None:
    """The round's task as swarm-api signs it: a one-step `direct-pr` workflow
    whose dispatch `continues` the root of `continues_task`."""
    world.docs[task_id] = {
        "tenant_id": tenant, "workflow_id": workflow_id, "state": "SUCCEEDED",
        "repository_url": world.repository_url,
        "metadata": {"dispatch": {"strategy": "direct-pr", "carrier": "checkpoints",
                                  "continues": continues}},
        "result_summary": {"git": {"pushed_head": pushed, "branch": f"swarm/{FIX_STEP}"}},
    }


def _at(world: MergeWorld, head: str) -> None:
    world.pr["head"]["sha"] = head
    world.serve_checks(head)


def _conflict_world(tmp_path, **round_task: Any) -> MergeWorld:
    """wf_858cb62dfe664ebb96b8's shape: a conflict-fix round merged main and pushed."""
    world = MergeWorld(tmp_path)
    assert world.pr["head"]["ref"] == f"swarm/{FIX_STEP}"
    assert world.docs[FIX_STEP]["metadata"]["dispatch"] == {
        "strategy": "integrate", "role": "integrator"}
    _record(world, merge.CONFLICT_ROUNDS_KEY, CONFLICT_ROUND, CONFLICT_WORKFLOW)
    _round_task(world, CONFLICT_ROUND, CONFLICT_WORKFLOW, MERGED_MAIN, **round_task)
    _at(world, MERGED_MAIN)
    return world


def _ci_world(tmp_path, **round_task: Any) -> MergeWorld:
    """wf_30cb76aa6a564c4aa984's shape: a CI-fix round pushed a one-line fix."""
    world = MergeWorld(tmp_path)
    _record(world, "rounds", CI_ROUND, CI_WORKFLOW)
    _round_task(world, CI_ROUND, CI_WORKFLOW, GUARD_FIX, **round_task)
    _at(world, GUARD_FIX)
    return world


def _head_moved(outcome) -> str:
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "head_moved", outcome.summary
    return outcome.summary["refusal"]["message"]


# ---------------------------------------------------------------------------
# 1. read as the direct path reads, the round's head is accepted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("build", "round_task", "head", "workflow"), [
    (_conflict_world, CONFLICT_ROUND, MERGED_MAIN, CONFLICT_WORKFLOW),
    (_ci_world, CI_ROUND, GUARD_FIX, CI_WORKFLOW),
], ids=["conflict-fix", "ci-fix"])
def test_an_integrate_fix_steps_own_round_head_is_accepted(tmp_path, build, round_task,
                                                           head, workflow):
    world = build(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == head
    assert outcome.summary["head_pushed_by"] == round_task
    assert (round_task, workflow) in world.verified
    assert "fix_records_unread" not in outcome.summary


def test_the_tick_signs_the_round_continuing_the_root_the_worker_compares():
    """The suspicion, refuted: the tick's root, the continuation's signed
    `continues` and `_branch_task` are one rule, for an opener that continues
    nothing (the integrator) and for one that continues a branch."""
    from swarm_api import mergewake

    class _Task:
        def __init__(self, metadata: dict[str, Any]) -> None:
            self.metadata = metadata

    class _Store:
        def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
            self.docs = docs

        def get_task(self, _tenant: str, task_id: str, submitted_by: Any = None) -> _Task:
            return _Task(self.docs[task_id]["metadata"])

    class _Ctx:
        def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
            self.store = _Store(docs)

    # The tick reads ids in swarm-api's own shape (`TASK_ID_RE`).
    fix_step, branch = "task_7cdf691e03c344cabfcb", "task_" + "b" * 20
    merge_doc = {"metadata": {"dispatch": {"merge_target": {"pull_request": fix_step}}}}
    for dispatch, root in [({"strategy": "integrate", "role": "integrator"}, fix_step),
                           ({"strategy": "direct-pr", "continues": branch}, branch)]:
        opener = {"metadata": {"dispatch": dispatch}}
        assert mergewake._target_and_root(_Ctx({fix_step: opener}), TENANT, merge_doc) == (
            fix_step, root)
        assert merge._branch_task(opener, fix_step) == root


# ---------------------------------------------------------------------------
# 2. the guard: anything else is still head_moved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("build", [_conflict_world, _ci_world], ids=["conflict-fix", "ci-fix"])
def test_a_round_continuing_another_branchs_task_is_head_moved(tmp_path, build):
    world = build(tmp_path, continues="task_other_branch")

    _head_moved(merge.run_merge(world.context()))
    assert world.merge_calls() == []


@pytest.mark.parametrize("build", [_conflict_world, _ci_world], ids=["conflict-fix", "ci-fix"])
def test_a_round_of_another_tenant_is_head_moved(tmp_path, build):
    world = build(tmp_path, tenant="research")

    _head_moved(merge.run_merge(world.context()))
    assert world.merge_calls() == []


@pytest.mark.parametrize(("build", "round_task"), [(_conflict_world, CONFLICT_ROUND),
                                                   (_ci_world, CI_ROUND)],
                         ids=["conflict-fix", "ci-fix"])
def test_a_merge_fix_entry_naming_an_unsigned_task_is_head_moved(tmp_path, build, round_task):
    world = build(tmp_path)
    world.unverified[round_task] = "signature_invalid"

    message = _head_moved(merge.run_merge(world.context()))
    assert world.merge_calls() == []
    # Read, then refused by its signature: the guard working, not a lost read.
    assert "could not read" not in message


# ---------------------------------------------------------------------------
# 3. a record the step could not read is named in the refusal (red first)
# ---------------------------------------------------------------------------

def _state_routes(world: MergeWorld, *refused: str):
    """`world.context()` whose upstream read refuses `refused` as the state
    routes do (`inputs._route_upstream` on `upstream_not_declared`)."""
    ctx = world.context()
    served = ctx.fetch_upstream

    def fetch(task_id: str) -> dict[str, Any]:
        if task_id in refused:
            raise InputUnavailable(
                f"upstream task {task_id} is not one this task declares as "
                "upstream, so the state routes refuse to read it")
        return served(task_id)

    return dataclasses.replace(ctx, fetch_upstream=fetch)


@pytest.mark.parametrize("build", [_conflict_world, _ci_world], ids=["conflict-fix", "ci-fix"])
def test_an_unreadable_own_record_is_named_in_the_head_moved_refusal(tmp_path, build):
    world = build(tmp_path)

    outcome = merge.run_merge(_state_routes(world, TASK))

    message = _head_moved(outcome)
    assert f"could not read its own task {TASK}, which records its rounds" in message
    assert "state routes refuse" in message
    (noted,) = outcome.summary[merge.FIX_RECORDS_UNREAD_KEY]
    assert noted.startswith(f"its own task {TASK}")


@pytest.mark.parametrize(("build", "round_task"), [(_conflict_world, CONFLICT_ROUND),
                                                   (_ci_world, CI_ROUND)],
                         ids=["conflict-fix", "ci-fix"])
def test_an_unreadable_round_task_is_named_in_the_head_moved_refusal(tmp_path, build,
                                                                     round_task):
    world = build(tmp_path)

    outcome = merge.run_merge(_state_routes(world, round_task))

    message = _head_moved(outcome)
    assert f"could not read round task {round_task} (" in message
    assert outcome.summary[merge.FIX_RECORDS_UNREAD_KEY] == [
        f"round task {round_task} (upstream task {round_task} is not one this task "
        "declares as upstream, so the state routes refuse to read it)"]


def test_unreadable_records_are_bounded_in_the_summary(tmp_path):
    world = MergeWorld(tmp_path)
    many = [f"task_round{n}" for n in range(merge.FIX_RECORDS_UNREAD_MAX + 3)]
    world.docs[TASK]["metadata"] = {merge.MERGE_FIX_METADATA_KEY: {"rounds": [
        {"round": n + 1, "head": f"{n + 1:040x}", "task_id": t} for n, t in enumerate(many)]}}
    _at(world, GUARD_FIX)

    outcome = merge.run_merge(_state_routes(world, *many))

    _head_moved(outcome)
    noted = outcome.summary[merge.FIX_RECORDS_UNREAD_KEY]
    assert len(noted) == merge.FIX_RECORDS_UNREAD_MAX
    assert all(len(n) <= len("round task ") + len(many[-1]) + 3
               + merge.FIX_RECORDS_UNREAD_CHARS for n in noted)
