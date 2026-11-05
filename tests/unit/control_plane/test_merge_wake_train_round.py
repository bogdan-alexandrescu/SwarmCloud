"""The merge wake tick submits the CI-fix round a second train ejection parks for (#3313).

The worker re-boards a pull request the repository's merge train ejected at a
green head once (`ready` again); ejected a second time at that head, it parks
`TRAIN_FIX_PENDING` with the train run's failing jobs (`failed`). The head's
own checks are green, so the tick never reads them as red: it submits one
CI-fix round through the rounds every red head gets (`_hand_off`,
`_submit_round`, key `rounds`, the same budget), whose prompt says the train
of main plus this pull request failed and carries the ejection comment's
failing jobs and their log tails. No free re-run: the worker's re-board was
it. A PR-side fix holds it, as it holds a red head's round.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from agent_worker import merge as worker_merge
from swarm_api import mergewake

from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    CHECK,
    HEAD,
    NUMBER,
    _fix_record,
    _fix_workflows,
    _members_hold_grants,
    _tick,
    _wait,
    clock,
    fixable_merge,
    github,
    pr_side_claim,
    tenant_tokens,
    wake_client,
)

TRAIN_JOB = "web / vitest (shard 3)"
JOB_ID = 987654
RUN_URL = "https://github.com/example-org/widgets/actions/runs/555"


def _ejection() -> str:
    return "\n".join([
        f"{worker_merge.REPOSITORY_TRAIN_EJECTED} and `ready` removed.** Train 7 -- main "
        "plus this pull request alone -- failed:",
        "",
        f"- [{TRAIN_JOB}]({RUN_URL}/job/{JOB_ID})",
        "",
        "With nothing else in the train, the failure is this change's. Fix it, then add "
        "`ready` again; if the failure was a flake, add `ready` again as it is.",
        f"(scripts/merge-train.sh, run {RUN_URL})",
    ])


def train_parked_merge(db, github, *, rounds: int = 2) -> dict:
    """`fixable_merge`, green at HEAD, parked TRAIN_FIX_PENDING after two ejections."""
    doc = fixable_merge(db, github, rounds=rounds)
    github.check_runs[HEAD] = []
    github.check(HEAD, CHECK, "success")
    doc["metadata"][mergewake.MERGE_WAIT_METADATA_KEY].update(
        code=mergewake.TRAIN_FIX_PENDING, pending=[mergewake.TRAIN_REBOARDED],
        failed=[TRAIN_JOB])
    github.add_comment(NUMBER, _ejection(), login="swarmcloud-merge[bot]")
    github.job_logs[JOB_ID] = (b"RUN v2.1.9\n FAIL  src/ladder.test.ts > ladder > climbs\n"
                               b"Error: Test timed out in 30000ms.\n")
    return doc


def test_the_train_codes_are_held_equal_to_the_workers():
    assert mergewake.TRAIN_FIX_PENDING == worker_merge.TRAIN_FIX_PENDING
    assert mergewake.CI_FIX_RUNNING == worker_merge.CI_FIX_RUNNING
    assert mergewake.TRAIN_REBOARDED == worker_merge.TRAIN_REBOARDED
    assert mergewake.REPOSITORY_TRAIN_EJECTED == worker_merge.REPOSITORY_TRAIN_EJECTED


def test_a_second_train_ejection_park_gets_one_ci_fix_round_naming_the_trains_failing_job(
    db, wake_client, github,
):
    train_parked_merge(db, github)

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (entry,) = _fix_record(db)["rounds"]
    assert entry["round"] == 1 and entry["head"] == HEAD and entry.get("task_id"), entry
    (workflow,) = _fix_workflows(db)
    (fix_step,) = workflow["steps"]
    prompt = db.docs[f"tasks/{fix_step['task_id']}"]["input"]["prompt"]
    assert "merge train" in prompt and TRAIN_JOB in prompt
    assert "Test timed out in 30000ms" in prompt, "the train job's log tail"
    assert "CI is red at" not in prompt and f"## {CHECK}" not in prompt, (
        "the head's own green check is not handed to the round as red")
    assert github.reruns == [] and github.run_reruns == [], "no free re-run: the re-board was it"
    assert _wait(db)[mergewake.WAKE_MARKER], "woken once the round is recorded"


def test_two_ticks_submit_the_train_round_once(db, wake_client, github, clock):
    train_parked_merge(db, github)

    first = _tick(wake_client).json()
    # The worker has not run yet: the park is as it was, its mark cleared.
    _wait(db).pop(mergewake.WAKE_MARKER)
    _wait(db).pop("checked_at", None)
    second = _tick(wake_client).json()

    assert first["report"]["fixing"] == 1, first
    assert second["report"]["fixing"] == 0, second
    assert len(_fix_record(db)["rounds"]) == 1
    assert len(_fix_workflows(db)) == 1


def test_with_no_round_asked_the_train_park_is_woken_to_refuse(db, wake_client, github):
    train_parked_merge(db, github, rounds=0)

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1 and body["report"]["fixing"] == 0, body
    assert _fix_record(db) == {} and _fix_workflows(db) == []
    assert _wait(db)[mergewake.WAKE_MARKER]


def test_a_pr_side_fix_holds_the_train_round(db, wake_client, github):
    train_parked_merge(db, github)
    pr_side_claim(db)

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0, body
    assert body["report"]["waiting"] == 1
    assert _fix_record(db) == {} and _fix_workflows(db) == []
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_a_running_train_round_is_waited_on_not_woken_green(db, wake_client, github):
    train_parked_merge(db, github)
    _tick(wake_client).json()
    wait = _wait(db)
    for key in (mergewake.WAKE_MARKER, "wake_reason", "checked_at"):
        wait.pop(key, None)
    wait["code"] = mergewake.CI_FIX_RUNNING

    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1 and body["report"]["woken"] == 0, body
    assert mergewake.WAKE_MARKER not in _wait(db)
