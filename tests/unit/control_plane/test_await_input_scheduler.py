"""An await-input step is parked, never leased, and released by its answer (HW-SCHED).

docs/design/human-steps.md §4.4. A workflow step whose runner profile is
`control_plane` (`await-input`) waits for a tenant member's answer, which
swarm-api writes (§4.6). The scheduler's whole part:

    when its dependencies succeed, it is PARKED(AWAITING_INPUT) with zero
        leases, on the finish event and on the tick alike (§4.4 step 1)
    a READY one is parked by admission and never leased, whatever the
        headroom (§4.4 step 2, the backstop)
    the HUMAN_REQUIRED sweep never ends it, at 25 hours or ever; no sweep
        promotes it (§4.4 step 3)
    under `fail_workflow` a failed sibling cancels it; under `continue` it
        keeps waiting (§4.4 step 3)
    its `task_finished` -- swarm-api's answer -- promotes and admits the
        next step within the same drain (§4.4 step 4)
    `expires_at` is `opened_at` plus the declared seconds, held to 24 hours,
        and a second park keeps the first window (§4.4, last paragraph)

RED FIRST. Against main before this lane, the dependency rule promoted the
step to READY and admission LEASED it: a container started for a step no
container can run, holding a slot (invariant 1). `scheduler.awaitinput` did
not exist either, so every test here failed at import.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scheduler.awaitinput import declared_seconds, is_control_plane, wait_window
from scheduler.loop import HUMAN_PARK_MAX_SECONDS, DrainReport
from swarm_common.models import EndCause
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import seed_pool, seed_task, seed_tenant

AWAIT = "await-input"
WF = "wf_review"
WAITING = "AWAITING_INPUT"


def _base(db, *, headroom: int = 50) -> None:
    seed_tenant(db, "eng", max_active=headroom)
    seed_pool(db, "global", hard_limit=headroom)


def _workflow(db, policy: str = "fail_workflow") -> None:
    db.docs[f"workflows/{WF}"] = {
        "id": WF,
        "tenant_id": "eng",
        "on_step_failure": policy,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }


def _declare(doc: dict, seconds: int | None = 3600) -> dict:
    block = {"form": "verdict", "artifact": "review.json", "on_expire": "fail"}
    if seconds is not None:
        block["expires_after_seconds"] = seconds
    doc["metadata"] = {"dispatch": {"await_input": block}}
    doc["step_id"] = doc["id"].removeprefix("task_")
    return doc


def _three_steps(db, *, draft_state: str = "SUCCEEDED") -> None:
    """draft -> review (await-input) -> revise, as the design's §1.3 runs it."""
    _workflow(db)
    seed_task(db, task_id="task_draft", tenant_id="eng", state=draft_state, workflow_id=WF)
    _declare(seed_task(
        db, task_id="task_review", tenant_id="eng", state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE", runner_profile=AWAIT,
        depends_on=("task_draft",), workflow_id=WF,
    ))
    seed_task(
        db, task_id="task_revise", tenant_id="eng", state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE", depends_on=("task_review",), workflow_id=WF,
    )


def _no_capacity_held(db) -> None:
    assert not any(key.startswith("leases/") for key in db.docs), sorted(db.docs)
    assert not any(key.startswith("attempts/") for key in db.docs), sorted(db.docs)
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["pools/tenant:eng"]["active"] == 0


def _waiting(doc: dict) -> None:
    assert (doc["state"], doc["park_reason"]) == ("PARKED", WAITING), doc
    assert doc["current_lease_id"] is None


def test_the_profile_is_the_control_plane_one():
    """The premise every test below rests on, read from the frozen catalogue."""
    assert RUNNER_PROFILES[AWAIT].control_plane is True
    assert is_control_plane(AWAIT) is True
    assert is_control_plane("mock") is False
    assert is_control_plane("no-such-profile") is False


# -- §4.4 step 1: the dependency rule parks it ---------------------------------


def test_when_draft_succeeds_the_finish_event_parks_the_await_step_with_zero_leases(
    db, make_scheduler, dispatcher
):
    _base(db)
    _three_steps(db)

    report = make_scheduler().release_dependants("task_draft")

    _waiting(db.docs["tasks/task_review"])
    assert db.docs["tasks/task_revise"]["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert report.leased == 0 and report.parked == 1, report.to_dict()
    assert dispatcher.dispatched == []
    _no_capacity_held(db)


def test_when_draft_succeeds_the_tick_parks_the_await_step_with_zero_leases(
    db, make_scheduler, dispatcher
):
    """The safety net: the same rule on the drain's dependency sweep."""
    _base(db)
    _three_steps(db)

    report = make_scheduler().drain()

    _waiting(db.docs["tasks/task_review"])
    assert db.docs["tasks/task_revise"]["state"] == "PARKED"
    assert report.leased == 0, report.to_dict()
    assert dispatcher.dispatched == []
    _no_capacity_held(db)


def test_the_park_passes_through_ready_and_says_so_in_its_events(db, make_scheduler):
    _base(db)
    _three_steps(db)

    make_scheduler().release_dependants("task_draft")

    events = sorted(
        (doc for key, doc in db.docs.items() if key.startswith("tasks/task_review/events/")),
        key=lambda event: event["at"],
    )
    assert [event["type"] for event in events] == ["ready", "parked"], events
    detail = events[1]["detail"]
    window = db.docs["tasks/task_review"]["metadata"]["await_input"]
    assert detail == {
        "reason": WAITING,
        "opened_at": window["opened_at"].isoformat(),
        "expires_at": window["expires_at"].isoformat(),
    }


def test_a_step_still_waiting_on_draft_is_not_parked_for_input(db, make_scheduler, dispatcher):
    """The control: no answer can be opened before the step's inputs exist."""
    _base(db)
    _three_steps(db, draft_state="RUNNING")

    make_scheduler().drain()

    assert db.docs["tasks/task_review"]["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert "await_input" not in db.docs["tasks/task_review"]["metadata"]
    assert dispatcher.dispatched == []


def test_an_ordinary_dependant_is_still_promoted_and_leased(db, make_scheduler, dispatcher):
    """The control: only a control_plane profile is parked; `mock` runs as before."""
    _base(db)
    _workflow(db)
    seed_task(db, task_id="task_draft", tenant_id="eng", state="SUCCEEDED", workflow_id=WF)
    seed_task(
        db, task_id="task_next", tenant_id="eng", state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE", depends_on=("task_draft",), workflow_id=WF,
    )

    make_scheduler().release_dependants("task_draft")

    assert db.docs["tasks/task_next"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_next"]


# -- §4.4 step 2: the admission backstop ---------------------------------------


def test_a_ready_await_step_is_parked_by_admission_and_never_leased(
    db, make_scheduler, dispatcher
):
    _base(db, headroom=500)
    doc = _declare(seed_task(db, task_id="task_review", tenant_id="eng", runner_profile=AWAIT))
    task = make_scheduler().store.get_task("task_review")
    scheduler = make_scheduler()
    report = DrainReport()

    assert scheduler._admit_one(task, report) is False

    _waiting(doc)
    assert report.leased == 0 and report.denied == 0 and report.parked == 1
    assert dispatcher.dispatched == []
    _no_capacity_held(db)


def test_a_ready_await_step_is_never_leased_by_a_drain_whatever_the_headroom(
    db, make_scheduler, dispatcher
):
    _base(db, headroom=500)
    _declare(seed_task(db, task_id="task_review", tenant_id="eng", runner_profile=AWAIT))
    seed_task(db, task_id="task_other", tenant_id="eng")

    scheduler = make_scheduler()
    first = scheduler.drain()
    second = scheduler.drain()

    _waiting(db.docs["tasks/task_review"])
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_other"]
    assert first.leased == 1 and second.leased == 0, (first.to_dict(), second.to_dict())
    assert not any(
        doc.get("task_id") == "task_review"
        for key, doc in db.docs.items() if key.startswith("leases/")
    )


# -- §4.4 step 3: nothing promotes it, the human-wait sweep never ends it -------


def _parked_hours_ago(db, hours: float) -> dict:
    then = datetime.now(timezone.utc) - timedelta(hours=hours)
    doc = _declare(seed_task(
        db, task_id="task_review", tenant_id="eng", state="PARKED", park_reason=WAITING,
        runner_profile=AWAIT, next_eligible_at=then, created_at=then, workflow_id=WF,
    ))
    doc["metadata"]["await_input"] = {"opened_at": then, "expires_at": then + timedelta(hours=1)}
    return doc


def test_the_human_wait_sweep_does_not_touch_it_at_25_hours(db, make_scheduler):
    _base(db)
    _workflow(db)
    doc = _parked_hours_ago(db, 25)
    before = dict(doc)

    assert make_scheduler()._expire_human_waits(DrainReport()) == 0

    assert doc == before
    assert not any(key.startswith("tasks/task_review/events/") for key in db.docs)


def test_a_whole_drain_at_25_hours_leaves_it_waiting(db, make_scheduler, dispatcher):
    """Past its own expires_at too: expiry is swarm-api's, which honours on_expire."""
    _base(db, headroom=500)
    _workflow(db)
    doc = _parked_hours_ago(db, 25)
    before = dict(doc)

    report = make_scheduler().drain()

    assert doc == before, report.to_dict()
    assert dispatcher.dispatched == []
    _no_capacity_held(db)


def test_under_fail_workflow_a_failed_sibling_cancels_it(db, make_scheduler, dispatcher):
    """The waiting step is the workflow's ONLY not-started step, so no other
    sweep reaches it: the await sweep is the one path to this cancel."""
    _base(db)
    _workflow(db, "fail_workflow")
    seed_task(db, task_id="task_lint", tenant_id="eng", state="FAILED", workflow_id=WF)
    doc = _parked_hours_ago(db, 1)

    report = make_scheduler().drain()

    assert doc["state"] == "CANCELLED", report.to_dict()
    assert doc["end_cause"] == EndCause.WORKFLOW_SWEEP.value
    assert report.failed_workflows_swept == 1
    _no_capacity_held(db)


def test_under_continue_a_failed_sibling_leaves_it_waiting(db, make_scheduler):
    """The control: the same workflow under `continue` cancels nothing."""
    _base(db)
    _workflow(db, "continue")
    seed_task(db, task_id="task_lint", tenant_id="eng", state="FAILED", workflow_id=WF)
    doc = _parked_hours_ago(db, 1)

    make_scheduler().drain()

    _waiting(doc)


# -- §4.4 step 4: the answer's finish event releases `revise` --------------------


def test_its_task_finished_promotes_and_admits_revise_in_the_same_drain(
    db, make_scheduler, dispatcher
):
    _base(db)
    _three_steps(db)
    scheduler = make_scheduler()
    scheduler.release_dependants("task_draft")
    _waiting(db.docs["tasks/task_review"])

    # swarm-api's answer transaction (§4.6 step 8), then its `task_finished`.
    db.docs["tasks/task_review"].update(
        {"state": "SUCCEEDED", "park_reason": None, "next_eligible_at": None,
         "completed_at": datetime.now(timezone.utc)}
    )
    report = scheduler.release_dependants("task_review")

    assert report.promoted_dependencies == 1, report.to_dict()
    assert db.docs["tasks/task_revise"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_revise"]


# -- the window -------------------------------------------------------------------


def test_expires_at_is_opened_at_plus_the_declared_seconds(db, make_scheduler):
    _base(db)
    _three_steps(db)
    _declare(db.docs["tasks/task_review"], 5400)

    make_scheduler().release_dependants("task_draft")

    doc = db.docs["tasks/task_review"]
    window = doc["metadata"]["await_input"]
    assert window["expires_at"] - window["opened_at"] == timedelta(seconds=5400)
    assert doc["next_eligible_at"] == window["opened_at"]
    # The signed declaration is read, never rewritten.
    assert doc["metadata"]["dispatch"]["await_input"]["expires_after_seconds"] == 5400


def test_the_window_is_held_to_24_hours():
    """A document no submission would accept still ends within the bound."""
    assert HUMAN_PARK_MAX_SECONDS == 86_400
    now = datetime.now(timezone.utc)
    for declared, expected in ((86_400, 86_400), (90_000, 86_400), (None, 86_400),
                               (True, 86_400), (-5, 86_400), (300, 300)):
        metadata = {"dispatch": {"await_input": {"expires_after_seconds": declared}}}
        assert declared_seconds(metadata, max_seconds=HUMAN_PARK_MAX_SECONDS) == expected
        window = wait_window(metadata, now, max_seconds=HUMAN_PARK_MAX_SECONDS)
        assert window == {"opened_at": now, "expires_at": now + timedelta(seconds=expected)}
    assert declared_seconds({}, max_seconds=HUMAN_PARK_MAX_SECONDS) == 86_400


def test_a_second_park_keeps_the_first_window(db, make_scheduler):
    """An operator's hand promotion is parked again, and must not extend the wait."""
    _base(db)
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    doc = _declare(seed_task(db, task_id="task_review", tenant_id="eng", runner_profile=AWAIT))
    doc["metadata"]["await_input"] = {"opened_at": opened, "expires_at": opened + timedelta(hours=1)}

    make_scheduler().drain()

    _waiting(doc)
    assert doc["metadata"]["await_input"] == {
        "opened_at": opened, "expires_at": opened + timedelta(hours=1)
    }
