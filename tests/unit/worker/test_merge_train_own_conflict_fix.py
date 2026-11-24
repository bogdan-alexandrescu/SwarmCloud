"""A merge step's own conflict fix never ejects it from the repository's merge train (issue 381).

On 2026-11-24 a merge step waited on main's merge train with its pull request
labelled `ready`. main moved, the pull request conflicted, and the step's own
`conflict-fix:1` round merged main in and pushed. auto-merge.yml's gate then
removed `ready` ("this pull request's head changed after the label was
applied"), and the step, which had parked on the train at that new head while
the label was still on, read the removal as an ejection and ended FAILED
`train_ejected`, leaving the pull request open, green and unlabelled.

The step now treats a head its own conflict fix round pushed (or one reached
from it only through merges of the default branch) as its own: with the
review's MERGE, and the green, conflict-free reading the gate above already
requires of any head, it labels `ready` again at that head, once, and goes
back to waiting on the train. A head anyone else moved is an ejection exactly
as before. When it cannot go on -- no MERGE verdict to label on, or `ready`
lost again at a head it already re-labelled -- it parks for a person with the
reason, never FAILED.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker import merge, mergetrain
from agent_worker.errors import ExitCode, FencedError
from swarm_common.models import utcnow
from swarm_common.states import TaskState

from merge_world import API, NUMBER, OPENER, PINNED, TASK, TENANT, MergeWorld

VARIABLE = f"{API}/actions/variables/MERGE_TRAIN"
LABELS = f"{API}/issues/{NUMBER}/labels"
COMMENTS = f"{API}/issues/{NUMBER}/comments"
TIMELINE = f"{API}/issues/{NUMBER}/timeline"
ROUND_TASK = "task_conflict1"
#: The head the conflict fix round pushed: main merged in, the conflict resolved.
RESOLVED = "7" * 40
#: auto-merge.yml's gate, on the round's push (the issue's words).
GATE = ("Removed `ready` and turned off auto-merge: this pull request's head changed after "
        "the label was applied.")
EJECTION = ("**Ejected from the merge train, and `ready` removed.** Train 9-1 -- main plus "
            "this pull request alone -- failed:\n\n* ci-gate: https://example.test/run/9")


def _train_on(world: MergeWorld) -> None:
    world.github.route("GET", VARIABLE, (200, {}, {"name": "MERGE_TRAIN", "value": "on"}))
    world.github.route("POST", LABELS, (200, {}, [{"name": "ready"}]))


def _labelled(world: MergeWorld) -> list[Any]:
    return world.github.calls("POST", LABELS)


def _park(world: MergeWorld, outcome: Any) -> None:
    """What the lifecycle's fenced park writes: the step's `merge_wait`."""
    assert outcome.state is TaskState.PARKED, outcome.message
    world.docs[TASK].setdefault("metadata", {})[control_mod.MERGE_WAIT_METADATA_KEY] = {
        **outcome.ci_wait, "wait_started_at": utcnow() - timedelta(minutes=30)}


def _claim_round(world: MergeWorld, *, state: str, pushed: str | None) -> None:
    """swarm-api's tick claimed conflict-fix:1 at PINNED and submitted its task."""
    world.docs[TASK]["metadata"][merge.MERGE_FIX_METADATA_KEY] = {merge.CONFLICT_ROUNDS_KEY: [
        {"round": 1, "head": PINNED, "task_id": ROUND_TASK, "workflow_id": "wf_conflict"}]}
    world.docs[ROUND_TASK] = {
        "tenant_id": TENANT, "workflow_id": "wf_conflict", "state": state,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": OPENER}},
        "result_summary": {"git": {"pushed_head": pushed} if pushed else {}},
    }


def _gate_removed_ready(world: MergeWorld) -> None:
    """The gate took `ready` off with the workflow's token: a Bot, and its comment."""
    world.pr["labels"] = []
    world.github.route("GET", COMMENTS, (200, {}, [
        {"id": 1, "body": GATE, "user": {"login": "swarmcloud-merge[bot]", "type": "Bot"}}]))
    world.github.route("GET", TIMELINE, (200, {}, [
        {"event": "unlabeled", "label": {"name": "ready"},
         "actor": {"login": "github-actions[bot]", "type": "Bot"},
         "created_at": "2026-11-24T15:44:40Z"}]))


def _through_own_conflict_fix(world: MergeWorld) -> None:
    """Drive the step from its first board to its own conflict fix's new head,
    parked on the train there while `ready` was still on (the incident)."""
    _train_on(world)
    # 1. Green at PINNED with the review's MERGE: labelled `ready`, waiting on the train.
    boarded = merge.run_merge(world.context())
    assert boarded.ci_wait["code"] == mergetrain.TRAIN_WAITING, boarded.message
    assert len(_labelled(world)) == 1
    _park(world, boarded)
    world.pr["labels"] = [{"name": "ready"}]
    # 2. main moved: the pull request conflicts, and goes to its conflict fix round.
    world.pr.update(mergeable=False, mergeable_state="dirty")
    conflicted = merge.run_merge(world.context())
    assert conflicted.ci_wait["code"] == merge.CONFLICT_FIX_PENDING, conflicted.message
    _park(world, conflicted)
    # 3. The tick claimed conflict-fix:1; it is running.
    _claim_round(world, state="RUNNING", pushed=None)
    running = merge.run_merge(world.context())
    assert running.ci_wait["code"] == merge.CONFLICT_FIX_RUNNING, running.message
    _park(world, running)
    # 4. The round merged main in and pushed RESOLVED. The step woke there,
    #    green, before the gate had run: `ready` still on, so it parked on the
    #    train at RESOLVED without labelling.
    _claim_round(world, state="SUCCEEDED", pushed=RESOLVED)
    world.pr.update(mergeable=True, mergeable_state="clean")
    world.pr["head"]["sha"] = RESOLVED
    world.serve_checks(RESOLVED)
    waiting = merge.run_merge(world.context())
    assert waiting.ci_wait["code"] == mergetrain.TRAIN_WAITING, waiting.message
    assert waiting.ci_wait["head"] == RESOLVED
    assert len(_labelled(world)) == 1
    _park(world, waiting)
    # 5. The gate saw the round's push and removed `ready`.
    _gate_removed_ready(world)


def test_the_steps_own_conflict_fix_is_labelled_ready_again_at_its_new_head(tmp_path):
    """The issue's fourth point, red first: before the fix this ends FAILED
    `train_ejected` quoting the gate's "head changed after the label"."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert "refusal" not in outcome.summary, outcome.summary.get("refusal")
    assert outcome.exit_code == ExitCode.PARKED and outcome.end_cause is None
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.ci_wait["head"] == RESOLVED
    assert outcome.ci_wait["pending"] == [merge.OWN_FIX_RELABELLED]
    labels = _labelled(world)
    assert len(labels) == 2 and labels[-1].body == {"labels": ["ready"]}
    record = outcome.summary["repository_train"]
    assert record["labelled"] is True and record["own_conflict_fix"] == ROUND_TASK
    assert record["head"] == RESOLVED
    # Never reported as an ejection (the issue's second point).
    assert "ejection" not in record
    assert "ejected" not in outcome.summary["wait"]["message"]
    assert ROUND_TASK in outcome.summary["wait"]["message"]
    assert world.merge_calls() == [] and world.update_calls() == []


def test_a_base_merge_on_top_of_the_own_fix_is_still_its_own(tmp_path):
    """A head reached from the round's push only through a merge of main is
    the step's own too: labelled again there."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    updated = world.github_merge(RESOLVED)
    world.pr["head"]["sha"] = updated
    metadata = world.docs[TASK]["metadata"]
    metadata[control_mod.MERGE_WAIT_METADATA_KEY]["head"] = updated
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert outcome.ci_wait["head"] == updated
    assert outcome.summary["repository_train"]["own_conflict_fix"] == ROUND_TASK
    assert len(_labelled(world)) == 2


def test_the_relabelled_park_still_ready_keeps_its_mark(tmp_path):
    """Back on the train and still `ready`: the park keeps the head's one re-label."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    _park(world, merge.run_merge(world.context()))
    world.pr["labels"] = [{"name": "ready"}]
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert outcome.ci_wait["pending"] == [merge.OWN_FIX_RELABELLED]
    assert len(_labelled(world)) == 2


def test_ready_lost_again_at_the_relabelled_head_parks_for_a_person(tmp_path):
    """The issue's third point: the step cannot go on (one re-label per head,
    so it cannot loop), and parks for a person with the reason, not FAILED."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    _park(world, merge.run_merge(world.context()))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.end_cause is None and "refusal" not in outcome.summary
    assert outcome.ci_wait is None
    wait = outcome.owner_wait
    assert wait["park_phase"] == merge.CI_WAIT_STUCK
    assert wait["why"] == merge.OWN_FIX_READY_LOST
    assert wait["head"] == RESOLVED and wait["pull_request"] == NUMBER
    assert GATE in wait["line"] and ROUND_TASK in wait["line"]
    assert len(_labelled(world)) == 2


def test_a_persons_ready_lets_the_step_label_its_own_head_once_more(tmp_path):
    """A person's "I'm ready" after that park re-arms the one re-label."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    _park(world, merge.run_merge(world.context()))
    world.docs[TASK]["metadata"]["human_ready"] = {"at": utcnow()}
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING, outcome.message
    assert len(_labelled(world)) == 3


def test_without_a_merge_verdict_the_own_head_is_never_labelled(tmp_path):
    """Never label a pull request whose review verdict is not MERGE: a step
    with no review parks for a person instead."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    world.target = {"pull_request": OPENER}
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    assert "refusal" not in outcome.summary
    assert outcome.owner_wait["why"] == merge.OWN_FIX_READY_LOST
    assert "MERGE" in outcome.owner_wait["line"]
    assert len(_labelled(world)) == 1


def test_a_head_the_step_did_not_move_is_still_an_ejection(tmp_path):
    """Control: the same gate removal at a head no conflict fix of this step
    pushed (here the opener's own) is `train_ejected`, exactly as before."""
    world = MergeWorld(tmp_path)
    _train_on(world)
    _claim_round(world, state="SUCCEEDED", pushed=RESOLVED)
    world.docs[TASK]["metadata"][control_mod.MERGE_WAIT_METADATA_KEY] = {
        "code": mergetrain.TRAIN_WAITING, "head": PINNED, "pull_request": NUMBER, "pending": []}
    _gate_removed_ready(world)
    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "train_ejected", outcome.message
    assert GATE in outcome.summary["refusal"]["message"]
    assert _labelled(world) == []


def test_the_trains_own_ejection_at_the_fix_head_keeps_its_re_board(tmp_path):
    """Control: the train's ejection of the round's head takes the train's
    path (one free re-board), not the own-fix re-label."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    world.github.route("GET", COMMENTS, (200, {}, [
        {"id": 1, "body": EJECTION, "user": {"login": "github-actions[bot]", "type": "Bot"}}]))
    outcome = merge.run_merge(world.context())

    assert outcome.ci_wait["pending"] == [merge.TRAIN_REBOARDED], outcome.message
    assert "own_conflict_fix" not in outcome.summary["repository_train"]


def test_a_cancelled_step_relabels_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    world.cancel = True
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.CANCELLED, outcome.message
    assert len(_labelled(world)) == 1


def test_a_stale_generation_relabels_nothing(tmp_path):
    """Invariant 5: the re-label is behind the fencing recheck."""
    world = MergeWorld(tmp_path)
    _through_own_conflict_fix(world)
    world.rechecks = 0
    world.stale_after = 1
    with pytest.raises(FencedError):
        merge.run_merge(world.context())
    assert len(_labelled(world)) == 1
