"""The merge step and the repository's own merge train (owner decisions
2026-10-14 "A1 Merge train" and 2026-11-03, turning it on).

.github/workflows/merge-train.yml and scripts/merge-train.sh board green
`ready` pull requests, test them together on main and squash-merge each;
`vars.MERGE_TRAIN` exactly `on` is the switch both workflows read. With it
on, the merge step, green and told MERGE, labels its pull request `ready` and
parks on the train instead of taking the merge slot and updating the branch;
it ends SUCCEEDED once the pull request is merged, and an ejection (the
train's comment, `ready` removed) is a refusal, or the gate's fix round when
the pull request now conflicts. With the switch off nothing changes: the
controls below merge directly and never read or write a label.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker import merge, mergetrain
from agent_worker.errors import ExitCode, FencedError
from swarm_common.models import EndCause, utcnow
from swarm_common.states import TaskState

from merge_world import API, MERGED, NUMBER, PINNED, TASK, MergeWorld

VARIABLE = f"{API}/actions/variables/MERGE_TRAIN"
LABELS = f"{API}/issues/{NUMBER}/labels"
COMMENTS = f"{API}/issues/{NUMBER}/comments"
EJECTION = ("**Ejected from the merge train, and `ready` removed.** Train 9-1 -- main plus "
            "this pull request alone -- failed:\n\n* ci-gate: https://example.test/run/9")


def _switch(world: MergeWorld, value: str | None = "on", *, status: int = 200) -> None:
    """Serve `vars.MERGE_TRAIN`; None leaves it unset (GitHub's 404)."""
    if value is None and status == 200:
        return
    body = ({"name": "MERGE_TRAIN", "value": value} if status == 200
            else {"message": "Resource not accessible by integration"})
    world.github.route("GET", VARIABLE, (status, {}, body))
    world.github.route("POST", LABELS, (200, {}, [{"name": "ready"}]))


def _comments(world: MergeWorld, *bodies: tuple[str, str]) -> None:
    world.github.route("GET", COMMENTS, (200, {}, [
        {"id": i, "body": body, "user": {"login": login, "type": "Bot" if "[bot]" in login
                                         else "User"}}
        for i, (login, body) in enumerate(bodies, 1)]))


def _parked_on_train(world: MergeWorld, head: str = PINNED) -> None:
    """The step's own record after an attempt that boarded the train (the fenced park)."""
    world.docs[TASK]["metadata"] = {control_mod.MERGE_WAIT_METADATA_KEY: {
        "code": mergetrain.TRAIN_WAITING, "head": head, "pull_request": NUMBER, "pending": [],
        "train_waits": 1, "first_parked_at": utcnow() - timedelta(minutes=20)}}


def _labelled(world: MergeWorld) -> list[Any]:
    return world.github.calls("POST", LABELS)


# ---------------------------------------------------------------------------
# The train off: nothing changes


@pytest.mark.parametrize("value,status", [(None, 200), ("off", 200), ("", 200), ("ON", 200),
                                          (None, 403)])
def test_with_the_train_off_the_step_merges_directly_and_never_labels(tmp_path, value, status):
    """The control. Unset, anything but exactly `on`, or unreadable on a base
    that does not require `merge-train`: the direct merge, as before."""
    world = MergeWorld(tmp_path)
    _switch(world, value, status=status)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["merged_by_this_task"] is True
    assert len(world.merge_calls()) == 1
    assert _labelled(world) == []
    assert "repository_train" not in outcome.summary


# ---------------------------------------------------------------------------
# The train on: label `ready` and park


def test_with_the_train_on_a_green_merge_verdict_is_labelled_ready_and_parks(tmp_path):
    """MUTATION: drop the switch read, and this merges directly."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None and outcome.retryable is False
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.ci_wait["head"] == PINNED and outcome.ci_wait["pull_request"] == NUMBER
    (label,) = _labelled(world)
    assert label.body == {"labels": ["ready"]}
    assert [k for k, v in label.headers.items() if world.token in str(v)] == ["Authorization"]
    assert outcome.summary["repository_train"] == {
        "switch": "on", "on_by": "variable", "head": PINNED, "label": "ready",
        "labelled": True}
    # No merge, no branch update, no slot, nothing recorded or closed yet.
    assert world.merge_calls() == [] and world.update_calls() == []
    assert "merge_slot" not in outcome.summary or not outcome.summary["merge_slot"].get("held")
    assert world.github.calls("POST", f"{API}/issues/{NUMBER}/comments") == []
    assert world.closed_issues() == []
    assert outcome.summary["merged_by_this_task"] is False


def test_a_pull_request_already_ready_parks_without_labelling_again(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    world.pr["labels"] = [{"name": "ready"}]
    _parked_on_train(world)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.summary["repository_train"]["labelled"] is False
    assert _labelled(world) == [] and world.merge_calls() == []


def test_the_label_waits_behind_the_fencing_recheck(tmp_path):
    """Invariant 5: a worker superseded before the label labels nothing."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    world.stale_after = 1
    with pytest.raises(FencedError):
        merge.run_merge(world.context())
    assert _labelled(world) == [] and world.merge_calls() == []


def test_a_token_that_cannot_read_variables_boards_where_the_base_requires_merge_train(tmp_path):
    """The runbook's ruleset change makes `merge-train` required: a direct
    merge into that base is refused, so the train is the way in."""
    world = MergeWorld(tmp_path)
    _switch(world, None, status=403)
    world.rules[0]["parameters"]["required_status_checks"].append({"context": "merge-train"})
    world.statuses = [{"context": "merge-train", "state": "success"}]
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.summary["repository_train"]["on_by"] == "required_check"
    assert len(_labelled(world)) == 1 and world.merge_calls() == []


# ---------------------------------------------------------------------------
# The wake: merged ends green


def test_the_wake_of_a_pull_request_the_train_merged_succeeds_and_closes_its_issues(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    world.statuses = [{"context": "merge-train", "state": "success"}]
    world.pr.update(merged=True, state="closed", merge_commit_sha=MERGED,
                    labels=[{"name": "ready"}])
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["merged_by_this_task"] is True
    assert outcome.summary["merged_through_repository_train"] is True
    assert outcome.summary["merge_commit"] == MERGED
    assert world.merge_calls() == [] and _labelled(world) == []
    assert world.closed_issues() == [7]
    (record,) = world.github.calls("POST", f"{API}/issues/{NUMBER}/comments")
    assert f"Merged by SwarmCloud task {TASK}" in record.body["body"]
    assert "merge train" in record.body["body"]


def test_a_pull_request_merged_without_the_train_while_parked_is_already_merged(tmp_path):
    """No `merge-train` status at the head: someone else merged it. Still SUCCEEDED."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    world.pr.update(merged=True, state="closed", merge_commit_sha=MERGED)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary.get("already_merged") is True
    assert "merged_through_repository_train" not in outcome.summary
    assert world.merge_calls() == []


# ---------------------------------------------------------------------------
# The wake: ejected follows the refusal path


def test_the_wake_of_an_ejected_pull_request_refuses_with_the_trains_reason(tmp_path):
    """MUTATION: label it `ready` again instead, which would loop on a change
    the train keeps ejecting."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", "**Left to the merge train.** ..."),
              ("swarmcloud-merge[bot]", EJECTION), ("a-person", "looking"))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "train_ejected"
    assert "ci-gate: https://example.test/run/9" in outcome.summary["refusal"]["message"]
    assert _labelled(world) == [] and world.merge_calls() == []
    assert world.closed_issues() == []


def test_ready_removed_by_the_gate_names_the_gates_comment(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", "Refused: #12 named in Depends on is not merged."))
    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "train_ejected"
    assert "#12 named in Depends on" in outcome.summary["refusal"]["message"]
    assert _labelled(world) == []


def test_an_ejected_pull_request_that_now_conflicts_goes_to_its_conflict_fix_round(tmp_path):
    """The gate runs before the train: a conflict with the new main parks for
    the conflict fix round, as a refused merge's conflict does."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    world.pr.update(mergeable=False, mergeable_state="dirty")
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == merge.CONFLICT_FIX_PENDING
    assert _labelled(world) == [] and world.merge_calls() == []


def test_a_new_head_after_a_fix_round_is_labelled_again(tmp_path):
    """The park was at another head: not an ejection of this one."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world, head="a" * 40)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert len(_labelled(world)) == 1


def test_a_workspace_train_target_never_reads_the_repository_switch(tmp_path):
    """TRAIN-WS (`merge_mode: train`) registers its entry as before."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    world.target["merge_mode"] = mergetrain.TRAIN
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.summary.get("merge_train") is not None
    assert "repository_train" not in outcome.summary
    assert world.github.calls("GET", VARIABLE) == [] and _labelled(world) == []
