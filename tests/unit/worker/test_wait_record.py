"""A park records the facts the "Why it's waiting" panel serves (WAIT-RECORD; part of #1782).

swarm-api's `waiting_detail` builds `waiting` from a task's park events and
serves a fact as null when no writer records it. Owner decision 2026-10-21
~04:10Z: the platform records them. What the worker's park writers now hold:

  * A CI park (`ControlPlane.park_ci_pending`, the merge step's `run.wait`)
    records `failed` -- the required checks whose LATEST run at the head
    failed -- beside `pending`, and `pull_request`, in the PARKED event and in
    `metadata.merge_wait`. A check is judged by its latest run, so a run
    cancelled and superseded by a success is not failed. Since CHECK-RULE
    (BGD 2026-10-22, part of #1999 and #1778) the merge decision reads the
    same latest run, per event, so the record and the decision agree.
  * A HUMAN_REQUIRED park (`handoff.park_human_required`) records
    `expires_at`: the park instant plus the scheduler's
    `human_park_max_seconds`.

ADDITIVE ONLY. Every field a reader read before keeps its value and type, so
a reader that ignores the new ones reads exactly what it read before.

No credentials, no network, no emulator. Each assertion names the mutation it
catches.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker import handoff as handoff_mod
from agent_worker import merge
from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.states import EventType, ParkReason, TaskState

from fakes import FakeFirestore, FakeTransactionRunner
from worker_seeds import TENANT, seed_attempt

from merge_world import ACTIONS_APP, CHECK, NUMBER, PINNED, TASK, MergeWorld

LINT = "ci / lint"

#: The fields a CI park's PARKED event carried before this lane: a reader
#: written against them must read the same values today.
CI_EVENT_FIELDS_BEFORE = {"reason", "next_eligible_at", "attempt_refunded", "wakes",
                          "code", "head", "pending"}
#: The fields `metadata.merge_wait` carried before this lane.
MERGE_WAIT_FIELDS_BEFORE = {"code", "head", "pull_request", "pending", "wakes", "slot_waits",
                            "train_waits", "updates", "first_parked_at", "parked_at"}


def _run(name: str, run_id: int, status: str, conclusion: str | None) -> dict[str, Any]:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion,
            "app": {"id": ACTIONS_APP}}


def _two_required(world: MergeWorld) -> None:
    world.rules = [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": CHECK, "integration_id": ACTIONS_APP},
                                   {"context": LINT, "integration_id": ACTIONS_APP}]}}]


def _rounds_left(world: MergeWorld) -> None:
    """A CI-fix round is due: a red reading parks for the tick, not `checks_failed`."""
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}


# ---------------------------------------------------------------------------
# The merge step's reading: failed and pending by name, and the PR
# ---------------------------------------------------------------------------


def test_a_ci_park_with_one_failed_and_one_pending_check_records_both_and_the_pr(tmp_path):
    world = MergeWorld(tmp_path)
    _two_required(world)
    _rounds_left(world)
    world.runs = [_run(CHECK, 11, "completed", "failure"), _run(LINT, 12, "in_progress", None)]

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    wait = outcome.ci_wait
    # MUTATION: `run.wait` drops the failed names (records no `failed`) -> fails.
    assert wait["failed"] == [CHECK], wait
    # MUTATION: the red path's wait keeps recording `pending=[]` -> fails.
    assert wait["pending"] == [LINT], wait
    # MUTATION: the park records no pull request number -> fails.
    assert wait["pull_request"] == NUMBER
    # MUTATION: the failed name carries its conclusion ("ci / unit (failure)")
    # rather than being the check's name -> fails on the equality above too.
    assert all("(" not in name for name in wait["failed"])


def test_a_cancelled_run_superseded_by_a_success_is_not_failed(tmp_path):
    """The newer run is the higher id, wherever GitHub lists it."""
    world = MergeWorld(tmp_path)
    _two_required(world)
    _rounds_left(world)
    world.runs = [_run(CHECK, 30, "completed", "success"),
                  _run(CHECK, 20, "completed", "cancelled"),
                  _run(LINT, 31, "in_progress", None)]

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    # MUTATION: judge a check by any failed run (the every-run rule
    # `required_check_state` had before CHECK-RULE) -> `failed == [CHECK]` -> fails.
    # MUTATION: judge by the last run in GitHub's listing, not the newest id
    # -> the cancelled run (listed last) is taken -> fails.
    assert outcome.ci_wait["failed"] == [], outcome.ci_wait
    # MUTATION: a superseded check is dropped from `pending` too -> fails.
    assert outcome.ci_wait["pending"] == [LINT]


def test_the_control_a_cancelled_run_that_is_the_newest_is_failed(tmp_path):
    """The control for the test above: the same runs, the ids swapped."""
    world = MergeWorld(tmp_path)
    _two_required(world)
    _rounds_left(world)
    world.runs = [_run(CHECK, 20, "completed", "success"),
                  _run(CHECK, 30, "completed", "cancelled"),
                  _run(LINT, 31, "in_progress", None)]

    outcome = merge.run_merge(world.context())

    # MUTATION: `failed` is always [] (the reading is never recorded) -> fails.
    assert outcome.ci_wait["failed"] == [CHECK], outcome.ci_wait


def test_a_wait_made_before_the_checks_were_read_records_no_failed(tmp_path):
    """A fact that was not read is not recorded: absent, never an empty list."""
    world = MergeWorld(tmp_path)
    # A conflict with its fix rounds left parks before any check is read (#1076).
    world.pr.update(mergeable=False, mergeable_state="dirty")

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.PARKED, outcome.message
    # MUTATION: `run.wait` writes `failed: []` when no check was read -> fails.
    assert "failed" not in outcome.ci_wait, outcome.ci_wait


# ---------------------------------------------------------------------------
# The park itself, over in-memory Firestore
# ---------------------------------------------------------------------------


def _control(db: FakeFirestore) -> ControlPlane:
    return ControlPlane(
        db, task_id="task_1", attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
                            generation=1, runner_profile="merge", stream=io.StringIO()),
        txn_runner=FakeTransactionRunner(db),
    )


def _parked(db: FakeFirestore) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]


def _ci_park(db: FakeFirestore, **extra: Any) -> bool:
    seed_attempt(db, state=TaskState.RUNNING, runner_profile="merge")
    return _control(db).park_ci_pending(
        code="checks_pending", head=PINNED, pull_request=NUMBER, pending=[LINT],
        max_wakes=merge.MERGE_CI_MAX_WAKES, fallback_seconds=merge.MERGE_CI_FALLBACK_SECONDS,
        **extra)


def test_the_ci_park_event_and_merge_wait_record_failed_and_the_pull_request():
    db = FakeFirestore()
    _ci_park(db, failed=[CHECK])

    detail = _parked(db)[-1]["detail"]
    # MUTATION: the event omits `failed` -> fails.
    assert detail["failed"] == [CHECK]
    # MUTATION: the event omits `pull_request` (it was only on the document) -> fails.
    assert detail["pull_request"] == NUMBER
    # MUTATION: the event drops `pending` beside `failed` -> fails.
    assert detail["pending"] == [LINT]
    wait = db.doc("tasks/task_1")["metadata"][control_mod.MERGE_WAIT_METADATA_KEY]
    # MUTATION: `merge_wait` does not keep `failed` (a listing reads it there) -> fails.
    assert wait["failed"] == [CHECK]


def test_a_reader_that_ignores_the_new_fields_reads_what_it_read_before():
    """The control: the same park with and without `failed` differs only by it."""
    with_failed, without = FakeFirestore(), FakeFirestore()
    _ci_park(with_failed, failed=[CHECK])
    _ci_park(without)

    def old_view(detail: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in detail.items()
                if k in CI_EVENT_FIELDS_BEFORE and k != "next_eligible_at"}

    new, old = _parked(with_failed)[-1]["detail"], _parked(without)[-1]["detail"]
    # MUTATION: an existing field changes value or type (say `pending` becomes
    # the failed names, or `wakes` a string) -> fails.
    assert old_view(new) == old_view(old) == {
        "reason": ParkReason.CI_PENDING.value, "attempt_refunded": True, "wakes": 1,
        "code": "checks_pending", "head": PINNED, "pending": [LINT]}
    # MUTATION: a field is renamed or removed -> fails.
    assert CI_EVENT_FIELDS_BEFORE <= set(new)
    # MUTATION: anything beyond the two new fields is added -> fails.
    assert set(new) - CI_EVENT_FIELDS_BEFORE == {"failed", "pull_request"}
    # MUTATION: a park with nothing read records `failed: []` -> fails.
    assert "failed" not in old
    for db in (with_failed, without):
        task = db.doc("tasks/task_1")
        # MUTATION: `blocked_by`'s CI entry gains or loses a key -> fails.
        assert task["blocked_by"] == [{"reason": ParkReason.CI_PENDING.value,
                                       "code": "checks_pending", "head": PINNED,
                                       "pending": [LINT]}]
        wait = task["metadata"][control_mod.MERGE_WAIT_METADATA_KEY]
        # MUTATION: a `merge_wait` field the scheduler or swarm-api reads goes -> fails.
        assert MERGE_WAIT_FIELDS_BEFORE <= set(wait)
        assert set(wait) - MERGE_WAIT_FIELDS_BEFORE <= {"failed"}


def _seed_merge(db: FakeFirestore) -> None:
    seed_attempt(db, runner_profile="merge", task_input={"prompt": "merge"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    doc["metadata"] = {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                    "merge_target": {"pull_request": "task_int"}}}


@pytest.fixture
def no_runner(monkeypatch):
    from agent_worker import lifecycle
    from fakes import ExplodingChildProcess

    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)


def test_the_lifecycle_hands_the_merge_steps_failed_names_to_the_park(
    db, worker_factory, monkeypatch, no_runner
):
    _seed_merge(db)

    def run(ctx):
        step = merge._Run(ctx, {"action": "merge", "merged_by_this_task": False})
        step.reading = (PINNED, [LINT], [CHECK])
        return step.wait(merge.CI_FIX_PENDING, f"at {PINNED}: {CHECK} (failure)", head=PINNED,
                         pull_request=NUMBER, pending=[])

    monkeypatch.setattr(merge, "run_merge", run)
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.PARKED

    detail = _parked(db)[-1]["detail"]
    # MUTATION: `lifecycle._park_ci_pending` does not pass `failed` on -> fails.
    assert detail["failed"] == [CHECK]
    assert detail["pending"] == [LINT] and detail["pull_request"] == NUMBER


# ---------------------------------------------------------------------------
# The person-needed park records when it expires
# ---------------------------------------------------------------------------


def test_the_workers_human_park_bound_is_the_schedulers():
    from scheduler import loop as scheduler_loop
    from scheduler.settings import SchedulerSettings

    # MUTATION: the worker's restated bound drifts from the scheduler's -> fails.
    assert handoff_mod.HUMAN_PARK_MAX_SECONDS == scheduler_loop.HUMAN_PARK_MAX_SECONDS
    # The scheduler reads `human_park_max_seconds` through `getattr` with that
    # default; while its settings have no such field the default IS the setting.
    # MUTATION: a deployment makes it configurable -> fails, until the worker
    # is handed the configured value.
    assert not hasattr(SchedulerSettings, "human_park_max_seconds")
    assert "human_park_max_seconds" not in getattr(SchedulerSettings, "__dataclass_fields__", {})


def test_a_human_required_park_records_expires_at_its_instant_plus_the_bound():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)

    handoff_mod.park_human_required(_control(db), max_resumes=handoff_mod.MAX_HUMAN_RESUMES,
                                    detail={"park_phase": "human_wait", "cause": "hold_expired"})

    detail = _parked(db)[-1]["detail"]
    task = db.doc("tasks/task_1")
    # The park instant: `next_eligible_at`, which the scheduler's dead-letter
    # sweep measures `human_park_max_seconds` from.
    instant = task["next_eligible_at"]
    assert detail["next_eligible_at"] == instant
    # MUTATION: no `expires_at` -> KeyError; computed from another clock read
    # than the park's own instant, or with another bound -> fails.
    assert detail["expires_at"] == instant + timedelta(
        seconds=handoff_mod.HUMAN_PARK_MAX_SECONDS)
    # MUTATION: an existing field changes -> fails.
    assert detail["cause"] == "hold_expired" and detail["attempt_refunded"] is True
    # MUTATION: it is written onto the task document instead of the event -> fails.
    assert "expires_at" not in task


def test_a_human_park_a_person_turned_into_ready_records_no_expiry():
    """The control: no park, nothing to expire."""
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    db.doc("tasks/task_1").setdefault("metadata", {})[handoff_mod.HUMAN_RETURNED_METADATA_KEY] = {
        "attempt_id": control.attempt_id, "generation": control.generation,
        "by": "person@example.com"}

    park = handoff_mod.park_human_required(control, max_resumes=handoff_mod.MAX_HUMAN_RESUMES,
                                           detail={"cause": "hold_expired"})

    assert park.returned_by == "person@example.com"
    ready = [e for e in db.events("task_1") if e["type"] == EventType.READY.value]
    # MUTATION: `expires_at` is added to the READY event too -> fails.
    assert ready and "expires_at" not in ready[-1]["detail"]
    assert _parked(db) == []

