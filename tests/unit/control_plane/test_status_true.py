"""Status reads stay true while a merge step's CI-fix round runs (STATUS-TRUE, part of #1914).

Owner decision 2026-10-22 (BGD), measured on `wf_15160fca1d07438ab594`
(QAFIX4-RUNS, PR 1905) 01:25-01:35Z: `ci-gate` concluded FAILURE, the merge
step's wake tick submitted round `ci-fix:1` and appended it to the lane's
workflow, and yet

  1. `GET /v1/workflows/{id}` derived UNKNOWN, "step_tasks_missing ...
     ci-fix:1", while the round's task read RUNNING. The route loaded the
     step tasks with ONE query, `workflow_id == <the lane>`, and a round's
     task belongs to the round's OWN workflow (`mergewake._hand_off` submits
     it as one): it is never on that page, however large the page is.
  2. The merge step's waiting line kept saying "1 checks pending (ci-gate)".
     The line is built from what the worker recorded when it parked, and the
     wake tick, which read the failure and handed it to the round, wrote no
     part of that reading anywhere the line reads.

What these tests hold: the workflow read joins every step's task, a round's
included, and a page the workflow outgrew, by reading the named step ids
directly; the tick records each CI reading it takes -- what is pending, what
failed, the round under way at that head -- and the waiting line prefers that
newer reading to the park's, saying when it was read.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import timedelta

from swarm_api import mergewake
from swarm_api.codec import task_from_dict
from swarm_api.waiting_detail import build_waiting
from swarm_common.models import TaskEvent
from swarm_common.states import EventType

from .conftest import auth_header, seed_task
from .test_merge_auto_fix_rounds import LANE, lane_merge
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    CHECK,
    HEAD,
    NOW,
    NUMBER,
    ROOT,
    _members_hold_grants,
    _tick,
    _wait,
    clock,
    github,
    tenant_tokens,
    wake_client,
)


def _round_task_id(db) -> str:
    steps = db.docs[f"workflows/{LANE}"]["steps"]
    (round_step,) = [s for s in steps if s["step_id"] == "ci-fix:1"]
    return round_step["task_id"]


def _lane_with_a_running_round(db, wake_client, github) -> str:
    """The measured shape: fix SUCCEEDED, merge PARKED on CI, ci-fix:1 RUNNING."""
    lane_merge(db, github)
    # The lane's own steps belong to the lane's workflow, as a submission
    # makes them; the round's task belongs to the round's own workflow.
    db.docs[f"tasks/{ROOT}"]["workflow_id"] = LANE
    db.docs["tasks/task_merge"]["workflow_id"] = LANE
    assert _tick(wake_client).json()["report"]["fixing"] == 1
    round_task = _round_task_id(db)
    db.docs[f"tasks/{round_task}"]["state"] = "RUNNING"
    return round_task


# --------------------------------------------------------------------------
# 1. the workflow read loads every step's task
# --------------------------------------------------------------------------

def test_a_workflow_with_a_running_ci_fix_round_reads_every_step(db, wake_client, github):
    round_task = _lane_with_a_running_round(db, wake_client, github)
    assert db.docs[f"tasks/{round_task}"]["workflow_id"] != LANE, "the measured shape"

    body = wake_client.get(f"/v1/workflows/{LANE}", headers=auth_header("alice")).json()

    rollup = body["workflow"]["rollup"]
    # MUTATION: loading the step tasks only by `workflow_id == <the lane>`
    # (the query this route made before) turns this red: the round's task
    # is not on that page and reads as missing.
    assert rollup["unreadable_steps"] == [], rollup
    assert rollup["complete"] is True, rollup
    # MUTATION: dropping a step's task from the join turns these red.
    assert rollup["counts"] == {"SUCCEEDED": 1, "PARKED": 1, "RUNNING": 1}, rollup
    assert rollup["fix_round_steps"] == ["ci-fix:1"]
    # A step holding capacity reads the workflow RUNNING (swarm_rollup.derive,
    # rule 3): the merge step is PARKED and the round RUNNING beneath it.
    assert body["workflow"]["state"] == "RUNNING", body["workflow"]
    states = {t["id"]: t["state"] for t in body["tasks"]}
    # MUTATION: serving only the page's tasks turns this red: the console's
    # workflow page draws a step from the task this list carries.
    assert states[round_task] == "RUNNING"
    assert states["task_merge"] == "PARKED" and states[ROOT] == "SUCCEEDED"


def test_a_workflow_with_more_tasks_than_one_page_still_reads_its_steps(db, wake_client, github):
    round_task = _lane_with_a_running_round(db, wake_client, github)
    # 200 newer tasks of the lane's workflow: its step tasks are past the
    # route's page of 200, newest first.
    newest = max(d["created_at"] for k, d in db.docs.items()
                 if k.count("/") == 1 and k.startswith("tasks/"))
    for n in range(200):
        seed_task(db, task_id=f"task_extra{n:03d}", tenant_id="eng", state="SUCCEEDED",
                  workflow_id=LANE, created_at=newest + timedelta(minutes=1, seconds=n))

    body = wake_client.get(f"/v1/workflows/{LANE}", headers=auth_header("alice")).json()

    rollup = body["workflow"]["rollup"]
    # MUTATION: deriving from the page alone (complete=False on a truncated
    # page, `step_tasks_unread`) turns this red; raising the page limit
    # instead of reading the named ids moves the cliff, it does not remove it.
    assert rollup["unreadable_steps"] == [], rollup
    assert body["workflow"]["state"] == "RUNNING", rollup
    assert {round_task, "task_merge", ROOT} <= {t["id"] for t in body["tasks"]}


# --------------------------------------------------------------------------
# 2. the wake tick records the reading it took, and the line reads it
# --------------------------------------------------------------------------

def test_a_red_reading_handed_to_a_round_is_recorded_on_the_wait(db, wake_client, github, clock):
    round_task = _lane_with_a_running_round(db, wake_client, github)
    reading = _wait(db).get("reading")
    # MUTATION: writing nothing on the `_FIXING` path (as before) turns this red.
    assert reading is not None, _wait(db)
    # MUTATION: recording the worker's pending list instead of the tick's turns this red.
    assert reading["pending"] == [] and reading["failed"] == [CHECK], reading
    assert reading["head"] == HEAD and reading["read_at"] == NOW
    assert reading["fix_round"]["round"] == 1 and reading["fix_round"]["task_id"] == round_task

    clock.now = NOW + timedelta(seconds=mergewake.CI_READ_SECONDS)
    assert _tick(wake_client).json()["report"]["waiting"] == 1
    reading = _wait(db)["reading"]
    # MUTATION: recording only `checked_at` on a tick that waits on the
    # round turns this red: the round's state is what the line names.
    assert reading["read_at"] == clock.now
    assert reading["fix_round"] == {"kind": "ci", "round": 1, "task_id": round_task,
                                    "state": "RUNNING"}
    # A record, never a wake: the merge stays parked while the round runs.
    assert mergewake.WAKE_MARKER not in _wait(db)


def _merge_line(db, *, as_of) -> dict:
    doc = db.docs["tasks/task_merge"]
    park = TaskEvent(
        event_id="ev_park", task_id="task_merge", tenant_id="eng", type=EventType.PARKED,
        at=NOW - timedelta(minutes=5),
        detail={"reason": "CI_PENDING", "code": "checks_pending", "head": HEAD,
                "pending": [CHECK], "pull_request": NUMBER, "wakes": 1},
    )
    return build_waiting(task_from_dict(doc), events=[park], as_of=as_of)


def test_the_waiting_line_names_the_failed_check_and_the_running_round(db, wake_client, github,
                                                                       clock):
    _lane_with_a_running_round(db, wake_client, github)
    clock.now = NOW + timedelta(seconds=mergewake.CI_READ_SECONDS)
    _tick(wake_client)

    w = _merge_line(db, as_of=clock.now)

    # MUTATION: building the line from the park's facts alone (as before)
    # turns these red: it read "1 checks pending (ci / unit)".
    assert "pending" not in w["summary"].split(";")[0], w["summary"]
    assert w["summary"].startswith(
        f"CI on PR #{NUMBER}: 1 failed ({CHECK}); fix round 1 running; read "
        f"{clock.now.strftime('%H:%MZ')}"), w["summary"]
    # MUTATION: labelling the detail with the park's time, not the tick's read, turns this red.
    assert w["detail"]["last_checked_at"] == clock.now
    assert w["detail"]["pending"] == [] and w["detail"]["failed"] == [CHECK]
    assert w["detail"]["fix_round"] == {"kind": "ci", "round": 1,
                                        "task_id": _round_task_id(db), "state": "RUNNING"}


def test_a_reading_at_another_head_is_not_served(db, wake_client, github, clock):
    _lane_with_a_running_round(db, wake_client, github)
    # A reading at a head the park is not waiting at (a tenant-written one,
    # or one the worker's next park would have replaced).
    _wait(db)["reading"] = {"head": "f" * 40, "read_at": clock.now, "pending": [],
                            "failed": [CHECK], "fix_round": {"kind": "ci", "round": 1}}

    w = _merge_line(db, as_of=clock.now)

    # MUTATION: serving a reading without checking its head against the
    # park's turns this red.
    assert w["summary"].startswith(f"CI on PR #{NUMBER}: 1 checks pending ({CHECK})"), w["summary"]
    assert w["detail"]["fix_round"] is None
