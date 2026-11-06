"""A merge step's CI wait that no re-check will end is handed to a person (part of #2688).

Reported by BGD on 2026-11-06: a speech relay lane's merge step
(task_ffe63d609c5d4662ba8e) waited on CI for 8 hours over 28 attempts and
nothing raised it. Each re-check is a new attempt and a new park, and
`first_parked_at` restarts at every head a repair round pushes, so neither
the step nor `/sc check` saw the wait as old. Two CI repair rounds ran and
were overtaken while main was red on the same checks.

What holds now:

  * every CI park carries the WHOLE wait (`control.ci_wait_span`):
    `wait_started_at` across re-checks and the heads repair rounds pushed,
    `rechecks`, and the heads each check failed at;
  * a red reading whose check failed at a head a repair round was claimed
    at, and fails again after it, parks HUMAN_REQUIRED with the evidence;
  * so does a wait past 90 minutes whose rounds are all spent;
  * a short wait, or one with rounds left and nothing repeated, re-checks
    as before.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

from agent_worker import control as control_mod
from agent_worker import merge
from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from fakes import FakeFirestore, FakeTransactionRunner
from merge_world import CHECK, OPENER, PINNED, TASK, TENANT, MergeWorld
from worker_seeds import seed_attempt

FIX_TASK = "task_fix1"
FIXED = "f" * 40
OTHER = "e" * 40


def _world(tmp_path, *, asked: int, waited: timedelta, rechecks: int,
           failed_heads: dict[str, list[str]] | None = None,
           main_red: dict[str, Any] | None = None) -> MergeWorld:
    """Round 1 claimed at PINNED (red), its task SUCCEEDED having pushed
    FIXED, and the step's record of the wait so far; the head is FIXED."""
    world = MergeWorld(tmp_path)
    started = utcnow() - waited
    wait: dict[str, Any] = {
        "code": "checks_pending", "head": FIXED, "wakes": 5, "updates": 0,
        "pull_request": 41,
        "first_parked_at": utcnow() - timedelta(minutes=10),
        "wait_started_at": started.isoformat(), "rechecks": rechecks,
        "failed_heads": failed_heads or {},
    }
    if main_red is not None:
        wait["main_red"] = main_red
    world.docs[TASK]["metadata"] = {
        "merge_fix_rounds": asked,
        merge.MERGE_FIX_METADATA_KEY: {"rounds": [{"round": 1, "head": PINNED, "task_id": FIX_TASK}]},
        control_mod.MERGE_WAIT_METADATA_KEY: wait,
    }
    world.docs[FIX_TASK] = {
        "tenant_id": TENANT, "workflow_id": "wf_fix", "state": "SUCCEEDED",
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": OPENER}},
        "result_summary": {"git": {"pushed_head": FIXED}},
    }
    world.pr["head"]["sha"] = FIXED
    world.serve_checks(FIXED)
    return world


def _red(world: MergeWorld) -> None:
    world.runs[0].update(conclusion="failure")


def _pending(world: MergeWorld) -> None:
    world.runs[0].update(status="in_progress", conclusion=None)


def _handed_to_person(outcome) -> dict[str, Any]:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.ci_wait is None, outcome.ci_wait
    assert outcome.owner_wait is not None, outcome.message
    assert outcome.owner_wait["park_phase"] == merge.CI_WAIT_STUCK
    assert "refusal" not in outcome.summary
    return outcome.owner_wait


# ---------------------------------------------------------------------------
# The merge step's decision
# ---------------------------------------------------------------------------


def test_the_same_check_red_after_a_repair_round_is_handed_to_a_person_with_the_evidence(tmp_path):
    """The incident's shape: 29 re-checks over 8 hours, round 1 of 2 ran at
    PINNED where CHECK was red, CHECK is red again at the round's FIXED, and
    main is red on CHECK too."""
    world = _world(tmp_path, asked=2, waited=timedelta(hours=8), rechecks=29,
                   failed_heads={CHECK: [PINNED]},
                   main_red={"checks": [CHECK], "head": OTHER})
    _red(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: no stuck rule -> parks CI_FIX_PENDING for a second round
    # the tick never claims, and re-checks every 16 minutes.
    stuck = _handed_to_person(outcome)
    assert stuck["why"] == merge.SAME_CHECK_AFTER_ROUND
    assert stuck["failed_again_after_round"] == [CHECK]
    assert stuck["failed_checks"] == [CHECK]
    assert stuck["waited_minutes"] >= 8 * 60
    assert stuck["re_checks"] == 30
    assert (stuck["fix_rounds_ran"], stuck["fix_rounds_asked"]) == (1, 2)
    assert stuck["fix_rounds_spent"] is False
    assert stuck["main_red_on_same"] == [CHECK] and stuck["main_red_head"] == OTHER
    line = stuck["line"]
    assert "8 h" in line and "30 re-checks" in line and CHECK in line
    assert "fix rounds 1 of 2" in line and "main is red" in line
    assert world.merge_calls() == []


def test_spent_repair_rounds_past_ninety_minutes_are_handed_to_a_person(tmp_path):
    """One round asked for and spent; CI still pending at its head after 3 h."""
    world = _world(tmp_path, asked=1, waited=timedelta(hours=3), rechecks=11)
    _pending(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: no stuck rule -> CI_PENDING again, until the 6 h refusal at
    # each new head.
    stuck = _handed_to_person(outcome)
    assert stuck["why"] == merge.ROUNDS_SPENT
    assert stuck["fix_rounds_spent"] is True
    assert stuck["re_checks"] == 12
    assert 3 * 60 - 1 <= stuck["waited_minutes"] <= 3 * 60 + 1


def test_a_short_wait_with_the_rounds_spent_re_checks_as_before(tmp_path):
    world = _world(tmp_path, asked=1, waited=timedelta(minutes=40), rechecks=2)
    _pending(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: hand any spent wait to a person -> fails.
    assert outcome.owner_wait is None
    assert outcome.ci_wait is not None and outcome.ci_wait["head"] == FIXED


def test_a_long_wait_with_rounds_left_and_no_repeated_check_re_checks(tmp_path):
    """Rounds left and nothing failed twice: the tick's next round is due."""
    world = _world(tmp_path, asked=2, waited=timedelta(hours=8), rechecks=29,
                   failed_heads={"another-check": [PINNED]})
    _red(world)

    outcome = merge.run_merge(world.context())

    assert outcome.owner_wait is None, outcome.message
    assert outcome.ci_wait["code"] == merge.CI_FIX_PENDING


def test_the_stuck_window_is_the_checks_parked_window():
    from swarm_mcp import check

    assert merge.CI_STUCK_SECONDS == check.CI_PENDING_MINUTES * 60


# ---------------------------------------------------------------------------
# The park's record of the whole wait
# ---------------------------------------------------------------------------


def _control(db: FakeFirestore) -> ControlPlane:
    return ControlPlane(
        db, task_id="task_1", attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
                            generation=1, runner_profile="merge", stream=io.StringIO()),
        txn_runner=FakeTransactionRunner(db),
    )


def _park(db: FakeFirestore, metadata: dict[str, Any], *, head: str,
          failed: list[str] | None = None) -> dict[str, Any]:
    seed_attempt(db, state=TaskState.RUNNING, runner_profile="merge")
    db.doc("tasks/task_1")["metadata"] = metadata
    _control(db).park_ci_pending(
        code="checks_pending", head=head, pull_request=41, pending=[CHECK],
        max_wakes=merge.MERGE_CI_MAX_WAKES, fallback_seconds=merge.MERGE_CI_FALLBACK_SECONDS,
        failed=failed)
    return db.doc("tasks/task_1")["metadata"][control_mod.MERGE_WAIT_METADATA_KEY]


def _last(started, **extra: Any) -> dict[str, Any]:
    return {"code": "checks_pending", "head": PINNED, "wakes": 3, "updates": 0,
            "first_parked_at": started, "wait_started_at": started, "rechecks": 7,
            "failed_heads": {CHECK: [PINNED]}, **extra}


def test_the_wait_is_carried_across_the_head_a_repair_round_pushed():
    db = FakeFirestore()
    started = utcnow() - timedelta(hours=8)
    metadata = {control_mod.MERGE_WAIT_METADATA_KEY: _last(started),
                "merge_fix": {"rounds": [{"round": 1, "head": PINNED}]}}

    wait = _park(db, metadata, head=FIXED, failed=[CHECK])

    # MUTATION: restart the whole wait at a new head, as `first_parked_at` does.
    assert wait["wait_started_at"] == started
    assert wait["first_parked_at"] > started
    assert wait["rechecks"] == 8
    assert wait["failed_heads"] == {CHECK: [PINNED, FIXED]}


def test_a_head_no_repair_round_worked_from_starts_a_new_wait():
    db = FakeFirestore()
    started = utcnow() - timedelta(hours=8)
    before = utcnow()

    wait = _park(db, {control_mod.MERGE_WAIT_METADATA_KEY: _last(started)}, head=FIXED)

    assert wait["wait_started_at"] >= before
    assert wait["rechecks"] == 1 and wait["failed_heads"] == {}


def test_a_persons_return_starts_a_new_wait():
    db = FakeFirestore()
    started = utcnow() - timedelta(hours=8)
    before = utcnow()
    metadata = {control_mod.MERGE_WAIT_METADATA_KEY: _last(started),
                "human_ready": {"at": (utcnow() - timedelta(minutes=1)).isoformat()}}

    wait = _park(db, metadata, head=PINNED)

    # MUTATION: ignore the return -> the step is handed straight back.
    assert wait["wait_started_at"] >= before and wait["rechecks"] == 1


def test_a_re_check_at_the_same_head_counts_and_keeps_the_start():
    db = FakeFirestore()
    started = utcnow() - timedelta(hours=2)

    wait = _park(db, {control_mod.MERGE_WAIT_METADATA_KEY: _last(
        started, main_red={"checks": [CHECK], "head": OTHER})}, head=PINNED)

    assert wait["wait_started_at"] == started and wait["rechecks"] == 8
    assert wait["main_red"] == {"checks": [CHECK], "head": OTHER}


# ---------------------------------------------------------------------------
# The lifecycle's park
# ---------------------------------------------------------------------------


def test_a_stuck_wait_parks_human_required_under_its_own_phase(db, worker_factory, monkeypatch):
    from agent_worker import lifecycle
    from fakes import ExplodingChildProcess

    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    seed_attempt(db, runner_profile="merge", task_input={"prompt": "merge"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    doc["metadata"] = {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                    "merge_target": {"pull_request": OPENER}}}
    evidence = {"why": merge.ROUNDS_SPENT, "head": FIXED, "pull_request": 41,
                "waited_minutes": 480, "re_checks": 30, "failed_checks": [CHECK],
                "failed_again_after_round": [], "fix_rounds_ran": 2, "fix_rounds_asked": 2,
                "fix_rounds_spent": True, "main_red_on_same": [], "main_red_head": None,
                "wait_started_at": None}

    def run(ctx):
        return merge._Run(ctx, {"action": "merge", "merged_by_this_task": False}).hand_to_person(
            dict(evidence))

    monkeypatch.setattr(merge, "run_merge", run)
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.PARKED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert task["current_lease_id"] is None
    (blocked,) = task["blocked_by"]
    # MUTATION: park it under `owner_questions` -> the console reads questions it has none of.
    assert blocked["park_phase"] == merge.CI_WAIT_STUCK
    assert blocked[merge.CI_WAIT_STUCK]["re_checks"] == 30
    assert "30 re-checks" in blocked[merge.CI_WAIT_STUCK]["line"]
    assert "owner_questions" not in blocked
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"]["reason"] == ParkReason.HUMAN_REQUIRED.value


# ---------------------------------------------------------------------------
# A person's return re-checks (review, part of #2688)
# ---------------------------------------------------------------------------


def test_a_step_a_person_returned_re_checks_rather_than_parking_again(tmp_path):
    """Handed to a person at 3 h with its rounds spent; they fixed it or
    re-ran CI and said "I'm ready" a minute ago, and CI is pending. The
    record still says 3 h (the next park restarts it), so the step re-checks
    rather than handing itself straight back."""
    world = _world(tmp_path, asked=1, waited=timedelta(hours=3), rechecks=11)
    world.docs[TASK]["metadata"]["human_ready"] = {
        "by": "owner@example.com", "at": (utcnow() - timedelta(minutes=1)).isoformat()}
    _pending(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: drop `_wait_restarts` from `ci_stuck` -> PARKED ci_wait_stuck.
    assert outcome.owner_wait is None, outcome.message
    assert outcome.ci_wait is not None and outcome.ci_wait["head"] == FIXED


def test_a_record_left_at_a_head_no_round_worked_from_is_a_new_wait(tmp_path):
    """The branch moved to FIXED by someone else's push: the record at OTHER
    ends there, so its age and failed heads are not this wait's."""
    world = _world(tmp_path, asked=2, waited=timedelta(hours=8), rechecks=29,
                   failed_heads={CHECK: [PINNED]})
    world.docs[TASK]["metadata"][control_mod.MERGE_WAIT_METADATA_KEY]["head"] = OTHER

    # MUTATION: drop `_wait_restarts` -> SAME_CHECK_AFTER_ROUND from OTHER's record.
    assert merge.ci_stuck(world.docs[TASK], head=FIXED, pull_request=41, failed=[CHECK], now=utcnow()) is None
