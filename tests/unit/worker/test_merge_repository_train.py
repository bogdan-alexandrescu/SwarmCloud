"""The merge step and the repository's own merge train (owner decisions
2026-10-14 "A1 Merge train" and 2026-11-03, turning it on).

.github/workflows/merge-train.yml and scripts/merge-train.sh board green
`ready` pull requests, test them together on main and squash-merge each;
`vars.MERGE_TRAIN` exactly `on` is the switch both workflows read. With it
on, the merge step, green and told MERGE, labels its pull request `ready` and
parks on the train instead of taking the merge slot and updating the branch;
it ends SUCCEEDED once the pull request is merged. The train's ejection of a
green head (its comment, `ready` removed) is re-boarded once at that head,
then handed to a CI-fix round, then refused (#3313); the gate's removal is a
refusal at once, and a pull request that now conflicts goes to the gate's fix
round. With the switch off nothing changes: the
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

from merge_world import API, CHECK, MERGED, NUMBER, PINNED, TASK, MergeWorld

VARIABLE = f"{API}/actions/variables/MERGE_TRAIN"
LABELS = f"{API}/issues/{NUMBER}/labels"
COMMENTS = f"{API}/issues/{NUMBER}/comments"
TIMELINE = f"{API}/issues/{NUMBER}/timeline"
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


def _comments(world: MergeWorld, *bodies: tuple[str, ...]) -> None:
    """(login, body) or (login, body, created_at): a `[bot]` login is a Bot."""
    world.github.route("GET", COMMENTS, (200, {}, [
        {"id": i, "body": body, "user": {"login": login, "type": "Bot" if "[bot]" in login
                                         else "User"},
         **({"created_at": rest[0]} if rest else {})}
        for i, (login, body, *rest) in enumerate(bodies, 1)]))


def _unlabeled_by(world: MergeWorld, login: str, kind: str) -> None:
    """The issue timeline: `ready` labelled, then removed by `login` (a `kind`)."""
    world.github.route("GET", TIMELINE, (200, {}, [
        {"event": "labeled", "label": {"name": "ready"},
         "actor": {"login": "swarmcloud-merge[bot]", "type": "Bot"},
         "created_at": "2026-11-05T11:10:02Z"},
        {"event": "commented", "actor": {"login": login, "type": kind}},
        {"event": "unlabeled", "label": {"name": "ready"},
         "actor": {"login": login, "type": kind}, "created_at": "2026-11-05T19:21:40Z"}]))


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
# The wake: the train's ejection re-boards once, then a round, then the refusal (#3313)


def _reboarded(world: MergeWorld, head: str = PINNED, *, code: str = mergetrain.TRAIN_WAITING,
               rounds: int | None = None) -> None:
    """The step's own record after its free re-board at `head` (or a park after it)."""
    _parked_on_train(world, head)
    metadata = world.docs[TASK]["metadata"]
    metadata[control_mod.MERGE_WAIT_METADATA_KEY].update(
        code=code, pending=[merge.TRAIN_REBOARDED])
    if rounds is not None:
        metadata[merge.MERGE_FIX_ROUNDS_KEY] = rounds


def test_a_first_train_ejection_at_a_green_head_is_labelled_ready_again_once(tmp_path):
    """#3313 (PR #3212: two ladder tests timed out on a loaded train shard
    while the head was green): the train's ejection of a green head is often
    a flake, so the first one at a head re-boards it.

    MUTATION: refuse `train_ejected` at once, as before #3313, and this fails."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", "**Left to the merge train.** ..."),
              ("github-actions[bot]", EJECTION), ("a-person", "looking"))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.end_cause is None
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.ci_wait["head"] == PINNED
    # The re-board is recorded where the fenced park keeps it.
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    (label,) = _labelled(world)
    assert label.body == {"labels": ["ready"]}
    record = outcome.summary["repository_train"]
    assert record["reboarded"] is True and record["labelled"] is True
    assert "ci-gate: https://example.test/run/9" in record["ejection"]
    assert world.merge_calls() == [] and world.closed_issues() == []


def test_a_re_boarded_pull_request_still_ready_keeps_its_re_board(tmp_path):
    """Waking `ready` after the re-board parks again with it recorded, so the
    next ejection at this head is the second, not a second free re-board."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    world.pr["labels"] = [{"name": "ready"}]
    _reboarded(world)
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    assert _labelled(world) == []


def test_a_second_train_ejection_at_the_same_head_parks_for_a_ci_fix_round(tmp_path):
    """MUTATION: re-label again, and a change the train keeps ejecting loops."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _reboarded(world, rounds=1)
    _comments(world, ("github-actions[bot]", EJECTION))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == merge.TRAIN_FIX_PENDING
    assert outcome.ci_wait["head"] == PINNED and outcome.ci_wait["pull_request"] == NUMBER
    assert outcome.ci_wait["failed"] == ["ci-gate"]
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    assert merge.TRAIN_FIX_PENDING in merge._RELEASE_ON_PARK
    assert _labelled(world) == [] and world.merge_calls() == []


def test_the_ejections_failing_lines_are_read_as_merge_train_sh_writes_them():
    """`eject_pr`'s failing lines are `- [name](url)`; `* name: url` is read too."""
    body = ("**Ejected from the merge train, and `ready` removed.** Train 9-1 -- main plus "
            "this pull request alone -- failed:\n\n- [web-unit (2)](https://e.test/j/1)\n"
            "- [ci-gate](https://e.test/j/2)\n\nWith nothing else in the train ...")
    assert merge.train_ejection_failed(body) == ["web-unit (2)", "ci-gate"]
    assert merge.train_ejection_failed(EJECTION) == ["ci-gate"]


def test_a_train_round_being_submitted_parks_ci_fix_running(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _reboarded(world, code=merge.TRAIN_FIX_PENDING, rounds=1)
    world.docs[TASK]["metadata"][merge.MERGE_FIX_METADATA_KEY] = {
        "rounds": [{"round": 1, "head": PINNED}]}
    _comments(world, ("github-actions[bot]", EJECTION))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == merge.CI_FIX_RUNNING
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    assert _labelled(world) == []


def test_a_second_train_ejection_with_no_round_refuses_train_ejected(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _reboarded(world)
    _comments(world, ("swarmcloud-merge[bot]", "**Left to the merge train.** ..."),
              ("github-actions[bot]", EJECTION), ("a-person", "looking"))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "train_ejected"
    message = outcome.summary["refusal"]["message"]
    assert "ci-gate: https://example.test/run/9" in message
    assert "after one re-board" in message
    assert _labelled(world) == [] and world.merge_calls() == []
    assert world.closed_issues() == []


@pytest.mark.parametrize("ended", ["no_change", "error"])
def test_a_train_round_that_ended_at_the_same_head_refuses_train_ejected(tmp_path, ended):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _reboarded(world, code=merge.CI_FIX_RUNNING, rounds=1)
    entry: dict[str, Any] = {"round": 1, "head": PINNED}
    if ended == "error":
        entry["error"] = "submit refused"
    else:
        entry["task_id"] = "round-1"
        world.docs["round-1"] = {"tenant_id": world.docs[TASK]["tenant_id"],
                                 "state": TaskState.SUCCEEDED.value,
                                 "result_summary": {"no_change": True}}
    world.docs[TASK]["metadata"][merge.MERGE_FIX_METADATA_KEY] = {"rounds": [entry]}
    _comments(world, ("github-actions[bot]", EJECTION))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "train_ejected"
    message = outcome.summary["refusal"]["message"]
    assert "after CI fix round 1" in message
    assert "ci-gate: https://example.test/run/9" in message
    assert _labelled(world) == [] and world.merge_calls() == []


def test_the_re_board_waits_behind_the_fencing_recheck(tmp_path):
    """Invariant 5: a worker superseded before the re-label labels nothing."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("github-actions[bot]", EJECTION))
    world.stale_after = 1
    with pytest.raises(FencedError):
        merge.run_merge(world.context())
    assert _labelled(world) == [] and world.merge_calls() == []


def test_a_step_cancelled_before_the_re_board_labels_nothing(tmp_path):
    """Cancelled after the step's first recheck, at the re-board's own."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("github-actions[bot]", EJECTION))
    recheck = world.recheck

    def cancelled_after_the_first() -> bool:
        world.cancel = world.rechecks >= 1
        return recheck()

    world.recheck = cancelled_after_the_first  # type: ignore[method-assign]
    outcome = merge.run_merge(world.context())

    assert world.rechecks == 2
    assert outcome.state is TaskState.CANCELLED, outcome.message
    assert _labelled(world) == [] and world.merge_calls() == []


def test_ready_removed_by_the_gate_names_the_gates_comment(tmp_path):
    """The gate's removal is not the train's ejection: no re-board, refused at once."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", "Refused: #12 named in Depends on is not merged."))
    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "train_ejected"
    assert "#12 named in Depends on" in outcome.summary["refusal"]["message"]
    assert _labelled(world) == []


def test_the_gates_removal_after_an_earlier_train_ejection_is_refused_at_once(tmp_path):
    """The latest comment says why `ready` is gone: a gate removal newer than
    an earlier train ejection is the gate's, refused at once, not re-boarded.

    MUTATION: prefer any ejection comment over a newer App comment, and this
    re-labels `ready`."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("github-actions[bot]", EJECTION),
              ("swarmcloud-merge[bot]", "Refused: #12 named in Depends on is not merged."),
              ("a-person", "looking"))
    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "train_ejected", outcome.message
    assert "#12 named in Depends on" in outcome.summary["refusal"]["message"]
    assert _labelled(world) == [] and world.merge_calls() == []


LEFT = "**Left to the merge train.** Green at the head, with the review's MERGE ..."
TAKEN_OUT = ("Taken out of the merge train by the owner: two console tests fail "
             "deterministically")


def test_ready_removed_by_a_person_is_their_decision_quoting_their_comment(tmp_path):
    """#3602, the incident (#3490 at 19:21:43Z, #3475 at 19:55:28Z): a person
    took `ready` off a train-parked pull request by hand and said why in a
    comment without the ejection marker. The timeline's `unlabeled` actor is
    a User, so the step ends with the person code, quoting the person -- not
    `train_ejected` with the platform's stale comment of 11:10.

    MUTATION: read only the comments, as before #3602, and this refuses
    `train_ejected` quoting "Left to the merge train"."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", LEFT, "2026-11-05T11:10:00Z"),
              ("a-person", TAKEN_OUT, "2026-11-05T19:21:43Z"))
    _unlabeled_by(world, "a-person", "User")
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == merge.READY_REMOVED_BY_PERSON
    assert merge.READY_REMOVED_BY_PERSON == "ready_removed_by_person"
    message = outcome.summary["refusal"]["message"]
    assert "a-person" in message
    assert TAKEN_OUT in message
    assert "Left to the merge train" not in message
    assert outcome.summary["repository_train"]["removed_by"] == "a-person"
    assert _labelled(world) == [] and world.merge_calls() == []
    assert world.closed_issues() == []


def test_ready_removed_by_the_trains_bot_is_re_boarded_as_before(tmp_path):
    """Control (a): merge-train.sh `eject_pr` removes `ready` with the
    workflow's token, so its actor is github-actions[bot], a Bot: the
    ejection is re-boarded once, as before #3602.

    MUTATION: treat any unlabeled actor as a person, and this refuses."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", LEFT), ("github-actions[bot]", EJECTION))
    _unlabeled_by(world, "github-actions[bot]", "Bot")
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    assert len(_labelled(world)) == 1
    assert "removed_by" not in outcome.summary["repository_train"]


def test_ready_removed_by_the_gates_bot_is_still_train_ejected_at_once(tmp_path):
    """Control (b): auto-merge.yml's gate removes `ready` as the workflow's
    bot too: refused `train_ejected` at once with the gate's comment.

    MUTATION: give a Bot removal the person code, and this fails."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("swarmcloud-merge[bot]", "Refused: #12 named in Depends on is not merged."))
    _unlabeled_by(world, "github-actions[bot]", "Bot")
    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "train_ejected", outcome.message
    assert "#12 named in Depends on" in outcome.summary["refusal"]["message"]
    assert _labelled(world) == [] and world.merge_calls() == []


def test_an_unreadable_timeline_keeps_the_comment_reading_and_records_why(tmp_path):
    """Control (c): the timeline GET answers 500. The step reads the comments
    as before #3602 -- here the train's ejection, re-boarded -- and records
    that the removal was not read.

    MUTATION: let the forge error escape, and the step fails instead."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    _comments(world, ("github-actions[bot]", EJECTION), ("a-person", TAKEN_OUT))
    world.github.route("GET", TIMELINE, (500, {}, {"message": "Server Error"}))
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    record = outcome.summary["repository_train"]
    assert record["removal_unread"]
    assert "removed_by" not in record
    assert len(_labelled(world)) == 1


def test_a_person_removal_under_review_not_yet_keeps_todays_path(tmp_path):
    """Control (d): the platform's own NOT_YET path (`_hold_not_yet`) takes
    `ready` off, possibly under a person's forge token, so a User actor on a
    pull request carrying `review-not-yet` is not read as a person's
    decision: today's comment-based path, here the gate's refusal.

    MUTATION: drop the `review-not-yet` exception, and this ends with the
    person code."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    world.pr["labels"] = [{"name": merge.REVIEW_NOT_YET_LABEL}]
    world.github.route("DELETE", f"{LABELS}/{merge.REVIEW_NOT_YET_LABEL}", (200, {}, []))
    _comments(world, ("swarmcloud-merge[bot]", "Refused: #12 named in Depends on is not merged."),
              ("a-person", TAKEN_OUT))
    _unlabeled_by(world, "a-person", "User")
    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "train_ejected", outcome.message
    assert "#12 named in Depends on" in outcome.summary["refusal"]["message"]
    assert "removed_by" not in outcome.summary["repository_train"]
    assert _labelled(world) == [] and world.merge_calls() == []


def test_the_trains_skip_comment_after_its_ejection_does_not_hide_the_ejection(tmp_path):
    """The train's "does not board" comment comes from the workflow's bot but
    never says why `ready` was lost: the ejection before it still decides.

    MUTATION: count the skip comment as the gate's, and this refuses."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _parked_on_train(world)
    skip = (f"<!-- merge-train-skip head={PINNED} reason=conflict -->\n"
            "**The merge train skips it: it conflicts with main.**")
    _comments(world, ("github-actions[bot]", EJECTION), ("github-actions[bot]", skip))
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    assert len(_labelled(world)) == 1


def test_a_re_boarded_pull_request_the_train_skips_names_why_and_keeps_its_re_board(tmp_path):
    """Both hold at once: the waiting line carries the train's skip reason at
    this head, and the park keeps the head's one re-board."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    world.pr["labels"] = [{"name": "ready"}]
    _reboarded(world)
    _comments(world, ("github-actions[bot]", EJECTION), ("github-actions[bot]", (
        f"<!-- merge-train-skip head={PINNED} reason=check:ci-gate -->\n"
        "**The merge train skips it: `ci-gate` failed.**")))
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED]
    assert outcome.summary["wait"]["message"].endswith("; the train skips it: `ci-gate` failed")
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


# ---------------------------------------------------------------------------
# The ruleset requires the train's own `merge-train` (#3179)
#
# The train posts `merge-train` only on a pull request it boarded, and it
# boards only `ready` ones: the gate before `_board` must not wait for it.


def _requires_merge_train(world: MergeWorld) -> None:
    world.rules[0]["parameters"]["required_status_checks"].append({"context": "merge-train"})


@pytest.mark.parametrize("value,status,on_by", [("on", 200, "variable"),
                                                (None, 403, "required_check")])
def test_a_green_head_is_labelled_though_merge_train_has_not_reported(tmp_path, value, status,
                                                                      on_by):
    """MUTATION (merge.py `_with_forge`): gate on `merge-train` with the train
    on, and this parks `checks_pending` on ["merge-train"] and never labels."""
    world = MergeWorld(tmp_path)
    _switch(world, value, status=status)
    _requires_merge_train(world)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.ci_wait
    assert outcome.ci_wait["pending"] == []
    assert outcome.summary["repository_train"]["on_by"] == on_by
    assert outcome.summary["required_checks"] == sorted([CHECK, "merge-train"])
    (label,) = _labelled(world)
    assert label.body == {"labels": ["ready"]}
    assert world.merge_calls() == [] and world.update_calls() == []
    # The switch is read once, before the gate.
    assert len(world.github.calls("GET", VARIABLE)) == 1


def test_a_red_other_check_still_goes_to_its_fix_round_with_merge_train_required(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _requires_merge_train(world)
    world.runs[0]["conclusion"] = "failure"
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == merge.CI_FIX_PENDING, outcome.ci_wait
    assert outcome.ci_wait["failed"] == [CHECK]
    assert _labelled(world) == [] and world.merge_calls() == []


def test_a_pending_other_check_still_waits_with_merge_train_required(tmp_path):
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _requires_merge_train(world)
    world.runs[0].update(status="in_progress", conclusion=None)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == "checks_pending", outcome.ci_wait
    assert outcome.ci_wait["pending"] == [CHECK]
    assert _labelled(world) == []


def test_with_the_train_off_a_required_merge_train_still_gates(tmp_path):
    """The variable reads off: `merge-train` is an ordinary required check."""
    world = MergeWorld(tmp_path)
    _switch(world, "off")
    _requires_merge_train(world)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == "checks_pending", outcome.ci_wait
    assert outcome.ci_wait["pending"] == ["merge-train"]
    assert _labelled(world) == [] and world.merge_calls() == []


def test_a_stale_generation_with_merge_train_required_labels_nothing(tmp_path):
    """Invariant 5."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    _requires_merge_train(world)
    world.stale_after = 1
    with pytest.raises(FencedError):
        merge.run_merge(world.context())
    assert _labelled(world) == [] and world.merge_calls() == []


@pytest.mark.parametrize("conclusion,code", [("success", mergetrain.TRAIN_WAITING),
                                             ("failure", merge.CI_FIX_PENDING)])
def test_a_base_requiring_only_merge_train_gates_on_every_other_check(tmp_path, conclusion,
                                                                       code):
    """Nothing else required: every other check at the head must be green, as
    on a base that requires none."""
    world = MergeWorld(tmp_path)
    _switch(world, "on")
    world.rules[0]["parameters"]["required_status_checks"] = [{"context": "merge-train"}]
    world.runs[0]["conclusion"] = conclusion
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == code, outcome.ci_wait
    assert len(_labelled(world)) == (1 if conclusion == "success" else 0)
    assert world.merge_calls() == []
