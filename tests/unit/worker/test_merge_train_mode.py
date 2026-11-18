"""The merge step in train mode registers its pull request instead of merging it (TRAIN-WS).

docs/merge-trains.md §5. swarm-api stamps the effective merge mode into the
SIGNED merge target at submission (`merge_target.merge_mode: "train"`, absent
for direct). In train mode the step does everything it does today up to the
merge -- the verdict, the head, conflicts and its fix rounds, its own checks
green -- and then, instead of the merge call, writes its entry in
`merge_train_entries` and parks CI_PENDING with `merge_train_waiting`. The
train coordinator lands it (or ejects it) and marks the park; on its next
attempt the step reads the outcome:

  * the pull request merged at its head and its entry LANDED -> SUCCEEDED,
    `merged_through_train`, its comment written and its issues closed;
  * its entry EJECTED -> FAILED `train_ejected`, with the train's reason;
  * RETURNED, or a new head from a fix round -> registered again, queued.

Direct mode (no `merge_mode` in the target) is unchanged: one merge call, no entry.
"""

from __future__ import annotations

import pytest

from agent_worker import merge, mergetrain
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from merge_world import MERGED, NUMBER, PINNED, TASK, TENANT, MergeWorld


def _train_world(tmp_path) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.target["merge_mode"] = mergetrain.TRAIN
    return world


def _entry(world: MergeWorld) -> dict | None:
    return world.slot_db.documents.get(
        f"{mergetrain.ENTRIES_COLLECTION}/{mergetrain.entry_id_for(TENANT, TASK)}")


def _parked_on_train(world: MergeWorld, outcome) -> None:
    world.docs[TASK]["metadata"][merge.MERGE_WAIT_METADATA_KEY] = {
        "code": outcome.ci_wait["code"], "head": outcome.ci_wait["head"],
        "pull_request": outcome.ci_wait["pull_request"]}


def test_direct_mode_merges_and_registers_nothing(tmp_path):
    world = MergeWorld(tmp_path)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    assert _entry(world) is None


def test_train_mode_registers_the_pull_request_and_parks_without_merging(tmp_path):
    world = _train_world(tmp_path)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert outcome.ci_wait["head"] == PINNED and outcome.ci_wait["pull_request"] == NUMBER
    assert world.merge_calls() == []
    entry = _entry(world)
    assert entry is not None
    assert entry["state"] == mergetrain.QUEUED
    assert entry["tenant_id"] == TENANT and entry["task_id"] == TASK
    assert entry["number"] == NUMBER and entry["head"] == PINNED
    assert entry["base"] == "main"
    assert entry["repository"] == "octo-org/widget-shop"
    # The squash commit's message is the step's own provenance.
    assert TASK in entry["message"]


def test_train_mode_still_refuses_a_red_pull_request_before_registering(tmp_path):
    world = _train_world(tmp_path)
    world.spend_conflict_rounds()
    world.pr["mergeable"] = False
    world.pr["mergeable_state"] = "dirty"
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.FAILED
    assert outcome.summary["refusal"]["code"] == "merge_conflict"
    assert _entry(world) is None


def test_train_mode_does_not_take_the_merge_slot(tmp_path):
    world = _train_world(tmp_path)
    merge.run_merge(world.context())
    assert not any(path.startswith("merge_slots/") for path in world.slot_db.documents)


def test_a_wake_while_queued_parks_again_and_keeps_its_place(tmp_path):
    world = _train_world(tmp_path)
    first = merge.run_merge(world.context())
    _parked_on_train(world, first)
    enqueued_at = _entry(world)["enqueued_at"]
    again = merge.run_merge(world.context())
    assert again.state is TaskState.PARKED and again.ci_wait["code"] == mergetrain.TRAIN_WAITING
    assert _entry(world)["enqueued_at"] == enqueued_at
    assert world.merge_calls() == []


def test_landed_by_the_train_succeeds_through_the_train(tmp_path):
    world = _train_world(tmp_path)
    first = merge.run_merge(world.context())
    _parked_on_train(world, first)
    # The coordinator squash-merged it at its head and recorded the landing.
    entry = _entry(world)
    entry.update(state=mergetrain.LANDED, merge_commit=MERGED)
    world.pr.update(merged=True, state="closed", merge_commit_sha=MERGED)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["merged_through_train"] is True
    assert outcome.summary["merged_by_this_task"] is True
    assert outcome.summary["merge_commit"] == MERGED
    assert world.merge_calls() == []
    assert world.closed_issues() == [7]


def test_ejected_by_the_train_fails_with_the_trains_reason(tmp_path):
    world = _train_world(tmp_path)
    first = merge.run_merge(world.context())
    _parked_on_train(world, first)
    _entry(world).update(state=mergetrain.EJECTED, reason="train_red",
                         detail="ci / unit (failure) on the train with #40")
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.FAILED
    assert outcome.summary["refusal"]["code"] == "train_ejected"
    assert "train_red" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_returned_by_the_train_is_registered_again(tmp_path):
    world = _train_world(tmp_path)
    first = merge.run_merge(world.context())
    _parked_on_train(world, first)
    _entry(world).update(state=mergetrain.RETURNED, reason="train_conflict", returns=1)
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.PARKED
    entry = _entry(world)
    assert entry["state"] == mergetrain.QUEUED and entry["returns"] == 1


def test_a_target_with_an_unknown_merge_mode_is_invalid():
    with pytest.raises(merge.TargetInvalid, match="merge_mode"):
        merge.parse_merge_target({"merge_target": {"pull_request": "task_int",
                                                   "merge_mode": "sometimes"}})


def test_the_target_swarm_api_writes_in_train_mode_is_one_the_worker_reads():
    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="direct-pr", carrier="patches").with_merge_target(
        pull_request="task_int", merge_mode="train",
    ).to_metadata()
    assert merge.parse_merge_target(block).merge_mode == mergetrain.TRAIN
    direct = DispatchOptions(strategy="direct-pr", carrier="patches").with_merge_target(
        pull_request="task_int", merge_mode="direct",
    ).to_metadata()
    assert "merge_mode" not in direct["merge_target"]
    assert merge.parse_merge_target(direct).merge_mode == mergetrain.DIRECT


# --------------------------------------------------------------------------
# the registration rule, pure
# --------------------------------------------------------------------------

FIELDS = {"tenant_id": TENANT, "task_id": TASK, "repository": "o/r",
          "repository_url": "https://github.com/o/r", "base": "main", "number": 5,
          "head": "a" * 40, "title": "t", "message": "m"}


def test_register_creates_a_queued_entry():
    new, entry = mergetrain.register_entry(None, fields=FIELDS, now="T1")
    assert new is not None and entry["state"] == mergetrain.QUEUED
    assert entry["enqueued_at"] == "T1" and entry["returns"] == 0


@pytest.mark.parametrize("state", [mergetrain.QUEUED, mergetrain.RIDING])
def test_register_leaves_a_live_entry_at_the_same_head_alone(state):
    existing = {**FIELDS, "state": state, "enqueued_at": "T0", "returns": 0}
    new, entry = mergetrain.register_entry(existing, fields=FIELDS, now="T1")
    assert new is None and entry is existing


@pytest.mark.parametrize("state", [mergetrain.LANDED, mergetrain.EJECTED])
def test_register_never_reopens_a_concluded_entry_at_the_same_head(state):
    existing = {**FIELDS, "state": state, "enqueued_at": "T0", "returns": 0}
    new, entry = mergetrain.register_entry(existing, fields=FIELDS, now="T1")
    assert new is None and entry["state"] == state


def test_register_requeues_a_returned_entry_or_a_new_head_keeping_its_place():
    returned = {**FIELDS, "state": mergetrain.RETURNED, "enqueued_at": "T0", "returns": 2,
                "reason": "train_conflict"}
    new, entry = mergetrain.register_entry(returned, fields=FIELDS, now="T1")
    assert new is not None and entry["state"] == mergetrain.QUEUED
    assert entry["enqueued_at"] == "T0" and entry["returns"] == 2
    assert "reason" not in entry
    moved = {**FIELDS, "state": mergetrain.QUEUED, "enqueued_at": "T0", "returns": 0}
    new, entry = mergetrain.register_entry(moved, fields={**FIELDS, "head": "b" * 40}, now="T1")
    assert new is not None and entry["head"] == "b" * 40 and entry["state"] == mergetrain.QUEUED


def test_register_refuses_another_tenants_entry():
    with pytest.raises(mergetrain.EntryTenantMismatch):
        mergetrain.register_entry({**FIELDS, "tenant_id": "other", "state": "queued"},
                                  fields=FIELDS, now="T1")
