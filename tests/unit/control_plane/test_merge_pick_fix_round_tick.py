"""swarm-api's wake tick starts a pick-fix round for a merge parked on an unbuilt pick.

PICK-FIX-ROUND (part of #2441; BGD 2026-10-26). A merge whose owner picked an
option the branch did not build parks CI_PENDING with
`owner_pick_fix_pending` (agent_worker.merge), one `pending` line per pick.
The tick reads the park as it reads a CI or a conflict park, and:

  1. reads ONLY the pull request (never its checks), then claims round 1 in
     `merge_fix.pick_rounds` under the round guard and submits one claude-code
     continuation of the pull request's branch, as the merge's submitter, whose
     brief names each pick, fenced as data, and asks for `picks-built.json`;
  2. waits on that round while it runs -- the park is the same, so a second
     tick claims nothing -- and wakes the step once it ends;
  3. with every round spent, or a pull request off the root's branch, wakes
     the step to refuse; merged or closed outside, wakes it to finish.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import timedelta

from swarm_api import issueci, mergewake
from swarm_common.models import WorkflowStep
from swarm_common.states import ParkReason, TaskState
from swarm_rollup import derive, read_steps

from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    HEAD, NOW, NUMBER, ROOT, _check_reads, _fix_record, _tick, _wait, clock, fixable_merge,
    github, tenant_tokens, wake_client,
)

IMPLEMENT = "task_" + "2" * 20
LINE = (f"pick {IMPLEMENT} question 0 (step implement): picked 'Needs a look', "
        "the branch builds 'Needs action'; question: Which tile does a full pool show?")


def pick_merge(db, github, *, code: str = "owner_pick_fix_pending", **wait):
    doc = fixable_merge(db, github, rounds=0, code=code, pending=[LINE], **wait)
    github.check_runs.pop(HEAD, None)  # a pick park never reads them
    return doc


def _pick_workflows(db) -> list[dict]:
    return [w for key, w in db.docs.items() if key.startswith("workflows/")
            and any(s.get("step_id") == mergewake.PICK_FIX_STEP for s in w.get("steps") or [])]


def test_a_pick_park_claims_a_pick_round_and_submits_one_continuation(db, wake_client, github):
    pick_merge(db, github)

    body = _tick(wake_client).json()

    # MUTATION: leave the pick codes to the CI reading and the tick reads checks
    # that do not matter, and starts no round.
    assert body["report"]["fixing"] == 1 and body["report"]["woken"] == 0, body
    assert _check_reads(github) == [], "a pick park's checks were read"
    task = db.docs["tasks/task_merge"]
    assert task["state"] == "PARKED" and task["park_reason"] == ParkReason.CI_PENDING.value
    assert mergewake.WAKE_MARKER not in _wait(db)

    (workflow,) = _pick_workflows(db)
    (step,) = workflow["steps"]
    fix_task = db.docs[f"tasks/{step['task_id']}"]
    assert fix_task["runner_profile"] == "claude-code"
    assert fix_task["submitted_by"] == "alice@example.com"
    assert fix_task["metadata"]["merge"] == "off", "a round never carries its own merge"
    assert fix_task["metadata"]["dispatch"]["continues"] == ROOT
    assert fix_task["metadata"]["merge_fix_round"] == {
        "kind": "pick", "merge_task": "task_merge", "round": 1, "head_sha": HEAD,
        "pull_request": NUMBER}
    prompt = fix_task["input"]["prompt"]
    assert f"#{NUMBER}" in prompt and "pick fix round 1 of at most 2" in prompt
    # The brief names the question, the pick and the build, and the report.
    assert LINE in prompt
    assert mergewake.PICKS_BUILT_NAME in prompt and "builds_pick" in prompt
    assert "force-push" in prompt

    (claimed,) = _fix_record(db)[mergewake.PICK_ROUNDS_KEY]
    assert claimed == {"round": 1, "head": HEAD, "claimed_at": NOW, "pull_head": HEAD,
                       "picks": [{"task_id": IMPLEMENT, "question_index": 0}],
                       "workflow_id": workflow["workflow_id"], "task_id": step["task_id"]}
    assert "rounds" not in _fix_record(db) and mergewake.CONFLICT_ROUNDS_KEY not in _fix_record(db)


def test_a_pick_round_still_running_is_waited_on_and_never_claimed_twice(
    db, wake_client, github, clock
):
    pick_merge(db, github)
    _tick(wake_client)
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)

    body = _tick(wake_client).json()

    # MUTATION: claim on every tick of a park still `pending` and one park
    # submits a round a minute.
    assert body["report"]["waiting"] == 1 and body["report"]["fixing"] == 0, body
    assert len(_pick_workflows(db)) == 1


def test_a_pick_round_that_ended_wakes_the_step(db, wake_client, github, clock):
    pick_merge(db, github)
    _tick(wake_client)
    (claimed,) = _fix_record(db)[mergewake.PICK_ROUNDS_KEY]
    db.docs[f"tasks/{claimed['task_id']}"]["state"] = "SUCCEEDED"
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == mergewake.FIX_ROUND_ENDED
    assert len(_pick_workflows(db)) == 1


def test_a_worker_parked_on_the_running_round_waits_for_it(db, wake_client, github, clock):
    pick_merge(db, github)
    _tick(wake_client)
    _wait(db)["code"] = "owner_pick_fix_running"
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)

    assert _tick(wake_client).json()["report"]["waiting"] == 1
    assert len(_pick_workflows(db)) == 1


def test_a_new_pending_park_after_an_ended_round_claims_the_next(db, wake_client, github, clock):
    pick_merge(db, github)
    _tick(wake_client)
    (claimed,) = _fix_record(db)[mergewake.PICK_ROUNDS_KEY]
    db.docs[f"tasks/{claimed['task_id']}"]["state"] = "SUCCEEDED"
    # The worker woke, found a pick round 1 was not sent for, and parked again.
    clock.now = NOW + timedelta(seconds=issueci.CI_READ_SECONDS)
    _wait(db)["parked_at"] = clock.now

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    assert [r["round"] for r in _fix_record(db)[mergewake.PICK_ROUNDS_KEY]] == [1, 2]


def test_a_pick_park_with_its_rounds_spent_wakes_the_step_to_refuse(db, wake_client, github):
    pick_merge(db, github)
    db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = {
        mergewake.PICK_ROUNDS_KEY: [
            {"round": n, "head": HEAD, "claimed_at": NOW - timedelta(hours=1),
             "error": "refused"}
            for n in range(1, mergewake.MERGE_PICK_ROUNDS_MAX + 1)]}

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == mergewake.OWNER_PICK_NOT_BUILT
    assert _pick_workflows(db) == []


def test_a_pick_park_on_another_branch_gets_no_round(db, wake_client, github):
    pick_merge(db, github)
    github.pulls[NUMBER]["head"]["ref"] = "swarm/task_" + "9" * 20

    _tick(wake_client)

    assert _wait(db)["wake_reason"] == mergewake.OWNER_PICK_NOT_BUILT
    assert _pick_workflows(db) == []


def test_a_pick_park_whose_pull_request_was_merged_wakes_it_to_finish(db, wake_client, github):
    pick_merge(db, github)
    github.pulls[NUMBER].update(state="closed", merged=True)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    # The reason the worker's `_ended_outside` reads.
    assert _wait(db)["wake_reason"] == mergewake.PULL_REQUEST_MERGED
    assert _pick_workflows(db) == []


def test_a_submitter_who_left_the_tenant_gets_no_pick_round(db, wake_client, github):
    pick_merge(db, github)
    db.docs["tasks/task_merge"]["submitted_by"] = "carol@example.com"

    _tick(wake_client)

    assert _pick_workflows(db) == []
    assert _wait(db)["wake_reason"] == mergewake.FIX_ROUND_REFUSED
    (claimed,) = _fix_record(db)[mergewake.PICK_ROUNDS_KEY]
    assert "no longer a member" in claimed["error"]


def test_the_brief_takes_only_well_formed_pick_lines():
    picks = mergewake.pick_keys([LINE, "pick not_a_task question 0: x", "checks_pending",
                                 f"pick {IMPLEMENT} question 3 (step implement): y"])
    assert picks == [{"task_id": IMPLEMENT, "question_index": 0},
                     {"task_id": IMPLEMENT, "question_index": 3}]


# --------------------------------------------------------------------------
# ROLLUP-PICK-FIX (part of #2441; BGD 2026-10-26): the round is a step
# `pick-fix:<n>` of the lane's workflow, as a CI or conflict round is.
# --------------------------------------------------------------------------

LANE = "wf_" + "e" * 20


def lane_pick_merge(db, github) -> dict:
    """`pick_merge`, as the merge step of a lane workflow LANE."""
    doc = pick_merge(db, github)
    doc["workflow_id"] = LANE
    db.docs[f"workflows/{LANE}"] = {
        "workflow_id": LANE, "tenant_id": "eng", "created_at": NOW, "updated_at": NOW,
        "state": "RUNNING", "submitted_by": "alice@example.com", "on_step_failure": "fail_workflow",
        "priority": 0, "cancel_requested": False,
        "steps": [
            {"step_id": "implement", "runner_profile": "claude-code", "input": {},
             "depends_on": [], "input_from": {}, "task_id": ROOT},
            {"step_id": "merge", "runner_profile": "merge", "input": {}, "depends_on": ["implement"],
             "input_from": {}, "task_id": "task_merge"},
        ],
    }
    return doc


def test_a_pick_round_is_appended_to_the_lanes_workflow_as_pick_fix_1(db, wake_client, github):
    lane_pick_merge(db, github)

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _pick_workflows(db)
    (fix_step,) = workflow["steps"]
    # MUTATION: leave `pick` out of swarm_rollup.FIX_ROUND_STEP_RE and
    # `_append_round_step` refuses the step: the lane shows no round.
    steps = db.docs[f"workflows/{LANE}"]["steps"]
    assert [s["step_id"] for s in steps] == ["implement", "merge", "pick-fix:1"]
    round_step = steps[-1]
    assert round_step["task_id"] == fix_step["task_id"]
    assert round_step["runner_profile"] == "claude-code"
    assert round_step["input"]["merge_fix_round"] == {
        "kind": "pick", "round": 1, "head_sha": HEAD, "pull_request": NUMBER,
        "merge_task": "task_merge", "workflow_id": workflow["workflow_id"]}

    # And the rollup reads it as a round of the merge, not a step of its own.
    readings = read_steps(
        [WorkflowStep(step_id=s["step_id"], runner_profile=s["runner_profile"], input={},
                      task_id=s["task_id"]) for s in steps],
        {ROOT: TaskState.SUCCEEDED, "task_merge": TaskState.SUCCEEDED,
         fix_step["task_id"]: TaskState.FAILED})
    rollup = derive(readings)
    assert rollup.fix_round_steps == ["pick-fix:1"]
    assert rollup.state == "SUCCEEDED", rollup
