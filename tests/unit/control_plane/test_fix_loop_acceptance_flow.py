"""The acceptance check's fix-loop workflow, through the real scheduler and swarm-api (issue 3).

`scripts/acceptance/groups/workflow.sh` "workflow: fix-loop" (docs/design/
fix-loop.md §8 row FL-ACCEPT) failed on dev with the loop step PARKED
DEPENDENCY_INCOMPLETE for its whole hour. Every test in test_fix_loop_drive.py
starts the loop with `park_running`, which writes the scheduler's park by hand,
so no test had carried a loop from DEPENDENCY_INCOMPLETE to its first round
through `Scheduler.release_dependants` and the drain. These do, on the check's
own workflow shape:

    review (mock, NOT_YET with one major finding, the root)
      -> fix (claude-code, the `integrate` integrator, gated on that NOT_YET)
      -> loop (fix-loop, max_rounds 3)

with the reviews saying NOT_YET, NOT_YET, MERGE: the loop leaves
DEPENDENCY_INCOMPLETE the moment fix SUCCEEDED, holding no lease (invariant
1), and appends re-review:1, review-fix:2 and re-review:2, in that order, and
no more. Run with the deployment's control-publish hold (60 s,
`CONTROL_PUBLISH_HOLD_SECONDS` in terraform/infra/locals.tf) and without it,
and with a merge step appended (`metadata.merge` "on", which a repository's
`merge_policy` or the platform's `merge_by_default` gives the check's
workflow without its asking) and without one.

Also the order the two subscribers can take: swarm-api's `task_finished` for
fix may be handled before the scheduler has parked the loop. The loop is then
not yet FIX_ROUNDS_RUNNING, the push drives nothing, and the per-tenant tick's
sweep drives it once fix ended over `SWEEP_AFTER_SECONDS` ago.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from swarm_api import fixloop
from swarm_common.states import ParkReason, TaskState

from .conftest import scheduler_settings, seed_pool
from .test_fix_loop_drive import (  # noqa: F401 -- fixtures
    FIX_HEAD,
    MERGE,
    REPO,
    ROUND_HEAD,
    Loop,
    _members_hold_grants,
    _tick,
    clock,
    finish,
    github,
    loop_client,
    loop_enabled,
    push,
    pushed,
    round_task,
)

#: The check's WF_FIX_LOOP_* values.
LOOP_FILE = "fix-loop-rounds.txt"
MAX_ROUNDS = 3
EXPECTED_ROUNDS = ["re-review:1", "review-fix:2", "re-review:2"]
#: The mock review's verdict.json: NOT_YET with one major finding, so the
#: worker's no-op precheck does not skip the fix.
FIRST_REVIEW = {"verdict": "NOT_YET", "findings": [
    {"severity": "major", "summary": f"{LOOP_FILE} has fewer than 2 non-empty lines"}]}
SECOND_REVIEW = {"verdict": "NOT_YET", "commit": FIX_HEAD, "findings": [
    {"severity": "major", "summary": f"{LOOP_FILE} has 1 non-empty lines; it needs 2"}]}
DEPENDENCY = ParkReason.DEPENDENCY_INCOMPLETE.value
RUNNING = ParkReason.FIX_ROUNDS_RUNNING.value


def acceptance_spec(merge: str | None = None) -> dict:
    """`_wf_fix_loop_body`, with the prompts shortened: what each agent writes is the test's."""
    metadata = {"source": "acceptance", "acceptance_run": "run-1", "check": "workflow: fix-loop"}
    if merge is not None:
        metadata["merge"] = merge
    return {
        "priority": 10, "metadata": metadata, "on_step_failure": "fail_workflow",
        "strategy": "integrate", "repository_url": REPO, "repository_ref": "main",
        "steps": [
            {"step_id": "review", "runner_profile": "mock",
             "input": {"prompt": "acceptance run-1 fix-loop review", "artifact_name": "verdict.json",
                       "artifact_text": json.dumps(FIRST_REVIEW), "steps": 1, "sleep_seconds": 0}},
            {"step_id": "fix", "runner_profile": "claude-code", "depends_on": ["review"],
             "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": f"Append one line to {LOOP_FILE}."}},
            {"step_id": "loop", "runner_profile": "fix-loop", "depends_on": ["fix"],
             "fix_loop": {"review": "review", "fix": "fix", "max_rounds": MAX_ROUNDS,
                          "fix_round": {"runner_profile": "claude-code",
                                        "prompt": f"Append one line to {LOOP_FILE}."},
                          "re_review": {"runner_profile": "claude-code",
                                        "prompt": f"Count the lines of {LOOP_FILE}."}}},
        ],
    }


def _park(loop: Loop, step: str) -> tuple[str, str | None]:
    doc = loop.doc(step)
    return doc["state"], doc.get("park_reason")


def _holds_nothing(db, loop: Loop) -> None:
    """Invariant 1: the loop is never leased, so no lease or attempt names it."""
    doc = loop.doc()
    assert doc["current_lease_id"] is None, doc
    assert doc.get("attempt_count", 0) == 0, doc
    for key, value in db.docs.items():
        if key.startswith(("leases/", "attempts/")):
            assert value.get("task_id") != loop.loop, (key, value)


def _to_fix_succeeded(client, db, objects, scheduler, clock, *, merge: str | None) -> Loop:
    """Submit, end review NOT_YET, run fix to SUCCEEDED as the deployment would."""
    loop = Loop(client, db, acceptance_spec(merge))
    assert _park(loop, "loop") == ("PARKED", DEPENDENCY)

    finish(db, objects, loop.tasks["review"], verdict=FIRST_REVIEW, completed_at=clock.now)
    scheduler.release_dependants(loop.tasks["review"])
    push(client, loop.tasks["review"])
    # swarm-api declined to publish the integrator and rang the review again.
    scheduler.release_dependants(loop.tasks["review"])
    clock.now += timedelta(seconds=90)
    scheduler.drain()
    assert _park(loop, "fix") != ("PARKED", DEPENDENCY), loop.doc("fix")
    assert _park(loop, "loop") == ("PARKED", DEPENDENCY)

    clock.now += timedelta(minutes=2)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD),
           completed_at=clock.now)
    return loop


def _run_rounds(client, db, objects, loop: Loop, clock) -> None:
    """re-review:1 says NOT_YET, review-fix:2 pushes, re-review:2 says MERGE."""
    assert list(loop.round_steps()) == EXPECTED_ROUNDS[:1]
    first = round_task(loop, "re-review:1")
    finish(db, objects, first, verdict=SECOND_REVIEW, completed_at=clock.now)
    push(client, first)
    assert list(loop.round_steps()) == EXPECTED_ROUNDS[:2]
    _holds_nothing(db, loop)

    fix = round_task(loop, "review-fix:2")
    finish(db, objects, fix, git=pushed(fix, ROUND_HEAD), completed_at=clock.now)
    push(client, fix)
    assert list(loop.round_steps()) == EXPECTED_ROUNDS
    _holds_nothing(db, loop)

    last = round_task(loop, "re-review:2")
    finish(db, objects, last, verdict={**MERGE, "commit": ROUND_HEAD}, completed_at=clock.now)
    push(client, last)


def _ended_on_merge(loop: Loop) -> None:
    doc = loop.doc()
    assert doc["state"] == TaskState.SUCCEEDED.value, doc
    assert doc["result_summary"]["fix_loop"]["verdict"] == "MERGE"
    assert list(loop.round_steps()) == EXPECTED_ROUNDS
    kinds = [(r["round"], r["kind"]) for r in doc["result_summary"]["fix_loop"]["rounds"]]
    assert kinds == [(1, "review"), (2, "fix"), (2, "review")]


@pytest.mark.parametrize("hold", [0.0, 60.0], ids=["no-hold", "deployed-hold"])
@pytest.mark.parametrize("merge", [None, "on"], ids=["no-merge", "merge-on"])
def test_the_loop_leaves_dependency_incomplete_when_fix_succeeds_and_runs_its_rounds_in_order(
        loop_client, db, objects, make_scheduler, clock, hold, merge):
    seed_pool(db, "global", hard_limit=50)
    scheduler = make_scheduler(
        settings=scheduler_settings(control_publish_hold_seconds=hold), now=clock)
    loop = _to_fix_succeeded(loop_client, db, objects, scheduler, clock, merge=merge)

    scheduler.release_dependants(loop.tasks["fix"])

    assert _park(loop, "loop") == ("PARKED", RUNNING)
    _holds_nothing(db, loop)
    body = push(loop_client, loop.tasks["fix"])
    assert body["fix_loop"]["submitted"] == 1, body
    _run_rounds(loop_client, db, objects, loop, clock)
    _ended_on_merge(loop)
    _holds_nothing(db, loop)


def test_the_drain_alone_releases_the_loop_when_the_finish_event_is_lost(
        loop_client, db, objects, make_scheduler, clock):
    seed_pool(db, "global", hard_limit=50)
    scheduler = make_scheduler(
        settings=scheduler_settings(control_publish_hold_seconds=60.0), now=clock)
    loop = _to_fix_succeeded(loop_client, db, objects, scheduler, clock, merge=None)

    clock.now += timedelta(seconds=30)
    scheduler.drain()

    assert _park(loop, "loop") == ("PARKED", RUNNING)
    _holds_nothing(db, loop)


def test_a_fix_push_read_before_the_scheduler_parks_the_loop_leaves_it_to_the_sweep(
        loop_client, db, objects, make_scheduler, clock):
    seed_pool(db, "global", hard_limit=50)
    scheduler = make_scheduler(
        settings=scheduler_settings(control_publish_hold_seconds=60.0), now=clock)
    loop = _to_fix_succeeded(loop_client, db, objects, scheduler, clock, merge=None)

    # swarm-api first: the loop is still DEPENDENCY_INCOMPLETE, so nothing is driven.
    body = push(loop_client, loop.tasks["fix"])
    assert body["fix_loop"]["driven"] == 0, body
    assert loop.round_steps() == {}
    scheduler.release_dependants(loop.tasks["fix"])
    assert _park(loop, "loop") == ("PARKED", RUNNING)

    clock.now += timedelta(seconds=fixloop.SWEEP_AFTER_SECONDS + 1)
    swept = _tick(loop_client)

    assert swept["fix_loop"]["driven"] == 1, swept
    _run_rounds(loop_client, db, objects, loop, clock)
    _ended_on_merge(loop)
    _holds_nothing(db, loop)

