"""A lane workflow's red or conflicting pull request runs bounded fix rounds by itself (SPEED-D2).

Owner decision 2026-10-14 ("Auto fix rounds"): the merge step of a compiled
lane workflow (implement -> review -> fix -> re-review -> merge) no longer
ends FAILED `checks_failed` the first time CI is red, nor waits for an
operator to dispatch a fixer by hand. It reuses what already exists -- the
merge wake tick's CI-fix rounds (MS7) and conflict rounds (#1076), which
build the issue-run loop's continuation (`issueci.ci_fix_continuation`) --
and adds what the decision asked for:

  1. `fix_rounds` on the workflow spec, stamped on the merge task as
     `metadata.merge_fix_rounds`; 2 by default beside a merge step, 0
     disables, and an issue run's merge (which has its own loop) keeps 0.
  2. A flake costs no agent: the first red reading at a head asks GitHub to
     re-run the failing Actions jobs, and a re-run that passes wakes the step
     green with no round spent.
  3. Each round is a step of the merge's own workflow (`ci-fix:<n>`,
     `conflict-fix:<n>`), visible where the workflow is, and the rollup
     counts it without letting a round that failed fail a workflow whose
     merge then succeeded.
  4. A conflict round's prompt names the files both sides changed; a CI
     round's carries the failing jobs and their log tails (bounded, redacted,
     read by the platform with the tenant's own token).

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from swarm_api import issueci, mergewake
from swarm_common.models import WorkflowStep
from swarm_common.states import TaskState
from swarm_rollup import derive, is_fix_round_step, read_steps

from .conftest import auth_header
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    CHECK,
    HEAD,
    MOVED,
    NOW,
    NUMBER,
    ROOT,
    _conflict_workflows,
    _fix_record,
    _fix_workflows,
    _members_hold_grants,
    _tick,
    _wait,
    clock,
    conflicting_merge,
    fixable_merge,
    github,
    pr_side_claim,
    tenant_tokens,
    wake_client,
)

LANE = "wf_" + "c" * 20


def _red(github, *, log: bytes | None = None) -> int:
    """A fresh failing run of CHECK at HEAD, the latest of its name."""
    github.check_runs[HEAD] = []
    check_id = github.check(HEAD, CHECK, "failure",
                            output={"title": "2 failed", "summary": "test_price_sort failed"})
    if log is not None:
        github.job_logs[check_id] = log
    return check_id


def lane_merge(db, github, *, rounds: int = 2) -> dict:
    """`fixable_merge`, as the merge step of a lane workflow LANE."""
    doc = fixable_merge(db, github, rounds=rounds)
    doc["workflow_id"] = LANE
    db.docs[f"workflows/{LANE}"] = {
        "workflow_id": LANE, "tenant_id": "eng", "created_at": NOW, "updated_at": NOW,
        "state": "RUNNING", "submitted_by": "alice@example.com", "on_step_failure": "fail_workflow",
        "priority": 0, "cancel_requested": False,
        "steps": [
            {"step_id": "fix", "runner_profile": "claude-code", "input": {}, "depends_on": [],
             "input_from": {}, "task_id": ROOT},
            {"step_id": "merge", "runner_profile": "merge", "input": {}, "depends_on": ["fix"],
             "input_from": {}, "task_id": "task_merge"},
        ],
    }
    return doc


def _later(clock, seconds: int) -> None:
    clock.now = clock.now + timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# 2. a flake costs no agent: the platform re-runs the failing jobs first
# --------------------------------------------------------------------------

def test_the_first_red_reading_reruns_the_failing_actions_job_and_spends_no_round(
    db, wake_client, github,
):
    fixable_merge(db, github, rounds=2)
    check_id = _red(github)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1, body
    assert body["report"]["fixing"] == 0 and body["report"]["woken"] == 0
    assert github.reruns == [check_id]
    assert _fix_workflows(db) == []
    assert "rounds" not in _fix_record(db)
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD and rerun["jobs"] == [check_id]
    assert "error" not in rerun
    assert mergewake.WAKE_MARKER not in _wait(db)


def test_a_rerun_that_passes_wakes_the_step_green_with_no_round_spent(
    db, wake_client, github, clock,
):
    fixable_merge(db, github, rounds=2)
    _red(github)
    github.rerun_answer = 201
    _tick(wake_client)

    github.check_runs[HEAD] = []
    github.check(HEAD, CHECK, "success")
    _later(clock, issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "green"
    assert _fix_workflows(db) == []
    assert "rounds" not in _fix_record(db), "a flake spent an agent round"


def test_a_rerun_that_fails_again_hands_the_red_to_one_round(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=2)
    _red(github)
    github.rerun_answer = 201
    _tick(wake_client)

    _red(github)  # the re-run's own check run: a new id, red again
    _later(clock, issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    assert len(_fix_workflows(db)) == 1
    assert len(github.reruns) == 1, "a head is re-run once, never per tick"


def test_a_rerun_github_has_not_started_yet_is_waited_on(db, wake_client, github, clock):
    """Right after the request GitHub may still serve the old failed run as
    the latest: that is the reading the re-run answers, not a second one."""
    fixable_merge(db, github, rounds=2)
    _red(github)
    github.rerun_answer = 201
    _tick(wake_client)

    _later(clock, issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1 and body["report"]["fixing"] == 0, body
    assert _fix_workflows(db) == []


def test_a_rerun_that_never_registers_goes_to_a_round_after_the_settle_window(
    db, wake_client, github, clock,
):
    fixable_merge(db, github, rounds=2)
    _red(github)
    github.rerun_answer = 201
    _tick(wake_client)

    _later(clock, mergewake.RERUN_SETTLE_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body


def test_a_rerun_github_refuses_goes_straight_to_a_round(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _red(github)  # rerun_answer stays 403: no `actions: write`

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD and "error" in rerun
    assert len(_fix_record(db)["rounds"]) == 1


def test_a_red_status_that_is_no_actions_job_is_not_rerun(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    github.check_runs[HEAD] = []
    github.check(HEAD, CHECK, "failure", app_slug="circleci-checks")
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert github.reruns == []
    assert body["report"]["fixing"] == 1, body


# A merge granted no rounds -- every issue run's merge, a lane's
# `fix_rounds: 0` -- still gets the one free re-run per head (#2257: PR
# #2229's one flaky shard on a hidden attempt refused an issue run's merge
# `checks_failed` at once). A re-run pushes nothing, so neither the round
# budget nor the branch the pull request is on bounds it.

def _no_rounds(doc: dict, rounds: int | None) -> dict:
    if rounds is None:
        doc["metadata"].pop("merge_fix_rounds")
    else:
        doc["metadata"]["merge_fix_rounds"] = rounds
    return doc


@pytest.mark.parametrize("rounds", [0, None])
def test_no_rounds_asked_still_reruns_the_failing_actions_job_once_per_head(
    db, wake_client, github, rounds,
):
    _no_rounds(fixable_merge(db, github), rounds)
    check_id = _red(github)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1, body
    assert body["report"]["woken"] == 0 and body["report"]["fixing"] == 0
    assert github.reruns == [check_id]
    assert ("POST", f"/repos/example-org/widgets/actions/jobs/{check_id}/rerun") \
        in github.writes()
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD and rerun["jobs"] == [check_id]
    assert rerun["requested_at"] == NOW and "error" not in rerun
    assert "rounds" not in _fix_record(db)
    assert _fix_workflows(db) == []
    assert mergewake.WAKE_MARKER not in _wait(db)


@pytest.mark.parametrize("rounds", [0, None])
@pytest.mark.parametrize("again", ["new-check-run", "settle-window-past"])
def test_no_rounds_asked_red_again_after_the_rerun_wakes_red(
    db, wake_client, github, clock, rounds, again,
):
    _no_rounds(fixable_merge(db, github), rounds)
    _red(github)
    github.rerun_answer = 201
    _tick(wake_client)

    if again == "new-check-run":
        _red(github)  # the re-run's own check run: a new id, red again
        _later(clock, issueci.CI_READ_SECONDS)
    else:
        _later(clock, mergewake.RERUN_SETTLE_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1 and body["report"]["fixing"] == 0, body
    assert _wait(db)["wake_reason"] == "red"
    assert len(github.reruns) == 1, "a head is re-run once, never per tick"
    assert _fix_workflows(db) == [] and "rounds" not in _fix_record(db)


def test_no_rounds_asked_refused_rerun_wakes_red_in_the_same_tick(db, wake_client, github):
    _no_rounds(fixable_merge(db, github), 0)
    _red(github)  # rerun_answer stays 403: no `actions: write`

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1 and body["report"]["fixing"] == 0, body
    assert _wait(db)["wake_reason"] == "red"
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD and "error" in rerun
    assert github.reruns == [] and _fix_workflows(db) == []


@pytest.mark.parametrize("app_slug", ["circleci-checks", None], ids=["other-app", "status"])
def test_no_rounds_asked_red_that_no_rerun_can_fix_wakes_red_at_once(
    db, wake_client, github, app_slug,
):
    _no_rounds(fixable_merge(db, github), 0)
    github.check_runs[HEAD] = []
    if app_slug is None:
        # A red commit status beside a red Actions job: re-running the job
        # could not turn the status green.
        github.require(CHECK, "ci / lint", app_id=None)
        github.statuses[HEAD] = [{"context": "ci / lint", "state": "failure"}]
        github.check(HEAD, CHECK, "failure")
    else:
        github.check(HEAD, CHECK, "failure", app_slug=app_slug)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "red"
    assert github.reruns == [] and mergewake.RERUNS_KEY not in _fix_record(db)


@pytest.mark.parametrize("rounds", [0, 2])
def test_a_merge_not_on_the_roots_branch_still_gets_the_rerun(
    db, wake_client, github, clock, rounds,
):
    """merge_pr's shape: no task whose branch a round would push to. A round
    is impossible; the re-run, which pushes nothing, is not."""
    doc = fixable_merge(db, github, rounds=rounds)
    doc["metadata"]["dispatch"].pop("merge_target")
    check_id = _red(github)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1 and body["report"]["woken"] == 0, body
    assert github.reruns == [check_id]

    _red(github)
    _later(clock, issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "red"
    assert _fix_workflows(db) == [] and len(github.reruns) == 1


def test_rounds_spent_new_head_gets_one_rerun_before_the_wake(db, wake_client, github, clock):
    fixable_merge(db, github, rounds=1)
    db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_FIX_METADATA_KEY] = {
        "rounds": [{"round": 1, "head": MOVED, "task_id": "task_" + "2" * 20}]}
    check_id = _red(github)
    github.rerun_answer = 201

    body = _tick(wake_client).json()

    assert body["report"]["rerun"] == 1 and body["report"]["woken"] == 0, body
    assert github.reruns == [check_id]
    (rerun,) = _fix_record(db)[mergewake.RERUNS_KEY]
    assert rerun["head"] == HEAD

    _red(github)
    _later(clock, issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "red"
    assert len(_fix_record(db)["rounds"]) == 1 and _fix_workflows(db) == []
    assert len(github.reruns) == 1


def test_no_rounds_asked_waits_on_a_live_pr_side_fix_without_a_rerun(db, wake_client, github):
    _no_rounds(fixable_merge(db, github), 0)
    _red(github)
    github.rerun_answer = 201
    pr_side_claim(db)

    body = _tick(wake_client).json()

    assert body["report"]["waiting"] == 1 and body["report"]["woken"] == 0, body
    assert github.reruns == [] and _fix_record(db) == {}
    assert mergewake.WAKE_MARKER not in _wait(db)


# --------------------------------------------------------------------------
# 3 + 4. the round is a step of the lane's workflow, with the excerpt
# --------------------------------------------------------------------------

def test_red_creates_one_ci_fix_step_on_the_lanes_workflow_with_the_log_excerpt(
    db, wake_client, github,
):
    lane_merge(db, github)
    _red(github, log=b"collected 12 items\nFAILED tests/test_sort.py::test_price_sort\n")

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _fix_workflows(db)
    (fix_step,) = workflow["steps"]
    fix_task = db.docs[f"tasks/{fix_step['task_id']}"]
    prompt = fix_task["input"]["prompt"]
    assert CHECK in prompt and "FAILED tests/test_sort.py::test_price_sort" in prompt
    assert len(prompt.encode()) < issueci.MAX_EXCERPT_BYTES + 4096, "the excerpt is bounded"
    # The round is a step of the lane's own workflow, after its own steps.
    steps = db.docs[f"workflows/{LANE}"]["steps"]
    assert [s["step_id"] for s in steps] == ["fix", "merge", "ci-fix:1"]
    round_step = steps[-1]
    assert round_step["task_id"] == fix_step["task_id"]
    assert round_step["runner_profile"] == "claude-code"
    assert round_step["resource_class"] == "standard", "the fix step's resource class"
    assert round_step["input"]["merge_fix_round"] == {
        "kind": "ci", "round": 1, "head_sha": HEAD, "pull_request": NUMBER,
        "merge_task": "task_merge", "workflow_id": workflow["workflow_id"]}


def test_a_dirty_pull_request_creates_one_conflict_fix_step_naming_the_files(
    db, wake_client, github,
):
    lane_merge(db, github, rounds=2)
    github.pulls[NUMBER].update(mergeable=False, mergeable_state="dirty")
    github.check_runs.pop(HEAD, None)
    github.pull_file_names[NUMBER] = ["apps/ui/LiveBrowser.tsx", "apps/api/only_ours.py"]
    github.base_changed = ["apps/ui/LiveBrowser.tsx", "docs/only_theirs.md"]

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    (workflow,) = _conflict_workflows(db)
    (fix_step,) = workflow["steps"]
    prompt = db.docs[f"tasks/{fix_step['task_id']}"]["input"]["prompt"]
    assert "apps/ui/LiveBrowser.tsx" in prompt
    assert "only_ours.py" not in prompt and "only_theirs.md" not in prompt
    steps = db.docs[f"workflows/{LANE}"]["steps"]
    assert [s["step_id"] for s in steps] == ["fix", "merge", "conflict-fix:1"]
    assert steps[-1]["input"]["merge_fix_round"]["kind"] == "conflict"
    assert github.reruns == [], "a conflict is never re-run: GitHub ran no checks"


def test_a_conflict_whose_files_cannot_be_read_still_gets_its_round(db, wake_client, github):
    lane_merge(db, github, rounds=2)
    github.pulls[NUMBER].update(mergeable=False, mergeable_state="dirty")
    github.check_runs.pop(HEAD, None)
    github.refuse[("GET", f"/repos/example-org/widgets/pulls/{NUMBER}/files")] = 403

    body = _tick(wake_client).json()

    assert body["report"]["fixing"] == 1, body
    assert len(_conflict_workflows(db)) == 1


def test_the_second_round_is_the_second_step_and_the_third_is_never_made(
    db, wake_client, github, clock,
):
    lane_merge(db, github, rounds=2)
    _red(github)
    _tick(wake_client)
    (first,) = _fix_record(db)["rounds"]
    db.docs[f"tasks/{first['task_id']}"].update(
        state="SUCCEEDED", result_summary={"git": {"pushed_head": MOVED}})

    # The round pushed MOVED; the worker re-parks there, and MOVED is red too.
    wait = _wait(db)
    wait.update(head=MOVED)
    wait.pop("checked_at", None)
    github.pulls[NUMBER]["head"]["sha"] = MOVED
    github.check(MOVED, CHECK, "failure")
    _later(clock, issueci.CI_READ_SECONDS)
    assert _tick(wake_client).json()["report"]["fixing"] == 1

    second = _fix_record(db)["rounds"][1]
    db.docs[f"tasks/{second['task_id']}"]["state"] = "FAILED"
    third = "d" * 40
    wait = _wait(db)
    wait.update(head=third)
    wait.pop("checked_at", None)
    github.pulls[NUMBER]["head"]["sha"] = third
    github.check(third, CHECK, "failure")
    _later(clock, issueci.CI_READ_SECONDS)
    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)["wake_reason"] == "red", "rounds spent: the worker refuses checks_failed"
    steps = [s["step_id"] for s in db.docs[f"workflows/{LANE}"]["steps"]]
    assert steps == ["fix", "merge", "ci-fix:1", "ci-fix:2"]


def test_a_merge_with_no_workflow_record_still_gets_its_round(db, wake_client, github):
    fixable_merge(db, github, rounds=2)
    _red(github)
    assert _tick(wake_client).json()["report"]["fixing"] == 1
    assert len(_fix_workflows(db)) == 1


def test_a_round_step_is_appended_once_however_often_it_is_recorded(db, github):
    lane_merge(db, github)
    step = {"step_id": "ci-fix:1", "runner_profile": "claude-code", "input": {},
            "depends_on": [], "input_from": {}, "task_id": "task_" + "7" * 20}
    assert mergewake._append_round_step(db, "eng", LANE, step) is True
    assert mergewake._append_round_step(db, "eng", LANE, step) is False
    assert mergewake._append_round_step(db, "research", LANE, {**step, "step_id": "ci-fix:2"}) \
        is False, "another tenant's workflow is never written"
    steps = [s["step_id"] for s in db.docs[f"workflows/{LANE}"]["steps"]]
    assert steps == ["fix", "merge", "ci-fix:1"]


# --------------------------------------------------------------------------
# 3. the rollup counts the round steps correctly
# --------------------------------------------------------------------------

def _derive(**states: str):
    steps = [WorkflowStep(step_id=sid, runner_profile="claude-code", input={},
                          task_id=f"task_{n}")
             for n, sid in enumerate(states)]
    by_task = {f"task_{n}": TaskState(state) for n, state in enumerate(states.values())}
    return derive(read_steps(steps, by_task))


def test_a_failed_round_does_not_fail_a_workflow_whose_merge_succeeded():
    rollup = _derive(**{"implement": "SUCCEEDED", "merge": "SUCCEEDED",
                        "ci-fix:1": "FAILED", "ci-fix:2": "SUCCEEDED"})
    assert rollup.state == "SUCCEEDED", rollup
    assert rollup.counts == {"SUCCEEDED": 3, "FAILED": 1}
    assert rollup.fix_round_steps == ["ci-fix:1", "ci-fix:2"]
    assert rollup.to_api()["fix_round_steps"] == ["ci-fix:1", "ci-fix:2"]


def test_a_failed_pick_round_does_not_fail_a_workflow_whose_merge_succeeded():
    """ROLLUP-PICK-FIX (BGD 2026-10-26): a pick-fix round is a merge round."""
    rollup = _derive(**{"implement": "SUCCEEDED", "merge": "SUCCEEDED",
                        "pick-fix:1": "FAILED", "pick-fix:2": "SUCCEEDED"})
    assert rollup.state == "SUCCEEDED", rollup
    assert rollup.fix_round_steps == ["pick-fix:1", "pick-fix:2"]


def test_the_round_step_pattern_takes_each_kind_the_tick_writes_and_nothing_else():
    """Every kind `mergewake.round_step_id` can write is a round, so no
    appended round is refused; a caller-shaped id never is one."""
    for key in mergewake.ROUND_KIND:
        assert is_fix_round_step(mergewake.round_step_id(key, 1)), key
    assert not is_fix_round_step("pick-fix:0")
    assert not is_fix_round_step("pick-fix-1")
    assert not is_fix_round_step("other-fix:1")


def test_a_running_round_keeps_the_workflow_running_while_the_merge_is_parked():
    rollup = _derive(**{"fix": "SUCCEEDED", "merge": "PARKED", "conflict-fix:1": "RUNNING"})
    assert rollup.state == "RUNNING"


def test_a_merge_that_failed_after_its_rounds_fails_the_workflow():
    rollup = _derive(**{"fix": "SUCCEEDED", "merge": "FAILED", "ci-fix:1": "SUCCEEDED"})
    assert rollup.state == "FAILED"


def test_a_round_still_queued_after_the_merge_ended_is_not_reported_finished():
    rollup = _derive(**{"fix": "SUCCEEDED", "merge": "SUCCEEDED", "ci-fix:1": "QUEUED"})
    assert rollup.state == "QUEUED"


def test_a_submitted_step_that_only_looks_like_a_round_is_counted_as_any_step():
    """A caller's step id cannot hold ':' (`WorkflowStepCreate.step_id`), so
    a step only the tick appended is excused; `ci-fix-1` is the caller's."""
    rollup = _derive(**{"implement": "SUCCEEDED", "ci-fix-1": "FAILED", "merge": "SUCCEEDED"})
    assert rollup.state == "FAILED"
    assert rollup.fix_round_steps == []


def test_a_workflow_of_nothing_but_rounds_reads_as_its_rounds():
    assert _derive(**{"ci-fix:1": "FAILED"}).state == "FAILED"


# --------------------------------------------------------------------------
# 1. `fix_rounds` on the spec, stamped on the merge task
# --------------------------------------------------------------------------

REPO = "https://github.com/octo-org/widget-shop.git"


def _lane(**overrides) -> dict:
    """implement -> review -> fix (gated) under `integrate`, merge on."""
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "metadata": {"merge": "on"},
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding"}},
        ],
    }
    spec.update(overrides)
    return spec


def _merge_task(client, db, spec) -> dict:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    (merge,) = [s for s in response.json()["workflow"]["steps"] if s["runner_profile"] == "merge"]
    return db.docs[f"tasks/{merge['task_id']}"]


def test_a_lane_workflow_gets_two_fix_rounds_by_default(client, db):
    assert _merge_task(client, db, _lane())["metadata"]["merge_fix_rounds"] == 2


@pytest.mark.parametrize("rounds", [0, 1, 3])
def test_fix_rounds_on_the_spec_is_what_the_merge_task_gets(client, db, rounds):
    task = _merge_task(client, db, _lane(fix_rounds=rounds))
    assert task["metadata"]["merge_fix_rounds"] == rounds


def test_metadata_merge_fix_rounds_is_still_honoured(client, db):
    task = _merge_task(client, db, _lane(metadata={"merge": "on", "merge_fix_rounds": 4}))
    assert task["metadata"]["merge_fix_rounds"] == 4


def test_fix_rounds_and_a_different_metadata_merge_fix_rounds_are_refused(client):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_lane(
        fix_rounds=1, metadata={"merge": "on", "merge_fix_rounds": 3}))
    assert response.status_code == 422, response.text
    assert "fix_rounds" in response.json()["message"]


@pytest.mark.parametrize("rounds", [-1, 6, True, "2"])
def test_fix_rounds_out_of_range_is_refused(client, rounds):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_lane(fix_rounds=rounds))
    assert response.status_code == 422, response.text


def test_fix_rounds_without_a_merge_step_is_refused(client):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_lane(fix_rounds=1, metadata={"merge": "off"}))
    assert response.status_code == 422, response.text
    assert "fix_rounds" in response.json()["message"]


def test_an_issue_runs_merge_keeps_no_rounds_it_has_its_own_loop(client, db):
    task = _merge_task(client, db, _lane(metadata={
        "merge": "on", "issue_run": {"run_id": "run_x", "issue": "octo-org/widget-shop#1"}}))
    assert task["metadata"]["merge_fix_rounds"] == 0


def test_only_the_merge_task_carries_the_stamp(client, db):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_lane())
    for step in response.json()["workflow"]["steps"]:
        metadata = db.docs[f"tasks/{step['task_id']}"]["metadata"]
        assert ("merge_fix_rounds" in metadata) == (step["runner_profile"] == "merge"), step
