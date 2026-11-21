"""A fix round whose task cannot be read has NOT ended (issue 52).

`_ended` read any failure of `ctx.fetch_upstream` -- a 5xx, a network blip
-- as "the round ended": `merge_conflict` was refused "after conflict fix
round N ... ended at this head" while that round may still have been
running, and `_live_round`'s one-writer-per-pull-request guard let a second
fix round start onto the same branch. `_task_state` already reads
unreadable as not ended; the round reads now agree with it: the step waits
CONFLICT_FIX_RUNNING, naming the failed read, and logs it with the task id.

The control: a round whose task reads terminal at this head still refuses
`merge_conflict` as before.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, PINNED, TASK, TENANT, MergeWorld

ROUND_TASK = "task_conflict1"
EARLIER = "a" * 40


class _Blip(ConnectionError):
    """A transient read failure of the round's task document."""


def _conflicting(world: MergeWorld) -> None:
    world.pr.update(mergeable=False, mergeable_state="dirty")
    world.runs = []


def _round(world: MergeWorld, *, key: str = merge.CONFLICT_ROUNDS_KEY,
           head: str = PINNED) -> None:
    world.docs[TASK]["metadata"] = {merge.MERGE_FIX_METADATA_KEY: {key: [
        {"round": 1, "head": head, "task_id": ROUND_TASK, "workflow_id": "wf_conflict"}]}}
    world.docs[ROUND_TASK] = {
        "tenant_id": TENANT, "workflow_id": "wf_conflict", "state": "RUNNING",
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": OPENER}},
        "result_summary": {"git": {}},
    }


def _unreadable_round(world: MergeWorld):
    """The step's context, with every read of ROUND_TASK raising `_Blip`."""
    ctx = world.context()
    read = ctx.fetch_upstream

    def fetch(task_id: str) -> dict[str, Any]:
        if task_id == ROUND_TASK:
            raise _Blip("firestore answered 503")
        return read(task_id)

    return replace(ctx, fetch_upstream=fetch)


def _waits_on_the_failed_read(world: MergeWorld, outcome) -> str:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None
    assert "refusal" not in outcome.summary, outcome.summary
    # CONFLICT_FIX_RUNNING, never _PENDING: _PENDING tells the tick the
    # next round is due, which would put a second writer on the branch.
    assert outcome.ci_wait["code"] == merge.CONFLICT_FIX_RUNNING, outcome.ci_wait
    message = outcome.summary["wait"]["message"]
    assert f"fix round 1 ({ROUND_TASK}) could not be read: _Blip; waiting" in message, message
    assert any(ROUND_TASK in line and "_Blip" in line for line in world.log.lines), \
        world.log.text
    assert world.merge_calls() == [] and world.update_calls() == []
    return message


def test_an_unreadable_conflict_round_at_this_head_waits_instead_of_refusing(tmp_path):
    """MUTATION (merge.py `_ended`): read a raising fetch as ended, and this
    refuses `merge_conflict` "after conflict fix round 1 ... ended"."""
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world)

    outcome = merge.run_merge(_unreadable_round(world))

    message = _waits_on_the_failed_read(world, outcome)
    assert message.startswith(f"pull request #{NUMBER} conflicts with main")
    assert "conflict fix round 1" in message


@pytest.mark.parametrize("key,kind", [(merge.CONFLICT_ROUNDS_KEY, "conflict"), ("rounds", "CI")])
def test_an_unreadable_round_at_an_earlier_head_starts_no_second_round(tmp_path, key, kind):
    """One writer per pull request: a round whose task cannot be read may
    still be pushing, so the next round is not due."""
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world, key=key, head=EARLIER)

    outcome = merge.run_merge(_unreadable_round(world))

    message = _waits_on_the_failed_read(world, outcome)
    assert f"{kind} fix round 1 ({ROUND_TASK})" in message
    assert "is due" not in message


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_a_terminal_round_at_this_head_still_refuses_merge_conflict(tmp_path, state):
    """The control: a round read as ended is `merge_conflict`, as today."""
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world)
    world.docs[ROUND_TASK]["state"] = state

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "merge_conflict"
    message = outcome.summary["refusal"]["message"]
    assert f"after conflict fix round 1 ({ROUND_TASK}) ended at this head" in message


def test_live_round_returns_an_unreadable_round_as_live(tmp_path):
    world = MergeWorld(tmp_path)
    _round(world, head=EARLIER)
    live = merge._live_round(_unreadable_round(world), world.docs[TASK])

    assert live is not None
    kind, entry = live[0], live[1]
    assert kind == "conflict" and entry["task_id"] == ROUND_TASK


def test_live_round_passes_over_an_ended_round(tmp_path):
    world = MergeWorld(tmp_path)
    _round(world, head=EARLIER)
    world.docs[ROUND_TASK]["state"] = "SUCCEEDED"
    assert merge._live_round(world.context(), world.docs[TASK]) is None
